"""Разговор с Asterisk: интерфейс управления, журнал звонков, файлы записей.

Три способа узнать о состоявшемся звонке, и все три здесь есть, потому
что в жизни встречаются все три:

* **AMI** — интерфейс управления Asterisk (обычно порт 5038). Модуль
  `cdr_manager` шлёт событие `Cdr` на каждый завершённый звонок: это
  самый точный источник, и он же единственный, который работает в
  реальном времени. Требует учётной записи в `manager.conf` — достаточно
  прав `read=call,cdr`, писать в АТС мы не собираемся вовсе.
* **Журнал CSV** — `/var/log/asterisk/cdr-csv/Master.csv`. Работает
  всегда, даже когда AMI закрыт правилами безопасности; читается с
  запомненной позиции, поэтому перечитывать гигабайтный файл не
  приходится.
* **Только папка записей** — когда до АТС не дотянуться вовсе, а записи
  лежат на общем диске. Тогда о звонке известно ровно то, что записано в
  имени файла, и раздел об этом честно говорит.

Чего здесь нет намеренно: команд в сторону АТС. Ни `Originate`, ни
`Redirect`, ни `Command` — соединение только слушает. Ключ от АТС в чужих
руках это возможность звонить за чужой счёт, и сервер распознавания не
должен уметь этого даже теоретически.
"""
from __future__ import annotations

import csv
import io
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import ASRHubError, ConfigError
from ..logging_setup import get_logger

log = get_logger("telephony")

#: Сколько ждать ответа АТС на действие.
ТАЙМАУТ = 10.0

#: Поля журнала CSV в порядке, в котором их пишет `cdr_csv`. Порядок
#: зафиксирован в самой Asterisk и не менялся годами.
ПОЛЯ_CSV = ("accountcode", "src", "dst", "dcontext", "clid", "channel",
            "dstchannel", "lastapp", "lastdata", "start", "answer", "end",
            "duration", "billsec", "disposition", "amaflags", "uniqueid",
            "userfield")

#: Как поля события AMI `Cdr` называются в самом событии.
ПОЛЯ_AMI = {
    "AccountCode": "accountcode", "Source": "src", "Destination": "dst",
    "DestinationContext": "dcontext", "CallerID": "clid", "Channel": "channel",
    "DestinationChannel": "dstchannel", "LastApplication": "lastapp",
    "LastData": "lastdata", "StartTime": "start", "AnswerTime": "answer",
    "EndTime": "end", "Duration": "duration", "BillableSeconds": "billsec",
    "Disposition": "disposition", "AMAFlags": "amaflags", "UniqueID": "uniqueid",
    "UserField": "userfield",
}


class AMIError(ASRHubError):
    """Сбой разговора с АТС: сеть, отказ входа, неожиданный ответ."""

    code = "telephony_error"
    http_status = 502
    hint = "Проверьте адрес, порт и учётную запись в manager.conf на АТС."


@dataclass
class Звонок:
    """Одна запись журнала звонков, приведённая к общему виду."""

    uniqueid: str = ""
    src: str = ""
    dst: str = ""
    clid: str = ""
    channel: str = ""
    dstchannel: str = ""
    context: str = ""
    disposition: str = ""
    duration: int = 0
    billsec: int = 0
    started_at: float = 0.0
    answered: bool = False
    userfield: str = ""
    accountcode: str = ""
    recording: str = ""
    direction: str = ""
    queue: str = ""
    agent: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        данные = {
            "uniqueid": self.uniqueid, "src": self.src, "dst": self.dst,
            "clid": self.clid, "channel": self.channel, "dstchannel": self.dstchannel,
            "context": self.context, "disposition": self.disposition,
            "duration": self.duration, "billsec": self.billsec,
            "started_at": self.started_at, "answered": self.answered,
            "userfield": self.userfield, "accountcode": self.accountcode,
            "recording": self.recording, "direction": self.direction,
            "queue": self.queue, "agent": self.agent,
        }
        return данные

    def поля_записи(self) -> dict[str, Any]:
        """То же самое без идентификатора — как раз для сохранения в базу.

        Идентификатор там передаётся отдельным аргументом, и оставлять его
        ещё и в наборе полей нельзя: Python отвечает на это «два значения
        для одного аргумента», причём в тот момент, когда звонок уже
        поставлен в очередь, — задание есть, записи о нём нет.
        """
        данные = self.to_dict()
        данные.pop("uniqueid", None)
        return данные


# ---------------------------------------------------------------------------
# Разбор журналов
# ---------------------------------------------------------------------------

def _время(значение: str) -> float:
    """Отметка времени Asterisk («2026-09-10 14:03:11») в секунды эпохи."""
    значение = (значение or "").strip()
    if not значение:
        return 0.0
    for образец in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(значение[:19], образец).timestamp()
        except ValueError:
            continue
    try:                                    # некоторые сборки пишут секунды эпохи
        return float(значение)
    except (TypeError, ValueError):
        return 0.0


def _целое(значение: Any) -> int:
    try:
        return int(float(str(значение).strip() or 0))
    except (TypeError, ValueError):
        return 0


def звонок_из_полей(поля: dict[str, str]) -> Звонок:
    """Общий вид из набора полей — что из AMI, что из CSV."""
    расположение = str(поля.get("disposition") or "").upper()
    return Звонок(
        uniqueid=str(поля.get("uniqueid") or "").strip(),
        src=str(поля.get("src") or "").strip(),
        dst=str(поля.get("dst") or "").strip(),
        clid=str(поля.get("clid") or "").strip(),
        channel=str(поля.get("channel") or "").strip(),
        dstchannel=str(поля.get("dstchannel") or "").strip(),
        context=str(поля.get("dcontext") or "").strip(),
        disposition=расположение,
        duration=_целое(поля.get("duration")),
        billsec=_целое(поля.get("billsec")),
        started_at=_время(str(поля.get("start") or "")),
        answered=расположение == "ANSWERED",
        userfield=str(поля.get("userfield") or "").strip(),
        accountcode=str(поля.get("accountcode") or "").strip(),
        raw={к: str(з) for к, з in поля.items()},
    )


def разобрать_cdr_строку(строка: str) -> Звонок | None:
    """Строка `Master.csv` — в звонок.

    Строка разбирается настоящим разбором CSV, а не делением по запятой:
    в имени звонящего запятые встречаются («Иванов, отдел продаж»), и
    деление по запятой сдвигало бы все поля правее — вместе с номером,
    длительностью и идентификатором.
    """
    строка = (строка or "").strip()
    if not строка:
        return None
    значения = _поля_csv(строка)
    if значения is None:
        return None
    поля = dict(zip(ПОЛЯ_CSV, значения, strict=False))
    звонок = звонок_из_полей(поля)
    return звонок if звонок.uniqueid else None


#: Идентификатор звонка у Asterisk — «секунды.порядковый», иногда с
#: приставкой имени системы. Проверка нужна не ради красоты: по ней видно,
#: что поля не сдвинулись при разборе.
_ПОХОЖ_НА_ID = re.compile(r"^[\w.\-]*\d+\.\d+$")


def _поля_csv(строка: str) -> list[str] | None:
    """Значения строки журнала — с запасным разбором на случай иного экранирования.

    Asterisk удваивает кавычку внутри поля, как велит стандарт. Но записи
    в тот же журнал дописывают и надстройки над станцией, и часть из них
    экранирует кавычку обратной косой. Разница вылезает только там, где в
    имени звонящего есть и кавычка, и запятая: стандартный разбор тогда
    разваливает поле надвое и сдвигает всё правее — вместе с номером,
    длительностью и идентификатором звонка.

    Поэтому разбор двойной: обычный, а если идентификатор в нужном месте
    не похож на идентификатор — ещё раз, с обратной косой как знаком
    экранирования. Похожесть проверяется образцом, а не длиной: сдвиг на
    одно поле даёт в этом месте название приложения или дату, и ни то ни
    другое на «1757500001.1» не похоже.
    """
    попытки = ({}, {"escapechar": "\\"})
    запасной: list[str] | None = None
    for настройки in попытки:
        try:
            значения = next(csv.reader(io.StringIO(строка), **настройки))
        except (csv.Error, StopIteration):
            continue
        if len(значения) < 15:
            continue
        if запасной is None:
            запасной = значения
        место = ПОЛЯ_CSV.index("uniqueid")
        if место < len(значения) and _ПОХОЖ_НА_ID.match(значения[место].strip()):
            return значения
    return запасной


def направление(звонок: Звонок, *, внутренние_знаков: int = 5,
                контексты: dict[str, str] | None = None) -> str:
    """Входящий, исходящий или внутренний.

    Сначала спрашиваем контекст: в `extensions.conf` он и заведён, чтобы
    отличать «звонят нам» от «звоним мы», и настройка `telephony_contexts`
    позволяет назвать свои. Если контекст ничего не сказал — смотрим на
    длину номеров: внутренний номер короткий, городской длинный. Это
    догадка, и она названа догадкой: раздел показывает направление, а не
    выдаёт его за факт биллинга.
    """
    имя = (звонок.context or "").lower()
    for образец, куда in (контексты or {}).items():
        if образец and образец.lower() in имя:
            return куда
    свой_src = len(re.sub(r"\D", "", звонок.src)) <= внутренние_знаков
    свой_dst = len(re.sub(r"\D", "", звонок.dst)) <= внутренние_знаков
    if свой_src and свой_dst:
        return "внутренний"
    if свой_dst and not свой_src:
        return "входящий"
    if свой_src and not свой_dst:
        return "исходящий"
    return ""


# ---------------------------------------------------------------------------
# Интерфейс управления AMI
# ---------------------------------------------------------------------------

class AMIClient:
    """Соединение с интерфейсом управления Asterisk — только на чтение.

    Живёт своим потоком: читает поток событий, собирает из них пакеты
    «ключ: значение» и отдаёт готовые события подписчику. Обрыв связи не
    считается ошибкой — АТС перезапускают, сеть моргает; поток ждёт и
    соединяется заново с растущей паузой.
    """

    def __init__(self, host: str, port: int = 5038, username: str = "",
                 secret: str = "", *, timeout: float = ТАЙМАУТ):
        self.host = host
        self.port = int(port or 5038)
        self.username = username
        self.secret = secret
        self.timeout = float(timeout or ТАЙМАУТ)
        self._sock: socket.socket | None = None
        self._буфер = b""
        self.banner = ""

    # --- соединение -------------------------------------------------------

    def connect(self) -> str:
        """Открывает соединение и входит. Возвращает приветствие АТС."""
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as exc:
            raise AMIError(f"АТС {self.host}:{self.port} недоступна: {exc}") from exc
        self.banner = self._строка().strip()
        ответ = self.action("Login", Username=self.username, Secret=self.secret,
                            Events="on")
        if str(ответ.get("Response", "")).lower() != "success":
            self.close()
            raise AMIError(
                "АТС отклонила вход: " + str(ответ.get("Message") or "нет ответа"),
                hint="Проверьте логин и пароль в manager.conf, а также permit/deny "
                     "для адреса этого сервера.")
        return self.banner

    def close(self) -> None:
        сокет, self._sock = self._sock, None
        if сокет is None:
            return
        try:
            сокет.sendall(b"Action: Logoff\r\n\r\n")
        except OSError:
            pass
        try:
            сокет.close()
        except OSError:
            pass

    # --- чтение -----------------------------------------------------------

    def _строка(self) -> str:
        while b"\r\n" not in self._буфер:
            кусок = self._прочитать()
            if not кусок:
                return ""
            self._буфер += кусок
        строка, _, self._буфер = self._буфер.partition(b"\r\n")
        return строка.decode("utf-8", "replace")

    def _прочитать(self) -> bytes:
        сокет = self._sock
        if сокет is None:
            raise AMIError("Соединение с АТС закрыто.")
        try:
            return сокет.recv(8192)
        except TimeoutError:
            return b""
        except OSError as exc:
            raise AMIError(f"Обрыв связи с АТС: {exc}") from exc

    def пакет(self, timeout: float | None = None) -> dict[str, str]:
        """Следующий пакет «ключ: значение» до пустой строки."""
        if timeout is not None and self._sock is not None:
            self._sock.settimeout(timeout)
        пакет: dict[str, str] = {}
        while True:
            строка = self._строка()
            if строка == "":
                if пакет:
                    return пакет
                return {}
            ключ, _, значение = строка.partition(":")
            if ключ:
                пакет[ключ.strip()] = значение.strip()

    def action(self, name: str, **поля: Any) -> dict[str, str]:
        """Отправляет действие и ждёт ответ на него.

        Список действий намеренно узкий: `Login`, `Logoff`, `Ping`,
        `CoreSettings`. Всё, что меняет состояние АТС, здесь не нужно, и
        отсутствие такой возможности — часть защиты.
        """
        разрешено = {"login", "logoff", "ping", "coresettings", "corestatus", "events"}
        if name.lower() not in разрешено:
            raise AMIError(f"Действие «{name}» не разрешено этим клиентом.")
        сокет = self._sock
        if сокет is None:
            raise AMIError("Нет соединения с АТС.")
        строки = [f"Action: {name}"]
        строки += [f"{к}: {з}" for к, з in поля.items() if з is not None]
        пакет = ("\r\n".join(строки) + "\r\n\r\n").encode("utf-8")
        try:
            сокет.sendall(пакет)
        except OSError as exc:
            raise AMIError(f"Не удалось отправить действие АТС: {exc}") from exc
        # Ответ может прийти не первым: между отправкой и ответом успевают
        # проскочить события. Читаем, пока не увидим Response.
        крайний = time.time() + self.timeout
        while time.time() < крайний:
            ответ = self.пакет()
            if not ответ:
                continue
            if "Response" in ответ:
                return ответ
        raise AMIError(f"АТС не ответила на действие «{name}» за {self.timeout:g} с.")

    # --- удобства ---------------------------------------------------------

    def ping(self) -> dict[str, str]:
        return self.action("Ping")

    def настройки(self) -> dict[str, str]:
        """Версия АТС и прочее из CoreSettings — для карточки состояния."""
        try:
            return self.action("CoreSettings")
        except AMIError:
            return {}

    def события(self, stop: threading.Event, *, on_event: Any = None,
                idle: float = 1.0) -> None:
        """Читает поток событий, пока не попросят остановиться."""
        while not stop.is_set():
            try:
                событие = self.пакет(timeout=idle)
            except AMIError:
                return
            if not событие:
                continue
            if on_event is not None:
                try:
                    on_event(событие)
                except Exception as exc:                     # noqa: BLE001
                    log.warning("Обработчик события АТС дал сбой: %s", exc)

    def __enter__(self) -> AMIClient:
        self.connect()
        return self

    def __exit__(self, *_: Any) -> bool:
        self.close()
        return False


def проверить(host: str, port: int, username: str, secret: str,
              *, timeout: float = ТАЙМАУТ) -> dict[str, Any]:
    """Короткая проверка связи: вход, версия, права — и сразу выход."""
    начало = time.perf_counter()
    клиент = AMIClient(host, port, username, secret, timeout=timeout)
    try:
        приветствие = клиент.connect()
        настройки = клиент.настройки()
        клиент.ping()
    finally:
        клиент.close()
    return {
        "ok": True,
        "banner": приветствие,
        "version": настройки.get("AsteriskVersion", ""),
        "ms": round((time.perf_counter() - начало) * 1000, 1),
    }


# ---------------------------------------------------------------------------
# Журнал CSV и файлы записей
# ---------------------------------------------------------------------------

def читать_csv(path: Path, *, offset: int = 0, limit: int = 500
               ) -> tuple[list[Звонок], int]:
    """Новые строки журнала с запомненной позиции.

    Позиция — смещение в байтах: журнал только дописывается, и читать его
    целиком на каждом заходе значило бы перечитывать гигабайт ради
    десятка строк. Если файл вдруг стал короче (его повернули по
    расписанию), начинаем с начала — иначе новые звонки пропали бы совсем.
    """
    try:
        размер = path.stat().st_size
    except OSError as exc:
        # Не AMIError: недоступный файл — это настройка и права, а не сбой
        # связи со станцией. Иначе ответ приходил с кодом 502 и подсказкой
        # про manager.conf, к которой нечего было применить: учётной записи
        # AMI у источника «журнал CDR» нет вовсе.
        raise ConfigError(
            f"Журнал звонков {path} не читается: {exc}",
            hint="Проверьте путь в настройке «Журнал звонков CDR» и права на "
                 "чтение у пользователя, от которого работает сервер.") from exc
    if offset > размер:
        offset = 0
    звонки: list[Звонок] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as файл:
        файл.seek(offset)
        for строка in файл:
            if not строка.endswith("\n"):
                # Строка ещё дописывается — оставляем её следующему заходу.
                break
            offset += len(строка.encode("utf-8"))
            звонок = разобрать_cdr_строку(строка)
            if звонок is not None:
                звонки.append(звонок)
            if len(звонки) >= limit:
                break
    return звонки, offset


#: Расширения записей, которые имеет смысл искать.
ЗАПИСИ = (".wav", ".mp3", ".gsm", ".ogg", ".WAV", ".alaw", ".ulaw", ".sln")


def найти_запись(звонок: Звонок, каталог: Path, *, шаблон: str = "",
                 окно_дней: int = 2) -> Path | None:
    """Ищет файл записи разговора.

    Порядок поиска — от точного к приблизительному: имя по шаблону, затем
    файл, в имени которого встречается идентификатор звонка, и только
    потом — по номеру и времени. Точный идентификатор есть почти всегда:
    MixMonitor обычно зовут с `${UNIQUEID}` в имени, и именно поэтому он
    здесь первый.
    """
    if not каталог or not звонок.uniqueid:
        return None
    try:
        if not каталог.is_dir():
            return None
    except OSError:
        return None

    if шаблон:
        имя = подставить(шаблон, звонок)
        # Проверка на выход за каталог — вторая после очистки полей в
        # `подставить`. Первая снимает «..» из значений, эта ловит всё
        # остальное: символическую ссылку внутри каталога записей, шаблон,
        # начинающийся со слэша, свойства файловой системы. Стоит она один
        # `resolve` на звонок, а отвечает за то, что распознавание не
        # прочитает /etc/shadow и не покажет его в интерфейсе.
        for кандидат in (каталог / имя,
                         *(каталог / f"{имя}{с}" for с in ЗАПИСИ)):
            найденный = _внутри(каталог, кандидат)
            if найденный is not None:
                return найденный

    граница = звонок.started_at - окно_дней * 86400 if звонок.started_at else 0
    лучшее: tuple[float, Path] | None = None
    for путь in каталог.rglob("*"):
        try:
            if not путь.is_file() or путь.suffix not in ЗАПИСИ:
                continue
            изменён = путь.stat().st_mtime
        except OSError:
            continue
        if граница and изменён < граница:
            continue
        имя = путь.name
        # Проверка «внутри каталога» нужна и здесь, а не только в ветке
        # шаблона: `rglob` идёт по именам, а `is_file()` идёт по ссылке, и
        # символическая ссылка наружу выглядит обычным файлом записи.
        подходит = (звонок.uniqueid and звонок.uniqueid in имя) or (
            звонок.src and звонок.dst and звонок.src in имя and звонок.dst in имя)
        if not подходит:
            continue
        настоящий = _внутри(каталог, путь)
        if настоящий is None:
            continue
        if звонок.uniqueid and звонок.uniqueid in имя:
            return настоящий
        близость = abs(изменён - (звонок.started_at or изменён))
        if лучшее is None or близость < лучшее[0]:
            лучшее = (близость, настоящий)
    return лучшее[1] if лучшее else None


def _внутри(каталог: Path, кандидат: Path) -> Path | None:
    """Файл, если он существует и лежит внутри каталога записей."""
    try:
        корень = каталог.resolve(strict=False)
        путь = кандидат.resolve(strict=False)
        if not путь.is_relative_to(корень):
            log.warning("Шаблон имени вывел за каталог записей: %s", кандидат)
            return None
        return путь if путь.is_file() else None
    except OSError:
        return None


#: Что нельзя пускать в имя файла из полей звонка. Номер и имя звонящего
#: приходят снаружи — их задаёт тот, кто звонит, — и попадают в путь,
#: который сервер потом открывает на чтение. Достаточно одного «../» в
#: caller ID, чтобы шаблон вида `${SRC}.wav` увёл поиск из каталога записей
#: куда угодно, а расшифровка отдала содержимое чужого файла в интерфейс.
_ОПАСНОЕ_В_ИМЕНИ = re.compile(r"[/\\\x00]|\.\.")


def _безопасно(значение: Any) -> str:
    """Значение поля звонка, пригодное для подстановки в путь."""
    очищено = _ОПАСНОЕ_В_ИМЕНИ.sub("_", str(значение or ""))
    return очищено.strip()


def подставить(шаблон: str, звонок: Звонок) -> str:
    """Подставляет поля звонка в шаблон имени файла.

    Понимает оба написания — свойское `{uniqueid}` и привычное по
    `extensions.conf` `${UNIQUEID}`: в шаблонах MixMonitor пишут второе, и
    справка параметра обещает именно его. Первая версия подставляла даты
    только в первом написании, поэтому пример из справки
    (`${YEAR}/${MONTH}/${DAY}/${UNIQUEID}.wav`) давал путь с literal-скобками,
    и запись не находилась никогда.
    """
    замены = {
        "uniqueid": звонок.uniqueid, "src": звонок.src, "dst": звонок.dst,
        "clid": звонок.clid, "channel": звонок.channel,
        "userfield": звонок.userfield, "accountcode": звонок.accountcode,
        "direction": звонок.direction, "queue": звонок.queue,
        "agent": звонок.agent,
    }
    if звонок.started_at:
        момент = datetime.fromtimestamp(звонок.started_at)
        замены.update({
            "date": момент.strftime("%Y-%m-%d"), "time": момент.strftime("%H%M%S"),
            "year": момент.strftime("%Y"), "month": момент.strftime("%m"),
            "day": момент.strftime("%d"), "hour": момент.strftime("%H"),
        })
    итог = шаблон
    for ключ, значение in замены.items():
        безопасное = _безопасно(значение)
        итог = итог.replace("{" + ключ + "}", безопасное)
        итог = итог.replace("${" + ключ.upper() + "}", безопасное)
    return итог
