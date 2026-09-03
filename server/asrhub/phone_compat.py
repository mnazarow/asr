"""Приём заданий в том виде, в каком его принимает phone_asr.

Зачем это здесь. В phone_asr сложился рабочий способ передачи данных на
расшифровку: вызывающая сторона не загружает файл, а сообщает, где он лежит,
и получает ответ не в этом же запросе, а отдельным обращением на свой адрес.
Для телефонии это единственный практичный порядок: запись появляется на
файловом сервере АТС, разговор длится минуты, и держать соединение открытым
всё это время незачем.

Модуль повторяет этот порядок дословно, чтобы приложение, которое сегодня
разговаривает с phone_asr, начало разговаривать с ASR Hub без единой правки:
тот же адрес маршрута, те же имена полей на входе и в обратном вызове, тот
же смысл частей. Расхождения были бы хуже, чем отсутствие совместимости:
поле, названное почти так же, ищут глазами дольше, чем читают заново.

Что при этом остаётся своим. Задание попадает в обычную очередь ASR Hub —
со своим владельцем, квотами, приоритетом, кешем, аналитикой и карточкой в
интерфейсе. Совместимость касается только краёв: как задание приходит и как
уходит результат.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AudioError, ConfigError
from .logging_setup import get_logger

log = get_logger("phone")

#: Суффикс, который phone_asr дописывает к base_url, получая адрес обратного
#: вызова. Значение зашито и там, поэтому здесь оно тоже значение по
#: умолчанию, а не выдумка: приёмник у пользователя называется именно так.
DEFAULT_CALLBACK_SUFFIX = "/callback-endpoint.php"

#: Метки говорящих. В phone_asr первый канал всегда SPEAKER_00, второй —
#: SPEAKER_01, и принимающая сторона различает стороны разговора по ним.
SPEAKER_LEFT = "SPEAKER_00"
SPEAKER_RIGHT = "SPEAKER_01"

#: Сколько ждать файл записи. У phone_asr тайм-аут 30 секунд на файл.
DOWNLOAD_TIMEOUT_S = 30.0


def normalise_base_url(value: str) -> str:
    """Приводит base_url к тому же виду, что и phone_asr.

    Там это делает валидатор pydantic: дописывает https://, если схемы нет, и
    снимает завершающую косую черту. Повторяем дословно — иначе один и тот же
    вызов давал бы разные адреса обратного вызова у двух серверов.
    """
    text = str(value or "").strip()
    if not text:
        raise ConfigError("Не указан base_url — некуда отправлять результат.",
                          hint="Это адрес вашего приёмника; к нему будет "
                               f"дописано {DEFAULT_CALLBACK_SUFFIX}")
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    return text.strip("/")


@dataclass(slots=True)
class PhoneRequest:
    """Тело запроса на расшифровку — поля те же, что у phone_asr."""

    call_id: str
    files: list[str]
    base_url: str
    part: int = 1
    total_parts: int = 1
    swap_sides: bool = False

    def target_url(self, suffix: str = DEFAULT_CALLBACK_SUFFIX) -> str:
        return f"{self.base_url}{suffix}"

    @property
    def uuid(self) -> str:
        """Ключ части разговора: тот же вид, что и в phone_asr."""
        return f"{self.call_id}_{self.part}"

    def base_path(self) -> str:
        """Имена файлов строкой JSON — поле base_path обратного вызова.

        Именно строкой, а не списком: в схеме phone_asr это `str`, внутрь
        кладётся json.dumps. Принимающая сторона разбирает её отдельно.
        """
        return json.dumps([str(name).split("/")[-1] for name in self.files],
                          ensure_ascii=False)


@dataclass(slots=True)
class Fetched:
    """Скачанная запись, приведённая к одному файлу для очереди."""

    path: Path
    channels: int
    filename: str
    sources: list[str] = field(default_factory=list)


def download(urls: list[str], workdir: Path, *, limit_bytes: int,
             check_url: Any = None) -> Fetched:
    """Забирает один или два файла и готовит из них вход для очереди.

    Правила те же, что в phone_asr:

    * один файл — как есть; стерео разведётся по каналам дальше в конвейере,
      моно уйдёт на диаризацию;
    * два файла — первый становится левым каналом, второй правым; оба должны
      быть моно, иначе неясно, что с чем совмещать.

    Проверка адреса обязательна и вынесена наружу параметром: сервер ходит по
    этим ссылкам сам, и без проверки маршрут стал бы способом читать
    внутреннюю сеть чужими руками — file:// прочитал бы диск, а
    http://169.254.169.254 отдал бы учётные данные облака.
    """
    if not urls:
        raise ConfigError("Список files пуст — нечего расшифровывать.")
    if len(urls) > 2:
        raise ConfigError(
            f"Ожидался один файл или два, получено {len(urls)}.",
            hint="Один файл — запись целиком (моно или стерео). Два — "
                 "раздельные дорожки сторон, каждая моно.")

    workdir.mkdir(parents=True, exist_ok=True)
    local: list[Path] = []
    for index, url in enumerate(urls):
        if check_url is not None:
            check_url(url)
        local.append(_fetch(url, workdir / f"часть-{index}{_suffix(url)}",
                            limit_bytes=limit_bytes))

    if len(local) == 1:
        channels = _channel_count(local[0])
        return Fetched(path=local[0], channels=channels,
                       filename=_name_of(urls[0]), sources=list(urls))

    for path in local:
        if _channel_count(path) != 1:
            raise AudioError(
                "Когда файлов два, каждый должен быть моно — это дорожки "
                "сторон разговора.",
                hint="Стереозапись присылайте одним файлом: сервер сам "
                     "разведёт её по каналам.")
    merged = _merge_to_stereo(local[0], local[1], workdir / "разговор.wav")
    return Fetched(path=merged, channels=2, filename=_name_of(urls[0]),
                   sources=list(urls))


def _suffix(url: str) -> str:
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return Path(name).suffix or ".wav"


def _name_of(url: str) -> str:
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return name or "запись.wav"


def encode_url(url: str) -> str:
    """Приводит адрес к виду, который принимает urllib.

    В нём путь обязан быть уже закодированным: буква вне ASCII валит запрос
    с «'ascii' codec can't encode characters», не дойдя до сети. phone_asr
    этого не замечал — aiohttp кодирует сам, — а имена записей на АТС
    кириллические сплошь и рядом. Уже закодированные адреса не трогаем:
    повторное кодирование превратило бы %20 в %2520.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(url)
    path = quote(parts.path, safe="/%:@&=+$,~")
    query = quote(parts.query, safe="/?:@&=+$,~%")
    try:
        netloc = parts.netloc.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        netloc = parts.netloc              # оставляем как есть: разберётся ниже
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def _fetch(url: str, target: Path, *, limit_bytes: int) -> Path:
    """Скачивает файл, не давая ему превысить предел загрузки."""
    request = urllib.request.Request(encode_url(url),
                                     headers={"User-Agent": "ASRHub/3.0"})
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response, \
                target.open("wb") as handle:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                written += len(chunk)
                # Предел проверяем по ходу, а не по заголовку: Content-Length
                # приходит не всегда и не обязан быть правдой, а диск
                # кончается по-настоящему.
                if limit_bytes and written > limit_bytes:
                    raise AudioError(
                        f"Запись по адресу {url} больше предела "
                        f"{limit_bytes // 1024 // 1024} МБ.",
                        hint="Поднимите max_upload_mb или присылайте "
                             "запись частями (part и total_parts).")
                handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise AudioError(f"Файл записи недоступен: {url} — ответ {exc.code}.",
                         hint="Проверьте, что ссылка открывается без "
                              "аутентификации и с самого сервера.") from exc
    except (urllib.error.URLError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise AudioError(f"Не удалось скачать файл записи: {url} ({exc}).",
                         hint="Проверьте доступность адреса с сервера.") from exc
    if written == 0:
        raise AudioError(f"По адресу {url} пустой файл.")
    return target


def _ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise AudioError("Для приёма записей по ссылке нужен ffprobe.",
                         hint="Он ставится вместе с ffmpeg.")
    return path


def _channel_count(path: Path) -> int:
    result = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60)
    value = (result.stdout or "").strip().splitlines()
    if result.returncode != 0 or not value:
        raise AudioError(f"Не удалось разобрать файл записи «{path.name}».",
                         hint="Проверьте, что по ссылке действительно звук.")
    try:
        return int(value[0])
    except ValueError as exc:
        raise AudioError(f"Неожиданный ответ ffprobe для «{path.name}».") from exc


def _merge_to_stereo(left: Path, right: Path, target: Path) -> Path:
    """Совмещает две моно-дорожки в стерео: левая — первый файл, правая — второй.

    Так дальше работает обычный конвейер ASR Hub: разделение по каналам он
    уже умеет, и переписывать его ради телефонии не нужно.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioError("Для двух раздельных дорожек нужен ffmpeg.",
                         hint="Либо присылайте одну стереозапись.")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(left), "-i", str(right),
         "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]",
         "-map", "[a]", "-ac", "2", str(target)],
        capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not target.exists():
        raise AudioError("Не удалось совместить дорожки сторон в стереозапись.",
                         hint=(result.stderr or "").strip()[:300] or None)
    return target


def callback_body(request: PhoneRequest, job: dict[str, Any],
                  segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Тело обратного вызова — поле в поле как у phone_asr.

    Набор и порядок полей повторены дословно, включая те, что phone_asr
    заполняет значением по умолчанию: `sentiment` там объявлено, но нигде не
    вычисляется. Убрать его значило бы сломать разборщик, который читает по
    схеме, ради экономии девяти байт.
    """
    from .pipeline.waveform import to_phone_asr

    status = "success" if job.get("status") == "completed" else "failed"
    dialogue = [
        {
            "speaker": str(segment.get("speaker") or SPEAKER_LEFT),
            "dialogue": str(segment.get("text") or "").strip(),
            "time": round(float(segment.get("start") or 0.0), 3),
            # time_end объявлен как PositiveFloat: ноль такую схему не пройдёт,
            # а сегмент нулевой длины иногда получается на щелчке в записи.
            "time_end": max(round(float(segment.get("end") or 0.0), 3), 0.001),
        }
        for segment in segments
        if str(segment.get("text") or "").strip()
    ]
    waveforms: list[str] = []
    if job.get("waveform"):
        waveforms = to_phone_asr(job["waveform"], compatible=True)

    return {
        "call_id": request.call_id,
        "base_path": request.base_path(),
        "part": request.part,
        "total_parts": request.total_parts,
        "true_duration": max(float(job.get("media_duration_s") or 0.0), 0.001),
        "sentiment": "neutral",
        "waveforms": waveforms,
        "formatted_dialogue": dialogue,
        "transcription": " ".join(item["dialogue"] for item in dialogue),
        "status": status,
        "error_message": job.get("error_message") or None,
    }
