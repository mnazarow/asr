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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import (
    ASRHubError,
    AudioError,
    AudioTooLong,
    BinaryMissing,
    NoSpeechDetected,
    UnsupportedFormat,
    без_путей,
)
from ..logging_setup import get_logger

log = get_logger("audio")

#: Меньше этого размера выход ffmpeg не несёт ни одного отсчёта: у WAV
#: только заголовок (44 байта), у прочих контейнеров — служебные блоки.
ПУСТОЙ_WAV = 128

#: Ниже этого пика запись считается молчащей. -50 дБ — с запасом: шум линии
#: и фон в комнате дают -40…-45, речь даже шёпотом громче.
ТИШИНА_ДБ = -50.0

#: Что делать с записью, в которой одна тишина. Текст один на два случая —
#: срез ушёл за конец и обрезка съела всё, — потому что делать в них нужно
#: одно и то же.
_СОВЕТ_ТИШИНА = (
    "Это не поломка файла и не сбой сервера: в записи нет звука. Если тишины "
    "быть не должно — проверьте запись разговоров на станции. Если такие "
    "разговоры обычны (сняли трубку и положили), отсейте их на приёме: "
    "«Наименьшая длительность» и «Пропускать неотвеченные» в настройках "
    "телефонии. Чтобы они доходили до распознавания и возвращали пустой "
    "текст, выключите «Обрезать тишину»."
)

#: Частота, на которой работает DeepFilterNet. Не настройка: модель обучена
#: на 48 кГц и другой не понимает.
DEEPFILTER_ЧАСТОТА = 48000

#: Сколько секунд записи мерить, решая, нужна ли очистка. Минуты хватает: мы
#: выбираем между «чистить» и «не чистить», а не считаем паспорт записи.
ЗАМЕР_СЕКУНД = 60.0

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
        raise AudioError(f"Файл не найден: {path.name}")
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
            raise AudioError(
                f"ffprobe не смог прочитать файл: {без_путей(str(exc))}") from exc
        if res.returncode != 0:
            raise UnsupportedFormat(path.name,
                                    без_путей((res.stderr or "").strip())[:200])
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


def silence_bounds(src: Path, settings: dict[str, Any],
                   duration_s: float | None = None) -> tuple[float, float | None]:
    """Тишина по краям записи: (сколько секунд её в начале, где начинается
    тишина в конце — или None, если запись не кончается тишиной).

    Один проход `silencedetect`, который читает звук потоком. Раньше конец
    обрезался цепочкой `areverse,silenceremove,areverse`, а `areverse`
    держит в памяти всю запись: 210 МБ на минуту стерео 48 кГц, двухчасовое
    совещание — около 25 ГБ, запись на четыре часа (предел по умолчанию) —
    около 50 ГБ на каждое задание. Ядро убивало ffmpeg, и задание падало с
    ложным «проверьте целостность файла», а то и весь сервер.

    Начало нужно величиной — на неё потом сдвигаются обратно все таймкоды;
    конец — точкой, до которой читать (`-t`). Пороги (-45 дБ, 0.1 с) те же,
    что были у обрезки фильтрами.
    """
    if not settings.get("audio_trim_silence"):
        return 0.0, None
    exe = _ffmpeg()
    cmd = [exe, "-hide_banner", "-nostdin", "-i", str(src),
           "-af", "silencedetect=noise=-45dB:d=0.1", "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError):
        return 0.0, None
    отрезки: list[list[float | None]] = []
    длительность = duration_s
    for line in (res.stderr or "").splitlines():
        try:
            if "silence_start:" in line:
                отрезки.append(
                    [float(line.split("silence_start:")[1].strip().split(" ")[0]), None])
            elif "silence_end:" in line and отрезки:
                отрезки[-1][1] = float(line.split("silence_end:")[1].strip().split(" ")[0])
            elif длительность is None and "time=" in line:
                # Итог прохода: «… time=00:02:05.12 …» — длина, если её не дали.
                часы, минуты, секунды = line.split("time=")[1].split(" ")[0].split(":")
                длительность = int(часы) * 3600 + int(минуты) * 60 + float(секунды)
        except (ValueError, IndexError):
            continue
    начало = 0.0
    if отрезки and (отрезки[0][0] or 0.0) <= 0.05:
        # Интересует только тишина, с которой запись начинается.
        начало = max(0.0, float(отрезки[0][1] if отрезки[0][1] is not None
                                 else (длительность or 0.0)))
    конец: float | None = None
    if отрезки:
        последний_старт, последний_конец = отрезки[-1]
        # Тишина идёт до самого конца: у последнего отрезка нет окончания
        # (так пишут старые ffmpeg) или окончание совпадает с концом файла
        # (так пишут новые).
        до_конца = последний_конец is None or (
            длительность is not None and последний_конец >= длительность - 0.05)
        if до_конца and последний_старт is not None and последний_старт > начало + 0.05:
            конец = float(последний_старт)
    return начало, конец


def lead_silence_s(src: Path, settings: dict[str, Any]) -> float:
    """Сколько секунд тишины в начале записи (см. `silence_bounds`)."""
    return silence_bounds(src, settings)[0]


def deepfilter_путь() -> str | None:
    """Путь к бинарю `deep-filter`, если он на машине есть.

    Почему отдельный бинарь, а не пакет с PyPI: питоновский DeepFilterNet
    собран расширением на Rust, и колёс новее Python 3.11 у него нет —
    установить его рядом с сервером на 3.14 нельзя вовсе. Готовый бинарь с
    той же моделью лежит в релизах проекта и ни от какого Python не зависит.
    """
    задан = os.environ.get("ASRHUB_DEEPFILTER", "").strip()
    if задан:
        return задан if Path(задан).is_file() else None
    return shutil.which("deep-filter")


def очистить_deepfilter(src: Path, workdir: Path,
                        settings: dict[str, Any]) -> tuple[Path, str]:
    """Прогоняет запись через DeepFilterNet. Возвращает (файл, замечание).

    Это единственное шумоподавление в наборе, у которого есть ЗАМЕРЕННАЯ
    польза для распознавания, а не только для человеческого уха: на
    умеренном шуме (отношение сигнал/шум от нуля децибел и выше) ошибка
    распознавания падает вдвое — 9,7 % против 4,4 % на наборе DNS. Там же
    измерена и обратная сторона: на очень шумной записи та же модель ошибку
    УВЕЛИЧИВАЕТ. Поэтому она идёт в паре с порогом (см. `нужна_очистка`),
    а не включается на всё подряд.

    Работает модель на 48 кГц, поэтому проход обкладывается ресемплингом.
    На телефонных 8 кГц это означает пустую верхнюю половину спектра —
    случай, в котором модель видит не то, на чём обучалась. Для 8 кГц
    очистку стоит включать только после собственного замера.
    """
    бинарь = deepfilter_путь()
    if бинарь is None:
        return src, ("DeepFilterNet выбран, но программа deep-filter не найдена — "
                     "шумоподавление пропущено")
    exe = _ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    на_вход = workdir / f"{src.stem}.dfn-in.wav"
    каталог = workdir / f"{src.stem}.dfn-out"
    каталог.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(src), "-vn", "-sn", "-dn", "-ac", "1",
             "-ar", str(DEEPFILTER_ЧАСТОТА), "-acodec", "pcm_s16le",
             str(на_вход)], capture_output=True, text=True, timeout=1800, check=True)
        subprocess.run([бинарь, "-o", str(каталог), str(на_вход)],
                       capture_output=True, text=True, timeout=3600, check=True)
    except (OSError, subprocess.SubprocessError) as сбой:
        log.warning("DeepFilterNet не отработал: %s", сбой)
        return src, f"DeepFilterNet не отработал ({сбой}) — шумоподавление пропущено"
    очищенный = каталог / на_вход.name
    if not очищенный.is_file() or очищенный.stat().st_size < ПУСТОЙ_WAV:
        # Пустой выход — это отказ, а не тишина: подавать его дальше значит
        # заменить разговор на ничто и назвать это обработкой.
        return src, "DeepFilterNet вернул пустой файл — взята исходная запись"
    return очищенный, ""


def оценить_snr(src: Path, секунд: float = ЗАМЕР_СЕКУНД) -> float | None:
    """Отношение сигнал/шум исходной записи, дБ. None — померить не вышло.

    Меряется по началу записи и без единого фильтра: решение «чистить или
    не чистить» принимается по тому, что пришло, а не по тому, что уже
    обработали.
    """
    exe = _ffmpeg()
    временный = src.with_suffix(".snr.wav")
    try:
        subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-t", f"{max(1.0, секунд):.1f}", "-i", str(src),
             "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
             "-acodec", "pcm_s16le", str(временный)],
            capture_output=True, text=True, timeout=600, check=True)
        from . import audio_profile  # noqa: PLC0415

        профиль = audio_profile.profile_file(временный, max_seconds=секунд)
        значение = профиль.get("snr_db")
        return float(значение) if значение is not None else None
    except (OSError, subprocess.SubprocessError, ASRHubError, ValueError, TypeError) as сбой:
        log.debug("SNR исходника не померен: %s", сбой)
        return None
    finally:
        временный.unlink(missing_ok=True)


def нужна_очистка(src: Path, settings: dict[str, Any]) -> tuple[bool, str]:
    """Стоит ли вообще чистить ЭТУ запись. Возвращает (да/нет, замечание).

    Безусловная очистка всего потока — приём, который измерения последних
    лет не подтверждают: на чистой записи любой шумоподавитель убирает
    вместе с шумом часть речи, и распознавание становится ХУЖЕ. Отдельная
    работа 2025 года прогнала одну из популярных моделей по сорока
    сочетаниям «запись × шум» и не нашла ни одного, где ошибка уменьшилась
    бы; в других работах выигрыш есть, но только на умеренном шуме.

    Отсюда правило: чистим по порогу. Запись, у которой сигнал/шум выше
    порога, идёт в распознавание как есть — и это не экономия времени, а
    прямая забота о точности.
    """
    порог = S.num(settings, "audio_denoise_below_snr_db", 0.0)
    if порог <= 0:
        return True, ""                      # порог не задан — как раньше
    snr = оценить_snr(src)
    if snr is None:
        return True, ""                      # не померили — не отменяем
    if snr >= порог:
        return False, (f"шумоподавление пропущено: сигнал/шум {snr:.0f} дБ "
                       f"выше порога {порог:g} дБ")
    return True, ""


def build_filter_chain(settings: dict[str, Any]) -> str:
    """Собирает цепочку фильтров ffmpeg по настройкам задания."""
    filters: list[str] = []

    highpass = S.integer(settings, "audio_highpass_hz", 0)
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
    # deepfilternet в цепочку фильтров не попадает: он работает отдельным
    # проходом до неё (см. `очистить_deepfilter`). Причина в частоте: модель
    # работает на 48 кГц и требует своего ресемплинга туда и обратно.

    # Тишина по краям (`audio_trim_silence`) фильтрами не режется: начало
    # снимается через -ss на отмеренную величину — она потом возвращается
    # ко всем таймкодам, — а конец через -t по отмеренной точке (см.
    # `silence_bounds`). Прежняя цепочка `areverse,…,areverse` держала в
    # памяти всю запись целиком.

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
    # Длина — ключом ВХОДА, до -i: она считается по исходному файлу, а не по
    # выходу, который после atempo короче или длиннее.
    if duration_s is not None:
        cmd += ["-t", f"{duration_s:.3f}"]
    cmd += ["-i", str(src)]

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
        raise AudioError(
            f"Не удалось запустить ffmpeg: {без_путей(str(exc))}") from exc

    размер = dst.stat().st_size if dst.exists() else 0
    # Размер и время исходника на момент отказа. Если файл в этот момент был
    # меньше, чем окажется потом, значит его читали, пока он ещё дописывался,
    # — и это видно сразу, вместо поисков причины в исправном файле.
    источник = _отпечаток(src)
    # «ffmpeg отказался» и «ffmpeg отработал, а звука не осталось» — разные
    # новости, и раньше они выглядели одинаково: «не смог обработать,
    # проверьте целостность файла». Второй случай при этом самый частый на
    # телефонии — молчаливый разговор, — и подсказка отправляла проверять
    # исправный файл. Отличаются они кодом возврата: при успехе ffmpeg
    # молчит, и в сообщении не оказывалось даже его слов.
    if res.returncode == 0 and размер < ПУСТОЙ_WAV:
        отказ = _пустой_результат(src, settings, start_s)
        отказ.details.update(источник)
        raise отказ
    # Обрезка тишины раньше шла фильтром ПОСЛЕ остальных, и запись, из
    # которой фильтры (или сама станция) не оставили ни одного громкого
    # отсчёта, выходила пустой — с объяснением, что виновато: тишина в
    # записи или фильтр. Теперь тишина по краям меряется до фильтров
    # (`silence_bounds`), и ту же проверку делаем явно: выход без единого
    # громкого отсчёта — тот же «пустой результат». Замер по готовому WAV
    # 16 кГц — быстрый проход без памяти под всю запись.
    if res.returncode == 0 and settings.get("audio_trim_silence") \
            and peak_db(dst) <= ТИШИНА_ДБ:
        отказ = _пустой_результат(src, settings, start_s)
        отказ.details.update(источник)
        raise отказ
    if res.returncode != 0 or размер < ПУСТОЙ_WAV:
        stderr = (res.stderr or "").strip()
        raise AudioError(
            f"ffmpeg не смог обработать «{src.name}».",
            # Вывод ffmpeg несёт полный путь к файлу, а подсказка уходит
            # клиенту и на заданный им адрес уведомления. Имя файла в ней
            # остаётся, каталог — нет.
            hint=("Проверьте целостность файла. Сообщение ffmpeg: "
                  + без_путей(stderr)[-500:])
                 if stderr else "Проверьте целостность файла.",
            details={"returncode": res.returncode, "stderr": stderr[-2000:],
                     **источник})
    return dst


def _отпечаток(src: Path) -> dict[str, Any]:
    """Размер и время правки файла — в том виде, в каком они были при отказе."""
    try:
        сведения = src.stat()
    except OSError:
        return {"source_bytes": 0}
    return {"source_bytes": сведения.st_size,
            "source_mtime": round(сведения.st_mtime, 3)}


def peak_db(src: Path) -> float:
    """Пиковая громкость записи в дБ. -inf (как -120) — ни одного отсчёта.

    Нужна там, где иначе пришлось бы гадать: «на выходе пусто» бывает и от
    тишины в записи, и от фильтров, которые эту запись выхолостили. Это
    разные починки — на станции и в настройках, — и отличить их можно только
    померив исходник.
    """
    exe = _ffmpeg()
    cmd = [exe, "-hide_banner", "-nostdin", "-i", str(src),
           "-af", "volumedetect", "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError):
        return 0.0                      # померить не вышло — не выдумываем
    for line in (res.stderr or "").splitlines():
        if "max_volume:" in line:
            try:
                return float(line.split("max_volume:")[1].strip().split(" ")[0])
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def _пустой_результат(src: Path, settings: dict[str, Any],
                      start_s: float | None) -> ASRHubError:
    """Называет причину, по которой после ffmpeg не осталось звука.

    Причин три, и лечатся они в трёх разных местах: тишина в записи — на
    станции, пустой файл — там же, но иначе, а отрезок мимо записи — в
    настройках задания. Общий ответ «проверьте целостность файла» не годится
    ни для одной: файл при этом целый.
    """
    try:
        длительность = probe(src).duration_s
    except ASRHubError:
        длительность = 0.0

    if длительность <= 0.05:
        return AudioError(
            f"В записи «{src.name}» нет ни одного отсчёта звука.",
            hint="Файл создан, но звук в него не попал. Это вопрос к тому, кто "
                 "его записал: на Asterisk — MixMonitor без ${UNIQUEID} или "
                 "права на каталог записей.",
            details={"duration_s": длительность, "empty_output": True})

    # Дальше догадка была бы дешёвой и неверной: «включена обрезка тишины —
    # значит, тишина». Обрезка убирает и то, что выхолостили фильтры, и
    # громкая запись выглядит тогда молчаливой. Поэтому исходник меряется.
    пик = peak_db(src)
    # Пометка «на выходе пусто» отличает этот отказ от настоящего сбоя
    # ffmpeg. Разница важна для стереозаписи: пустой канал — это факт о
    # записи, и терять из-за него второго собеседника нельзя, а вот битый
    # файл обязан остаться отказом.
    подробности: dict[str, Any] = {"duration_s": длительность, "peak_db": пик,
                                   "empty_output": True}
    if start_s is not None:
        подробности["start_s"] = start_s

    if пик <= ТИШИНА_ДБ:
        return NoSpeechDetected(
            f"Запись «{src.name}» молчит: за все {длительность:.1f} с "
            f"громкость не поднялась выше {пик:.0f} дБ.",
            hint=_СОВЕТ_ТИШИНА, details=подробности)

    # Запись громкая, а на выходе пусто — значит, звук убрали по дороге.
    # Цепочка фильтров в подсказке: без неё человек идёт проверять запись,
    # которая заведомо в порядке.
    цепочка = build_filter_chain(settings)
    подробности["filters"] = цепочка
    частота = 0
    try:
        частота = probe(src).sample_rate
    except ASRHubError:
        pass
    подробности["sample_rate"] = частота
    return AudioError(
        f"Запись «{src.name}» звучит ({пик:.0f} дБ), но после обработки "
        f"звука не осталось.",
        hint=_совет_фильтры(settings, частота, цепочка), details=подробности)


def _совет_фильтры(settings: dict[str, Any], частота: int, цепочка: str) -> str:
    """Что именно из обработки могло выхолостить запись.

    Порядок подозреваемых не случаен: первым идёт фильтр высоких частот с
    порогом выше половины частоты дискретизации. На телефонных записях (8 кГц,
    потолок 4 кГц) он срезает весь голос, и это самая частая из причин.
    """
    части = ["Файл целый и звучит — значит, звук убрала обработка, а не "
             "запись."]
    порог = S.integer(settings, "audio_highpass_hz", 0)
    # Порог сравнивается с частотой записи, а не с абстрактным числом.
    # Телефонная запись идёт на 8 кГц, и выше 4 кГц в ней нет ничего: один и
    # тот же фильтр безобиден на студийной записи и смертелен на звонке.
    if порог and частота:
        потолок = частота // 2
        if порог >= потолок:
            части.append(
                f"«Фильтр высоких частот» стоит на {порог} Гц, а запись идёт "
                f"на {частота} Гц — выше {потолок} Гц в ней нет ничего. "
                f"Поставьте 0 или не выше 200.")
        elif порог * 4 >= частота:
            части.append(
                f"«Фильтр высоких частот» стоит на {порог} Гц при записи на "
                f"{частота} Гц — это почти весь её диапазон (потолок "
                f"{потолок} Гц). Для речи хватает 80–200 Гц.")
    elif порог >= 1000:
        части.append(f"Первый подозреваемый — «Фильтр высоких частот» "
                     f"({порог} Гц): для речи хватает 80–200 Гц.")
    if str(settings.get("audio_denoise") or "none") != "none":
        части.append("Проверьте «Шумоподавление»: временно поставьте none.")
    if settings.get("audio_trim_silence"):
        части.append("«Обрезать тишину» убирает то, что осталось после "
                     "фильтров, — выключите её, чтобы увидеть результат.")
    if цепочка:
        части.append(f"Цепочка обработки: {цепочка}")
    return " ".join(части)


def channel_count(path: Path) -> int:
    try:
        return probe(path).channels
    except AudioError:
        return 1


@dataclass(slots=True)
class Prepared:
    """Подготовленные файлы вместе со сдвигом системы координат.

    Подготовка меняет время: обрезка начальной тишины сдвигает всё на
    `offset_s`, изменение темпа сжимает или растягивает в `speed` раз.
    Движок работает уже в новых координатах, поэтому исходное время
    считается как `offset_s + t * speed`. Без этих двух чисел таймкоды
    в субтитрах не соответствуют исходной записи.
    """

    channels: list[tuple[str, Path]]
    offset_s: float = 0.0
    speed: float = 1.0
    #: Каналы, от которых после обработки не осталось звука. Не отказ:
    #: в телефонной стереозаписи молчащий канал — обычное дело, а второй
    #: собеседник при этом говорит и должен быть распознан.
    silent: list[str] = field(default_factory=list)
    #: Что стоит сказать человеку о подготовке. Едет в предупреждения задания.
    warnings: list[str] = field(default_factory=list)

    def to_source_time(self, value: float) -> float:
        """Время подготовленного файла -> время исходной записи."""
        return self.offset_s + value * self.speed

    def to_prepared_time(self, value: float) -> float:
        """Обратный перевод: время исходной записи — во время подготовленной.

        Нужен там, где реплики уже вернулись в координаты исходника, а
        работать надо по подготовленному звуку: полоса громкости по
        говорящим индексирует отсчёты подготовленного файла, и брать для
        этого исходные секунды значит промахнуться ровно на длину
        обрезанной тишины.
        """
        if self.speed <= 0:
            return max(0.0, value - self.offset_s)
        return max(0.0, (value - self.offset_s) / self.speed)

    @property
    def shifted(self) -> bool:
        return abs(self.offset_s) > 1e-3 or abs(self.speed - 1.0) > 1e-3


def prepare(src: Path, workdir: Path, settings: dict[str, Any]) -> Prepared:
    """Готовит один или несколько WAV-файлов к распознаванию.

    Возвращает подготовленные каналы и сдвиг системы координат.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    info = probe(src)

    limit = S.integer(settings, "audio_max_duration_s", 0)
    if limit and info.duration_s > limit:
        raise AudioTooLong(info.duration_s, limit)
    if info.duration_s <= 0.05:
        raise AudioError(f"Длительность файла «{src.name}» близка к нулю.",
                         hint="Возможно, файл повреждён или содержит только заголовок.")

    замечания: list[str] = []
    # Шумоподавление решается ПЕРВЫМ: и «нужно ли оно этой записи», и
    # отдельный проход DeepFilterNet, который в цепочку фильтров ffmpeg не
    # укладывается. Первым — потому что начальная тишина меряется дальше, и
    # мерить её надо по тому самому звуку, который потом обрежут. Дальше по
    # коду настройки идут уже поправленными: `audio_denoise` в них может
    # оказаться выключенным, и это осознанно.
    настройки = dict(settings)
    if str(настройки.get("audio_denoise") or "none") != "none":
        чистить, почему = нужна_очистка(src, настройки)
        if not чистить:
            настройки["audio_denoise"] = "none"
            замечания.append(почему)
        elif str(настройки.get("audio_denoise")) == "deepfilternet":
            src, беда = очистить_deepfilter(src, workdir, настройки)
            настройки["audio_denoise"] = "none"
            if беда:
                замечания.append(беда)
    settings = настройки

    # Тишина по краям отмеряется один раз на исходном файле и одинаково
    # применяется ко всем каналам: иначе левый и правый разъехались бы во
    # времени между собой.
    offset, конец_речи = silence_bounds(src, settings, info.duration_s or None)
    длина = (конец_речи - offset) if конец_речи is not None and конец_речи > offset else None
    speed = float(settings.get("audio_speed") or 1.0)

    mode = str(settings.get("audio_channels") or "mono")

    if mode in ("left", "right") and info.channels < 2:
        # Второго канала нет. `pan=mono|c0=c1` на одноканальном файле даёт
        # тишину, и отказ винил бы фильтры — при исправной записи и
        # безобидной настройке. Настройка «взять правый канал» на моно
        # означает ровно одно: брать то, что есть.
        замечания.append(
            f"Запись одноканальная, а в настройках выбран "
            f"{'левый' if mode == 'left' else 'правый'} канал — взят "
            f"единственный.")
        mode = "mono"

    if mode == "split" and info.channels >= 2:
        outputs: list[tuple[str, Path]] = []
        молчат: list[str] = []
        первый_отказ: ASRHubError | None = None
        for label, channel in (("Канал 1", "left"), ("Канал 2", "right")):
            dst = workdir / f"{src.stem}.{channel}.wav"
            try:
                convert(src, dst, settings, channel=channel, start_s=offset or None,
                        duration_s=длина)
            except ASRHubError as сбой:
                # Пустой канал — не отказ задания. В телефонной записи
                # молчит то клиент, то оператор, и раньше такой канал
                # уносил с собой уже готовую расшифровку второго: задание
                # падало целиком с «после обработки звука не осталось».
                # Настоящий сбой ffmpeg (битый файл) пометки не несёт и
                # по-прежнему останавливает всё.
                if not (getattr(сбой, "details", None) or {}).get("empty_output"):
                    raise
                первый_отказ = первый_отказ or сбой
                молчат.append(label)
                continue
            outputs.append((label, dst))
        if not outputs:
            # Молчат оба — вот это уже отказ, и текст у него точный.
            raise первый_отказ or AudioError(
                f"В записи «{src.name}» не осталось звука ни в одном канале.")
        if молчат:
            замечания.append("После обработки не осталось звука в каналах: "
                             + ", ".join(молчат))
        return Prepared(outputs, offset_s=offset, speed=speed,
                        silent=молчат, warnings=замечания)

    dst = workdir / f"{src.stem}.prepared.wav"
    # Канал передаётся явно: `convert` без него перечитывает настройку, а
    # настройка могла быть «right» на одноканальном файле — её мы уже
    # заменили на «mono» выше, и подменять обратно незачем.
    convert(src, dst, settings, channel=mode, start_s=offset or None, duration_s=длина)
    return Prepared([("", dst)], offset_s=offset, speed=speed, warnings=замечания)


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
