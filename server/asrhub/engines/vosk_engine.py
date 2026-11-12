"""Адаптер Vosk — потоковое распознавание на CPU без GPU."""
from __future__ import annotations

import json
import wave
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import urlretrieve

from ..errors import DependencyMissing, EngineError, ModelNotDownloaded
from .base import Engine, ProgressCallback, Segment, TranscriptionResult


class VoskEngine(Engine):
    id = "vosk"
    supports_word_timestamps = True
    supports_streaming = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import vosk  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен пакет vosk: pip install vosk"

    def _model_dir(self, settings: dict[str, Any]) -> Path:
        """Каталог модели — по точному имени, а не по подстроке.

        Поиск по подстроке путал версии: у `vosk-ru-0.54` источник —
        репозиторий «alphacep/vosk-model-ru», и под «vosk-model-ru» подходил
        каталог «vosk-model-ru-0.42». Выбрана 0.54 — распознавала 0.42, а
        подписано было 0.54. Здесь годится только каталог с ровно этим
        именем — прямо в `vosk/` или на уровень глубже (архив бывает
        распакован с лишней вложенностью), — и снимок Hugging Face, если
        источник — репозиторий.
        """
        models_dir = Path(settings.get("models_dir") or ".")
        base = models_dir / "vosk"
        source = self.spec.source
        name = source.rsplit("/", 1)[-1].replace(".zip", "")
        target = base / name
        if target.is_dir():
            return target
        if base.is_dir():
            for child in base.iterdir():
                вложенный = child / name
                if child.is_dir() and вложенный.is_dir():
                    return вложенный
        if not source.startswith("http") and "/" in source:
            from ..model_files import find_local  # noqa: PLC0415

            снимок = find_local(models_dir, source, self.spec.revision or "")
            if снимок is not None:
                ревизии = sorted((снимок / "snapshots").glob("*")) if (
                    снимок / "snapshots").is_dir() else []
                return ревизии[-1] if ревизии else снимок
        raise ModelNotDownloaded(self.spec.id, self.spec.disk_mb)

    def download(self, settings: dict[str, Any]) -> Path:
        """Скачивает и распаковывает модель. Используется менеджером моделей."""
        source = self.spec.source
        if not source.startswith("http"):
            raise EngineError(
                f"Модель «{self.spec.id}» скачивается с Hugging Face, "
                "используйте общий загрузчик моделей.")
        base = Path(settings.get("models_dir") or ".") / "vosk"
        base.mkdir(parents=True, exist_ok=True)
        archive = base / source.rsplit("/", 1)[-1]
        urlretrieve(source, archive)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(base)
        archive.unlink(missing_ok=True)
        return self._model_dir(settings)

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from vosk import Model, SetLogLevel  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("vosk", "vosk", cause=exc) from exc
        SetLogLevel(-1)
        return Model(str(self._model_dir(settings)))

    def stream_session(self, settings: dict[str, Any]) -> Any:
        """Настоящий поток: распознаватель держит состояние между кусками.

        Vosk для этого и сделан — та же модель, что и для файлов, но вместо
        чтения WAV ей скармливают куски по мере поступления, и после
        каждого можно спросить текущую гипотезу.
        """

        self.ensure_loaded(settings)
        return _VoskStream(self._model, settings)

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        from vosk import KaldiRecognizer  # type: ignore

        with wave.open(str(audio_path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise EngineError(
                    "Vosk принимает только моно WAV 16 бит.",
                    hint="Оставьте включённой предобработку аудио — она приводит файл к нужному виду.")
            rate = wf.getframerate()
            total_frames = wf.getnframes()
            recognizer = KaldiRecognizer(self._model, rate)
            # Слова просим всегда: только по ним у реплики есть начало и
            # конец. С SetWords(False) все реплики выходили 0.0–0.0, и
            # субтитры, диаризация и переход к реплике в записи ломались.
            # Не просили слов — отбрасываем их на выходе.
            recognizer.SetWords(True)
            recognizer.SetPartialWords(False)

            results: list[dict[str, Any]] = []
            processed = 0
            block = 8000
            while True:
                data = wf.readframes(block)
                if not data:
                    break
                processed += len(data) // 2
                if recognizer.AcceptWaveform(data):
                    results.append(json.loads(recognizer.Result()))
                if total_frames:
                    self.report(progress, 0.05 + 0.9 * processed / total_frames, "распознавание")
            results.append(json.loads(recognizer.FinalResult()))

        segments: list[Segment] = []
        for item in results:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            words: list[dict[str, Any]] = [{
                "word": str(w.get("word", "")),
                "start": round(float(w.get("start", 0.0)), 3),
                "end": round(float(w.get("end", 0.0)), 3),
                "confidence": round(float(w.get("conf", 0.0)), 4),
            } for w in (item.get("result") or [])]
            start = words[0]["start"] if words else (segments[-1].end if segments else 0.0)
            end = words[-1]["end"] if words else start
            confidences = [w["confidence"] for w in words if w["confidence"]]
            segments.append(Segment(
                start=start, end=end, text=text,
                confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
                language=self.language_for(settings),
                words=words if settings.get("word_timestamps", True) else [],
            ))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments,
            language=self.language_for(settings) or "ru",
            duration=segments[-1].end if segments else 0.0,
            meta={"streaming_capable": True},
        )


class _VoskStream:
    """Состояние потокового распознавания Vosk.

    accept() возвращает пару (вид, текст) или None, если сказать пока
    нечего: «final» — законченная фраза, «partial» — текущая гипотеза,
    которая ещё может измениться.
    """

    def __init__(self, model: Any, settings: dict[str, Any]) -> None:
        from vosk import KaldiRecognizer  # type: ignore

        self._recognizer = KaldiRecognizer(model, 16000)
        self._recognizer.SetWords(bool(settings.get("word_timestamps", True)))

    def accept(self, pcm: bytes) -> tuple[str, str] | None:
        if self._recognizer.AcceptWaveform(pcm):
            text = str(json.loads(self._recognizer.Result()).get("text", "")).strip()
            return ("final", text) if text else None
        text = str(json.loads(self._recognizer.PartialResult()).get("partial", "")).strip()
        return ("partial", text) if text else None

    def finish(self) -> str:
        return str(json.loads(self._recognizer.FinalResult()).get("text", "")).strip()

    def close(self) -> None:
        self._recognizer = None
