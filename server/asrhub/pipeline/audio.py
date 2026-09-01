"""Предобработка аудио: анализ, конвертация, нормализация, нарезка.

Вся тяжёлая работа делегируется ffmpeg — он есть на всех трёх целевых
платформах и обрабатывает практически любые контейнеры, включая видео.
Если ffmpeg недоступен, модуль умеет читать несжатый WAV средствами
стандартной библиотеки, чтобы сервер оставался работоспособным.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import AudioError, AudioTooLong, BinaryMissing, UnsupportedFormat
from ..logging_setup import get_logger

log = get_logger("audio")

AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".amr", ".ac3", ".caf", ".mp2", ".w64",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp",
}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(slots=True)
class AudioInfo:
    path: str
    duration_s: float
    sample_rate: int
    channels: int
    codec: str
    bitrate: int
    format_name: str
    size_bytes: int
    has_video: bool = False
    peak_db: float | None = None
    rms_db: float | None = None
    silence_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise BinaryMissing(
            "ffmpeg",
            "ffmpeg нужен для чтения сжатых форматов и видео. "
            "Debian/Ubuntu: apt install ffmpeg. macOS: brew install ffmpeg. "
            "Windows: winget install Gyan.FFmpeg или choco install ffmpeg.")
    return exe


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """Быстрый хеш файла: начало, конец и размер. Для кеша этого достаточно."""
    digest = hashlib.blake2b(digest_size=16)
    size = path.stat().st_size
    digest.update(str(size).encode())
    with path.open("rb") as fh:
        digest.update(fh.read(chunk))
        if size > chunk * 2:
            fh.seek(-chunk, os.SEEK_END)
            digest.update(fh.read(chunk))
    return digest.hexdigest()


def probe(path: Path) -> AudioInfo:
    """Определяет параметры файла. Использует ffprobe, при его отсутствии — WAV-разбор."""
    path = Path(path)
    if not path.exists():
        raise AudioError(f"Файл не найден: {path}")
    if path.stat().st_size == 0:
        raise AudioError(f"Файл пуст: {path.name}")

    suffix = path.suffix.lower()
    probe_exe = _ffprobe()
    if probe_exe:
        cmd = [probe_exe, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.SubprocessError as exc:
            raise AudioError(f"ffprobe не смог прочитать файл: {exc}") from exc
        if res.returncode != 0:
            raise UnsupportedFormat(path.name, (res.stderr or "").strip()[:200])
        try:
            data = json.loads(res.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise AudioError(f"Не удалось разобрать вывод ffprobe: {exc}") from exc

        streams = data.get("streams", [])
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        fmt = data.get("format", {})
        if audio is None:
            raise AudioError(
                f"В файле «{path.name}» нет звуковой дорожки.",
                hint="Проверьте файл: возможно, это видео без звука или повреждённый контейнер.")
        duration = float(audio.get("duration") or fmt.get("duration") or 0.0)
        return AudioInfo(
            path=str(path),
            duration_s=duration,
            sample_rate=int(audio.get("sample_rate") or 0),
            channels=int(audio.get("channels") or 1),
            codec=str(audio.get("codec_name") or ""),
            bitrate=int(float(fmt.get("bit_rate") or 0)),
            format_name=str(fmt.get("format_name") or ""),
            size_bytes=path.stat().st_size,
            has_video=video is not None and video.get("codec_name") not in ("mjpeg", "png"),
        )

    if suffix != ".wav":
        raise UnsupportedFormat(
            path.name,
            "ffprobe недоступен — без него читаются только несжатые файлы WAV")
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return AudioInfo(
                path=str(path),
                duration_s=frames / rate if rate else 0.0,
                sample_rate=rate,
                channels=wf.getnchannels(),
                codec="pcm",
                bitrate=rate * wf.getnchannels() * wf.getsampwidth() * 8,
                format_name="wav",
                size_bytes=path.stat().st_size,
            )
    except wave.Error as exc:
        raise AudioError(f"Не удалось прочитать WAV: {exc}") from exc


def analyze_levels(path: Path, max_seconds: float = 600.0) -> dict[str, float]:
    """Измеряет уровни громкости и долю тишины — используется в аналитике качества."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return {}
    cmd = [exe, "-hide_banner", "-nostats", "-t", str(max_seconds), "-i", str(path),
           "-af", "volumedetect,silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    except subprocess.SubprocessError:
        return {}
    out: dict[str, float] = {}
    silence_total = 0.0
    for line in (res.stderr or "").splitlines():
        if "max_volume:" in line:
            try:
                out["peak_db"] = float(line.split("max_volume:")[1].strip().split(" ")[0])
            except (ValueError, IndexError):
                pass
        elif "mean_volume:" in line:
            try:
                out["rms_db"] = float(line.split("mean_volume:")[1].strip().split(" ")[0])
            except (ValueError, IndexError):
                pass
        elif "silence_duration:" in line:
            try:
                silence_total += float(line.split("silence_duration:")[1].strip().split(" ")[0])
            except (ValueError, IndexError):
                pass
    if silence_total:
        out["silence_seconds"] = round(silence_total, 2)
    return out


def build_filter_chain(settings: dict[str, Any]) -> str:
    """Собирает цепочку фильтров ffmpeg по настройкам задания."""
    filters: list[str] = []

    highpass = int(settings.get("audio_highpass_hz") or 0)
    if highpass > 0:
        filters.append(f"highpass=f={highpass}")

    denoise = str(settings.get("audio_denoise") or "none")
    if denoise == "afftdn":
        filters.append("afftdn=nf=-25")
    elif denoise == "arnndn":
        model = os.environ.get("ASRHUB_ARNNDN_MODEL", "")
        if model and Path(model).exists():
            filters.append(f"arnndn=m={model}")
        else:
            log.warning("arnndn выбран, но файл модели не задан — шумоподавление пропущено")

    if settings.get("audio_trim_silence"):
        filters.append(
            "silenceremove=start_periods=1:start_duration=0.1:start_threshold=-45dB:"
            "detection=peak,areverse,"
            "silenceremove=start_periods=1:start_duration=0.1:start_threshold=-45dB:"
            "detection=peak,areverse")

    speed = float(settings.get("audio_speed") or 1.0)
    if abs(speed - 1.0) > 1e-3:
        remaining = speed
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        while remaining < 0.5:
            filters.append("atempo=0.5")
            remaining /= 0.5
        filters.append(f"atempo={remaining:.4f}")

    if settings.get("audio_normalize"):
        target = float(settings.get("audio_target_lufs") or -18.0)
        filters.append(f"loudnorm=I={target}:TP=-1.5:LRA=11")

    return ",".join(filters)


def convert(src: Path, dst: Path, settings: dict[str, Any], *,
            channel: str | None = None, start_s: float | None = None,
            duration_s: float | None = None) -> Path:
    """Приводит файл к WAV 16 бит с нужной частотой и каналом.

    channel: None — по настройке audio_channels; «left»/«right» — конкретный канал.
    """
    exe = _ffmpeg()
    rate = int(settings.get("audio_sample_rate") or 16000)
    mode = channel or str(settings.get("audio_channels") or "mono")

    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start_s is not None:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(src)]
    if duration_s is not None:
        cmd += ["-t", f"{duration_s:.3f}"]

    chain = build_filter_chain(settings)
    if mode == "left":
        chain = ("pan=mono|c0=c0," + chain) if chain else "pan=mono|c0=c0"
    elif mode == "right":
        chain = ("pan=mono|c0=c1," + chain) if chain else "pan=mono|c0=c1"
    if chain:
        cmd += ["-af", chain]

    cmd += ["-vn", "-sn", "-dn", "-ac", "1", "-ar", str(rate),
            "-acodec", "pcm_s16le", "-f", "wav", str(dst)]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("Конвертация аудио не завершилась за час.",
                         hint="Файл слишком большой или ffmpeg завис.") from exc
    except OSError as exc:
        raise AudioError(f"Не удалось запустить ffmpeg: {exc}") from exc

    if res.returncode != 0 or not dst.exists() or dst.stat().st_size < 128:
        stderr = (res.stderr or "").strip()
        raise AudioError(
            f"ffmpeg не смог обработать «{src.name}».",
            hint=("Проверьте целостность файла. Сообщение ffmpeg: " + stderr[-500:])
                 if stderr else "Проверьте целостность файла.",
            details={"returncode": res.returncode, "stderr": stderr[-2000:]})
    return dst


def channel_count(path: Path) -> int:
    try:
        return probe(path).channels
    except AudioError:
        return 1


def prepare(src: Path, workdir: Path, settings: dict[str, Any]) -> list[tuple[str, Path]]:
    """Готовит один или несколько WAV-файлов к распознаванию.

    Возвращает список пар (метка канала, путь). Для режима «split» на
    стереозаписи получится два файла.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    info = probe(src)

    limit = int(settings.get("audio_max_duration_s") or 0)
    if limit and info.duration_s > limit:
        raise AudioTooLong(info.duration_s, limit)
    if info.duration_s <= 0.05:
        raise AudioError(f"Длительность файла «{src.name}» близка к нулю.",
                         hint="Возможно, файл повреждён или содержит только заголовок.")

    mode = str(settings.get("audio_channels") or "mono")
    if mode == "split" and info.channels >= 2:
        outputs: list[tuple[str, Path]] = []
        for label, channel in (("Канал 1", "left"), ("Канал 2", "right")):
            dst = workdir / f"{src.stem}.{channel}.wav"
            convert(src, dst, settings, channel=channel)
            outputs.append((label, dst))
        return outputs

    dst = workdir / f"{src.stem}.prepared.wav"
    convert(src, dst, settings)
    return [("", dst)]


def read_wav_mono(path: Path) -> tuple[list[float], int]:
    """Читает WAV в список float в диапазоне [-1, 1]. Без numpy — для лёгких задач."""
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2:
                raise AudioError("Ожидается 16-битный WAV.")
            rate = wf.getframerate()
            channels = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
    except wave.Error as exc:
        raise AudioError(f"Не удалось прочитать WAV: {exc}") from exc
    count = len(raw) // 2
    values = struct.unpack(f"<{count}h", raw[:count * 2])
    if channels > 1:
        values = tuple(
            sum(values[i:i + channels]) / channels for i in range(0, len(values), channels))
    return [v / 32768.0 for v in values], rate


def load_samples(path: Path):
    """Загружает отсчёты как numpy-массив float32, если numpy доступен."""
    try:
        import numpy as np  # type: ignore
    except ModuleNotFoundError:
        values, rate = read_wav_mono(path)
        return values, rate
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def write_wav(path: Path, samples, sample_rate: int = 16000) -> Path:
    """Записывает отсчёты float в WAV 16 бит."""
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(samples, dtype="float32")
        clipped = np.clip(arr, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype("<i2").tobytes()
    except ModuleNotFoundError:
        pcm = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, float(v))) * 32767)) for v in samples)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return path


def slice_wav(src: Path, dst: Path, start_s: float, end_s: float) -> Path:
    """Вырезает фрагмент из подготовленного WAV без повторного вызова ffmpeg."""
    with wave.open(str(src), "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        total = wf.getnframes()
        begin = max(0, int(start_s * rate))
        finish = min(total, int(math.ceil(end_s * rate)))
        if finish <= begin:
            raise AudioError(f"Некорректный интервал: {start_s:.2f}–{end_s:.2f} с")
        wf.setpos(begin)
        frames = wf.readframes(finish - begin)
    with wave.open(str(dst), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)
    return dst


def estimate_processing_time(duration_s: float, rtf: float) -> float:
    return duration_s * max(rtf, 0.001)


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
