"""Голос опроса: текст вопроса — в звук, который понимает телефон.

Синтез речи делает не сервер, а программа, установленная рядом: RHVoice,
Piper или eSpeak NG. Сервер находит первую из них сам, либо берёт команду
из настройки `qc_tts_command`. Так не тянется за собой ни PyTorch, ни
модель в сотни мегабайт, а голос выбирает тот, кто отвечает за то, как
организация звучит по телефону.

Фраза может быть и готовой записью диктора: текст вида `file:/путь/к.wav`
проигрывается как есть.

Каждая фраза готовится один раз и лежит в кеше в двух видах — A-law и
μ-law, 8 кГц: кодек выбирает станция при ответе, а синтез занимает секунды,
которых в разговоре нет.

Синтез по образцу голоса (XTTS) идёт через постоянную службу (`tts_service`,
настройка qc_tts_service): модель загружается один раз, а не на каждую
фразу. Свою службу задаёт qc_tts_service_command — тот же обмен строками JSON.
"""
from __future__ import annotations

import hashlib
import json
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..logging_setup import get_logger

log = get_logger("qc")

#: Известные синтезаторы: (имя, программа, команда, голос по умолчанию).
#: В команде `{out}` — куда писать WAV, `{text}` — файл с текстом (UTF-8),
#: `{voice}` — голос. Текст подаётся ещё и на стандартный ввод.
ДВИЖКИ: tuple[tuple[str, str, str, str], ...] = (
    ("RHVoice", "RHVoice-test", "RHVoice-test -p {voice} -o {out}", "anna"),
    ("Piper", "piper", "piper --model {voice} --output_file {out}", ""),
    ("eSpeak NG", "espeak-ng", "espeak-ng -v {voice} -s 145 -w {out} -f {text}", "ru"),
    ("eSpeak", "espeak", "espeak -v {voice} -s 145 -w {out} -f {text}", "ru"),
)

#: Синтезаторы, говорящие голосом из образца (клонирование по короткой
#: записи): (имя, программа, команда). Берутся, когда образец загружен.
#: `{sample}` — файл образца, `{say}` — сам текст фразы отдельным аргументом.
ДВИЖКИ_ПО_ОБРАЗЦУ: tuple[tuple[str, str, str], ...] = (
    ("XTTS (Coqui TTS)", "tts",
     "tts --model_name tts_models/multilingual/multi-dataset/xtts_v2 --language_idx ru "
     "--speaker_wav {sample} --text {say} --out_path {out}"),
)

ЗВУКОВЫЕ = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".gsm", ".alaw", ".ulaw", ".sln")
#: Предел длины одной фразы: вопрос по телефону длиннее не слушают.
ДЛИНА_ФРАЗЫ = 600
СРОК_СИНТЕЗА_С = 60
#: Фраза «audio:имя» — запись, загруженная в раздел «Контроль качества».
ЗАГРУЖЕННАЯ = "audio:"
#: Пределы загруженной записи: размер файла и длительность звучания.
ПРЕДЕЛ_ЗАПИСИ_МБ = 50
ПРЕДЕЛ_ЗАПИСИ_С = 900


#: Образец голоса: не короче и не длиннее (секунд). Трёх секунд хватает
#: модели, но ровный голос получается с 10–30 секунд чистой речи.
ОБРАЗЕЦ_ОТ_С, ОБРАЗЕЦ_ДО_С = 3, 180
#: Синтез по образцу — нейросеть: на процессоре фраза готовится минуты.
СРОК_ПО_ОБРАЗЦУ_С = 900
#: Служба синтеза: сколько ждать готовности (первая загрузка модели — это
#: и скачивание весов) и через сколько простоя остановить, освободив память.
СРОК_ГОТОВНОСТИ_С = 900
ПРОСТОЙ_СЛУЖБЫ_С = 1800
#: Служба не поднялась — столько не пробовать снова (синтез идёт по фразе).
ПАУЗА_ПОСЛЕ_СБОЯ_С = 600


class СлужбаНеГотова(Exception):
    """Служба синтеза не запустилась или умерла — можно синтезировать по фразе."""


class СлужбаСинтеза:
    """Постоянный процесс синтеза: модель загружается один раз.

    Команда запускается при первой фразе и живёт, пока идут опросы; простой
    дольше ПРОСТОЙ_СЛУЖБЫ_С — процесс останавливается, память свободна. Обмен
    — строками JSON (см. `tts_service`): запрос `{"text", "out", "voice",
    "sample"}`, ответ `{"ok": true}`; первой строкой — `{"ready": true}`.
    Фразы идут по одной: модель на видеокарте одна.
    """

    def __init__(self, команда: list[str], *, окружение: dict[str, str] | None = None) -> None:
        self.команда = list(команда)
        self._окружение = окружение
        self._процесс: subprocess.Popen[bytes] | None = None
        self._строки: queue.Queue[bytes] = queue.Queue()
        self._замок = threading.Lock()
        self.последний_раз = 0.0
        self.запусков = 0
        self.сбой_в = 0.0
        self.сбой = ""

    @property
    def жива(self) -> bool:
        return self._процесс is not None and self._процесс.poll() is None

    def _читать(self, процесс: subprocess.Popen[bytes]) -> None:
        assert процесс.stdout is not None
        for строка in процесс.stdout:
            self._строки.put(строка)
        self._строки.put(b"")                               # конец вывода

    def _строка(self, срок: float) -> dict[str, Any]:
        конец = time.monotonic() + срок
        while True:
            осталось = конец - time.monotonic()
            if осталось <= 0:
                raise TimeoutError
            try:
                строка = self._строки.get(timeout=осталось)
            except queue.Empty as exc:
                raise TimeoutError from exc
            if not строка:
                raise СлужбаНеГотова("служба синтеза завершилась")
            try:
                данные = json.loads(строка.decode("utf-8", "replace"))
            except ValueError:
                continue                                    # постороннее в выводе
            if isinstance(данные, dict):
                return данные

    def _запустить(self) -> None:
        if self.сбой_в and time.monotonic() - self.сбой_в < ПАУЗА_ПОСЛЕ_СБОЯ_С:
            raise СлужбаНеГотова(self.сбой)
        self.остановить()
        self._строки = queue.Queue()
        try:
            процесс = subprocess.Popen(                     # noqa: S603
                self.команда, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self._окружение)
        except OSError as exc:
            self._сбой(f"служба синтеза не запускается: {exc}")
        self._процесс = процесс
        self.запусков += 1
        threading.Thread(target=self._читать, args=(процесс,), daemon=True,
                         name="asrhub-tts-service").start()
        try:
            готовность = self._строка(СРОК_ГОТОВНОСТИ_С)
        except (TimeoutError, СлужбаНеГотова):
            self._сбой("служба синтеза не сообщила о готовности")
        if not готовность.get("ready"):
            self._сбой(f"служба синтеза не готова: {готовность.get('error') or 'без причины'}")
        log.info("Служба синтеза готова (%s)", готовность.get("device") or "устройство не названо")

    def _сбой(self, причина: str) -> None:
        self.сбой, self.сбой_в = причина, time.monotonic()
        self.остановить()
        raise СлужбаНеГотова(причина)

    def синтезировать(self, текст: str, выход: Path, *, голос: str = "", образец: str = "",
                      срок: float = СРОК_ПО_ОБРАЗЦУ_С) -> None:
        with self._замок:
            if not self.жива:
                self._запустить()
            процесс = self._процесс
            assert процесс is not None and процесс.stdin is not None
            запрос = {"text": текст, "out": str(выход), "voice": голос, "sample": образец}
            try:
                процесс.stdin.write((json.dumps(запрос, ensure_ascii=False) + "\n").encode())
                процесс.stdin.flush()
                ответ = self._строка(срок)
            except (OSError, СлужбаНеГотова) as exc:
                self.остановить()
                raise СлужбаНеГотова(f"служба синтеза оборвалась: {exc}") from exc
            except TimeoutError as exc:
                # Зависла на фразе — останавливается; следующая фраза её поднимет.
                self.остановить()
                raise ConfigError(f"Служба синтеза не уложилась в {срок:g} с.") from exc
            finally:
                self.последний_раз = time.monotonic()
        if not ответ.get("ok"):
            raise ConfigError(f"Служба синтеза не выдала звук: {ответ.get('error') or 'отказ'}.")

    def остановить(self) -> None:
        процесс, self._процесс = self._процесс, None
        if процесс is None:
            return
        try:
            if процесс.stdin is not None:
                процесс.stdin.close()
            процесс.terminate()
            процесс.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            try:
                процесс.kill()
            except OSError:
                pass


_СЛУЖБЫ: dict[tuple[str, ...], СлужбаСинтеза] = {}
_ЗАМОК_СЛУЖБ = threading.Lock()


def служба(команда: list[str]) -> СлужбаСинтеза:
    """Служба под эту команду (одна на команду); простаивающие — остановить."""
    ключ = tuple(команда)
    with _ЗАМОК_СЛУЖБ:
        сейчас = time.monotonic()
        for другая in _СЛУЖБЫ.values():
            if другая.жива and сейчас - другая.последний_раз > ПРОСТОЙ_СЛУЖБЫ_С:
                log.info("Служба синтеза простаивает %d мин — остановлена",
                         ПРОСТОЙ_СЛУЖБЫ_С // 60)
                другая.остановить()
        if ключ not in _СЛУЖБЫ:
            _СЛУЖБЫ[ключ] = СлужбаСинтеза(команда)
        return _СЛУЖБЫ[ключ]


def остановить_службы() -> None:
    with _ЗАМОК_СЛУЖБ:
        for другая in _СЛУЖБЫ.values():
            другая.остановить()


def питон_программы(программа: str) -> str:
    """Python, которым запускается программа (`tts` из Coqui): из её первой строки.

    Coqui TTS часто ставят в своё окружение, не в окружение сервера: службе
    нужен тот Python, где он установлен.
    """
    путь = shutil.which(программа)
    if not путь:
        return ""
    try:
        with open(путь, "rb") as файл:
            первая = файл.readline(512).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    if not первая.startswith("#!"):
        return ""
    части = первая[2:].split()
    if not части:
        return ""
    if Path(части[0]).name == "env" and len(части) > 1:
        return shutil.which(части[1]) or ""
    return части[0] if "python" in Path(части[0]).name and Path(части[0]).is_file() else ""


def каталог_образца(кеш: Path) -> Path:
    return кеш.parent / "voice"


def образец(кеш: Path) -> Path | None:
    """Загруженный образец голоса или None."""
    файл = каталог_образца(кеш) / "sample.wav"
    return файл if файл.is_file() and файл.stat().st_size > 1000 else None


def сведения_образца(кеш: Path) -> dict[str, Any]:
    файл = образец(кеш)
    if файл is None:
        return {"exists": False}
    размер = файл.stat().st_size
    имя = каталог_образца(кеш) / "sample.name"
    return {"exists": True, "bytes": размер, "uploaded_at": файл.stat().st_mtime,
            # WAV 16 бит, 22,05 кГц, моно — так его пишет `принять_образец`.
            "seconds": round(max(0, размер - 44) / (22050 * 2), 1),
            "name": имя.read_text(encoding="utf-8").strip() if имя.is_file() else "sample.wav"}


def принять_образец(источник: Path, кеш: Path, имя: str = "") -> dict[str, Any]:
    """Любой звуковой файл — в образец голоса: WAV, моно, 22,05 кГц.

    Прежний образец заменяется только годным новым.
    """
    каталог = каталог_образца(кеш)
    каталог.mkdir(parents=True, exist_ok=True)
    временный = каталог / "sample.part.wav"
    try:
        итог = subprocess.run(                              # noqa: S603
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(источник),
             "-vn", "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le",
             "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
             "-f", "wav", str(временный)],
            capture_output=True, timeout=СРОК_СИНТЕЗА_С, check=False)
        if итог.returncode != 0 or not временный.is_file():
            причина = (итог.stderr or b"").decode("utf-8", "replace").strip()[-200:]
            raise ConfigError(f"Файл не читается как звук: {причина or 'ffmpeg вернул ошибку'}.")
        секунд = max(0, временный.stat().st_size - 44) / (22050 * 2)
        if секунд < ОБРАЗЕЦ_ОТ_С:
            raise ConfigError(
                f"Образец короче {ОБРАЗЕЦ_ОТ_С} секунд ({секунд:.1f} с).",
                hint="Запишите 10–30 секунд ровной речи одного человека, без музыки и шума.")
        if секунд > ОБРАЗЕЦ_ДО_С:
            raise ConfigError(
                f"Образец длиннее {ОБРАЗЕЦ_ДО_С // 60} минут ({секунд / 60:.1f} мин).",
                hint="Хватит 10–30 секунд чистой речи: вырежьте лучший отрывок.")
        временный.replace(каталог / "sample.wav")
        (каталог / "sample.name").write_text(имя or источник.name, encoding="utf-8")
    finally:
        временный.unlink(missing_ok=True)
    return сведения_образца(кеш)


def убрать_образец(кеш: Path) -> bool:
    был = образец(кеш) is not None
    for имя in ("sample.wav", "sample.name"):
        (каталог_образца(кеш) / имя).unlink(missing_ok=True)
    return был


def каталог_записей(кеш: Path) -> Path:
    """Где лежат загруженные записи: рядом с кешем фраз."""
    return кеш.parent / "audio"


def имя_годится(имя: str) -> bool:
    """Имя загруженной записи: без каталогов, со звуковым расширением."""
    имя = str(имя or "")
    return (bool(имя) and len(имя) <= 120 and Path(имя).name == имя
            and not имя.startswith(".") and Path(имя).suffix.lower() in ЗВУКОВЫЕ)


@dataclass(slots=True)
class Фраза:
    """Готовый звук фразы в обоих кодеках."""

    alaw: bytes
    ulaw: bytes
    секунд: float

    def звук(self, кодек: str) -> bytes:
        return self.alaw if кодек == "PCMA" else self.ulaw


def движок(settings: Any, голос_образец: Path | None = None) -> dict[str, Any]:
    """Чем будет синтезироваться речь: команда, голос и готовность.

    `голос_образец` — загруженный образец голоса. С ним берётся синтезатор,
    говорящий по образцу (`clone: True`); если такого нет, речь идёт обычным
    голосом, а `warning` говорит, почему образец не используется.
    """
    своя = str(settings.get("qc_tts_command") or "").strip()
    голос = str(settings.get("qc_tts_voice") or "").strip()
    основа: dict[str, Any] = {"clone": False, "warning": "", "sample": "", "service": []}
    своя_служба = str(settings.get("qc_tts_service_command") or "").strip()
    if своя_служба:
        try:
            части = shlex.split(своя_служба)
            программа = части[0]
        except (ValueError, IndexError):
            return {**основа, "name": "своя служба синтеза", "command": своя_служба,
                    "voice": голос, "available": False,
                    "reason": "команда службы не разбирается: проверьте кавычки"}
        есть = bool(shutil.which(программа)) or Path(программа).is_file()
        return {**основа, "name": "своя служба синтеза", "command": своя_служба,
                "voice": голос, "available": есть, "service": части,
                "reason": "" if есть else f"программа «{программа}» не найдена",
                "clone": голос_образец is not None,
                "sample": str(голос_образец) if голос_образец is not None else ""}
    if своя:
        try:
            программа = shlex.split(своя)[0]
        except (ValueError, IndexError):
            return {**основа, "name": "своя команда", "command": своя, "voice": голос,
                    "available": False, "reason": "команда не разбирается: проверьте кавычки"}
        есть = bool(shutil.which(программа)) or Path(программа).is_file()
        описание = {**основа, "name": "своя команда", "command": своя, "voice": голос,
                    "available": есть,
                    "reason": "" if есть else f"программа «{программа}» не найдена"}
        if "{sample}" in своя:
            if голос_образец is None:
                описание.update(available=False, reason="команда синтеза ждёт образец голоса "
                                "({sample}), а он не загружен")
            else:
                описание.update(clone=True, sample=str(голос_образец))
        elif голос_образец is not None:
            описание["warning"] = ("образец голоса загружен, но в своей команде синтеза нет "
                                   "{sample}: звучит голос команды")
        return описание
    почему_нет = ""
    if голос_образец is not None:
        for имя, программа, команда in ДВИЖКИ_ПО_ОБРАЗЦУ:
            if shutil.which(программа):
                описание = {**основа, "name": имя, "command": команда, "voice": "по образцу",
                            "available": True, "reason": "", "clone": True,
                            "sample": str(голос_образец)}
                питон = питон_программы(программа) if bool(
                    settings.get("qc_tts_service", True)) else ""
                if питон:
                    # Модель — один раз на службу, а не на каждую фразу; не
                    # поднялась служба — фраза синтезируется командой.
                    описание["service"] = [питон, str(Path(__file__).with_name(
                        "tts_service.py"))]
                return описание
        почему_нет = ("образец голоса загружен, но синтезатор, говорящий по образцу, не "
                      "найден (Coqui TTS: pip install coqui-tts): звучит обычный голос")
    for имя, программа, команда, голос_движка in ДВИЖКИ:
        if shutil.which(программа):
            выбран = голос or голос_движка
            if not выбран:
                return {**основа, "name": имя, "command": команда, "voice": "",
                        "available": False, "warning": почему_нет,
                        "reason": f"{имя} найден, но не задан голос: укажите файл модели "
                                  "(.onnx) в «Голос синтеза»"}
            return {**основа, "name": имя, "command": команда, "voice": выбран,
                    "available": True, "reason": "", "warning": почему_нет}
    return {**основа, "name": "", "command": "", "voice": голос, "available": False,
            "warning": почему_нет,
            "reason": "синтезатор речи не найден: установите RHVoice, Piper или eSpeak NG "
                      "либо задайте свою команду в «Команда синтеза речи»"}


def _ffmpeg() -> str:
    программа = shutil.which("ffmpeg")
    if not программа:
        raise ConfigError("Не найден ffmpeg: он приводит голос к телефонному виду "
                          "(8 кГц, G.711).", hint="Установите ffmpeg на сервере.")
    return программа


def _в_телефонный(источник: Path, каталог: Path, имя: str) -> None:
    """Любой звук — в два файла кеша: A-law и μ-law, 8 кГц, моно."""
    for расширение, вид in ((".alaw", "alaw"), (".ulaw", "mulaw")):
        цель = каталог / f"{имя}{расширение}"
        временный = каталог / f"{имя}{расширение}.part"
        итог = subprocess.run(                              # noqa: S603
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(источник),
             "-vn", "-ac", "1", "-ar", "8000",
             # Телефонная полоса и выравнивание громкости: синтезаторы
             # звучат тихо, а записи диктора — как придётся.
             "-af", "highpass=f=200,lowpass=f=3400,dynaudnorm=p=0.9",
             "-f", вид, str(временный)],
            capture_output=True, timeout=СРОК_СИНТЕЗА_С, check=False)
        if итог.returncode != 0 or not временный.is_file() or not временный.stat().st_size:
            временный.unlink(missing_ok=True)
            причина = (итог.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise ConfigError(f"Звук фразы не готовится: {причина or 'ffmpeg вернул ошибку'}.")
        временный.replace(цель)


def _синтезировать(текст: str, описание: dict[str, Any], выход: Path) -> None:
    срок_службы = СРОК_ПО_ОБРАЗЦУ_С if описание.get("clone") else СРОК_СИНТЕЗА_С
    if описание.get("service"):
        своя = описание.get("name") == "своя служба синтеза"
        try:
            служба(list(описание["service"])).синтезировать(
                текст, выход, голос=str(описание.get("voice") or ""),
                образец=str(описание.get("sample") or ""), срок=срок_службы)
        except СлужбаНеГотова as exc:
            if своя:
                raise ConfigError(f"Своя служба синтеза: {exc}.",
                                  hint="Проверьте qc_tts_service_command: служба должна "
                                       "первой строкой ответить {\"ready\": true}.") from exc
            log.warning("Служба синтеза: %s — фраза синтезируется командой", exc)
        else:
            if выход.is_file() and выход.stat().st_size >= 100:
                return
            raise ConfigError("Служба синтеза ответила «готово», а звука нет: "
                              f"{выход.name} пуст или не создан.")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8",
                                     delete=False) as файл:
        файл.write(текст + "\n")
        файл_текста = Path(файл.name)
    try:
        # Подстановка — по частям уже разобранной команды, без оболочки:
        # ни текст вопроса, ни имя голоса не станут командой.
        срок = СРОК_ПО_ОБРАЗЦУ_С if описание.get("clone") else СРОК_СИНТЕЗА_С
        части = [ч.replace("{out}", str(выход)).replace("{text}", str(файл_текста))
                  .replace("{voice}", str(описание.get("voice") or ""))
                  .replace("{sample}", str(описание.get("sample") or ""))
                  .replace("{say}", текст)
                 for ч in shlex.split(str(описание["command"]))]
        try:
            итог = subprocess.run(части, input=(текст + "\n").encode("utf-8"),  # noqa: S603
                                  capture_output=True, timeout=срок, check=False)
        except FileNotFoundError as exc:
            raise ConfigError(f"Синтезатор речи не запускается: {exc}.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConfigError(f"Синтезатор речи не уложился в {срок} с.") from exc
        if итог.returncode != 0 or not выход.is_file() or выход.stat().st_size < 100:
            причина = (итог.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise ConfigError(
                f"Синтезатор речи ({описание.get('name')}) не выдал звук: "
                f"{причина or 'пустой файл'}.",
                hint="Проверьте команду и голос в настройках контроля качества; "
                     "кнопка «Прослушать» покажет ту же ошибку.")
    finally:
        файл_текста.unlink(missing_ok=True)


def приготовить(текст: str, settings: Any, каталог: Path, *,
                образец_голоса: str = "") -> Фраза:
    """Фраза из кеша или свежий синтез. `каталог` — кеш (создаётся).

    `образец_голоса` — имя загруженной аудиозаписи, которой группа говорит
    вместо общего образца (у отдела продаж — свой голос).
    """
    текст = " ".join(str(текст or "").split())
    if not текст:
        raise ConfigError("Пустая фраза: проговаривать нечего.")
    if текст.startswith(ЗАГРУЖЕННАЯ):
        имя = текст[len(ЗАГРУЖЕННАЯ):].strip()
        if not имя_годится(имя):
            raise ConfigError(f"«{имя}» — не имя загруженной аудиозаписи.")
        if not (каталог_записей(каталог) / имя).is_file():
            raise ConfigError(f"Аудиозапись «{имя}» не найдена: загрузите её заново "
                              "или уберите из группы.")
        текст = f"file:{каталог_записей(каталог) / имя}"
    if len(текст) > ДЛИНА_ФРАЗЫ and not текст.startswith("file:"):
        raise ConfigError(f"Фраза длиннее {ДЛИНА_ФРАЗЫ} знаков: по телефону её не дослушают.")
    каталог.mkdir(parents=True, exist_ok=True)
    if текст.startswith("file:"):
        источник = Path(текст[5:].strip()).expanduser()
        if источник.suffix.lower() not in ЗВУКОВЫЕ:
            raise ConfigError(f"«{источник.name}» — не звуковой файл.")
        if not источник.is_file():
            raise ConfigError(f"Файл фразы не найден: {источник}.")
        сведения = источник.stat()
        ключ = f"file|{источник}|{сведения.st_size}|{int(сведения.st_mtime)}"
        описание: dict[str, Any] = {}
    else:
        свой = каталог_записей(каталог) / образец_голоса if образец_голоса else None
        if свой is not None and not (имя_годится(образец_голоса) and свой.is_file()):
            raise ConfigError(f"Образец голоса группы «{образец_голоса}» не найден среди "
                              "загруженных аудиозаписей.")
        описание = движок(settings, свой or образец(каталог))
        if not описание["available"]:
            raise ConfigError("Нечем проговорить текст: " + описание["reason"] + ".")
        ключ = f"tts|{описание['command']}|{описание['voice']}|{текст}"
        if описание["clone"]:
            # Новый образец — новый голос: прежние фразы из кеша не годятся.
            отпечаток = Path(описание["sample"]).stat()
            ключ += f"|{отпечаток.st_size}|{отпечаток.st_mtime_ns}"
    имя = hashlib.sha256(ключ.encode("utf-8")).hexdigest()[:32]
    alaw, ulaw = каталог / f"{имя}.alaw", каталог / f"{имя}.ulaw"
    if not (alaw.is_file() and ulaw.is_file()):
        if текст.startswith("file:"):
            _в_телефонный(источник, каталог, имя)
        else:
            сырой = каталог / f"{имя}.src.wav"
            try:
                _синтезировать(текст, описание, сырой)
                _в_телефонный(сырой, каталог, имя)
            finally:
                сырой.unlink(missing_ok=True)
    данные = alaw.read_bytes()
    return Фраза(alaw=данные, ulaw=ulaw.read_bytes(), секунд=len(данные) / 8000.0)


def в_wav(фраза: Фраза) -> bytes:
    """Фраза как обычный WAV (16 бит, 8 кГц) — для кнопки «Прослушать»."""
    import struct  # noqa: PLC0415

    from .sip import РАСКОДИРОВАТЬ  # noqa: PLC0415

    таблица = РАСКОДИРОВАТЬ["PCMA"]
    данные = struct.pack(f"<{len(фраза.alaw)}h", *(таблица[б] for б in фраза.alaw))
    заголовок = (b"RIFF" + struct.pack("<I", 36 + len(данные)) + b"WAVEfmt "
                 + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
                 + b"data" + struct.pack("<I", len(данные)))
    return заголовок + данные


def очистить_кеш(каталог: Path, *, старше_дней: int = 90) -> int:
    """Убирает фразы, к которым давно не обращались."""
    import time  # noqa: PLC0415

    if not каталог.is_dir():
        return 0
    порог = time.time() - старше_дней * 86400
    убрано = 0
    for файл in каталог.iterdir():
        try:
            if файл.is_file() and файл.stat().st_atime < порог and файл.stat().st_mtime < порог:
                файл.unlink()
                убрано += 1
        except OSError:
            continue
    return убрано
