"""Адаптер whisper.cpp — вызов внешнего бинарника whisper-cli.

Лучший вариант для macOS (Metal и Core ML) и для серверов без GPU.
Значения VAD по умолчанию в whisper.cpp сильно отличаются от faster-whisper,
поэтому ASR Hub всегда передаёт их явно.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..errors import BinaryMissing, EngineError, ModelNotDownloaded
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_BIN_CANDIDATES = ["whisper-cli", "whisper-cpp", "main"]


class WhisperCppEngine(Engine):
    id = "whisper_cpp"
    supports_word_timestamps = True
    outputs_punctuation = True

    @classmethod
    def _find_binary(cls) -> str | None:
        env = os.environ.get("ASRHUB_WHISPER_CPP")
        if env and Path(env).exists():
            return env
        for name in _BIN_CANDIDATES:
            found = shutil.which(name)
            if found:
                return found
        for base in (Path.home() / ".local/share/asrhub/whisper.cpp/build/bin",
                     Path("/opt/whisper.cpp/build/bin"),
                     Path("/usr/local/lib/whisper.cpp/build/bin")):
            for name in _BIN_CANDIDATES:
                candidate = base / name
                if candidate.exists():
                    return str(candidate)
        return None

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        if cls._find_binary():
            return True, ""
        return False, ("Не найден бинарник whisper-cli. Соберите whisper.cpp: "
                       "bash scripts/models.sh install-engine whisper-cpp")

    def _model_file(self, settings: dict[str, Any]) -> Path:
        models_dir = Path(settings.get("models_dir") or ".") / "whisper.cpp"
        name = self.spec.source
        candidate = models_dir / f"ggml-{name}.bin"
        if candidate.exists():
            return candidate
        env_dir = os.environ.get("ASRHUB_WHISPER_CPP_MODELS")
        if env_dir:
            alt = Path(env_dir) / f"ggml-{name}.bin"
            if alt.exists():
                return alt
        raise ModelNotDownloaded(self.spec.id, self.spec.disk_mb)

    def _load(self, settings: dict[str, Any]) -> Any:
        binary = self._find_binary()
        if not binary:
            raise BinaryMissing(
                "whisper-cli",
                "Соберите whisper.cpp: bash scripts/models.sh install-engine whisper-cpp")
        return {"binary": binary, "model": str(self._model_file(settings))}

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        binary = self._model["binary"]
        model_file = self._model["model"]

        with tempfile.TemporaryDirectory(prefix="whispercpp-",
                                         dir=settings.get("temp_dir") or None) as tmp:
            out_base = Path(tmp) / "out"
            cmd = [
                binary, "-m", model_file, "-f", str(audio_path),
                "-oj", "-of", str(out_base),
                "-t", str(int(settings.get("cpu_threads") or 0) or os.cpu_count() or 4),
                "-bs", str(int(settings.get("beam_size") or 5)),
                "-bo", str(int(settings.get("best_of") or 5)),
                "-tp", str(float(settings.get("temperature") or 0.0)),
                "-et", str(float(settings.get("compression_ratio_threshold") or 2.4)),
                "-lpt", str(float(settings.get("logprob_threshold") or -1.0)),
                "-nth", str(float(settings.get("no_speech_threshold") or 0.6)),
            ]
            language = self.language_for(settings)
            cmd += ["-l", language or "auto"]
            if str(settings.get("task")) == "translate":
                cmd.append("-tr")
            if not settings.get("condition_on_previous_text", False):
                cmd += ["-mc", "0"]
            if not settings.get("temperature_fallback", True):
                cmd.append("-nf")
            if settings.get("word_timestamps", True):
                cmd += ["-dtw", self.spec.source.split("-q")[0]]
            if not settings.get("flash_attention", True):
                cmd.append("-nfa")
            if self.resolve_device(settings) == "cpu":
                cmd.append("-ng")
            prompt = str(settings.get("initial_prompt") or "").strip()
            if prompt:
                cmd += ["--prompt", prompt]
                if settings.get("carry_initial_prompt"):
                    cmd.append("--carry-initial-prompt")
            if settings.get("vad_enabled", True):
                vad_model = os.environ.get("ASRHUB_WHISPER_CPP_VAD", "")
                if vad_model and Path(vad_model).exists():
                    cmd += ["--vad", "-vm", vad_model,
                            "-vt", str(float(settings.get("vad_threshold") or 0.5)),
                            "-vspd", str(int(settings.get("vad_min_speech_ms") or 250)),
                            "-vsd", str(int(settings.get("vad_min_silence_ms") or 500)),
                            "-vp", str(int(settings.get("vad_speech_pad_ms") or 200))]

            self.report(progress, 0.1, "распознавание")
            try:
                res = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=int(settings.get("job_timeout_s") or 7200) or None,
                                     check=False)
            except subprocess.TimeoutExpired as exc:
                raise EngineError("whisper.cpp не завершился за отведённое время.") from exc
            except OSError as exc:
                raise BinaryMissing("whisper-cli", str(exc)) from exc

            if res.returncode != 0:
                raise EngineError(
                    "whisper.cpp завершился с ошибкой.",
                    hint=(res.stderr or "").strip()[-800:] or "Проверьте файл модели и параметры.",
                    details={"returncode": res.returncode})

            json_path = out_base.with_suffix(".json")
            if not json_path.exists():
                raise EngineError("whisper.cpp не создал файл результата.",
                                  hint=(res.stderr or "")[-500:])
            data = json.loads(json_path.read_text(encoding="utf-8"))

        segments: list[Segment] = []
        for item in data.get("transcription", []):
            offsets = item.get("offsets", {})
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", 0)) / 1000.0
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            tokens = item.get("tokens") or []
            probs = [float(t.get("p", 0)) for t in tokens if isinstance(t, dict) and "p" in t]
            words = []
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                word = str(token.get("text", "")).strip()
                if not word or word.startswith("[") or word.startswith("<"):
                    continue
                offs = token.get("offsets", {})
                words.append({
                    "word": word,
                    "start": round(float(offs.get("from", offsets.get("from", 0))) / 1000.0, 3),
                    "end": round(float(offs.get("to", offsets.get("to", 0))) / 1000.0, 3),
                    "confidence": round(float(token.get("p", 0.0)), 4),
                })
            segments.append(Segment(
                start=start, end=end, text=text,
                confidence=round(sum(probs) / len(probs), 4) if probs else None,
                language=str(data.get("result", {}).get("language") or ""),
                words=words if settings.get("word_timestamps", True) else [],
            ))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments,
            language=str(data.get("result", {}).get("language") or ""),
            duration=segments[-1].end if segments else 0.0,
            meta={"binary": binary, "model_file": model_file},
        )
