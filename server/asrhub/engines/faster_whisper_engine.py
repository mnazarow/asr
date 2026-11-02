"""Адаптер faster-whisper (CTranslate2) — основной движок семейства Whisper.

Учтено:
* transcribe() возвращает ленивый генератор — ошибки всплывают при итерации,
  поэтому обход завёрнут в обработчик;
* имя параметра log_prob_threshold отличается от logprob_threshold в openai-whisper;
* у BatchedInferencePipeline другие значения по умолчанию;
* рассогласование версий cuDNN — самая частая ошибка установки, для неё
  выдаётся подробная подсказка.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import DependencyMissing, classify_exception
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_CT2_NAMES = {
    "large-v3": "large-v3", "large-v2": "large-v2", "large-v1": "large-v1",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "distil-large-v3": "distil-whisper/distil-large-v3-ct2",
    "distil-whisper/distil-large-v3.5-ct2": "distil-whisper/distil-large-v3.5-ct2",
}


class FasterWhisperEngine(Engine):
    id = "faster_whisper"
    supports_word_timestamps = True
    supports_batching = True
    outputs_punctuation = True
    режет_сам = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import faster_whisper  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен пакет faster-whisper: pip install faster-whisper"

    @staticmethod
    def _карт_ctranslate2() -> int | None:
        """Сколько карт видит CTranslate2; None — спросить не у кого."""
        try:
            import ctranslate2  # type: ignore

            return int(ctranslate2.get_cuda_device_count())
        except Exception:                                   # noqa: BLE001
            return None

    def resolve_device(self, settings: dict[str, Any]) -> str:
        """«auto» — по CTranslate2, а не по torch.

        faster-whisper torch не нужен: сборка torch без CUDA (Windows,
        requirements/cpu.txt) рядом с CTranslate2 и cuDNN уводила «auto» на
        процессор при исправной карте. Не у кого спросить — как раньше.
        """
        if str(settings.get("device") or "auto").strip().lower() == "auto":
            карт = self._карт_ctranslate2()
            if карт is not None:
                return "cuda" if карт > 0 else "cpu"
        return super().resolve_device(settings)

    def проверить_устройство(self, устройство: str) -> tuple[bool, str]:
        if not устройство.startswith("cuda"):
            return True, ""
        карт = self._карт_ctranslate2()
        if карт is None:
            return super().проверить_устройство(устройство)
        _, _, номер = устройство.partition(":")
        нужна = int(номер) if номер.strip().isdigit() else 0
        if карт <= нужна:
            return False, (f"CTranslate2 видит карт: {карт}, а запрошена {нужна} "
                           "(номера с 0). Проверьте CUDA_VISIBLE_DEVICES и cuDNN.")
        return True, ""

    def cache_key(self, settings: dict[str, Any]) -> str:
        return "|".join([
            self.spec.source, self.resolve_device(settings),
            self.resolve_compute_type(settings, self.resolve_device(settings)),
            str(settings.get("cpu_threads") or 0), str(settings.get("num_workers") or 1),
        ])

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("faster_whisper", "faster-whisper", cause=exc) from exc

        device = self.resolve_device(settings)
        if device == "mps":
            self.log.info("faster-whisper не поддерживает MPS — используется CPU с int8")
            device = "cpu"
        ct_device = "cuda" if device.startswith("cuda") else ("cpu" if device != "rocm" else "cuda")
        device_index = 0
        if ":" in device:
            try:
                device_index = int(device.split(":", 1)[1])
            except ValueError:
                device_index = 0

        compute_type = self.resolve_compute_type(settings, device)
        if ct_device == "cpu" and compute_type in ("float16", "int8_float16", "bfloat16"):
            compute_type = "int8"

        # Репозиторий — из той же таблицы, по которой ищутся веса на диске:
        # загрузка здесь, в разделе «Модели» и в models.sh кладёт их в один
        # и тот же каталог и находит одинаково.
        from ..model_files import huggingface_repo

        name = huggingface_repo(_CT2_NAMES.get(self.spec.source, self.spec.source))
        try:
            return WhisperModel(
                name,
                device=ct_device,
                device_index=device_index,
                compute_type=compute_type,
                cpu_threads=S.integer(settings, "cpu_threads", 0),
                num_workers=int(settings.get("num_workers") or 1),
                download_root=str(settings.get("models_dir") or "") or None,
            )
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _build_options(self, settings: dict[str, Any]) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "language": self.language_for(settings),
            "task": str(settings.get("task") or "transcribe"),
            "beam_size": int(settings.get("beam_size") or 5),
            "best_of": int(settings.get("best_of") or 5),
            "patience": float(settings.get("patience") or 1.0),
            "length_penalty": S.num(settings, "length_penalty", 1.0),
            "repetition_penalty": float(settings.get("repetition_penalty") or 1.0),
            "no_repeat_ngram_size": S.integer(settings, "no_repeat_ngram_size", 0),
            "compression_ratio_threshold": float(
                settings.get("compression_ratio_threshold") or 2.4),
            "log_prob_threshold": S.num(settings, "logprob_threshold", -1.0),
            "no_speech_threshold": S.num(settings, "no_speech_threshold", 0.6),
            "condition_on_previous_text": bool(settings.get("condition_on_previous_text", False)),
            "prompt_reset_on_temperature": S.num(
                settings, "prompt_reset_on_temperature", 0.5),
            "suppress_blank": bool(settings.get("suppress_blank", True)),
            "word_timestamps": bool(settings.get("word_timestamps", True)),
            "multilingual": bool(settings.get("multilingual_detection", False)),
        }

        if settings.get("temperature_fallback", True):
            base = S.num(settings, "temperature", 0.0)
            opts["temperature"] = [base, 0.2, 0.4, 0.6, 0.8, 1.0]
        else:
            opts["temperature"] = [S.num(settings, "temperature", 0.0)]

        prompt = str(settings.get("initial_prompt") or "").strip()
        if prompt:
            opts["initial_prompt"] = prompt
        hotwords = str(settings.get("hotwords") or "").strip()
        if hotwords:
            opts["hotwords"] = hotwords

        threshold = S.num(settings, "hallucination_silence_threshold", 0.0)
        if threshold > 0 and opts["word_timestamps"]:
            opts["hallucination_silence_threshold"] = threshold

        max_new = S.integer(settings, "max_new_tokens", 0)
        if max_new > 0:
            opts["max_new_tokens"] = max_new

        # Пустое значение — «ничего не подавлять» (пример каталога «Без
        # подавления»). Раньше `or "-1"` превращал пустую строку обратно в
        # набор по умолчанию, и выключить подавление было нельзя.
        raw_suppress = settings.get("suppress_tokens")
        raw_suppress = "-1" if raw_suppress is None else str(raw_suppress).strip()
        try:
            opts["suppress_tokens"] = [int(x) for x in raw_suppress.split(",") if x.strip()]
        except ValueError:
            opts["suppress_tokens"] = [-1]

        if self.language_for(settings) is None:
            opts["language_detection_threshold"] = S.num(
                settings, "language_detection_threshold", 0.5)
            opts["language_detection_segments"] = int(
                settings.get("language_detection_segments") or 1)

        if settings.get("vad_enabled", True):
            opts["vad_filter"] = True
            opts["vad_parameters"] = {
                "threshold": float(settings.get("vad_threshold") or 0.5),
                "min_speech_duration_ms": S.integer(settings, "vad_min_speech_ms", 250),
                "max_speech_duration_s": float(settings.get("vad_max_speech_s") or 30.0),
                "min_silence_duration_ms": S.integer(settings, "vad_min_silence_ms", 500),
                "speech_pad_ms": S.integer(settings, "vad_speech_pad_ms", 200),
            }
            neg = settings.get("vad_neg_threshold")
            if neg is not None:
                opts["vad_parameters"]["neg_threshold"] = float(neg)
        else:
            opts["vad_filter"] = False
        return opts

    @staticmethod
    def _почему_не_пакетом(options: dict[str, Any]) -> list[str]:
        """Что из настроек пакетный конвейер не исполнит; пусто — можно пакетом.

        BatchedInferencePipeline (faster-whisper 1.1+) берёт только первую
        температуру, жёстко ставит condition_on_previous_text=False и
        hallucination_silence_threshold=None, а без VAD на записи от 30 с
        бросает «No clip timestamps found». Пакет 8 стоял по умолчанию, и
        каскад температур и пропуск длинной тишины, которые обещает пресет
        «Whisper: устойчивый режим», молча не действовали. Проверено по
        исходнику библиотеки, не запуском.
        """
        причины = []
        if not options.get("vad_filter"):
            причины.append("поиск речи выключен")
        if len(options.get("temperature") or []) > 1:
            причины.append("каскад температур")
        if options.get("condition_on_previous_text"):
            причины.append("учёт предыдущего текста")
        if options.get("hallucination_silence_threshold"):
            причины.append("пропуск длинной тишины")
        return причины

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        options = self._build_options(settings)
        batch_size = int(settings.get("batch_size") or 1)

        model = self._model
        runner = model
        без_пакета = self._почему_не_пакетом(options) if batch_size > 1 else []
        if без_пакета:
            self.log.info("Пакетный режим не используется: %s", ", ".join(без_пакета))
        if batch_size > 1 and self.supports_batching and not без_пакета:
            try:
                from faster_whisper import BatchedInferencePipeline  # type: ignore

                runner = BatchedInferencePipeline(model=model)
                options["batch_size"] = batch_size
                # У пакетного конвейера свои умолчания — восстанавливаем ожидаемые.
                options.setdefault("without_timestamps", not options.get("word_timestamps", True))
            except Exception as exc:
                self.log.info("Пакетный режим недоступен (%s), обработка по одному", exc)
                runner = model
                options.pop("batch_size", None)

        self.report(progress, 0.05, "запуск распознавания")
        try:
            iterator, info = runner.transcribe(str(audio_path), **options)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        segments: list[Segment] = []
        try:
            # Генератор ленивый: настоящая работа и настоящие ошибки — здесь.
            for seg in iterator:
                words = []
                for word in (getattr(seg, "words", None) or []):
                    words.append({
                        "word": getattr(word, "word", "").strip(),
                        "start": round(float(getattr(word, "start", 0.0)), 3),
                        "end": round(float(getattr(word, "end", 0.0)), 3),
                        "confidence": round(float(getattr(word, "probability", 0.0)), 4),
                    })
                logprob = getattr(seg, "avg_logprob", None)
                confidence = None
                if logprob is not None:
                    confidence = round(min(1.0, max(0.0, 2.718281828 ** float(logprob))), 4)
                segments.append(Segment(
                    start=float(getattr(seg, "start", 0.0)),
                    end=float(getattr(seg, "end", 0.0)),
                    text=str(getattr(seg, "text", "")).strip(),
                    confidence=confidence,
                    no_speech_prob=getattr(seg, "no_speech_prob", None),
                    compression_ratio=getattr(seg, "compression_ratio", None),
                    temperature=getattr(seg, "temperature", None),
                    language=getattr(info, "language", None),
                    words=words,
                ))
                if duration > 0:
                    self.report(progress, min(0.97, 0.05 + 0.92 * (segments[-1].end / duration)),
                                "распознавание")
        except Exception as exc:
            if segments:
                self.log.error("Распознавание прервано после %d сегментов: %s", len(segments), exc)
                raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        return TranscriptionResult(
            segments=segments,
            language=str(getattr(info, "language", "") or ""),
            language_probability=getattr(info, "language_probability", None),
            duration=duration,
            meta={
                "batch_size": options.get("batch_size", 1),
                **({"batched_skipped": без_пакета} if без_пакета else {}),
                "vad_filter": options.get("vad_filter"),
                "compute_type": self.resolve_compute_type(
                    settings, self.resolve_device(settings)),
            },
        )
