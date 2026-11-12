"""SRTP для телефона контроля качества: шифрованный звук (RFC 3711, SDES — RFC 4568).

Звук опроса — голос клиента и его ответы. Открытый RTP читается любым, кто
видит трафик между станцией и сервером. SRTP шифрует звук (AES-128 в режиме
счётчика) и подписывает каждый пакет (HMAC-SHA1, 80 или 32 бита): чужой или
испорченный пакет отбрасывается, а не проигрывается.

Ключи передаются в SDP строкой `a=crypto` (SDES): у каждой стороны свой ключ
на свою передачу. В открытом SIP ключ виден так же, как звук, поэтому SDES
имеет смысл вместе с SIP по TLS (`qc_sip_transport: tls`).

Шифр AES берётся из пакета `cryptography`, если он установлен, иначе — свой,
на чистом Python (таблицы T, сверен с FIPS-197 и с `cryptography` в
проверках): опрос — 50 пакетов в секунду в каждую сторону, это ему по силам.
RTCP не шифруется и не шлётся: телефон его не отправляет, а входящий
станции не читается.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass

#: Поддерживаемые наборы и длина подписи в байтах.
НАБОРЫ = {"AES_CM_128_HMAC_SHA1_80": 10, "AES_CM_128_HMAC_SHA1_32": 4}
НАБОР_ПО_УМОЛЧАНИЮ = "AES_CM_128_HMAC_SHA1_80"

# ---------------------------------------------------------------------------
# AES-128, только шифрование (режиму счётчика расшифровка не нужна)
# ---------------------------------------------------------------------------


def _таблица_замен() -> list[int]:
    """S-блок AES, вычисленный, а не вписанный: опечатка в 256 числах незаметна."""
    замены = [0] * 256
    p = q = 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF       # p · 3
        q ^= q << 1                                                   # q / 3
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q
        for сдвиг in (1, 2, 3, 4):
            x ^= ((q << сдвиг) | (q >> (8 - сдвиг))) & 0xFF
        замены[p] = x ^ 0x63
        if p == 1:
            break
    замены[0] = 0x63
    return замены


_S = _таблица_замен()


def _x2(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) if a & 0x100 else a


_TE0 = [(_x2(s) << 24) | (s << 16) | (s << 8) | (_x2(s) ^ s) for s in _S]
_TE1 = [((t >> 8) | (t << 24)) & 0xFFFFFFFF for t in _TE0]
_TE2 = [((t >> 16) | (t << 16)) & 0xFFFFFFFF for t in _TE0]
_TE3 = [((t >> 24) | (t << 8)) & 0xFFFFFFFF for t in _TE0]
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


class _AESPython:
    """AES-128 на чистом Python: расширение ключа и шифрование блока."""

    def __init__(self, ключ: bytes) -> None:
        if len(ключ) != 16:
            raise ValueError("ключ AES-128 — 16 байт")
        w = list(struct.unpack(">4I", ключ))
        for i in range(4, 44):
            t = w[i - 1]
            if i % 4 == 0:
                t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
                t = ((_S[t >> 24] << 24) | (_S[(t >> 16) & 255] << 16)
                     | (_S[(t >> 8) & 255] << 8) | _S[t & 255])
                t ^= _RCON[i // 4 - 1] << 24
            w.append(w[i - 4] ^ t)
        self._к = w

    def блок(self, данные: bytes) -> bytes:
        к = self._к
        s0, s1, s2, s3 = struct.unpack(">4I", данные)
        s0 ^= к[0]
        s1 ^= к[1]
        s2 ^= к[2]
        s3 ^= к[3]
        for р in range(1, 10):
            о = 4 * р
            t0 = (_TE0[s0 >> 24] ^ _TE1[(s1 >> 16) & 255] ^ _TE2[(s2 >> 8) & 255]
                  ^ _TE3[s3 & 255] ^ к[о])
            t1 = (_TE0[s1 >> 24] ^ _TE1[(s2 >> 16) & 255] ^ _TE2[(s3 >> 8) & 255]
                  ^ _TE3[s0 & 255] ^ к[о + 1])
            t2 = (_TE0[s2 >> 24] ^ _TE1[(s3 >> 16) & 255] ^ _TE2[(s0 >> 8) & 255]
                  ^ _TE3[s1 & 255] ^ к[о + 2])
            t3 = (_TE0[s3 >> 24] ^ _TE1[(s0 >> 16) & 255] ^ _TE2[(s1 >> 8) & 255]
                  ^ _TE3[s2 & 255] ^ к[о + 3])
            s0, s1, s2, s3 = t0, t1, t2, t3
        r0 = ((_S[s0 >> 24] << 24) | (_S[(s1 >> 16) & 255] << 16)
              | (_S[(s2 >> 8) & 255] << 8) | _S[s3 & 255]) ^ к[40]
        r1 = ((_S[s1 >> 24] << 24) | (_S[(s2 >> 16) & 255] << 16)
              | (_S[(s3 >> 8) & 255] << 8) | _S[s0 & 255]) ^ к[41]
        r2 = ((_S[s2 >> 24] << 24) | (_S[(s3 >> 16) & 255] << 16)
              | (_S[(s0 >> 8) & 255] << 8) | _S[s1 & 255]) ^ к[42]
        r3 = ((_S[s3 >> 24] << 24) | (_S[(s0 >> 16) & 255] << 16)
              | (_S[(s1 >> 8) & 255] << 8) | _S[s2 & 255]) ^ к[43]
        return struct.pack(">4I", r0, r1, r2, r3)


class _AESБиблиотека:
    """AES-128 из `cryptography`: в разы быстрее своего."""

    def __init__(self, ключ: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers import (  # noqa: PLC0415
            Cipher,
            algorithms,
            modes,
        )

        self._шифр = Cipher(algorithms.AES(ключ), modes.ECB()).encryptor()

    def блок(self, данные: bytes) -> bytes:
        return self._шифр.update(данные)


def шифр_aes(ключ: bytes, *, свой: bool = False) -> _AESPython | _AESБиблиотека:
    """AES-128 для режима счётчика: из `cryptography`, если есть, иначе свой."""
    if not свой:
        try:
            return _AESБиблиотека(ключ)
        except ImportError:
            pass
    return _AESPython(ключ)


def поток_ключа(шифр: _AESPython | _AESБиблиотека, iv: int, длина: int) -> bytes:
    """Поток ключа AES-CM: E(k, IV) || E(k, IV+1) || … (RFC 3711, 4.1.1)."""
    куски = []
    for н in range((длина + 15) // 16):
        куски.append(шифр.блок(((iv + н) & ((1 << 128) - 1)).to_bytes(16, "big")))
    return b"".join(куски)[:длина]


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


# ---------------------------------------------------------------------------
# Ключи сеанса и защита пакетов
# ---------------------------------------------------------------------------

def ключи_сеанса(мастер_ключ: bytes, мастер_соль: bytes, *, свой_aes: bool = False,
                 rtcp: bool = False) -> tuple[bytes, bytes, bytes]:
    """Ключ шифрования, ключ подписи и соль сеанса (RFC 3711, 4.3; kdr = 0)."""
    if len(мастер_ключ) != 16 or len(мастер_соль) != 14:
        raise ValueError("мастер-ключ — 16 байт, соль — 14 байт")
    шифр = шифр_aes(мастер_ключ, свой=свой_aes)
    соль = int.from_bytes(мастер_соль, "big")

    def вывести(метка: int, длина: int) -> bytes:
        x = (метка << 48) ^ соль
        return поток_ключа(шифр, x << 16, длина)

    основа = 3 if rtcp else 0
    return вывести(основа, 16), вывести(основа + 1, 20), вывести(основа + 2, 14)


@dataclass
class Ключ:
    """Ключ одной стороны из строки `a=crypto`: набор, мастер-ключ и соль."""

    тег: int
    набор: str
    мастер: bytes

    @property
    def строка(self) -> str:
        """Значение атрибута SDP: `1 AES_CM_128_HMAC_SHA1_80 inline:…`."""
        return f"{self.тег} {self.набор} inline:{base64.b64encode(self.мастер).decode()}"


def новый_ключ(тег: int = 1, набор: str = НАБОР_ПО_УМОЛЧАНИЮ) -> Ключ:
    return Ключ(тег, набор, os.urandom(30))


def разобрать_крипто(значение: str) -> Ключ | None:
    """`a=crypto:<тег> <набор> inline:<ключ+соль>[|срок][|MKI:длина]` → Ключ.

    Строки с MKI и незнакомые наборы пропускаются (None): MKI телефон не
    поддерживает, а сменить ключ посреди разговора станция может и новым
    предложением SDP.
    """
    части = значение.strip().split()
    if len(части) < 3 or not части[0].isdigit() or части[1] not in НАБОРЫ:
        return None
    ключи = части[2]
    if not ключи.startswith("inline:"):
        return None
    ключ, *хвост = ключи[7:].split("|")
    if any(ч.upper().startswith("MKI") or ":" in ч for ч in хвост):
        return None
    try:
        мастер = base64.b64decode(ключ + "=" * (-len(ключ) % 4), validate=True)
    except ValueError:
        return None
    if len(мастер) != 30:
        return None
    return Ключ(int(части[0]), части[1], мастер)


class SRTP:
    """Защита или раскрытие пакетов RTP одной стороны разговора.

    Отправляющая сторона ведёт счётчик переполнений номера (ROC) сама;
    принимающая угадывает его по последнему принятому номеру (RFC 3711,
    приложение A) и принимает новое значение только после верной подписи.
    """

    def __init__(self, ключ: Ключ, *, свой_aes: bool = False) -> None:
        self.ключ = ключ
        self.длина_подписи = НАБОРЫ[ключ.набор]
        к_ш, к_п, соль = ключи_сеанса(ключ.мастер[:16], ключ.мастер[16:], свой_aes=свой_aes)
        self._шифр = шифр_aes(к_ш, свой=свой_aes)
        self._подпись = hmac.new(к_п, digestmod=hashlib.sha1)
        self._соль = int.from_bytes(соль, "big")
        self._roc = 0
        self._последний: int | None = None      # s_l: старший принятый (или отправленный) номер
        self.отвергнуто = 0

    def _поток(self, ssrc: int, индекс: int, длина: int) -> bytes:
        iv = (self._соль << 16) ^ (ssrc << 64) ^ (индекс << 16)
        return поток_ключа(self._шифр, iv, длина)

    def _тег(self, данные: bytes, roc: int) -> bytes:
        подпись = self._подпись.copy()
        подпись.update(данные + struct.pack(">I", roc))
        return подпись.digest()[:self.длина_подписи]

    @staticmethod
    def _заголовок(пакет: bytes) -> int:
        длина = 12 + 4 * (пакет[0] & 0x0F)
        if пакет[0] & 0x10 and len(пакет) >= длина + 4:
            длина += 4 + 4 * struct.unpack(">H", пакет[длина + 2:длина + 4])[0]
        return длина

    def защитить(self, пакет: bytes) -> bytes:
        """RTP → SRTP: зашифрованная нагрузка и подпись в конце."""
        номер, ssrc = struct.unpack(">H", пакет[2:4])[0], struct.unpack(">I", пакет[8:12])[0]
        if self._последний is not None and номер < self._последний \
                and self._последний - номер > 0x8000:
            self._roc = (self._roc + 1) & 0xFFFFFFFF            # номер пошёл по кругу
        self._последний = номер
        начало = self._заголовок(пакет)
        нагрузка = пакет[начало:]
        индекс = (self._roc << 16) | номер
        зашифровано = пакет[:начало] + _xor(нагрузка, self._поток(ssrc, индекс, len(нагрузка)))
        return зашифровано + self._тег(зашифровано, self._roc)

    def раскрыть(self, пакет: bytes) -> bytes | None:
        """SRTP → RTP; None — подпись не сошлась (чужой, испорченный пакет)."""
        if len(пакет) < 12 + self.длина_подписи:
            self.отвергнуто += 1
            return None
        тело, тег = пакет[:-self.длина_подписи], пакет[-self.длина_подписи:]
        номер, ssrc = struct.unpack(">H", тело[2:4])[0], struct.unpack(">I", тело[8:12])[0]
        roc = self._roc
        if self._последний is not None:
            if self._последний < 0x8000:
                if номер - self._последний > 0x8000:
                    roc = (self._roc - 1) & 0xFFFFFFFF
            elif self._последний - 0x8000 > номер:
                roc = (self._roc + 1) & 0xFFFFFFFF
        if not hmac.compare_digest(self._тег(тело, roc), тег):
            self.отвергнуто += 1
            return None
        if self._последний is None or roc == (self._roc + 1) & 0xFFFFFFFF:
            self._roc, self._последний = roc, номер
        elif roc == self._roc and номер > self._последний:
            self._последний = номер
        начало = self._заголовок(тело)
        нагрузка = тело[начало:]
        индекс = (roc << 16) | номер
        return тело[:начало] + _xor(нагрузка, self._поток(ssrc, индекс, len(нагрузка)))
