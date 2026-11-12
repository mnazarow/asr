"""Телефон SIP: сервер звонит через Asterisk, как обычный аппарат.

Контролю качества нужно перезвонить клиенту, проговорить вопросы и услышать
нажатые кнопки. Самый простой для станции способ — завести серверу
внутренний номер, как любому телефону: тогда исходящий звонок идёт по тем
же правилам, тем же маршрутам и попадает в тот же журнал CDR, что звонки
сотрудников, а администратору АТС не нужны ни AMI, ни ARI, ни правка
диалплана.

Здесь — ровно столько SIP, сколько нужно аппарату, который только звонит:

* регистрация (REGISTER) и вызов (INVITE) с дайджест-аутентификацией
  (MD5 и SHA-256, с `qop=auth` и без);
* транспорт UDP (`rport`, повторы запроса), TCP или TLS — одно соединение
  со станцией, по нему же приходят её запросы; CANCEL по сроку ответа;
* звук G.711 (PCMA и PCMU) по RTP, кадры по 20 мс; по желанию — SRTP с
  ключами в SDP (SDES, см. `srtp`);
* кнопки телефона тремя способами, какими их присылает Asterisk:
  события RTP (RFC 4733, `dtmfmode=rfc2833`), SIP INFO и тоны в самом звуке
  (`inband`) — по двум частотам, алгоритмом Гёрцеля.

Входящие вызовы принимаются, только если задан обработчик (`входящие`,
настройка qc_inbound); без него на INVITE телефон отвечает «занято». Чего
нет намеренно: перевод, удержание, DTLS-SRTP (WebRTC). Библиотек нет тоже:
чистый Python (`ssl` из стандартной библиотеки), сокеты и поток чтения на
каждый.
"""
from __future__ import annotations

import hashlib
import math
import queue
import random
import re
import select
import socket
import ssl
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from . import srtp as шифр_звука

log = get_logger("qc")

#: Кадр звука: 20 мс при 8 кГц.
КАДР = 160
ШАГ_С = 0.02
#: Байт тишины в каждом кодеке.
ТИШИНА = {"PCMA": b"\xd5", "PCMU": b"\xff"}
#: Номера полезной нагрузки RTP у кодеков G.711.
НОМЕР_КОДЕКА = {"PCMA": 8, "PCMU": 0}
КНОПКИ = "0123456789*#ABCD"
#: Первый повтор запроса по UDP и предел ожидания ответа (RFC 3261: T1, 64·T1).
T1 = 0.5
СРОК_ЗАПРОСА_С = 16.0
#: Транспорты SIP. По TCP и TLS запрос не повторяется: доставку держит соединение.
ТРАНСПОРТЫ = ("udp", "tcp", "tls")
#: Предел одного сообщения SIP в потоке TCP/TLS.
ПРЕДЕЛ_СООБЩЕНИЯ = 65535

_ЧАСТОТЫ_СТРОК = (697.0, 770.0, 852.0, 941.0)
_ЧАСТОТЫ_СТОЛБЦОВ = (1209.0, 1336.0, 1477.0, 1633.0)
_РАСКЛАДКА = ("123A", "456B", "789C", "*0#D")


class ОшибкаSIP(Exception):
    """Станция отказала или не ответила. `код` — ответ SIP, 0 — нет ответа."""

    def __init__(self, сообщение: str, код: int = 0) -> None:
        super().__init__(сообщение)
        self.код = код


class НеОтвечает(ОшибкаSIP):
    """Абонент не снял трубку за отведённое время (или 408/480)."""


class Занято(ОшибкаSIP):
    """Абонент занят или отклонил вызов (486, 600, 603)."""


# ---------------------------------------------------------------------------
# G.711: таблицы раскодирования — для поиска тонов кнопок в звуке
# ---------------------------------------------------------------------------

def _таблица_alaw() -> list[int]:
    таблица = []
    for байт in range(256):
        значение = байт ^ 0x55
        знак = значение & 0x80
        порядок = (значение & 0x70) >> 4
        мантисса = значение & 0x0F
        образец = (мантисса << 4) + 8
        if порядок:
            образец = (образец + 0x100) << (порядок - 1)
        таблица.append(образец if знак else -образец)
    return таблица


def _таблица_ulaw() -> list[int]:
    таблица = []
    for байт in range(256):
        значение = ~байт & 0xFF
        знак = значение & 0x80
        порядок = (значение & 0x70) >> 4
        мантисса = значение & 0x0F
        образец = ((мантисса << 3) + 0x84) << порядок
        образец -= 0x84
        таблица.append(-образец if знак else образец)
    return таблица


РАСКОДИРОВАТЬ = {"PCMA": _таблица_alaw(), "PCMU": _таблица_ulaw()}


def _сжать_alaw(образец: int) -> int:
    знак = 0x80 if образец >= 0 else 0
    величина = min(abs(образец), 32635)
    if величина < 256:
        байт = величина >> 4
    else:
        порядок = величина.bit_length() - 8
        байт = (порядок << 4) | ((величина >> (порядок + 3)) & 0x0F)
    return (байт | знак) ^ 0x55


def _сжать_ulaw(образец: int) -> int:
    знак = 0x80 if образец < 0 else 0
    величина = min(abs(образец), 32635) + 0x84
    порядок = величина.bit_length() - 8
    мантисса = (величина >> (порядок + 3)) & 0x0F
    return ~(знак | (порядок << 4) | мантисса) & 0xFF


def сжать(pcm: bytes, кодек: str) -> bytes:
    """16-битный звук (little-endian, 8 кГц, моно) — в G.711."""
    сжатие = _сжать_alaw if кодек == "PCMA" else _сжать_ulaw
    образцы = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    return bytes(сжатие(о) for о in образцы)


def тон_кнопки(кнопка: str, *, мс: int = 120, кодек: str = "PCMA") -> bytes:
    """Двухчастотный тон кнопки в G.711 — для проверок и пробных прогонов."""
    for строка, ряд in enumerate(_РАСКЛАДКА):
        if кнопка in ряд:
            низ, верх = _ЧАСТОТЫ_СТРОК[строка], _ЧАСТОТЫ_СТОЛБЦОВ[ряд.index(кнопка)]
            break
    else:
        raise ValueError(f"нет такой кнопки: {кнопка!r}")
    число = 8 * мс
    образцы = (int(9000 * (math.sin(2 * math.pi * низ * н / 8000)
                           + math.sin(2 * math.pi * верх * н / 8000)))
               for н in range(число))
    return сжать(struct.pack(f"<{число}h", *образцы), кодек)


class ИскательТонов:
    """Кнопки по тонам в звуке: две частоты из восьми, алгоритм Гёрцеля.

    Кадр — 20 мс. Кнопка засчитывается, когда один и тот же тон держится два
    кадра подряд, и повторно — только после паузы: удержанная кнопка — одно
    нажатие. Голос проходит мимо: у него нет двух чистых частот с такой
    долей энергии кадра.
    """

    def __init__(self, кодек: str = "PCMA") -> None:
        self.таблица = РАСКОДИРОВАТЬ[кодек]
        self._коэффициенты = [2.0 * math.cos(2.0 * math.pi * ч / 8000.0)
                              for ч in (*_ЧАСТОТЫ_СТРОК, *_ЧАСТОТЫ_СТОЛБЦОВ)]
        self._прошлая = ""
        self._подряд = 0
        self._выдана = ""

    def _кадр(self, данные: bytes) -> str:
        образцы = [self.таблица[б] for б in данные]
        число = len(образцы)
        if число < 80:
            return ""
        энергия = sum(о * о for о in образцы)
        if энергия < число * 250_000:           # тише примерно −36 дБ — не тон
            return ""
        мощности = []
        for коэф in self._коэффициенты:
            п1 = п2 = 0.0
            for о in образцы:
                п1, п2 = о + коэф * п1 - п2, п1
            мощности.append(п1 * п1 + п2 * п2 - коэф * п1 * п2)
        строка = max(range(4), key=lambda н: мощности[н])
        столбец = max(range(4), key=lambda н: мощности[4 + н])
        низ, верх = мощности[строка], мощности[4 + столбец]
        # Обе частоты несут почти всю энергию кадра и сравнимы между собой.
        if (низ + верх) < 0.55 * энергия * число / 2:
            return ""
        if низ > 8 * верх or верх > 8 * низ:
            return ""
        return _РАСКЛАДКА[строка][столбец]

    def подать(self, данные: bytes) -> str:
        """Кадр звука; ответ — кнопка, если она только что нажата, иначе ''."""
        кнопка = self._кадр(данные)
        if кнопка and кнопка == self._прошлая:
            self._подряд += 1
        else:
            self._подряд = 1 if кнопка else 0
        self._прошлая = кнопка
        if not кнопка:
            self._выдана = ""
            return ""
        if self._подряд >= 2 and self._выдана != кнопка:
            self._выдана = кнопка
            return кнопка
        return ""


# ---------------------------------------------------------------------------
# Разбор и сборка сообщений SIP
# ---------------------------------------------------------------------------

_КРАТКИЕ = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
            "l": "content-length", "c": "content-type", "k": "supported"}


@dataclass
class Сообщение:
    """Запрос или ответ SIP."""

    начало: str
    заголовки: list[tuple[str, str]] = field(default_factory=list)
    тело: bytes = b""

    @property
    def ответ(self) -> bool:
        return self.начало.startswith("SIP/2.0")

    @property
    def код(self) -> int:
        части = self.начало.split(" ", 2)
        return int(части[1]) if self.ответ and len(части) > 1 and части[1].isdigit() else 0

    @property
    def причина(self) -> str:
        части = self.начало.split(" ", 2)
        return части[2] if self.ответ and len(части) > 2 else ""

    @property
    def метод(self) -> str:
        return "" if self.ответ else self.начало.split(" ", 1)[0].upper()

    def все(self, имя: str) -> list[str]:
        имя = имя.lower()
        return [з for к, з in self.заголовки if к == имя]

    def взять(self, имя: str, запасное: str = "") -> str:
        найдено = self.все(имя)
        return найдено[0] if найдено else запасное

    @property
    def cseq(self) -> tuple[int, str]:
        части = self.взять("cseq").split()
        if len(части) == 2 and части[0].isdigit():
            return int(части[0]), части[1].upper()
        return 0, ""


def разобрать(данные: bytes) -> Сообщение | None:
    """Датаграмма — в сообщение; мусор и пустые «keep-alive» — None."""
    голова, _, тело = данные.partition(b"\r\n\r\n")
    try:
        строки = голова.decode("utf-8", "replace").split("\r\n")
    except Exception:                                       # noqa: BLE001
        return None
    if not строки or not строки[0].strip() or "SIP/2.0" not in строки[0]:
        return None
    заголовки: list[tuple[str, str]] = []
    for строка in строки[1:]:
        if строка[:1] in (" ", "\t") and заголовки:         # продолжение заголовка
            имя, значение = заголовки[-1]
            заголовки[-1] = (имя, значение + " " + строка.strip())
            continue
        имя, знак, значение = строка.partition(":")
        if not знак:
            continue
        имя = имя.strip().lower()
        заголовки.append((_КРАТКИЕ.get(имя, имя), значение.strip()))
    return Сообщение(строки[0].strip(), заголовки, тело)


def собрать(начало: str, заголовки: list[tuple[str, str]], тело: bytes = b"") -> bytes:
    строки = [начало]
    строки += [f"{имя}: {значение}" for имя, значение in заголовки]
    строки.append(f"Content-Length: {len(тело)}")
    return ("\r\n".join(строки) + "\r\n\r\n").encode("utf-8") + тело


def _метка(значение: str) -> str:
    совпало = re.search(r";\s*tag=([^;>\s]+)", значение)
    return совпало.group(1) if совпало else ""


def _адрес_в_скобках(значение: str) -> str:
    совпало = re.search(r"<([^>]+)>", значение)
    return совпало.group(1) if совпало else значение.split(";", 1)[0].strip()


def разобрать_вызов(значение: str) -> dict[str, str]:
    """Параметры вызова аутентификации: `Digest realm="…", nonce="…"`."""
    _, _, хвост = значение.partition(" ")
    поля: dict[str, str] = {"scheme": значение.split(" ", 1)[0].lower()}
    for совпало in re.finditer(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))', хвост):
        поля[совпало.group(1).lower()] = (совпало.group(2) if совпало.group(2) is not None
                                          else совпало.group(3))
    return поля


def ответ_на_вызов(вызов: dict[str, str], *, метод: str, адрес: str,
                   пользователь: str, пароль: str, счётчик: int = 1) -> str:
    """Заголовок Authorization на вызов Digest (RFC 2617, 7616)."""
    алгоритм = (вызов.get("algorithm") or "MD5").upper()
    основа = "sha256" if алгоритм.startswith("SHA-256") else "md5"

    def х(текст: str) -> str:
        return hashlib.new(основа, текст.encode("utf-8")).hexdigest()

    realm, nonce = вызов.get("realm", ""), вызов.get("nonce", "")
    первая = х(f"{пользователь}:{realm}:{пароль}")
    вторая = х(f"{метод}:{адрес}")
    части = [f'Digest username="{пользователь}"', f'realm="{realm}"',
             f'nonce="{nonce}"', f'uri="{адрес}"']
    способы = [с.strip() for с in (вызов.get("qop") or "").split(",") if с.strip()]
    if "auth" in способы:
        cnonce = uuid.uuid4().hex[:16]
        nc = f"{счётчик:08x}"
        if алгоритм.endswith("-SESS"):
            первая = х(f"{первая}:{nonce}:{cnonce}")
        отклик = х(f"{первая}:{nonce}:{nc}:{cnonce}:auth:{вторая}")
        части += [f'response="{отклик}"', "qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        части.append(f'response="{х(f"{первая}:{nonce}:{вторая}")}"')
    части.append(f"algorithm={алгоритм}")
    if вызов.get("opaque"):
        части.append(f'opaque="{вызов["opaque"]}"')
    return ", ".join(части)


def _выбрать_вызов(сообщение: Сообщение) -> tuple[str, dict[str, str]] | None:
    """Вызов, на который умеем ответить; из нескольких — первый посильный."""
    for имя, ответный in (("www-authenticate", "Authorization"),
                          ("proxy-authenticate", "Proxy-Authorization")):
        for значение in сообщение.все(имя):
            вызов = разобрать_вызов(значение)
            алгоритм = (вызов.get("algorithm") or "MD5").upper()
            if вызов.get("scheme") == "digest" and алгоритм in (
                    "MD5", "MD5-SESS", "SHA-256", "SHA-256-SESS"):
                return ответный, вызов
    return None


def предложение_sdp(адрес: str, порт: int, *, кодек: str = "",
                    кнопки: int = 101, крипто: str = "") -> bytes:
    """SDP-предложение. `кодек` — ответ на повторное приглашение в идущем
    разговоре: только кодек, который уже идёт (RFC 3264, 6.1: ответ не
    расширяет набор), и тот номер событий кнопок, что выбрала станция.
    `крипто` — ключ SRTP строкой `a=crypto` (SDES): тогда профиль RTP/SAVP."""
    номер = random.randint(10_000_000, 99_999_999)
    кодеки = [кодек] if кодек in НОМЕР_КОДЕКА else ["PCMA", "PCMU"]
    профиль = "RTP/SAVP" if крипто else "RTP/AVP"
    строки = [
        "v=0", f"o=- {номер} {номер} IN IP4 {адрес}", "s=ASR Hub",
        f"c=IN IP4 {адрес}", "t=0 0",
        f"m=audio {порт} {профиль} {' '.join(str(НОМЕР_КОДЕКА[к]) for к in кодеки)} {кнопки}",
        *[f"a=rtpmap:{НОМЕР_КОДЕКА[к]} {к}/8000" for к in кодеки],
        f"a=rtpmap:{кнопки} telephone-event/8000", f"a=fmtp:{кнопки} 0-16",
        *([f"a=crypto:{крипто}"] if крипто else []),
        "a=ptime:20", "a=sendrecv",
    ]
    return ("\r\n".join(строки) + "\r\n").encode("ascii")


def разобрать_sdp(тело: bytes) -> dict[str, Any]:
    """Куда слать звук, каким кодеком и каким номером приходят кнопки."""
    адрес = ""
    порт = 0
    нагрузки: list[int] = []
    карта: dict[int, str] = {}
    профиль = ""
    крипто: list[шифр_звука.Ключ] = []
    в_звуке = False
    for строка in тело.decode("utf-8", "replace").splitlines():
        строка = строка.strip()
        if строка.startswith("m="):
            части = строка[2:].split()
            в_звуке = bool(части) and части[0] == "audio" and not порт
            if в_звуке and len(части) >= 4:
                порт = int(части[1]) if части[1].isdigit() else 0
                профиль = части[2].upper()
                нагрузки = [int(ч) for ч in части[3:] if ч.isdigit()]
        elif строка.startswith("a=crypto:") and в_звуке:
            ключ = шифр_звука.разобрать_крипто(строка[9:])
            if ключ is not None:
                крипто.append(ключ)
        elif строка.startswith("c=") and (в_звуке or not адрес):
            части = строка[2:].split()
            if len(части) >= 3:
                адрес = части[2].split("/")[0]
        elif строка.startswith("a=rtpmap:") and в_звуке:
            номер_строкой, _, имя = строка[9:].partition(" ")
            if номер_строкой.isdigit():
                карта[int(номер_строкой)] = имя.split("/")[0].upper()
    кодек = ""
    for номер in нагрузки:
        имя = карта.get(номер) or {0: "PCMU", 8: "PCMA"}.get(номер, "")
        if имя in НОМЕР_КОДЕКА:
            кодек = имя
            break
    кнопки = next((н for н in нагрузки if карта.get(н) == "TELEPHONE-EVENT"), 0)
    return {"адрес": адрес, "порт": порт, "кодек": кодек, "кнопки": кнопки,
            "профиль": профиль, "крипто": крипто}


def выбрать_ключ(ключи: list[шифр_звука.Ключ], *, тег: int | None = None
                 ) -> шифр_звука.Ключ | None:
    """Ключ SRTP станции: с нашим тегом (ответ на наше предложение) или
    первый посильный из её предложения."""
    for ключ in ключи:
        if тег is None or ключ.тег == тег:
            return ключ
    return None


def местный_адрес(узел: str, порт: int) -> str:
    """Адрес, с которого этот сервер виден станции."""
    пробный = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        пробный.connect((узел, порт or 5060))
        return пробный.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        пробный.close()


# ---------------------------------------------------------------------------
# Разговор: звук и кнопки
# ---------------------------------------------------------------------------

class Разговор:
    """Установленный вызов: проиграть фразу, дождаться кнопки, положить трубку."""

    def __init__(self, телефон: Телефон, call_id: str, сокет: socket.socket) -> None:
        self.телефон = телефон
        self.call_id = call_id
        self._сокет = сокет
        self.кодек = "PCMA"
        self.номер_кнопок = 101
        self.куда: tuple[str, int] | None = None
        self.кнопки: queue.Queue[str] = queue.Queue()
        self.завершён = threading.Event()
        self.кем_завершён = ""
        self.начат = 0.0
        # Поля диалога SIP — заполняет телефон после ответа 200.
        self.цель = ""
        self.маршрут: list[str] = []
        self.от = ""
        self.кому = ""
        self.cseq = 1
        self._ssrc = random.getrandbits(32)
        self._номер = random.randint(0, 0xFFFF)
        self._метка_времени = random.getrandbits(32)
        self._первый = True
        self._искатель: ИскательТонов | None = None
        self._событие = (-1, -1)            # (метка времени RTP, код кнопки)
        #: Последняя принятая кнопка: (кнопка, когда, каким способом).
        self._последняя: tuple[str, float, str] = ("", 0.0, "")
        self._поток: threading.Thread | None = None
        #: Заголовок учётных данных, с которым станция приняла INVITE.
        self.учётные: tuple[str, str] | None = None
        self.получено = 0
        self.отправлено = 0
        #: Звук разговора, PCM 16 бит 8 кГц, по времени от начала: клиент и
        #: сервер раздельно. Нужен разбору автоответчика, голосовым ответам
        #: и записи опроса; копится, только если `записывать`.
        self.записывать = False
        self._звук_клиента = bytearray()
        self._звук_сервера = bytearray()
        self._ноль = 0.0
        #: SRTP: свой ключ (из нашего SDP) шифрует передачу, ключ станции —
        #: приём. None — звук открытый.
        self.ключ_srtp: шифр_звука.Ключ | None = None
        self.srtp_отправка: шифр_звука.SRTP | None = None
        self.srtp_приём: шифр_звука.SRTP | None = None

    @property
    def шифрован(self) -> bool:
        return self.srtp_отправка is not None and self.srtp_приём is not None

    def включить_srtp(self, свой: шифр_звука.Ключ, их: шифр_звука.Ключ) -> None:
        """Ключи согласованы: свой — на передачу, ключ станции — на приём.

        Набор (длина подписи) — тот, что выбрала станция; передача с тем же
        мастер-ключом, что был в нашем SDP.
        """
        свой = шифр_звука.Ключ(их.тег, их.набор, свой.мастер)
        if self.srtp_отправка is None or self.srtp_отправка.ключ != свой:
            self.srtp_отправка = шифр_звука.SRTP(свой)
        if self.srtp_приём is None or self.srtp_приём.ключ != их:
            self.srtp_приём = шифр_звука.SRTP(их)
        self.ключ_srtp = свой

    # -- приём -------------------------------------------------------------

    def слушать(self) -> None:
        self._искатель = ИскательТонов(self.кодек)
        self._поток = threading.Thread(target=self._приём, name="asrhub-qc-rtp", daemon=True)
        self._поток.start()

    def _приём(self) -> None:
        self._сокет.settimeout(0.5)
        while not self.завершён.is_set():
            try:
                данные, откуда = self._сокет.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(данные) < 12 or данные[0] >> 6 != 2:
                continue
            if self.srtp_приём is not None:
                # Чужой или испорченный пакет подпись не проходит и не
                # слушается вовсе: ни как звук, ни как кнопка.
                раскрыто = self.srtp_приём.раскрыть(данные)
                if раскрыто is None:
                    continue
                данные = раскрыто
            # Симметричный RTP: за NAT станция шлёт звук не с того адреса,
            # что назвала в SDP. Отвечаем туда, откуда он пришёл на деле.
            if self.куда is None or (self.получено == 0 and откуда != self.куда):
                self.куда = откуда
            self.получено += 1
            вид = данные[1] & 0x7F
            начало = 12 + 4 * (данные[0] & 0x0F)
            if данные[0] & 0x10 and len(данные) >= начало + 4:   # расширение заголовка
                начало += 4 + 4 * struct.unpack("!H", данные[начало + 2:начало + 4])[0]
            нагрузка = данные[начало:]
            if вид == self.номер_кнопок and len(нагрузка) >= 4:
                метка = struct.unpack("!I", данные[4:8])[0]
                код = нагрузка[0]
                # Одно нажатие — много пакетов с одной меткой времени.
                if (метка, код) != self._событие and код < len(КНОПКИ):
                    self._событие = (метка, код)
                    self._кнопка(КНОПКИ[код], "событие")
            elif вид == НОМЕР_КОДЕКА.get(self.кодек) and self._искатель is not None:
                кнопка = self._искатель.подать(нагрузка)
                if кнопка:
                    self._кнопка(кнопка, "тон")
                if self.записывать:
                    self._уложить(self._звук_клиента, нагрузка)

    def _кнопка(self, кнопка: str, способ: str) -> None:
        """Нажатие — в очередь, если это не то же нажатие другим способом.

        Станция может прислать одну кнопку дважды: событием RFC 4733 и тоном
        в звуке (Asterisk глушит тон после распознавания, но первые 40–60 мс
        проходят), или INFO и тоном. Та же кнопка другим способом в пределах
        секунды — то же нажатие. Раньше гасился только тон после события, а
        тон, распознанный раньше события, давал двойное нажатие — и лишняя
        цифра становилась ответом на следующий вопрос.
        """
        сейчас = time.monotonic()
        прошлая, когда, каким = self._последняя
        if кнопка == прошлая and способ != каким and сейчас - когда < 1.0:
            # То же нажатие вторым способом. Запоминаем способ: следующее
            # событие с новой меткой — уже новое нажатие, а не эхо тона.
            self._последняя = (кнопка, когда, способ)
            return
        self._последняя = (кнопка, сейчас, способ)
        self.кнопки.put(кнопка)

    def кнопка_из_info(self, кнопка: str) -> None:
        self._кнопка(кнопка, "info")

    # -- передача ----------------------------------------------------------

    # -- звук разговора ------------------------------------------------------

    #: Предел копимого звука: 15 минут на сторону.
    ПРЕДЕЛ_ЗВУКА = 15 * 60 * 8000 * 2

    def начать_запись(self) -> None:
        self._ноль = time.monotonic()
        self._звук_клиента.clear()
        self._звук_сервера.clear()
        self.записывать = True

    def _уложить(self, куда: bytearray, нагрузка: bytes) -> None:
        """Кадр G.711 → PCM на своё место по времени (пропуски — тишина)."""
        таблица = РАСКОДИРОВАТЬ.get(self.кодек)
        if таблица is None or not self._ноль:
            return
        место = int((time.monotonic() - self._ноль) * 8000) * 2
        if место > self.ПРЕДЕЛ_ЗВУКА:
            return
        if len(куда) < место:
            куда.extend(bytes(место - len(куда)))
        куда.extend(struct.pack(f"<{len(нагрузка)}h", *(таблица[б] for б in нагрузка)))

    def звук_клиента(self, *, с: float = 0.0) -> bytes:
        """PCM клиента от момента `с` (секунды от начала записи)."""
        return bytes(self._звук_клиента[int(max(0.0, с) * 8000) * 2:])

    def сейчас_с(self) -> float:
        return time.monotonic() - self._ноль if self._ноль else 0.0

    def запись_стерео(self) -> bytes:
        """Обе стороны: слева клиент, справа сервер — PCM 16 бит 8 кГц."""
        длина = max(len(self._звук_клиента), len(self._звук_сервера)) // 2
        клиент = self._звук_клиента + bytes(длина * 2 - len(self._звук_клиента))
        сервер = self._звук_сервера + bytes(длина * 2 - len(self._звук_сервера))
        вместе = bytearray(длина * 4)
        вместе[0::4], вместе[1::4] = клиент[0::2], клиент[1::2]
        вместе[2::4], вместе[3::4] = сервер[0::2], сервер[1::2]
        return bytes(вместе)

    def _кадр(self, нагрузка: bytes) -> None:
        if self.записывать:
            self._уложить(self._звук_сервера, нагрузка)
        if self.куда is None:
            return
        заголовок = struct.pack("!BBHII", 0x80,
                                НОМЕР_КОДЕКА[self.кодек] | (0x80 if self._первый else 0),
                                self._номер, self._метка_времени, self._ssrc)
        self._первый = False
        self._номер = (self._номер + 1) & 0xFFFF
        self._метка_времени = (self._метка_времени + КАДР) & 0xFFFFFFFF
        пакет = заголовок + нагрузка
        if self.srtp_отправка is not None:
            пакет = self.srtp_отправка.защитить(пакет)
        try:
            self._сокет.sendto(пакет, self.куда)
            self.отправлено += 1
        except OSError:
            pass

    def сбросить_кнопки(self) -> None:
        while True:
            try:
                self.кнопки.get_nowait()
            except queue.Empty:
                return

    def проиграть(self, звук: bytes, *, прерывать: bool = True) -> str:
        """Звук в кодеке разговора; ответ — кнопка, если ею перебили, иначе ''.

        Кадры уходят по часам (20 мс), а не «сколько успел»: иначе фраза
        звучит быстрее или рвётся, а станция копит и выбрасывает пакеты.
        """
        тишина = ТИШИНА[self.кодек]
        срок = time.monotonic()
        for начало in range(0, len(звук), КАДР):
            if self.завершён.is_set():
                return ""
            if прерывать:
                try:
                    return self.кнопки.get_nowait()
                except queue.Empty:
                    pass
            кадр = звук[начало:начало + КАДР]
            if len(кадр) < КАДР:
                кадр += тишина * (КАДР - len(кадр))
            self._кадр(кадр)
            срок += ШАГ_С
            пауза = срок - time.monotonic()
            if пауза > 0:
                time.sleep(пауза)
            elif пауза < -0.5:                  # сильно отстали — не догоняем залпом
                срок = time.monotonic()
        return ""

    def ждать_кнопку(self, срок_с: float) -> str:
        """Ждёт нажатия, передавая тишину; '' — не дождались или трубку положили."""
        тишина = ТИШИНА[self.кодек] * КАДР
        конец = time.monotonic() + max(0.0, срок_с)
        срок = time.monotonic()
        while time.monotonic() < конец and not self.завершён.is_set():
            try:
                return self.кнопки.get_nowait()
            except queue.Empty:
                pass
            self._кадр(тишина)
            срок += ШАГ_С
            пауза = срок - time.monotonic()
            if пауза > 0:
                time.sleep(пауза)
            elif пауза < -0.5:
                срок = time.monotonic()
        try:
            return self.кнопки.get_nowait()
        except queue.Empty:
            return ""

    def молчать(self, срок_с: float) -> None:
        """Пауза с передачей тишины; кнопки при этом не теряются."""
        тишина = ТИШИНА[self.кодек] * КАДР
        срок = time.monotonic()
        конец = срок + max(0.0, срок_с)
        while срок < конец and not self.завершён.is_set():
            self._кадр(тишина)
            срок += ШАГ_С
            пауза = срок - time.monotonic()
            if пауза > 0:
                time.sleep(пауза)

    @property
    def жив(self) -> bool:
        return not self.завершён.is_set()

    def положить(self) -> None:
        """Кладёт трубку: BYE, если разговор ещё идёт, и закрывает звук."""
        if not self.завершён.is_set():
            self.кем_завершён = self.кем_завершён or "сервер"
            self.завершён.set()
            try:
                self.телефон._bye(self)
            except Exception as exc:                        # noqa: BLE001
                log.debug("BYE не ушёл: %s", exc)
        self.телефон._забыть(self)
        try:
            self._сокет.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Телефон
# ---------------------------------------------------------------------------

class Телефон:
    """Аппарат SIP: один внутренний номер станции, с которого звонит сервер."""

    АГЕНТ = "ASRHub-QC"

    def __init__(self, узел: str, порт: int = 5060, *, пользователь: str, пароль: str = "",
                 имя: str = "", местный: str = "", срок_регистрации: int = 300,
                 транспорт: str = "udp", проверять_сертификат: bool = True,
                 файл_ца: str = "", srtp: bool = False) -> None:
        self.узел = str(узел).strip()
        self.транспорт = str(транспорт or "udp").strip().lower()
        if self.транспорт not in ТРАНСПОРТЫ:
            raise ОшибкаSIP(f"Транспорт SIP «{транспорт}» не поддерживается: udp, tcp или tls.")
        # У TLS свой порт (5061); 5060 по умолчанию для TLS — почти всегда
        # недосмотр, а не настройка станции.
        self.порт = int(порт or 5060)
        if self.транспорт == "tls" and self.порт == 5060:
            self.порт = 5061
        self.пользователь = str(пользователь).strip()
        self.пароль = пароль
        self.имя = имя
        self.срок_регистрации = срок_регистрации
        #: Шифровать звук (SRTP, ключи в SDP): вызов без согласия станции на
        #: шифрование не состоится. Входящее шифрованное предложение
        #: принимается и без этого флага.
        self.srtp = bool(srtp)
        self._проверять = bool(проверять_сертификат)
        self._файл_ца = str(файл_ца or "").strip()
        try:
            self._ip_станции = socket.gethostbyname(self.узел)
        except OSError as exc:
            raise ОшибкаSIP(f"Адрес станции «{self.узел}» не найден: {exc}") from exc
        self._местный_задан = местный.strip()
        self.местный = self._местный_задан or местный_адрес(self._ip_станции, self.порт)
        self._сокет: socket.socket | None = None
        #: TCP/TLS: одно соединение со станцией — по нему же её запросы к нам.
        self._соединение: socket.socket | None = None
        self._замок_связи = threading.Lock()
        self._есть_связь = threading.Event()
        self.местный_порт = 0
        if self.надёжный:
            self._соединиться()
        else:
            self._сокет = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self._сокет.bind(("0.0.0.0", 0))            # noqa: S104 — станция отвечает извне
            except OSError as exc:
                self._сокет.close()
                raise ОшибкаSIP(f"Не удалось открыть порт для SIP: {exc}") from exc
            self.местный_порт = self._сокет.getsockname()[1]
        self._ответы: dict[tuple[str, int], queue.Queue[Сообщение]] = {}
        self._разговоры: dict[str, Разговор] = {}
        #: Наши 200 на повторные приглашения, ждущие ACK: (call-id, CSeq) → когда.
        self._ждут_ack: dict[tuple[str, int], float] = {}
        #: Входящие вызовы (опрос без обратного звонка): обработчик разговора
        #: `(разговор, INVITE)`; None — на INVITE телефон отвечает «занято».
        self.входящие: Any = None
        #: Вызовы, от которых отказались до ответа (не дождались, отменили):
        #: call-id → когда. Поздний 200 на такой вызов — ACK и сразу BYE.
        self._брошенные: dict[str, float] = {}
        self._замок = threading.Lock()
        self._закрыт = threading.Event()
        self._call_id_рег = uuid.uuid4().hex
        self._cseq_рег = 0
        self._метка_рег = uuid.uuid4().hex[:10]
        self.зарегистрирован_до = 0.0
        self.последняя_ошибка = ""
        self._поток = threading.Thread(target=self._чтение, name="asrhub-qc-sip", daemon=True)
        self._поток.start()

    # -- низкий уровень ----------------------------------------------------

    @property
    def надёжный(self) -> bool:
        """TCP или TLS: запросы не повторяются, ответы идут тем же соединением."""
        return self.транспорт != "udp"

    def _via(self, ветка: str = "") -> str:
        return (f"SIP/2.0/{self.транспорт.upper()} {self.местный}:{self.местный_порт};"
                f"branch={ветка or 'z9hG4bK' + uuid.uuid4().hex[:16]};rport")

    @property
    def _параметр_транспорта(self) -> str:
        return f";transport={self.транспорт}" if self.надёжный else ""

    @property
    def _контакт(self) -> str:
        return (f"<sip:{self.пользователь}@{self.местный}:{self.местный_порт}"
                f"{self._параметр_транспорта}>")

    def адрес_станции(self, кто: str = "") -> str:
        """URI запроса к станции: `sip:[кто@]узел[:порт][;transport=…]`."""
        порт = "" if (self.порт == 5060 and not self.надёжный) else f":{self.порт}"
        return f"sip:{кто + '@' if кто else ''}{self.узел}{порт}{self._параметр_транспорта}"

    def _контекст_tls(self) -> ssl.SSLContext:
        контекст = ssl.create_default_context(cafile=self._файл_ца or None)
        if not self._проверять:
            контекст.check_hostname = False
            контекст.verify_mode = ssl.CERT_NONE
        return контекст

    def _соединиться(self) -> None:
        """Соединение TCP/TLS со станцией (заново — после обрыва)."""
        try:
            сырое = socket.create_connection((self._ip_станции, self.порт), timeout=10.0)
        except OSError as exc:
            raise ОшибкаSIP(
                f"Станция {self.узел}:{self.порт} не принимает SIP по "
                f"{self.транспорт.upper()}: {exc}. Проверьте порт и транспорт на станции "
                "(у TLS обычно 5061).") from exc
        соединение: socket.socket
        if self.транспорт == "tls":
            try:
                соединение = self._контекст_tls().wrap_socket(сырое, server_hostname=self.узел)
            except (ssl.SSLError, OSError) as exc:
                сырое.close()
                совет = ("сертификат станции не подтверждён: укажите файл удостоверяющего "
                         "центра (qc_sip_ca_file) или, только для проверки, выключите "
                         "проверку (qc_sip_tls_verify)"
                         if isinstance(exc, ssl.SSLCertVerificationError)
                         else "проверьте, что на этом порту станция ждёт TLS")
                raise ОшибкаSIP(f"TLS со станцией {self.узел}:{self.порт} не установлен: "
                                f"{exc}; {совет}.") from exc
        else:
            соединение = сырое
        соединение.settimeout(0.2)
        старое = self._соединение
        self._соединение = соединение
        self.местный_порт = соединение.getsockname()[1]
        if not self._местный_задан:
            self.местный = соединение.getsockname()[0]
        self._есть_связь.set()
        if старое is not None:
            try:
                старое.close()
            except OSError:
                pass

    @property
    def _от(self) -> str:
        имя = self.имя.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")
        return (f'"{имя}" ' if имя else "") + f"<sip:{self.пользователь}@{self.узел}>"

    def _послать(self, данные: bytes) -> None:
        if not self.надёжный:
            assert self._сокет is not None
            try:
                self._сокет.sendto(данные, (self._ip_станции, self.порт))
            except OSError as exc:
                raise ОшибкаSIP(f"Станция {self.узел}:{self.порт} недоступна: {exc}") from exc
            return
        with self._замок_связи:
            for попытка in (1, 2):
                try:
                    if self._соединение is None:
                        self._соединиться()
                    assert self._соединение is not None
                    self._соединение.sendall(данные)
                    return
                except (OSError, ОшибкаSIP) as exc:
                    # Станция закрыла простаивавшее соединение — одно новое.
                    if self._соединение is not None:
                        try:
                            self._соединение.close()
                        except OSError:
                            pass
                    self._соединение = None
                    self._есть_связь.clear()
                    if попытка == 2 or self._закрыт.is_set():
                        if isinstance(exc, ОшибкаSIP):
                            raise
                        raise ОшибкаSIP(
                            f"Станция {self.узел}:{self.порт} недоступна: {exc}") from exc

    def _чтение(self) -> None:
        if self.надёжный:
            self._чтение_потока()
            return
        assert self._сокет is not None
        self._сокет.settimeout(0.5)
        while not self._закрыт.is_set():
            try:
                данные, откуда = self._сокет.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            self._входящее(данные, откуда)

    def _чтение_потока(self) -> None:
        """TCP/TLS: сообщения из потока байт — по Content-Length (RFC 3261, 18.3)."""
        буфер = b""
        текущее: socket.socket | None = None
        while not self._закрыт.is_set():
            if not self._есть_связь.wait(0.5):
                continue
            соединение = self._соединение
            if соединение is None:
                self._есть_связь.clear()
                continue
            if соединение is not текущее:
                текущее, буфер = соединение, b""
            try:
                # Чтение и запись одного соединения TLS из разных потоков
                # OpenSSL не допускает: ждём данных без замка, читаем — под
                # тем же замком, что и пишем.
                if not (isinstance(соединение, ssl.SSLSocket) and соединение.pending()):
                    готово, _, _ = select.select([соединение], [], [], 0.5)
                    if not готово:
                        continue
                with self._замок_связи:
                    if self._соединение is not соединение:
                        continue
                    кусок = соединение.recv(65535)
            except (TimeoutError, ssl.SSLWantReadError):
                continue
            except (OSError, ValueError):
                кусок = b""
            if not кусок:
                # Станция закрыла соединение: следующий запрос откроет новое.
                with self._замок_связи:
                    if self._соединение is соединение:
                        self._соединение = None
                        self._есть_связь.clear()
                try:
                    соединение.close()
                except OSError:
                    pass
                continue
            буфер += кусок
            while True:
                буфер = буфер.lstrip(b"\r\n")      # проверки связи — пустые строки
                голова, найдено, хвост = буфер.partition(b"\r\n\r\n")
                if not найдено:
                    if len(буфер) > ПРЕДЕЛ_СООБЩЕНИЯ:
                        буфер = b""
                    break
                совпало = re.search(rb"(?im)^(?:content-length|l)\s*:\s*(\d+)", голова)
                длина = int(совпало.group(1)) if совпало else 0
                if длина > ПРЕДЕЛ_СООБЩЕНИЯ:
                    буфер = b""
                    break
                if len(хвост) < длина:
                    break
                данные = голова + b"\r\n\r\n" + хвост[:длина]
                буфер = хвост[длина:]
                self._входящее(данные, (self._ip_станции, self.порт))

    def _входящее(self, данные: bytes, откуда: tuple[str, int]) -> None:
        сообщение = разобрать(данные)
        if сообщение is None:
            return
        try:
            if сообщение.ответ:
                номер, _ = сообщение.cseq
                with self._замок:
                    очередь = self._ответы.get((сообщение.взять("call-id"), номер))
                    разговор = self._разговоры.get(сообщение.взять("call-id"))
                    брошен = сообщение.взять("call-id") in self._брошенные
                if очередь is not None:
                    очередь.put(сообщение)
                elif (разговор is not None and разговор.цель
                      and сообщение.cseq[1] == "INVITE" and 200 <= сообщение.код < 300):
                    # Станция повторяет 200: наш ACK потерялся — шлём ещё раз,
                    # иначе она положит трубку через 32 секунды.
                    self._ack(разговор)
                elif (брошен and сообщение.cseq[1] == "INVITE"
                      and 200 <= сообщение.код < 300):
                    self._положить_брошенный(сообщение)
            else:
                self._запрос(сообщение, откуда)
        except Exception as exc:                        # noqa: BLE001
            log.debug("Сообщение SIP не обработано: %s", exc)

    def _ответить(self, запрос: Сообщение, откуда: tuple[str, int], код: int,
                  причина: str, *, тело: bytes = b"",
                  ещё: list[tuple[str, str]] | None = None, метка_кому: str = "") -> None:
        заголовки = [("Via", з) for з in запрос.все("via")]
        кому = запрос.взять("to")
        # Своя метка диалога — одна на все ответы того же вызова (повторы 200
        # на входящий INVITE), иначе станция видит каждый раз новый диалог.
        if not _метка(кому):
            кому += f";tag={метка_кому or uuid.uuid4().hex[:10]}"
        заголовки += [("From", запрос.взять("from")), ("To", кому),
                      ("Call-ID", запрос.взять("call-id")), ("CSeq", запрос.взять("cseq")),
                      ("User-Agent", self.АГЕНТ)]
        заголовки += ещё or []
        данные = собрать(f"SIP/2.0 {код} {причина}", заголовки, тело)
        try:
            if self.надёжный:
                self._послать(данные)
            else:
                assert self._сокет is not None
                self._сокет.sendto(данные, откуда)
        except (OSError, ОшибкаSIP):
            pass

    def _запрос(self, запрос: Сообщение, откуда: tuple[str, int]) -> None:
        """Запросы станции: конец разговора, кнопки в INFO, проверки связи."""
        метод = запрос.метод
        with self._замок:
            разговор = self._разговоры.get(запрос.взять("call-id"))
        if метод == "ACK":
            # ACK на наш 200 к повторному приглашению: повторять ответ хватит.
            with self._замок:
                self._ждут_ack.pop((запрос.взять("call-id"), запрос.cseq[0]), None)
            return
        if метод == "BYE":
            self._ответить(запрос, откуда, 200, "OK")
            if разговор is not None and not разговор.завершён.is_set():
                разговор.кем_завершён = "абонент"
                разговор.завершён.set()
            return
        if метод == "INFO":
            self._ответить(запрос, откуда, 200, "OK")
            if разговор is not None:
                кнопка = _кнопка_из_info(запрос)
                if кнопка:
                    разговор.кнопка_из_info(кнопка)
            return
        if метод in ("OPTIONS", "NOTIFY", "UPDATE", "MESSAGE"):
            self._ответить(запрос, откуда, 200, "OK", ещё=[
                ("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, INFO, NOTIFY, UPDATE")])
            return
        if метод == "INVITE":
            if разговор is not None and разговор.куда is not None:
                # Повторное приглашение в идущем разговоре (обновление сессии,
                # смена адреса звука): подтверждаем тем же предложением.
                описание = разобрать_sdp(запрос.тело)
                if разговор.ключ_srtp is not None and запрос.тело:
                    # Шифрованный разговор шифрованным и остаётся: новое
                    # предложение без ключа не принимается, новый ключ
                    # станции — принимается.
                    их = выбрать_ключ(описание["крипто"])
                    if их is None:
                        self._ответить(запрос, откуда, 488, "Not Acceptable Here")
                        return
                    разговор.включить_srtp(разговор.ключ_srtp, их)
                if описание["адрес"] and описание["порт"]:
                    разговор.куда = (описание["адрес"], описание["порт"])
                порт = разговор._сокет.getsockname()[1]
                ответ_200: dict[str, Any] = {
                    "тело": предложение_sdp(self.местный, порт, кодек=разговор.кодек,
                                            кнопки=разговор.номер_кнопок,
                                            крипто=разговор.ключ_srtp.строка
                                            if разговор.ключ_srtp else ""),
                    "ещё": [("Contact", self._контакт), ("Content-Type", "application/sdp")]}
                self._ответить(запрос, откуда, 200, "OK", **ответ_200)
                # 200 на INVITE повторяется, пока не придёт ACK (RFC 3261,
                # 13.3.1.4): потерянный ответ иначе роняет разговор на строгих
                # станциях — они ждут 32 с и кладут трубку.
                self._повторять_200(запрос, откуда, ответ_200)
            elif разговор is None and self.входящие is not None:
                self._принять_входящий(запрос, откуда)
            else:
                # Входящие выключены (qc_inbound) — «занято».
                self._ответить(запрос, откуда, 486, "Busy Here")
            return
        if метод == "CANCEL":
            self._ответить(запрос, откуда, 200, "OK")
            return
        self._ответить(запрос, откуда, 405, "Method Not Allowed")

    def _принять_входящий(self, запрос: Сообщение, откуда: tuple[str, int]) -> None:
        """Входящий вызов: снять трубку и отдать разговор обработчику.

        Станция переводит клиента на телефон сервера после разговора с
        оператором — опрос без обратного звонка. Ответ — только кодеком из
        предложения станции (G.711), повторы 200 — до ACK; повторный INVITE
        того же вызова получает тот же ответ.
        """
        call_id = запрос.взять("call-id")
        описание = разобрать_sdp(запрос.тело)
        if not описание["кодек"] or not описание["порт"]:
            self._ответить(запрос, откуда, 488, "Not Acceptable Here")
            return
        их = выбрать_ключ(описание["крипто"])
        if (их is None and (self.srtp or "SAVP" in описание["профиль"])):
            # Нужно шифрование, а ключа нет (или он в непонятном виде).
            self._ответить(запрос, откуда, 488, "Not Acceptable Here")
            return
        звук = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        звук.bind(("0.0.0.0", 0))                           # noqa: S104
        порт = звук.getsockname()[1]
        разговор = Разговор(self, call_id, звук)
        метка = uuid.uuid4().hex[:10]
        разговор.от = f"{запрос.взять('to')};tag={метка}"
        разговор.кому = запрос.взять("from")
        контакт = запрос.взять("contact")
        разговор.цель = _адрес_в_скобках(контакт) if контакт else f"sip:{self.узел}"
        # У принимающей стороны маршрут — в порядке Record-Route как есть.
        разговор.маршрут = list(запрос.все("record-route"))
        разговор.cseq = 0
        разговор.кодек = описание["кодек"]
        разговор.номер_кнопок = описание["кнопки"] or 101
        разговор.куда = (описание["адрес"] or откуда[0], описание["порт"])
        if их is not None:
            разговор.включить_srtp(шифр_звука.новый_ключ(их.тег, их.набор), их)
        with self._замок:
            self._разговоры[call_id] = разговор
        ответ: dict[str, Any] = {
            "тело": предложение_sdp(self.местный, порт, кодек=разговор.кодек,
                                    кнопки=разговор.номер_кнопок,
                                    крипто=разговор.ключ_srtp.строка
                                    if разговор.ключ_srtp else ""),
            "ещё": [("Contact", self._контакт), ("Content-Type", "application/sdp")],
            "метка_кому": метка}
        self._ответить(запрос, откуда, 100, "Trying")
        self._ответить(запрос, откуда, 200, "OK", **ответ)
        self._повторять_200(запрос, откуда, ответ)
        разговор.начат = time.time()
        разговор.слушать()
        обработчик = self.входящие
        threading.Thread(target=обработчик, args=(разговор, запрос), daemon=True,
                         name="asrhub-qc-inbound").start()

    def _повторять_200(self, запрос: Сообщение, откуда: tuple[str, int],
                       ответ: dict[str, Any]) -> None:
        ключ = (запрос.взять("call-id"), запрос.cseq[0])
        with self._замок:
            self._ждут_ack[ключ] = time.monotonic()

        def повторы() -> None:
            пауза = T1
            конец = time.monotonic() + 64 * T1
            while time.monotonic() < конец:
                time.sleep(пауза)
                with self._замок:
                    if ключ not in self._ждут_ack:
                        return
                self._ответить(запрос, откуда, 200, "OK", **ответ)
                пауза = min(пауза * 2, 4.0)
            with self._замок:
                self._ждут_ack.pop(ключ, None)

        threading.Thread(target=повторы, name="asrhub-qc-200", daemon=True).start()

    def _обмен(self, собрать_запрос: Any, call_id: str, номер: int, *,
               срок_с: float = СРОК_ЗАПРОСА_С, окончательный: bool = True) -> Сообщение:
        """Шлёт запрос с повторами и ждёт ответ (по умолчанию — окончательный)."""
        очередь: queue.Queue[Сообщение] = queue.Queue()
        with self._замок:
            self._ответы[(call_id, номер)] = очередь
        try:
            данные = собрать_запрос()
            self._послать(данные)
            начало = time.monotonic()
            повтор = T1
            следующий = начало + повтор
            while True:
                осталось = срок_с - (time.monotonic() - начало)
                if осталось <= 0:
                    raise ОшибкаSIP(f"Станция {self.узел}:{self.порт} не отвечает на запросы SIP.")
                try:
                    ответ = очередь.get(timeout=min(осталось, max(0.05, следующий - time.monotonic())))
                except queue.Empty:
                    if time.monotonic() >= следующий and повтор < 8 and not self.надёжный:
                        self._послать(данные)
                        повтор *= 2
                        следующий = time.monotonic() + повтор
                    continue
                if ответ.код >= 200 or not окончательный:
                    return ответ
                следующий = float("inf")        # предварительный ответ: повторы не нужны
        finally:
            with self._замок:
                self._ответы.pop((call_id, номер), None)

    # -- регистрация -------------------------------------------------------

    def зарегистрировать(self, срок: int | None = None) -> int:
        """REGISTER; ответ — на сколько секунд станция приняла регистрацию."""
        срок = self.срок_регистрации if срок is None else срок
        адрес = self.адрес_станции() if self.надёжный else f"sip:{self.узел}"
        учётные: tuple[str, str] | None = None
        for _ in range(3):
            self._cseq_рег += 1
            номер = self._cseq_рег

            def запрос(номер: int = номер, учётные: tuple[str, str] | None = учётные) -> bytes:
                заголовки = [
                    ("Via", self._via()), ("Max-Forwards", "70"),
                    ("From", f"{self._от};tag={self._метка_рег}"),
                    ("To", f"<sip:{self.пользователь}@{self.узел}>"),
                    ("Call-ID", self._call_id_рег), ("CSeq", f"{номер} REGISTER"),
                    ("Contact", f"{self._контакт};expires={срок}"),
                    ("Expires", str(срок)), ("User-Agent", self.АГЕНТ),
                ]
                if учётные:
                    заголовки.append(учётные)
                return собрать(f"REGISTER {адрес} SIP/2.0", заголовки)

            ответ = self._обмен(запрос, self._call_id_рег, номер, срок_с=8.0)
            if ответ.код in (401, 407):
                вызов = _выбрать_вызов(ответ)
                if вызов is None or учётные is not None:
                    self.последняя_ошибка = "станция не приняла логин или пароль"
                    raise ОшибкаSIP("Станция не приняла логин или пароль телефона.", ответ.код)
                учётные = (вызов[0], ответ_на_вызов(
                    вызов[1], метод="REGISTER", адрес=адрес,
                    пользователь=self.пользователь, пароль=self.пароль))
                continue
            if 200 <= ответ.код < 300:
                принято = срок
                for контакт in ответ.все("contact"):
                    совпало = re.search(r"expires=(\d+)", контакт)
                    if совпало:
                        принято = int(совпало.group(1))
                        break
                else:
                    if ответ.взять("expires").isdigit():
                        принято = int(ответ.взять("expires"))
                self.зарегистрирован_до = time.time() + принято if срок else 0.0
                self.последняя_ошибка = ""
                return принято
            self.последняя_ошибка = f"{ответ.код} {ответ.причина}"
            raise ОшибкаSIP(f"Станция отказала в регистрации: {ответ.код} {ответ.причина}.",
                            ответ.код)
        raise ОшибкаSIP("Регистрация не удалась: станция повторяет запрос пароля.", 401)

    # -- вызов -------------------------------------------------------------

    def позвонить(self, номер: str, *, ждать_ответа_с: float = 30.0) -> Разговор:
        """Набирает номер; ответ — разговор, когда абонент снял трубку."""
        номер = re.sub(r"[^0-9A-Za-z+*#_.\-]", "", str(номер))
        if not номер:
            raise ОшибкаSIP("Не задан номер для вызова.")
        звук = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        звук.bind(("0.0.0.0", 0))                           # noqa: S104
        порт_звука = звук.getsockname()[1]
        call_id = f"{uuid.uuid4().hex}@{self.местный}"
        разговор = Разговор(self, call_id, звук)
        адрес = self.адрес_станции(номер)
        метка = uuid.uuid4().hex[:10]
        от = f"{self._от};tag={метка}"
        кому = f"<sip:{номер}@{self.узел}>"
        if self.srtp:
            разговор.ключ_srtp = шифр_звука.новый_ключ()
        тело = предложение_sdp(self.местный, порт_звука, крипто=разговор.ключ_srtp.строка
                               if разговор.ключ_srtp else "")
        with self._замок:
            self._разговоры[call_id] = разговор
        cseq = 0
        учётные: tuple[str, str] | None = None
        try:
            for _ in range(3):
                cseq += 1
                ветка = "z9hG4bK" + uuid.uuid4().hex[:16]

                def запрос(cseq: int = cseq, ветка: str = ветка,
                           учётные: tuple[str, str] | None = учётные) -> bytes:
                    заголовки = [
                        ("Via", self._via(ветка)), ("Max-Forwards", "70"),
                        ("From", от), ("To", кому), ("Call-ID", call_id),
                        ("CSeq", f"{cseq} INVITE"), ("Contact", self._контакт),
                        ("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, INFO, NOTIFY, UPDATE"),
                        ("User-Agent", self.АГЕНТ), ("Content-Type", "application/sdp"),
                    ]
                    if учётные:
                        заголовки.append(учётные)
                    return собрать(f"INVITE {адрес} SIP/2.0", заголовки, тело)

                ответ = self._вызов(запрос, call_id, cseq, ветка, адрес, от, кому,
                                    ждать_ответа_с)
                if ответ.код in (401, 407):
                    self._ack_отказа(адрес, ветка, от, ответ, call_id, cseq)
                    вызов = _выбрать_вызов(ответ)
                    if вызов is None or учётные is not None:
                        raise ОшибкаSIP("Станция не приняла логин или пароль телефона.",
                                        ответ.код)
                    учётные = (вызов[0], ответ_на_вызов(
                        вызов[1], метод="INVITE", адрес=адрес,
                        пользователь=self.пользователь, пароль=self.пароль))
                    continue
                if 200 <= ответ.код < 300:
                    разговор.учётные = учётные
                    self._установить(разговор, ответ, от, cseq)
                    return разговор
                self._ack_отказа(адрес, ветка, от, ответ, call_id, cseq)
                raise _ошибка_вызова(ответ)
            raise ОшибкаSIP("Вызов не удался: станция повторяет запрос пароля.", 401)
        except BaseException:
            разговор.завершён.set()
            self._забыть(разговор)
            звук.close()
            raise

    def _вызов(self, запрос: Any, call_id: str, cseq: int, ветка: str, адрес: str,
               от: str, кому: str, ждать_с: float) -> Сообщение:
        """INVITE: ждёт окончательный ответ; по сроку — отменяет вызов."""
        очередь: queue.Queue[Сообщение] = queue.Queue()
        with self._замок:
            self._ответы[(call_id, cseq)] = очередь
        try:
            данные = запрос()
            self._послать(данные)
            начало = time.monotonic()
            повтор = T1
            следующий = начало + повтор
            был_ответ = False
            отменён = False
            отмена = b""
            отмена_принята = False
            конец = начало + max(5.0, ждать_с)
            while True:
                сейчас = time.monotonic()
                if not был_ответ and сейчас - начало > СРОК_ЗАПРОСА_С:
                    raise ОшибкаSIP(f"Станция {self.узел}:{self.порт} не отвечает на вызов.")
                if сейчас >= конец and not отменён:
                    if not был_ответ:
                        raise НеОтвечает("Станция не начала вызов за отведённое время.", 408)
                    # Абонент не снял трубку: CANCEL той же веткой, что INVITE.
                    отмена = собрать(f"CANCEL {адрес} SIP/2.0", [
                        ("Via", self._via(ветка)), ("Max-Forwards", "70"),
                        ("From", от), ("To", кому), ("Call-ID", call_id),
                        ("CSeq", f"{cseq} CANCEL"), ("User-Agent", self.АГЕНТ)])
                    self._послать(отмена)
                    отменён = True
                    # CANCEL повторяется, пока станция его не подтвердит: по
                    # UDP он может потеряться, и тогда телефон клиента звонил
                    # бы дальше, а снятая трубка встречала тишину.
                    повтор = T1
                    следующий = сейчас + повтор
                    конец = сейчас + 8.0
                    continue
                if сейчас >= конец and отменён:
                    raise НеОтвечает("Абонент не ответил.", 487)
                try:
                    ответ = очередь.get(timeout=max(0.05, min(конец, следующий) - сейчас))
                except queue.Empty:
                    if self.надёжный:
                        continue                # по TCP/TLS повторы не нужны
                    if not был_ответ and time.monotonic() >= следующий and повтор < 8:
                        self._послать(данные)
                        повтор *= 2
                        следующий = time.monotonic() + повтор
                    elif (отменён and not отмена_принята and time.monotonic() >= следующий
                          and повтор < 4):
                        self._послать(отмена)
                        повтор *= 2
                        следующий = time.monotonic() + повтор
                    continue
                _, метод = ответ.cseq
                if метод == "CANCEL":
                    отмена_принята = True
                    continue
                if метод != "INVITE":
                    continue
                был_ответ = True
                if not отменён:
                    следующий = float("inf")
                if ответ.код >= 200:
                    if отменён and ответ.код == 487:
                        self._ack_отказа(адрес, ветка, от, ответ, call_id, cseq)
                        raise НеОтвечает("Абонент не ответил.", 487)
                    return ответ
        finally:
            with self._замок:
                self._ответы.pop((call_id, cseq), None)

    def _ack_отказа(self, адрес: str, ветка: str, от: str, ответ: Сообщение,
                    call_id: str, cseq: int) -> None:
        """ACK на отказ: та же ветка, что у INVITE, и To с меткой из ответа."""
        try:
            self._послать(собрать(f"ACK {адрес} SIP/2.0", [
                ("Via", self._via(ветка)), ("Max-Forwards", "70"), ("From", от),
                ("To", ответ.взять("to")), ("Call-ID", call_id),
                ("CSeq", f"{cseq} ACK"), ("User-Agent", self.АГЕНТ)]))
        except ОшибкаSIP:
            pass

    def _установить(self, разговор: Разговор, ответ: Сообщение, от: str, cseq: int) -> None:
        описание = разобрать_sdp(ответ.тело)
        разговор.от = от
        разговор.кому = ответ.взять("to")
        разговор.cseq = cseq
        контакт = ответ.взять("contact")
        разговор.цель = _адрес_в_скобках(контакт) if контакт else f"sip:{self.узел}"
        разговор.маршрут = list(reversed(ответ.все("record-route")))
        self._ack(разговор)
        if not описание["кодек"] or not описание["порт"]:
            разговор.кем_завершён = "сервер"
            разговор.завершён.set()
            self._bye(разговор)
            raise ОшибкаSIP("Станция не предложила G.711 (alaw или ulaw): "
                            "разрешите кодек у номера телефона.", 488)
        if разговор.ключ_srtp is not None:
            их = выбрать_ключ(описание["крипто"], тег=разговор.ключ_srtp.тег)
            if их is None:
                # Станция ответила открытым звуком — разговор без шифрования
                # не ведём: ключ клиента и его ответы ушли бы в сеть как есть.
                разговор.кем_завершён = "сервер"
                разговор.завершён.set()
                self._bye(разговор)
                raise ОшибкаSIP("Станция не согласилась шифровать звук (SRTP): включите у "
                                "номера media_encryption=sdes (chan_pjsip) или "
                                "encryption=yes (chan_sip), либо выключите qc_srtp.", 488)
            разговор.включить_srtp(разговор.ключ_srtp, их)
        разговор.кодек = описание["кодек"]
        разговор.номер_кнопок = описание["кнопки"] or 101
        разговор.куда = (описание["адрес"] or self._ip_станции, описание["порт"])
        разговор.начат = time.time()
        разговор.слушать()

    def _в_диалоге(self, разговор: Разговор, метод: str, cseq: int,
                   ещё: list[tuple[str, str]] | None = None) -> bytes:
        заголовки = [("Via", self._via()), ("Max-Forwards", "70")]
        заголовки += [("Route", м) for м in разговор.маршрут]
        заголовки += [("From", разговор.от), ("To", разговор.кому),
                      ("Call-ID", разговор.call_id), ("CSeq", f"{cseq} {метод}"),
                      ("Contact", self._контакт), ("User-Agent", self.АГЕНТ)]
        заголовки += ещё or []
        return собрать(f"{метод} {разговор.цель} SIP/2.0", заголовки)

    def _ack(self, разговор: Разговор) -> None:
        # Строгие станции и SBC ждут в ACK те же учётные данные, что в
        # INVITE (у ACK на 2xx тот же CSeq): без них ACK отвергается, 200
        # повторяется, а через 32 с вызов рвётся.
        учётные = getattr(разговор, "учётные", None)
        self._послать(self._в_диалоге(разговор, "ACK", разговор.cseq,
                                      [учётные] if учётные else None))

    def _bye(self, разговор: Разговор) -> None:
        if not разговор.цель:
            return
        учётные: tuple[str, str] | None = None
        for _ in range(2):
            разговор.cseq += 1
            номер = разговор.cseq

            def запрос(номер: int = номер, учётные: tuple[str, str] | None = учётные) -> bytes:
                return self._в_диалоге(разговор, "BYE", номер, [учётные] if учётные else None)

            try:
                ответ = self._обмен(запрос, разговор.call_id, номер, срок_с=4.0)
            except ОшибкаSIP:
                return
            if ответ.код in (401, 407) and учётные is None:
                вызов = _выбрать_вызов(ответ)
                if вызов is None:
                    return
                учётные = (вызов[0], ответ_на_вызов(
                    вызов[1], метод="BYE", адрес=разговор.цель,
                    пользователь=self.пользователь, пароль=self.пароль))
                continue
            return

    def _забыть(self, разговор: Разговор) -> None:
        with self._замок:
            self._разговоры.pop(разговор.call_id, None)
            if not разговор.цель:
                # Вызов не состоялся: если абонент всё же снимет трубку (CANCEL
                # потерялся или разминулся с ответом), станция пришлёт 200 —
                # его надо подтвердить и сразу положить, иначе клиент слышит
                # тишину до разрыва по сроку.
                сейчас = time.monotonic()
                self._брошенные = {к: т for к, т in self._брошенные.items() if сейчас - т < 64}
                self._брошенные[разговор.call_id] = сейчас

    def _положить_брошенный(self, ответ: Сообщение) -> None:
        """Поздний 200 на брошенный вызов: ACK и BYE в его диалоге."""
        номер, _ = ответ.cseq
        контакт = ответ.взять("contact")
        цель = _адрес_в_скобках(контакт) if контакт else f"sip:{self.узел}"
        маршрут = [("Route", м) for м in reversed(ответ.все("record-route"))]
        общие = [("From", ответ.взять("from")), ("To", ответ.взять("to")),
                 ("Call-ID", ответ.взять("call-id"))]
        try:
            self._послать(собрать(f"ACK {цель} SIP/2.0", [
                ("Via", self._via()), ("Max-Forwards", "70"), *маршрут, *общие,
                ("CSeq", f"{номер} ACK"), ("User-Agent", self.АГЕНТ)]))
            self._послать(собрать(f"BYE {цель} SIP/2.0", [
                ("Via", self._via()), ("Max-Forwards", "70"), *маршрут, *общие,
                ("CSeq", f"{номер + 1} BYE"), ("User-Agent", self.АГЕНТ)]))
        except ОшибкаSIP:
            pass

    @property
    def разговоров(self) -> int:
        with self._замок:
            return len(self._разговоры)

    def закрыть(self, *, снять_регистрацию: bool = True) -> None:
        with self._замок:
            идущие = list(self._разговоры.values())
        for разговор in идущие:
            разговор.положить()
        if снять_регистрацию and self.зарегистрирован_до > time.time():
            try:
                self.зарегистрировать(0)
            except ОшибкаSIP:
                pass
        self._закрыт.set()
        self._есть_связь.set()                  # разбудить поток чтения
        for сокет in (self._сокет, self._соединение):
            if сокет is None:
                continue
            try:
                сокет.close()
            except OSError:
                pass


def _кнопка_из_info(запрос: Сообщение) -> str:
    """Кнопка из тела INFO: `Signal=5` (dtmf-relay) или просто `5` (dtmf)."""
    текст = запрос.тело.decode("utf-8", "replace")
    совпало = re.search(r"Signal\s*=\s*([0-9A-D*#])", текст, re.I)
    if совпало:
        return совпало.group(1).upper()
    текст = текст.strip()
    if текст in ("10", "11"):                   # application/dtmf: 10 — «*», 11 — «#»
        return "*#"[int(текст) - 10]
    return текст.upper() if len(текст) == 1 and текст.upper() in КНОПКИ else ""


def _ошибка_вызова(ответ: Сообщение) -> ОшибкаSIP:
    код, причина = ответ.код, ответ.причина
    if код in (486, 600, 603):
        return Занято(f"Абонент занят или отклонил вызов ({код} {причина}).", код)
    if код in (408, 480, 487):
        return НеОтвечает(f"Абонент не отвечает ({код} {причина}).", код)
    if код == 404:
        return ОшибкаSIP("Станция не знает такого номера (404): проверьте префикс "
                         "набора и контекст телефона.", код)
    if код == 403:
        return ОшибкаSIP("Станция запретила вызов (403): у телефона нет права "
                         "звонить на этот номер.", код)
    if код == 488:
        return ОшибкаSIP("Станция не приняла кодеки (488): разрешите alaw или ulaw.", код)
    return ОшибкаSIP(f"Вызов не удался: {код} {причина}.", код)
