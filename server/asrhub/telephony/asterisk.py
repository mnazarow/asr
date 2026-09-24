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
from datetime import datetime, tzinfo
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
    #: Смещение конца строки журнала CDR, из которой взят звонок (−1 — не из
    #: журнала). По нему позиция чтения двигается за каждым обработанным
    #: звонком, а не за всей порцией сразу.
    конец_строки: int = -1
    #: Какой файл журнала читался (номер inode): позиция без него ничего не
    #: значит, когда журнал поворачивают — повёрнутый хвост дочитывается по
    #: своему смещению, новый файл — по своему.
    inode_журнала: int = 0

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

    @classmethod
    def из_записи(cls, строка: dict[str, Any]) -> Звонок:
        """Звонок обратно из строки таблицы — для отложенных.

        Звонок, который не удалось поставить сразу (запись ещё пишется,
        очередь отказала, каталог не задан), кладётся в таблицу с пометкой
        и разбирается следующим заходом уже отсюда. Из журнала он больше не
        придёт никогда: позиция чтения сдвинута, а событие AMI — вообще
        разовое.
        """
        звонок = cls()
        for имя in ("src", "dst", "clid", "channel", "dstchannel",
                    "context", "disposition", "recording", "direction",
                    "queue", "agent", "userfield", "accountcode"):
            setattr(звонок, имя, str(строка.get(имя) or ""))
        звонок.duration = int(строка.get("duration") or 0)
        звонок.billsec = int(строка.get("billsec") or 0)
        звонок.started_at = float(строка.get("started_at") or 0.0)
        звонок.answered = bool(строка.get("answered"))
        # Идентификатор — тот, что дала станция, а не ключ архива: по нему
        # ищется файл записи, и «golovnoy:1789.5» в имени файла не встретится
        # никогда. Ключ архива собирается заново тем, кто будет сохранять.
        звонок.uniqueid = str(строка.get("pbx_uid") or строка.get("uniqueid") or "")
        звонок.raw = {}
        return звонок

    def поля_записи(self) -> dict[str, Any]:
        """То же самое без идентификатора — как раз для сохранения в базу.

        Идентификатор там передаётся отдельным аргументом, и оставлять его
        ещё и в наборе полей нельзя: Python отвечает на это «два значения
        для одного аргумента», причём в тот момент, когда звонок уже
        поставлен в очередь, — задание есть, записи о нём нет.
        """
        данные = self.to_dict()
        # Идентификатор станции едет отдельным полем: ключ архива у звонка
        # свой (станция плюс идентификатор), а искать запись и спрашивать
        # АТС надо по тому, что дала она.
        данные["pbx_uid"] = данные.pop("uniqueid", "")
        return данные


# ---------------------------------------------------------------------------
# Разбор журналов
# ---------------------------------------------------------------------------

def _время(значение: str, пояс: tzinfo | None = None) -> float:
    """Отметка времени Asterisk («2026-09-10 14:03:11») в секунды эпохи.

    Asterisk пишет местное время станции без пояса. `пояс` — пояс станции;
    None — пояс сервера, как было всегда. Разница не косметическая: сервер
    в контейнере живёт по всемирному времени, и без пояса станции каждый
    звонок московской АТС сдвигался на три часа — вместе с выдержкой,
    окном поиска записи по номерам и всеми отчётами.
    """
    значение = (значение or "").strip()
    if not значение:
        return 0.0
    for образец in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            момент = datetime.strptime(значение[:19], образец)
        except ValueError:
            continue
        if пояс is not None:
            момент = момент.replace(tzinfo=пояс)
        return момент.timestamp()
    try:                                    # некоторые сборки пишут секунды эпохи
        return float(значение)
    except (TypeError, ValueError):
        return 0.0


def _целое(значение: Any) -> int:
    try:
        return int(float(str(значение).strip() or 0))
    except (TypeError, ValueError):
        return 0


def звонок_из_полей(поля: dict[str, str], *, пояс: tzinfo | None = None) -> Звонок:
    """Общий вид из набора полей — что из AMI, что из CSV.

    `пояс` — часовой пояс станции (см. `_время`).
    """
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
        started_at=_время(str(поля.get("start") or ""), пояс),
        answered=расположение == "ANSWERED",
        userfield=str(поля.get("userfield") or "").strip(),
        accountcode=str(поля.get("accountcode") or "").strip(),
        raw={к: str(з) for к, з in поля.items()},
    )


def разобрать_cdr_строку(строка: str, *, пояс: tzinfo | None = None) -> Звонок | None:
    """Строка `Master.csv` — в звонок.

    Строка разбирается настоящим разбором CSV, а не делением по запятой:
    в имени звонящего запятые встречаются («Иванов, отдел продаж»), и
    деление по запятой сдвигало бы все поля правее — вместе с номером,
    длительностью и идентификатором. `пояс` — часовой пояс станции.
    """
    строка = (строка or "").strip()
    if not строка:
        return None
    значения = _поля_csv(строка)
    if значения is None:
        return None
    поля = dict(zip(ПОЛЯ_CSV, значения, strict=False))
    звонок = звонок_из_полей(поля, пояс=пояс)
    return звонок if звонок.uniqueid else None


#: Идентификатор звонка у Asterisk — «секунды.порядковый», иногда с
#: приставкой имени системы. Проверка нужна не ради красоты: по ней видно,
#: что поля не сдвинулись при разборе.
_ПОХОЖ_НА_ID = re.compile(r"^[\w.\-]*\d+\.\d+$")

#: Значение `amaflags` — ровно одна цифра от нуля до трёх. Именно оно
#: оказывается на месте идентификатора, когда журнал сдвинут на колонку.
_ПОХОЖ_НА_AMAFLAGS = re.compile(r"^[0-3]$")


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
    # Ни один разбор не нашёл идентификатор там, где он должен быть.
    # Отдавать сдвинутую строку нельзя: в позиции идентификатора у неё
    # окажется `amaflags` — это 0, 1, 2 или 3, — и `call_exists` на
    # четвёртом же звонке начнёт отвечать «уже импортирован» всему
    # остальному журналу. Докстрока обещала, что сдвиг будет замечен;
    # замечать его и молча возвращать сдвинутое — хуже, чем не заметить.
    if запасной is None:
        return None
    сдвинутый = _выровнять_по_id(запасной)
    if сдвинутый is not None:
        return сдвинутый
    # Образец `_ПОХОЖ_НА_ID` описывает идентификатор Asterisk, но не всякая
    # станция его соблюдает: бывают и счётчики, и свои схемы. Поэтому
    # отвергаем не «не совпало с образцом», а конкретный признак сдвига —
    # в позиции идентификатора оказался `amaflags`, то есть одна цифра.
    # Из-за него `call_exists` на четвёртом же звонке начинал отвечать
    # «уже импортирован» всему остальному журналу.
    место = ПОЛЯ_CSV.index("uniqueid")
    значение = запасной[место].strip() if место < len(запасной) else ""
    if not значение or _ПОХОЖ_НА_AMAFLAGS.match(значение):
        log.warning("Строка журнала звонков не разобрана: на месте идентификатора "
                    "«%s» — похоже, колонки сдвинуты", значение)
        return None
    return запасной


def _выровнять_по_id(значения: list[str]) -> list[str] | None:
    """Если журнал сдвинут на колонку-другую — находим сдвиг по идентификатору.

    Часть сборок и надстроек добавляет в `Master.csv` свои колонки. Пока
    сдвиг одинаков для всей строки, его видно по единственному полю,
    похожему на идентификатор звонка: по нему и выравниваем. Если таких
    полей несколько или ни одного — не гадаем.
    """
    места = [i for i, з in enumerate(значения) if _ПОХОЖ_НА_ID.match(з.strip())]
    if len(места) != 1:
        return None
    сдвиг = места[0] - ПОЛЯ_CSV.index("uniqueid")
    if сдвиг <= 0 or сдвиг + len(ПОЛЯ_CSV) > len(значения) + 1:
        return None
    log.info("Журнал звонков сдвинут на %d колонк(и) — выравниваем по идентификатору",
             сдвиг)
    return значения[сдвиг:]


def длины_внутренних(значение: Any) -> set[int]:
    """Набор длин внутреннего номера из настройки — в любом написании.

    Одной длины не хватает: в организации, которая росла или объединялась,
    рядом живут трёхзначные добавочные старого офиса, четырёхзначные нового
    и шестизначные номера, совпадающие с табельными. Одно число объявляло
    внешними либо одни, либо другие — и направление у половины звонков
    получалось наугад.

    Принимаем и число, и список, и строку «3, 4, 6» — настройку правят
    руками, и требовать от человека ровно одного написания незачем.
    """
    if значение is None or значение == "":
        return set()
    куски: list[Any]
    if isinstance(значение, (list, tuple, set)):
        куски = list(значение)
    elif isinstance(значение, str):
        куски = re.split(r"[^0-9]+", значение)
    else:
        куски = [значение]
    длины = set()
    for кусок in куски:
        try:
            # Через float, а не сразу в int: в JSON-настройке число легко
            # оказывается «4.0» — это та же четвёрка, и отбрасывать её
            # молча значило бы потерять целую длину номера.
            число = int(float(str(кусок).strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= число <= 12:
            длины.add(число)
    return длины


def правила_контекстов(значение: Any) -> list[tuple[str, str]]:
    """Пары «контекст → направление» с сохранением порядка.

    Порядок важен: контексты именуют как придётся, и `from-internal`
    попадает внутрь `from-internal-custom`. Кто написан первым, тот и
    решает — как в диалплане. Словарь порядок сохраняет, список пар
    позволяет задать один контекст дважды с разными условиями, а строка
    «from-trunk=входящий, from-internal=исходящий» — это то, что человек
    наберёт быстрее всего.
    """
    if not значение:
        return []
    пары: list[tuple[str, str]] = []
    if isinstance(значение, dict):
        пары = [(str(к), str(з)) for к, з in значение.items()]
    elif isinstance(значение, str):
        for кусок in re.split(r"[;,\n]+", значение):
            если_есть = кусок.split("=", 1)
            if len(если_есть) == 2:
                пары.append((если_есть[0].strip(), если_есть[1].strip()))
    else:
        for элемент in значение:
            if isinstance(элемент, dict):
                контекст = str(элемент.get("context") or элемент.get("контекст") or "")
                куда = str(элемент.get("direction") or элемент.get("направление") or "")
                if контекст and куда:
                    пары.append((контекст, куда))
            elif isinstance(элемент, (list, tuple)) and len(элемент) == 2:
                пары.append((str(элемент[0]), str(элемент[1])))
    return [(к.strip(), з.strip()) for к, з in пары if к.strip() and з.strip()]


def направление(звонок: Звонок, *, внутренние_знаков: Any = 5,
                контексты: Any = None) -> str:
    """Входящий, исходящий или внутренний.

    Сначала спрашиваем контекст: в `extensions.conf` он и заведён, чтобы
    отличать «звонят нам» от «звоним мы». Правил может быть сколько угодно,
    и порядок у них тот же, что задан: первое совпавшее решает.

    Если контекст ничего не сказал — смотрим на длину номеров: внутренний
    номер короткий, городской длинный. Длин тоже может быть несколько.
    Это догадка, и она названа догадкой: раздел показывает направление, а
    не выдаёт его за факт биллинга.
    """
    имя = (звонок.context or "").lower()
    for образец, куда in правила_контекстов(контексты):
        if образец.lower() in имя:
            return куда

    длины = длины_внутренних(внутренние_знаков) or {5}
    предел = max(длины)

    def свой(номер: str) -> bool:
        цифр = len(re.sub(r"\D", "", номер))
        if not цифр:
            return False
        # Точное совпадение с одной из заданных длин — уверенно «свой».
        # Иначе сравниваем с наибольшей: настройка «3, 4, 6» описывает
        # длины, а не потолок, но номер в пять цифр между тремя и шестью
        # разумнее считать внутренним, чем городским.
        return цифр in длины or цифр <= предел

    свой_src, свой_dst = свой(звонок.src), свой(звонок.dst)
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
                 secret: str = "", *, timeout: float = ТАЙМАУТ,
                 события: str = "cdr"):
        self.host = host
        self.port = int(port or 5038)
        self.username = username
        self.secret = secret
        self.timeout = float(timeout or ТАЙМАУТ)
        #: Какие классы событий просить у АТС при входе. Забору нужны только
        #: `Cdr`: поток читается пять секунд раз в минуту, и на загруженной
        #: станции события каналов, очередей и набора номера забивали буфер
        #: так, что до `Cdr` чтение не добиралось. Разбору забора нужно всё
        #: («on») — он отличает «событий нет вовсе» от «нет именно Cdr»;
        #: проверке связи не нужно ничего («off»).
        self.события = str(события or "cdr")
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
                            Events=self.события)
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
        """Кусок из сокета. Пусто — «событий сейчас нет», не «всё кончилось».

        Различать эти два состояния обязательно, и раньше они были
        неразличимы. `recv` возвращает пустые байты ровно в одном случае —
        станция закрыла соединение (перезапуск Asterisk, `manager reload`,
        обрыв сети, выход по тайм-ауту сессии). Тайм-аут же чтения — это
        «за секунду ничего не пришло», совершенно обычное дело между
        звонками. Обоих раньше сводили к `b""`, и вышло вот что: после
        обрыва клиент оставался «подключённым», каждый заход читал пустоту,
        считал, что новых звонков нет, и уходил спать. Станция замолкала
        навсегда — молча, с пустым `last_error` и бодрым «соединение
        установлено» в разделе. Возвращалась она только с перезапуском
        сервера.

        Теперь конец соединения — это ошибка. Импортёр её ловит, закрывает
        сокет и на следующем заходе подключается заново: обрыв станции
        стоит одного пропущенного такта вместо вечного молчания.
        """
        сокет = self._sock
        if сокет is None:
            raise AMIError("Соединение с АТС закрыто.")
        try:
            кусок = сокет.recv(8192)
        except TimeoutError:
            return b""
        except OSError as exc:
            raise AMIError(f"Обрыв связи с АТС: {exc}") from exc
        if not кусок:
            raise AMIError(
                f"АТС {self.host}:{self.port} закрыла соединение.",
                hint="Обычно это перезапуск Asterisk или «manager reload». "
                     "Сервер подключится заново на следующем заходе; если "
                     "обрывы идут подряд — проверьте в manager.conf срок "
                     "сессии и permit/deny для адреса этого сервера.")
        return кусок

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
    клиент = AMIClient(host, port, username, secret, timeout=timeout, события="off")
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

def _читать_файл(path: Path, offset: int, limit: int, пояс: tzinfo | None,
                 inode: int) -> tuple[list[Звонок], int]:
    """Целые строки файла журнала с позиции `offset` — не больше `limit` звонков."""
    звонки: list[Звонок] = []
    # Файл читается ДВОИЧНО, а позиция считается по настоящим байтам.
    # Текстовое чтение с `errors="replace"` меняло длину: байт, не
    # сложившийся в UTF-8 (имя звонящего из SIP-заголовка в CP1251 —
    # обычное дело на российских станциях), превращался в U+FFFD и
    # кодировался обратно тремя байтами вместо одного. Позиция уезжала
    # вперёд, и следующий звонок пропадал целиком; дрейф накапливался
    # строка за строкой.
    with path.open("rb") as файл:
        файл.seek(offset)
        for сырая in файл:
            if not сырая.endswith(b"\n"):
                # Строка ещё дописывается — оставляем её следующему заходу.
                break
            offset += len(сырая)
            звонок = разобрать_cdr_строку(сырая.decode("utf-8", "replace"), пояс=пояс)
            if звонок is not None:
                звонок.конец_строки = offset
                звонок.inode_журнала = inode
                звонки.append(звонок)
            if len(звонки) >= limit:
                break
    return звонки, offset


def _недоступен(path: Path, exc: OSError) -> ConfigError:
    # Не AMIError: недоступный файл — это настройка и права, а не сбой
    # связи со станцией. Иначе ответ приходил с кодом 502 и подсказкой
    # про manager.conf, к которой нечего было применить: учётной записи
    # AMI у источника «журнал CDR» нет вовсе.
    return ConfigError(
        f"Журнал звонков {path} не читается: {exc}",
        hint="Проверьте путь в настройке «Журнал звонков CDR» и права на "
             "чтение у пользователя, от которого работает сервер.")


def читать_csv(path: Path, *, offset: int = 0, limit: int = 500,
               пояс: tzinfo | None = None) -> tuple[list[Звонок], int]:
    """Новые строки журнала с запомненной позиции.

    Позиция — смещение в байтах: журнал только дописывается, и читать его
    целиком на каждом заходе значило бы перечитывать гигабайт ради
    десятка строк. Если файл вдруг стал короче (его повернули по
    расписанию), начинаем с начала — иначе новые звонки пропали бы совсем.
    Повёрнутый хвост дочитывает `читать_журнал`, которому известен inode.
    """
    try:
        сведения = path.stat()
    except OSError as exc:
        raise _недоступен(path, exc) from exc
    if offset > сведения.st_size:
        offset = 0
    return _читать_файл(path, offset, limit, пояс, сведения.st_ino)


#: Хвосты повёрнутых журналов, которые сервер не распаковывает: сжатая
#: копия — это уже архив, и её хвост дочитан до сжатия или не будет никогда.
_СЖАТЫЕ = (".gz", ".bz2", ".xz", ".zst", ".zip")


def _повёрнутый(path: Path, inode: int) -> Path | None:
    """Прежний файл журнала после ротации — по номеру inode.

    logrotate переименовывает `Master.csv` в `Master.csv.1` (или
    `Master.csv-20260910`) и заводит новый файл. Номер inode у
    переименованного остаётся прежним — по нему и узнаём, куда уехал хвост,
    который сервер не успел прочитать. Ищем только среди соседей с тем же
    началом имени: номер inode после удаления файла достаётся другим.
    """
    if not inode:
        return None
    try:
        соседи = list(path.parent.iterdir())
    except OSError:
        return None
    for сосед in соседи:
        имя = сосед.name
        if имя == path.name or not имя.startswith(path.name):
            continue
        if имя.lower().endswith(_СЖАТЫЕ):
            continue
        try:
            if сосед.is_file() and сосед.stat().st_ino == inode:
                return сосед
        except OSError:
            continue
    return None


def _копия_хвоста(path: Path, offset: int) -> tuple[Path, int] | None:
    """Копия, в которую уехал хвост журнала при ротации с `copytruncate`.

    Так поворачивают журнал, который программа держит открытым: содержимое
    копируется в `Master.csv.1`, а сам файл обрезается до нуля — номер inode
    у него прежний, и узнать ротацию можно только по тому, что файл стал
    короче. Смещения в копии те же, что были в журнале, поэтому хвост
    дочитывается с запомненного места. Берётся самая свежая копия не короче
    этого места, изменённая за последние двое суток: более старая копия —
    это уже прошлые ротации.
    """
    лучшая: tuple[float, Path, int] | None = None
    try:
        соседи = list(path.parent.iterdir())
    except OSError:
        return None
    граница = time.time() - 2 * 86400
    for сосед in соседи:
        имя = сосед.name
        if имя == path.name or not имя.startswith(path.name) \
                or имя.lower().endswith(_СЖАТЫЕ):
            continue
        try:
            сведения = сосед.stat()
        except OSError:
            continue
        if not сосед.is_file() or сведения.st_size < offset or сведения.st_mtime < граница:
            continue
        if лучшая is None or сведения.st_mtime > лучшая[0]:
            лучшая = (сведения.st_mtime, сосед, сведения.st_ino)
    if лучшая is None:
        return None
    # Запомненное место обязано быть границей строки и в копии: иначе это
    # не наша копия (скажем, журнал обрезали руками, а рядом лежит вчерашний
    # повёрнутый), и чтение с середины строки дало бы мусор.
    if offset > 0:
        try:
            with лучшая[1].open("rb") as файл:
                файл.seek(offset - 1)
                if файл.read(1) != b"\n":
                    return None
        except OSError:
            return None
    return лучшая[1], лучшая[2]


def читать_журнал(path: Path, *, offset: int = 0, inode: int = 0, limit: int = 500,
                  пояс: tzinfo | None = None) -> tuple[list[Звонок], int, int]:
    """Новые звонки журнала с учётом ротации: звонки, позиция и inode файла.

    Позиция — это пара «какой файл (inode) и где в нём». Раньше помнилось
    только смещение, и ротация замечалась лишь по тому, что файл стал
    короче: всё, что станция успела дописать в старый файл между последним
    заходом и ротацией, пропадало — при опросе раз в минуту это звонки
    последней минуты каждого дня.

    Теперь, если inode сменился, сначала дочитывается хвост повёрнутого
    файла с запомненного места — порциями, как обычный журнал, — и только
    когда он кончится, чтение переходит на новый файл с начала. Позицию
    вызывающий хранит вместе с inode (см. `Звонок.inode_журнала`). Сжатую
    или удалённую копию дочитать нечем — тогда новый файл читается с начала,
    как раньше.
    """
    try:
        сведения = path.stat()
    except OSError as exc:
        raise _недоступен(path, exc) from exc
    if inode and inode != сведения.st_ino:
        старый = _повёрнутый(path, inode)
        if старый is not None:
            try:
                звонки, конец = _читать_файл(старый, offset, limit, пояс, inode)
            except OSError as exc:
                log.warning("Повёрнутый журнал %s не дочитан: %s", старый, exc)
                звонки, конец = [], offset
            if звонки:
                return звонки, конец, inode
            log.info("Хвост повёрнутого журнала %s дочитан — дальше %s с начала",
                     старый.name, path.name)
        else:
            log.info("Журнал %s повернули, а прежний файл не найден (сжат или "
                     "удалён) — читаем новый с начала", path)
        offset = 0
    elif offset > сведения.st_size:
        # Файл стал короче при том же inode — ротация с copytruncate: хвост
        # лежит в копии по тем же смещениям.
        копия = _копия_хвоста(path, offset)
        if копия is not None:
            try:
                звонки, конец = _читать_файл(копия[0], offset, limit, пояс, копия[1])
            except OSError as exc:
                log.warning("Копия журнала %s не дочитана: %s", копия[0], exc)
                звонки, конец = [], offset
            if звонки:
                return звонки, конец, копия[1]
        offset = 0
    звонки, конец = _читать_файл(path, offset, limit, пояс, сведения.st_ino)
    return звонки, конец, сведения.st_ino


#: Расширения записей, которые имеет смысл искать.
ЗАПИСИ = (".wav", ".mp3", ".gsm", ".ogg", ".WAV", ".alaw", ".ulaw", ".sln")

#: То же без оглядки на регистр: «.MP3» — такая же запись, как «.mp3».
_ЗАПИСИ_НИЖНИМ = frozenset(с.lower() for с in ЗАПИСИ)

#: Идентификатор звонка в имени файла — «секунды.порядковый», как его
#: подставляет MixMonitor из ${UNIQUEID}. По нему строится указатель.
_ID_В_ИМЕНИ = re.compile(r"\d{6,}\.\d+")


def _запись_ли(путь: Path) -> bool:
    return путь.suffix.lower() in _ЗАПИСИ_НИЖНИМ


class УказательЗаписей:
    """Файлы каталога записей: один обход на заход, а не на каждый звонок.

    `найти_запись` обходила весь каталог на КАЖДЫЙ звонок. На станции с
    архивом за три года это сотни тысяч файлов, и порция из пятидесяти
    звонков превращалась в пятьдесят полных обходов диска. Указатель
    обходит каталог один раз и дальше отвечает из памяти — так же, как
    `Записи` у агента на станции.

    Собирается лениво — первым звонком, которому понадобился, — и
    пересобирается, когда свежему звонку запись не нашлась: MixMonitor мог
    закрыть файл уже после обхода. Но не чаще раза в полминуты: на сборе
    архива промахов тысячи, а обход стоит секунд.
    """

    ПЕРЕСОБИРАТЬ_НЕ_ЧАЩЕ_С = 30.0

    def __init__(self, каталог: Path) -> None:
        self.каталог = каталог
        #: (время изменения, имя, путь) — по возрастанию времени: окно по
        #: номерам выбирается двоичным поиском, а не перебором всего архива.
        self._файлы: list[tuple[float, str, Path]] = []
        self._времена: list[float] = []
        self._по_id: dict[str, list[tuple[float, Path]]] = {}
        self._собран = 0.0
        #: Сколько раз обходили каталог — видно в проверках и в журнале.
        self.обходов = 0

    def собрать(self) -> None:
        файлы: list[tuple[float, str, Path]] = []
        по_id: dict[str, list[tuple[float, Path]]] = {}
        try:
            for путь in self.каталог.rglob("*"):
                try:
                    if not _запись_ли(путь) or not путь.is_file():
                        continue
                    изменён = путь.stat().st_mtime
                except OSError:
                    continue
                файлы.append((изменён, путь.name, путь))
                for токен in _ID_В_ИМЕНИ.findall(путь.name):
                    по_id.setdefault(токен, []).append((изменён, путь))
        except OSError as exc:
            log.warning("Каталог записей %s обойти не удалось: %s", self.каталог, exc)
        файлы.sort(key=lambda з: з[0])
        self._файлы = файлы
        self._времена = [з[0] for з in файлы]
        self._по_id = по_id
        self._собран = time.time()
        self.обходов += 1

    def _готов(self) -> None:
        if not self._собран:
            self.собрать()

    def файлы(self) -> list[tuple[float, str, Path]]:
        self._готов()
        return self._файлы

    def в_окне(self, начало: float, конец: float) -> list[tuple[float, str, Path]]:
        """Файлы, изменённые в промежутке [начало, конец]."""
        import bisect  # noqa: PLC0415

        self._готов()
        слева = bisect.bisect_left(self._времена, начало)
        справа = bisect.bisect_right(self._времена, конец)
        return self._файлы[слева:справа]

    def по_идентификатору(self, uniqueid: str) -> list[tuple[float, Path]]:
        """Файлы, в имени которых стоит этот идентификатор (или его числовая часть)."""
        self._готов()
        найдено: list[tuple[float, Path]] = []
        for токен in dict.fromkeys(_ID_В_ИМЕНИ.findall(uniqueid) or [uniqueid]):
            найдено.extend(self._по_id.get(токен, []))
        return найдено

    def пересобрать_для(self, звонок: Звонок) -> bool:
        """Пересобрать, если запись свежего звонка могла лечь после обхода."""
        if not self._собран or time.time() - self._собран < self.ПЕРЕСОБИРАТЬ_НЕ_ЧАЩЕ_С:
            return False
        конец = (звонок.started_at or 0) + (звонок.duration or 0)
        if not звонок.started_at or конец < self._собран - 3600:
            return False
        self.собрать()
        return True


def _id_в_имени(uniqueid: str, имя: str) -> bool:
    """Стоит ли идентификатор звонка в имени файла ОТДЕЛЬНО, а не частью другого.

    Подстрока без границ путала соседние звонки: «1757500001.1» находится
    внутри «1757500001.12.wav», и звонку №1 доставалась запись звонка №12 —
    две записи в одну секунду на загруженной станции не редкость.
    """
    if not uniqueid:
        return False
    return re.search(rf"(?<!\d){re.escape(uniqueid)}(?!\d)", имя) is not None


def _маска_ли(имя: str) -> bool:
    return any(з in имя for з in "*?[")


def _по_маске(каталог: Path, маска: str, звонок: Звонок, окно: float) -> Path | None:
    """Шаблон со звёздочкой: «out-${DST}-${SRC}-${YEAR}${MONTH}${DAY}-*.wav».

    Такой пример стоял в справке параметра, а поиск понимал шаблон только
    как точное имя: звёздочка искалась буквально, и запись не находилась
    никогда. Из подошедших берётся ближайшая по времени к началу разговора
    (и не дальше окна подбора, если оно задано).
    """
    лучшее: tuple[float, Path] | None = None
    try:
        кандидаты = sorted(каталог.glob(маска))[:1000]
    except (OSError, ValueError, NotImplementedError) as exc:
        log.warning("Шаблон имени «%s» не разобрался как маска: %s", маска, exc)
        return None
    for кандидат in кандидаты:
        if not _запись_ли(кандидат) and кандидат.suffix:
            continue
        настоящий = _внутри(каталог, кандидат)
        if настоящий is None:
            continue
        try:
            изменён = настоящий.stat().st_mtime
        except OSError:
            continue
        близость = abs(изменён - (звонок.started_at or изменён))
        if звонок.started_at and окно and близость > окно:
            continue
        if лучшее is None or близость < лучшее[0]:
            лучшее = (близость, настоящий)
    return лучшее[1] if лучшее else None


def найти_запись(звонок: Звонок, каталог: Path, *, шаблон: str = "",
                 окно_дней: int = 2, окно_минут: int = 120,
                 пояс: tzinfo | None = None,
                 указатель: УказательЗаписей | None = None) -> Path | None:
    """Ищет файл записи разговора.

    Порядок поиска — от точного к приблизительному: имя по шаблону, затем
    файл, в имени которого стоит идентификатор звонка, и только потом — по
    номерам и времени. Точный идентификатор есть почти всегда: MixMonitor
    обычно зовут с `${UNIQUEID}` в имени, и именно поэтому он здесь первый.

    У двух последних способов разная точность, и ограничения у них тоже
    разные. `окно_дней` — грубая отсечка по возрасту файла: она экономит
    обход каталога и одинаково действует на оба способа. `окно_минут` —
    точное окно вокруг начала разговора, и работает оно только там, где
    поиск идёт ПО НОМЕРАМ: имя вида `79161234567-79995554433-*.wav`
    повторяется у каждого разговора этой пары, поэтому без окна звонку
    досталась бы просто ближайшая по времени запись — хоть вчерашняя, хоть
    позавчерашняя. Совпадение по идентификатору окном не ограничивается: он
    уникален, и если файл с ним лежит в каталоге — это он и есть, даже если
    станция дописала его через сутки.

    `пояс` — часовой пояс станции: даты в шаблоне имени MixMonitor
    подставляет по её часам. `указатель` — обход каталога, общий на заход
    (см. `УказательЗаписей`); без него каталог обходится здесь же.
    """
    if not каталог or not звонок.uniqueid:
        return None
    try:
        if not каталог.is_dir():
            return None
    except OSError:
        return None
    окно = max(0.0, float(окно_минут or 0)) * 60.0

    if шаблон:
        имя = подставить(шаблон, звонок, пояс=пояс)
        if _маска_ли(имя):
            найденный = _по_маске(каталог, имя, звонок, окно)
            if найденный is not None:
                return найденный
        else:
            # Проверка на выход за каталог — вторая после очистки полей в
            # `подставить`. Первая снимает «..» из значений, эта ловит всё
            # остальное: символическую ссылку внутри каталога записей,
            # шаблон, начинающийся со слэша, свойства файловой системы.
            # Стоит она один `resolve` на звонок, а отвечает за то, что
            # распознавание не прочитает /etc/shadow и не покажет его в
            # интерфейсе.
            for кандидат in (каталог / имя,
                             *(каталог / f"{имя}{с}" for с in ЗАПИСИ)):
                найденный = _внутри(каталог, кандидат)
                if найденный is not None:
                    return найденный

    своё = указатель if указатель is not None else УказательЗаписей(каталог)
    найденный = _подобрать(звонок, каталог, своё, окно_дней, окно)
    if найденный is None and указатель is not None and указатель.пересобрать_для(звонок):
        найденный = _подобрать(звонок, каталог, указатель, окно_дней, окно)
    return найденный


def _подобрать(звонок: Звонок, каталог: Path, указатель: УказательЗаписей,
               окно_дней: int, окно: float) -> Path | None:
    """Поиск по идентификатору, затем по номерам — по готовому указателю."""
    граница = звонок.started_at - окно_дней * 86400 if звонок.started_at else 0
    # Идентификатор. Проверка «внутри каталога» нужна и здесь: обход идёт
    # по именам, а `is_file()` — по ссылке, и символическая ссылка наружу
    # выглядит обычным файлом записи.
    for изменён, путь in указатель.по_идентификатору(звонок.uniqueid):
        if граница and изменён < граница:
            continue
        if not _id_в_имени(звонок.uniqueid, путь.name) and not any(
                _id_в_имени(токен, путь.name)
                for токен in _ID_В_ИМЕНИ.findall(звонок.uniqueid)):
            continue
        настоящий = _внутри(каталог, путь)
        if настоящий is not None:
            return настоящий
    if not _ID_В_ИМЕНИ.search(звонок.uniqueid):
        # Идентификатор не того вида, что попадает в указатель, — ищем его
        # в именах перебором, но с той же границей числа.
        for изменён, имя, путь in указатель.файлы():
            if (not граница or изменён >= граница) and _id_в_имени(звонок.uniqueid, имя):
                настоящий = _внутри(каталог, путь)
                if настоящий is not None:
                    return настоящий
    if not (звонок.src and звонок.dst):
        return None
    # Номера. Окно — вокруг начала разговора, в обе стороны. Без него сюда
    # попадал файл, записанный через несколько суток после звонка:
    # `граница` отсекает только слишком СТАРЫЕ файлы, а вперёд ограничения
    # не было вовсе, и разговор той же пары номеров, состоявшийся в четверг,
    # доставался звонку понедельника — просто потому, что более близкой
    # записи в каталоге не нашлось. Окно выбирается из указателя двоичным
    # поиском: перебирать ради него весь архив незачем.
    if звонок.started_at and окно:
        кандидаты = указатель.в_окне(звонок.started_at - окно, звонок.started_at + окно)
    else:
        кандидаты = указатель.файлы()
    лучшее: tuple[float, Path] | None = None
    for изменён, имя, путь in кандидаты:
        if граница and изменён < граница:
            continue
        if not (_номер_в_имени(звонок.src, имя) and _номер_в_имени(звонок.dst, имя)):
            continue
        близость = abs(изменён - (звонок.started_at or изменён))
        if лучшее is not None and близость >= лучшее[0]:
            continue
        настоящий = _внутри(каталог, путь)
        if настоящий is not None:
            лучшее = (близость, настоящий)
    return лучшее[1] if лучшее else None


def _номер_в_имени(номер: str, имя: str) -> bool:
    """Встречается ли номер в имени файла ОТДЕЛЬНЫМ числом.

    Подстрока без границ подбирает чужую запись: в имени
    `out-79161234567-79995554433-20260910-101020.wav` найдутся и «101», и
    «102» — оба внутри времени «101020». Звонок 101 → 102, у которого своей
    записи нет, получал чужой внешний разговор: он уходил на распознавание,
    ложился в журнал как звонок 101 → 102 и доставался тому владельцу,
    которому сопоставлен номер 101.

    Границей считается всё, что не цифра: точка, дефис, подчёркивание,
    начало и конец имени.
    """
    if not номер:
        return False
    return re.search(rf"(?<!\d){re.escape(номер)}(?!\d)", имя) is not None


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


def подставить(шаблон: str, звонок: Звонок, *, пояс: tzinfo | None = None) -> str:
    """Подставляет поля звонка в шаблон имени файла.

    Понимает оба написания — свойское `{uniqueid}` и привычное по
    `extensions.conf` `${UNIQUEID}`: в шаблонах MixMonitor пишут второе, и
    справка параметра обещает именно его. Первая версия подставляла даты
    только в первом написании, поэтому пример из справки
    (`${YEAR}/${MONTH}/${DAY}/${UNIQUEID}.wav`) давал путь с literal-скобками,
    и запись не находилась никогда.

    Даты — ещё и под короткими именами `${YYYY}`, `${MM}`, `${DD}`, `${HH}`:
    форма станции в интерфейсе приводила примеры именно с ними, а сервер
    их не знал, и шаблон, написанный по подсказке, не находил ни одной
    записи. И по-русски — `{год}`, `{месяц}`, `{день}`, `{час}`, `{дата}`,
    как у агента на станции. Время — по часам станции (`пояс`): MixMonitor
    называет файл по её времени, а не по времени сервера.
    """
    замены = {
        "uniqueid": звонок.uniqueid, "src": звонок.src, "dst": звонок.dst,
        "clid": звонок.clid, "channel": звонок.channel,
        "userfield": звонок.userfield, "accountcode": звонок.accountcode,
        "direction": звонок.direction, "queue": звонок.queue,
        "agent": звонок.agent,
    }
    if звонок.started_at:
        момент = datetime.fromtimestamp(звонок.started_at, tz=пояс)
        год, месяц, день = момент.strftime("%Y"), момент.strftime("%m"), момент.strftime("%d")
        час, дата = момент.strftime("%H"), момент.strftime("%Y-%m-%d")
        замены.update({
            "date": дата, "time": момент.strftime("%H%M%S"),
            "year": год, "month": месяц, "day": день, "hour": час,
            "minute": момент.strftime("%M"),
            "yyyy": год, "mm": месяц, "dd": день, "hh": час,
            "год": год, "месяц": месяц, "день": день, "час": час, "дата": дата,
        })
    итог = шаблон
    for ключ, значение in замены.items():
        безопасное = _безопасно(значение)
        итог = итог.replace("{" + ключ + "}", безопасное)
        итог = итог.replace("${" + ключ.upper() + "}", безопасное)
    return итог
