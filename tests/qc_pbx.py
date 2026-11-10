"""Мини-АТС для проверок контроля качества.

Отвечает телефону сервера по SIP так, как отвечает Asterisk: спрашивает
пароль (Digest) у REGISTER и INVITE, «звонит» абоненту, снимает трубку и
ведёт себя как клиент — слушает фразы и нажимает кнопки по сценарию. Всё на
127.0.0.1, без внешних программ.

Сценарий абонента (`кнопки`) — что он нажимает после каждой прослушанной
фразы. Фраза считается прослушанной, когда после звука наступила тишина.
"""
from __future__ import annotations

import hashlib
import random
import socket
import struct
import threading
import time
import uuid
from typing import Any

from asrhub.qc import sip


class МиниАТС:
    def __init__(self, *, пароль: str = "secret", способ: str = "rfc2833",
                 кнопки: list[str] | None = None, ответ: str = "answer",
                 положить_после: int | None = None, qop: bool = True,
                 без_пароля: bool = False, кодек: str = "PCMA") -> None:
        self.пароль = пароль
        self.способ = способ                # rfc2833 | info | inband
        self.кнопки = list(кнопки or [])
        self.ответ = ответ                  # answer | busy | noanswer | notfound
        self.положить_после = положить_после
        self.qop = qop
        self.без_пароля = без_пароля
        self.кодек = кодек
        self.сокет = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.сокет.bind(("127.0.0.1", 0))
        self.порт = self.сокет.getsockname()[1]
        self.звук = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.звук.bind(("127.0.0.1", 0))
        self.порт_звука = self.звук.getsockname()[1]
        self.nonce = uuid.uuid4().hex
        self.журнал: list[str] = []         # методы запросов по порядку
        self.набрано: list[str] = []        # номера из INVITE
        self.регистраций = 0
        self.снята_регистрация = False
        self.получено_звука = 0
        self.нажато: list[str] = []
        self.прослушано = 0
        self.bye_от_телефона = threading.Event()
        self.отменён = threading.Event()
        self.от_заголовок = ""
        self._стоп = threading.Event()
        self._разговор: dict[str, Any] = {}
        self._потоки = [threading.Thread(target=self._sip, daemon=True)]
        self._потоки[0].start()

    # -- SIP ----------------------------------------------------------------

    def _ответить(self, запрос: sip.Сообщение, откуда: Any, код: int, причина: str, *,
                  тело: bytes = b"", ещё: list[tuple[str, str]] | None = None,
                  метка: str = "") -> None:
        кому = запрос.взять("to")
        if метка and "tag=" not in кому:
            кому += f";tag={метка}"
        заголовки = [("Via", з) for з in запрос.все("via")]
        заголовки += [("From", запрос.взять("from")), ("To", кому),
                      ("Call-ID", запрос.взять("call-id")), ("CSeq", запрос.взять("cseq"))]
        заголовки += ещё or []
        self.сокет.sendto(sip.собрать(f"SIP/2.0 {код} {причина}", заголовки, тело), откуда)

    def _пароль_верен(self, запрос: sip.Сообщение) -> bool:
        if self.без_пароля:
            return True
        строка = запрос.взять("authorization")
        if not строка:
            return False
        поля = sip.разобрать_вызов(строка)

        def х(текст: str) -> str:
            return hashlib.md5(текст.encode()).hexdigest()   # noqa: S324

        первая = х(f"{поля.get('username')}:asterisk:{self.пароль}")
        вторая = х(f"{запрос.метод}:{поля.get('uri')}")
        if поля.get("qop") == "auth":
            ожидается = х(f"{первая}:{self.nonce}:{поля.get('nc')}:{поля.get('cnonce')}:auth:{вторая}")
        else:
            ожидается = х(f"{первая}:{self.nonce}:{вторая}")
        return поля.get("response") == ожидается and поля.get("nonce") == self.nonce

    def _спросить_пароль(self, запрос: sip.Сообщение, откуда: Any) -> None:
        вызов = f'Digest realm="asterisk", nonce="{self.nonce}", algorithm=MD5'
        if self.qop:
            вызов += ', qop="auth"'
        self._ответить(запрос, откуда, 401, "Unauthorized",
                       ещё=[("WWW-Authenticate", вызов)], метка=uuid.uuid4().hex[:8])

    def _sip(self) -> None:
        self.сокет.settimeout(0.2)
        while not self._стоп.is_set():
            try:
                данные, откуда = self.сокет.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            сообщение = sip.разобрать(данные)
            if сообщение is None or сообщение.ответ:
                continue
            метод = сообщение.метод
            self.журнал.append(метод)
            if метод == "REGISTER":
                if not self._пароль_верен(сообщение):
                    self._спросить_пароль(сообщение, откуда)
                    continue
                срок = сообщение.взять("expires", "300")
                if срок == "0":
                    self.снята_регистрация = True
                else:
                    self.регистраций += 1
                self._ответить(сообщение, откуда, 200, "OK", метка="reg", ещё=[
                    ("Contact", f"{сообщение.взять('contact').split(';')[0]};expires={срок}")])
            elif метод == "INVITE":
                if not self._пароль_верен(сообщение):
                    self._спросить_пароль(сообщение, откуда)
                    continue
                if сообщение.взять("call-id") in self._разговор:
                    continue                                # повтор запроса
                номер = сообщение.начало.split()[1].split(":")[1].split("@")[0]
                self.набрано.append(номер)
                self.от_заголовок = сообщение.взять("from")
                self._ответить(сообщение, откуда, 100, "Trying")
                if self.ответ == "notfound":
                    self._ответить(сообщение, откуда, 404, "Not Found", метка="nf")
                    continue
                if self.ответ == "busy":
                    self._ответить(сообщение, откуда, 486, "Busy Here", метка="busy")
                    continue
                self._ответить(сообщение, откуда, 180, "Ringing", метка="abc")
                self._разговор = {"call-id": сообщение.взять("call-id"), "invite": сообщение,
                                  "откуда": откуда, "sdp": sip.разобрать_sdp(сообщение.тело),
                                  "contact": сообщение.взять("contact")}
                if self.ответ == "answer":
                    тело = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\n"
                            f"t=0 0\r\nm=audio {self.порт_звука} RTP/AVP "
                            f"{sip.НОМЕР_КОДЕКА[self.кодек]} 101\r\n"
                            f"a=rtpmap:{sip.НОМЕР_КОДЕКА[self.кодек]} {self.кодек}/8000\r\n"
                            "a=rtpmap:101 telephone-event/8000\r\n").encode()
                    self._ответить(сообщение, откуда, 200, "OK", тело=тело, метка="abc", ещё=[
                        ("Contact", f"<sip:pbx@127.0.0.1:{self.порт}>"),
                        ("Content-Type", "application/sdp")])
            elif метод == "ACK":
                if self._разговор and self.ответ == "answer" and "поток" not in self._разговор:
                    поток = threading.Thread(target=self._абонент, daemon=True)
                    self._разговор["поток"] = поток
                    поток.start()
            elif метод == "CANCEL":
                self.отменён.set()
                self._ответить(сообщение, откуда, 200, "OK", метка="abc")
                if self._разговор:
                    self._ответить(self._разговор["invite"], откуда, 487,
                                   "Request Terminated", метка="abc")
            elif метод == "BYE":
                self.bye_от_телефона.set()
                self._ответить(сообщение, откуда, 200, "OK")
            else:
                self._ответить(сообщение, откуда, 200, "OK")

    # -- абонент ------------------------------------------------------------

    def _rtp(self, вид: int, нагрузка: bytes, состояние: dict[str, int], *,
             метка: int | None = None, маркер: bool = False) -> None:
        заголовок = struct.pack("!BBHII", 0x80, вид | (0x80 if маркер else 0),
                                состояние["номер"] & 0xFFFF,
                                (состояние["время"] if метка is None else метка) & 0xFFFFFFFF,
                                0x1234)
        состояние["номер"] += 1
        self.звук.sendto(заголовок + нагрузка, self._разговор["куда"])

    def _нажать(self, кнопка: str, состояние: dict[str, int]) -> None:
        self.нажато.append(кнопка)
        if self.способ == "info":
            invite = self._разговор["invite"]
            цель = sip._адрес_в_скобках(self._разговор["contact"])
            тело = f"Signal={кнопка}\r\nDuration=160\r\n".encode()
            self.сокет.sendto(sip.собрать(f"INFO {цель} SIP/2.0", [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{self.порт};branch=z9hG4bK{uuid.uuid4().hex[:8]}"),
                ("From", invite.взять("to") + ";tag=abc"), ("To", invite.взять("from")),
                ("Call-ID", invite.взять("call-id")), ("CSeq", f"{random.randint(2, 999)} INFO"),
                ("Content-Type", "application/dtmf-relay")], тело), self._разговор["откуда"])
        elif self.способ == "inband":
            тон = sip.тон_кнопки(кнопка, мс=120, кодек=self.кодек)
            for начало in range(0, len(тон), 160):
                self._rtp(sip.НОМЕР_КОДЕКА[self.кодек], тон[начало:начало + 160], состояние)
                состояние["время"] += 160
                time.sleep(0.02)
        else:
            код = sip.КНОПКИ.index(кнопка)
            метка = состояние["время"]
            for шаг in range(6):                # три пакета события и три — его конца
                конец = 0x80 if шаг >= 3 else 0
                self._rtp(101, struct.pack("!BBH", код, конец | 10, 160 * (шаг + 1)),
                          состояние, метка=метка, маркер=шаг == 0)
                time.sleep(0.02)
            состояние["время"] += 160 * 8

    def _абонент(self) -> None:
        описание = self._разговор["sdp"]
        self._разговор["куда"] = (описание["адрес"], описание["порт"])
        состояние = {"номер": 1, "время": 1000}
        тишина = sip.ТИШИНА[self.кодек]
        очередь = list(self.кнопки)
        звучало = False
        тихо_подряд = 0
        self.звук.settimeout(0.5)
        пусто_подряд = 0
        while not self._стоп.is_set() and not self.bye_от_телефона.is_set():
            try:
                данные, _ = self.звук.recvfrom(2048)
            except TimeoutError:
                пусто_подряд += 1
                if пусто_подряд > 20:
                    return
                continue
            except OSError:
                return
            пусто_подряд = 0
            нагрузка = данные[12:]
            self.получено_звука += len(нагрузка)
            if нагрузка.count(тишина) > len(нагрузка) * 0.9:
                тихо_подряд += 1
            else:
                звучало = True
                тихо_подряд = 0
            if звучало and тихо_подряд >= 8:    # фраза кончилась 160 мс назад
                звучало = False
                self.прослушано += 1
                if self.положить_после is not None and self.прослушано > self.положить_после:
                    self._положить()
                    return
                if очередь:
                    кнопка = очередь.pop(0)
                    if кнопка:                  # пустая — фразу слушают молча
                        self._нажать(кнопка, состояние)

    def _положить(self) -> None:
        invite = self._разговор["invite"]
        цель = sip._адрес_в_скобках(self._разговор["contact"])
        self.сокет.sendto(sip.собрать(f"BYE {цель} SIP/2.0", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{self.порт};branch=z9hG4bK{uuid.uuid4().hex[:8]}"),
            ("From", invite.взять("to") + ";tag=abc"), ("To", invite.взять("from")),
            ("Call-ID", invite.взять("call-id")), ("CSeq", "77 BYE")]),
            self._разговор["откуда"])

    def закрыть(self) -> None:
        self._стоп.set()
        for сокет in (self.сокет, self.звук):
            try:
                сокет.close()
            except OSError:
                pass
