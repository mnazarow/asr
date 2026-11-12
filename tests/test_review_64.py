"""Заход 64: SIP по TCP и TLS и шифрованный звук (SRTP) у телефона опросов.

Телефон сервера звонил только по UDP и с открытым звуком: его можно было
ставить лишь в доверенной сети станции. Теперь сигнализация — по TCP или
TLS (одно соединение, по нему же запросы станции), а звук — SRTP с ключами
в SDP (SDES). Шифр сверяется с векторами RFC 3711 и FIPS-197, с пакетом
`cryptography` и — если установлен — с libsrtp (`pylibsrtp`) байт в байт;
разговор целиком — с мини-АТС по TLS.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from asrhub.qc import sip, survey, tts
from asrhub.qc import srtp as шифр
from qc_pbx import МиниАТС
from test_qc import ГОЛОС, ГРУППА

# --- шифр -------------------------------------------------------------------

def test_aes_по_fips_197_и_совпадает_с_библиотекой():
    ключ = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    блок = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert шифр._AESPython(ключ).блок(блок).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert шифр._S[0] == 0x63 and шифр._S[0x53] == 0xED
    pytest.importorskip("cryptography")
    for _ in range(50):
        ключ, блок = os.urandom(16), os.urandom(16)
        assert шифр._AESPython(ключ).блок(блок) == шифр._AESБиблиотека(ключ).блок(блок)


def test_поток_ключа_и_ключи_сеанса_по_rfc_3711():
    # RFC 3711, B.2: поток ключа AES-CM.
    aes = шифр.шифр_aes(bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C"), свой=True)
    поток = шифр.поток_ключа(aes, int("F0F1F2F3F4F5F6F7F8F9FAFBFCFD0000", 16), 48)
    assert поток.hex().upper() == ("E03EAD0935C95E80E166B16DD92B4EB4"
                                   "D23513162B02D0F72A43A2FE4A5F97AB"
                                   "41E95B3BB0A2E8DD477901E4FCA894C0")
    # RFC 3711, B.3: вывод ключей сеанса.
    к_ш, к_п, соль = шифр.ключи_сеанса(bytes.fromhex("E1F97A0D3E018BE0D64FA32C06DE4139"),
                                       bytes.fromhex("0EC675AD498AFEEBB6960B3AABE6"),
                                       свой_aes=True)
    assert к_ш.hex().upper() == "C61E7A93744F39EE10734AFE3FF7A087"
    assert соль.hex().upper() == "30CBBC08863D8C85D49DB34A9AE1"
    assert к_п.hex().upper() == "CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4"


def _rtp(номер: int, нагрузка: bytes, ssrc: int = 0xDEADBEEF) -> bytes:
    return struct.pack("!BBHII", 0x80, 8, номер & 0xFFFF, 160 * номер, ssrc) + нагрузка


@pytest.mark.parametrize("набор", list(шифр.НАБОРЫ))
def test_защита_и_раскрытие_через_переполнение_номера(набор):
    ключ = шифр.новый_ключ(1, набор)
    передача, приём = шифр.SRTP(ключ), шифр.SRTP(ключ)
    for н in range(0xFFF0, 0x10010):                       # номер пакета идёт по кругу
        пакет = _rtp(н, os.urandom(160))
        защищён = передача.защитить(пакет)
        assert len(защищён) == len(пакет) + шифр.НАБОРЫ[набор]
        assert защищён[12:172] != пакет[12:]
        assert приём.раскрыть(защищён) == пакет
    assert передача._roc == приём._roc == 1


def test_чужой_и_испорченный_пакет_не_проходят():
    ключ = шифр.новый_ключ()
    передача, приём = шифр.SRTP(ключ), шифр.SRTP(ключ)
    защищён = bytearray(передача.защитить(_rtp(7, b"\xd5" * 160)))
    защищён[20] ^= 1
    assert приём.раскрыть(bytes(защищён)) is None
    assert шифр.SRTP(шифр.новый_ключ()).раскрыть(передача.защитить(_rtp(8, b"x" * 160))) is None
    assert приём.раскрыть(_rtp(9, b"\xd5" * 160)) is None      # открытый RTP
    assert приём.отвергнуто == 2


def test_совпадает_с_libsrtp_байт_в_байт():
    pylibsrtp = pytest.importorskip("pylibsrtp")
    for набор, профиль in (("AES_CM_128_HMAC_SHA1_80",
                            pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80),
                           ("AES_CM_128_HMAC_SHA1_32",
                            pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_32)):
        мастер = os.urandom(30)
        наш = шифр.SRTP(шифр.Ключ(1, набор, мастер), свой_aes=True)
        их = pylibsrtp.Session(policy=pylibsrtp.Policy(
            key=мастер, ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND, srtp_profile=профиль))
        for н in range(0xFFF8, 0x10008):
            пакет = _rtp(н, os.urandom(160))
            assert наш.защитить(пакет) == их.protect(пакет)


def test_строка_crypto_разбирается_и_собирается():
    ключ = шифр.новый_ключ(3)
    assert шифр.разобрать_крипто(ключ.строка) == ключ
    assert шифр.разобрать_крипто(ключ.строка + "|2^31") == ключ          # срок жизни
    assert шифр.разобрать_крипто(ключ.строка + "|2^31|1:4") is None      # MKI
    assert шифр.разобрать_крипто("1 AES_256_CM_HMAC_SHA1_80 inline:AAAA") is None
    assert шифр.разобрать_крипто("1 AES_CM_128_HMAC_SHA1_80 inline:QUJD") is None
    sdp = sip.предложение_sdp("10.0.0.5", 40000, крипто=ключ.строка)
    описание = sip.разобрать_sdp(sdp)
    assert b"RTP/SAVP" in sdp and описание["профиль"] == "RTP/SAVP"
    assert описание["крипто"] == [ключ] and описание["кодек"] == "PCMA"
    assert b"RTP/AVP " in sip.предложение_sdp("10.0.0.5", 40000)


# --- разговор по TLS и SRTP -----------------------------------------------------

def _сертификат(каталог: Path) -> tuple[str, str]:
    """Самоподписанный сертификат на 127.0.0.1 — cryptography или openssl."""
    серт, ключ = каталог / "pbx.crt", каталог / "pbx.key"
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        if shutil.which("openssl") is None:
            pytest.skip("нет ни cryptography, ни openssl для сертификата")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:P-256", "-nodes", "-days", "2",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(ключ), "-out", str(серт)],
                       check=True, capture_output=True, timeout=60)
        return str(серт), str(ключ)
    import ipaddress

    закрытый = ec.generate_private_key(ec.SECP256R1())
    имя = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    сейчас = datetime.datetime.now(datetime.timezone.utc)
    сертификат = (x509.CertificateBuilder().subject_name(имя).issuer_name(имя)
                  .public_key(закрытый.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(сейчас - datetime.timedelta(minutes=5))
                  .not_valid_after(сейчас + datetime.timedelta(days=2))
                  .add_extension(x509.SubjectAlternativeName(
                      [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                  .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                  .sign(закрытый, hashes.SHA256()))
    серт.write_bytes(сертификат.public_bytes(serialization.Encoding.PEM))
    ключ.write_bytes(закрытый.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    return str(серт), str(ключ)


class _Настройки:
    def get(self, ключ, запасное=None):
        return {"qc_tts_command": ГОЛОС}.get(ключ, запасное)


def _опрос(телефон: sip.Телефон, tmp_path: Path) -> tuple[dict, sip.Разговор]:
    группа = survey.разобрать_группы([ГРУППА])[0]
    звуки = {т: tts.приготовить(т, _Настройки(), tmp_path / "tts") for т in группа.фразы()}
    assert телефон.зарегистрировать() == 300
    разговор = телефон.позвонить("989991112233")
    итог = survey.провести(разговор, группа, звуки, ждать_кнопку_с=2)
    разговор.положить()
    return итог, разговор


def test_опрос_по_tls_с_шифрованным_звуком(tmp_path):
    серт = _сертификат(tmp_path)
    станция = МиниАТС(транспорт="tls", сертификат=серт, srtp=True, кнопки=["4", "2"])
    телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900", пароль="secret",
                          транспорт="tls", файл_ца=серт[0], srtp=True)
    try:
        итог, разговор = _опрос(телефон, tmp_path)
    finally:
        телефон.закрыть()
        станция.закрыть()
    assert [о["digit"] for о in итог["answers"]] == [4, 2], (итог, станция.нажато)
    assert разговор.шифрован and разговор.srtp_приём.отвергнуто == 0
    # Одно соединение на всё: регистрация, вызов, BYE и снятие регистрации.
    assert станция.соединений == 1 and станция.снята_регистрация
    assert станция.получено_звука > 8000 and станция.открытых_пакетов == 0
    assert станция.srtp_приём is not None and станция.srtp_приём.отвергнуто == 0


def test_опрос_по_tcp_без_шифрования(tmp_path):
    станция = МиниАТС(транспорт="tcp", кнопки=["5", "1"])
    телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900", пароль="secret",
                          транспорт="tcp")
    try:
        итог, разговор = _опрос(телефон, tmp_path)
    finally:
        телефон.закрыть()
        станция.закрыть()
    assert [о["digit"] for о in итог["answers"]] == [5, 1]
    assert not разговор.шифрован and станция.открытых_пакетов > 0
    assert "INVITE" in станция.журнал and станция.журнал.count("INVITE") == 2   # 401 и с паролем


def test_чужой_сертификат_станции_отвергается_с_подсказкой(tmp_path):
    серт = _сертификат(tmp_path)
    станция = МиниАТС(транспорт="tls", сертификат=серт)
    try:
        with pytest.raises(sip.ОшибкаSIP, match="qc_sip_ca_file"):
            sip.Телефон("127.0.0.1", станция.порт, пользователь="900", транспорт="tls")
        # Без проверки — соединяется (только для настройки).
        телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900",
                              пароль="secret", транспорт="tls", проверять_сертификат=False)
        try:
            assert телефон.зарегистрировать() == 300
        finally:
            телефон.закрыть()
    finally:
        станция.закрыть()


def test_станция_без_шифрования_получает_отбой(tmp_path):
    станция = МиниАТС(кнопки=["1"])                     # отвечает открытым RTP/AVP
    телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900", пароль="secret",
                          srtp=True)
    try:
        телефон.зарегистрировать()
        with pytest.raises(sip.ОшибкаSIP, match="SRTP") as сбой:
            телефон.позвонить("989991112233")
        assert сбой.value.код == 488
        assert станция.bye_от_телефона.wait(3)
    finally:
        телефон.закрыть()
        станция.закрыть()


def test_tls_с_портом_5060_идёт_на_5061_и_транспорт_в_адресах():
    телефон = sip.Телефон.__new__(sip.Телефон)
    телефон.транспорт, телефон.узел, телефон.порт = "tls", "pbx.local", 5061
    телефон.пользователь, телефон.местный, телефон.местный_порт = "900", "10.0.0.2", 40123
    assert телефон.адрес_станции("200") == "sip:200@pbx.local:5061;transport=tls"
    assert телефон._контакт == "<sip:900@10.0.0.2:40123;transport=tls>"
    assert телефон._via().startswith("SIP/2.0/TLS 10.0.0.2:40123;")
    with pytest.raises(sip.ОшибкаSIP, match="не поддерживается"):
        sip.Телефон("127.0.0.1", 5060, пользователь="900", транспорт="sctp")


def test_настройки_доходят_до_телефона():
    from asrhub.catalog.params import PARAMS

    ключи = {п.key for п in PARAMS}
    assert {"qc_sip_transport", "qc_sip_tls_verify", "qc_sip_ca_file", "qc_srtp"} <= ключи
    исходник = Path(survey.__file__).read_text(encoding="utf-8")
    for ключ in ("qc_sip_transport", "qc_sip_tls_verify", "qc_sip_ca_file", "qc_srtp"):
        assert исходник.count(f'"{ключ}"') >= 2, ключ     # и в подписи телефона, и в вызове


@pytest.mark.parametrize("srtp_обязателен", [False, True])
def test_входящий_шифрованный_вызов_получает_свой_ключ(srtp_обязателен):
    """Станция переводит клиента с RTP/SAVP и ключом — телефон отвечает своим
    ключом с тем же тегом; без ключа при qc_srtp — 488, а не открытый звук."""
    import socket
    import threading
    import time
    import uuid

    станция = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    станция.bind(("127.0.0.1", 0))
    станция.settimeout(3)
    порт = станция.getsockname()[1]
    телефон = sip.Телефон("127.0.0.1", порт, пользователь="900", пароль="x",
                          местный="127.0.0.1", srtp=srtp_обязателен)
    готово = threading.Event()
    разговоры: list[sip.Разговор] = []
    телефон.входящие = lambda разговор, _запрос: (разговоры.append(разговор), готово.set())
    адрес_телефона = ("127.0.0.1", телефон._сокет.getsockname()[1])

    def пригласить(крипто: str) -> sip.Сообщение:
        тело = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio 40000 {'RTP/SAVP' if крипто else 'RTP/AVP'} 8 101\r\n"
                "a=rtpmap:8 PCMA/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
                + (f"a=crypto:{крипто}\r\n" if крипто else "")).encode()
        станция.sendto(sip.собрать("INVITE sip:900@127.0.0.1 SIP/2.0", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{порт};branch=z9hG4bK{uuid.uuid4().hex[:8]}"),
            ("From", "<sip:79991234567@127.0.0.1>;tag=pbx1"), ("To", "<sip:900@127.0.0.1>"),
            ("Call-ID", f"{uuid.uuid4().hex}@pbx"), ("CSeq", "1 INVITE"),
            ("Contact", f"<sip:pbx@127.0.0.1:{порт}>"),
            ("Content-Type", "application/sdp")], тело), адрес_телефона)
        конец = time.time() + 3
        while time.time() < конец:
            сообщение = sip.разобрать(станция.recvfrom(65535)[0])
            if сообщение is not None and сообщение.ответ and сообщение.код >= 200:
                return сообщение
        raise AssertionError("нет окончательного ответа")

    try:
        ключ_станции = шифр.новый_ключ(2, "AES_CM_128_HMAC_SHA1_32")
        ответ = пригласить(ключ_станции.строка)
        assert ответ.код == 200
        описание = sip.разобрать_sdp(ответ.тело)
        assert описание["профиль"] == "RTP/SAVP"
        (наш,) = описание["крипто"]
        assert (наш.тег, наш.набор) == (2, "AES_CM_128_HMAC_SHA1_32")
        assert наш.мастер != ключ_станции.мастер
        assert готово.wait(3) and разговоры[0].шифрован
        assert разговоры[0].srtp_приём.ключ == ключ_станции
        разговоры[0].завершён.set()
        открытый = пригласить("")
        assert открытый.код == (488 if srtp_обязателен else 200)
    finally:
        for разговор in разговоры:
            разговор.завершён.set()
        телефон.закрыть()
        станция.close()


# --- служба синтеза -------------------------------------------------------------

_ЗВУК_WAV = '''
import math, struct, wave
def записать(путь, частота=440.0, секунд=0.4):
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1); файл.setsampwidth(2); файл.setframerate(22050)
        файл.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * частота * н / 22050)))
                                  for н in range(int(22050 * секунд))))
'''


@pytest.fixture()
def без_служб():
    yield
    tts.остановить_службы()
    tts._СЛУЖБЫ.clear()


def test_своя_служба_синтеза_один_процесс_на_все_фразы(tmp_path, без_служб):
    журнал = tmp_path / "pids.txt"
    скрипт = tmp_path / "service.py"
    скрипт.write_text(_ЗВУК_WAV + f'''
import json, os, sys
print("загрузка модели…")                      # посторонний вывод не мешает обмену
print(json.dumps({{"ready": True, "device": "cpu"}}), flush=True)
for строка in sys.stdin:
    запрос = json.loads(строка)
    open({str(журнал)!r}, "a").write(f"{{os.getpid()}} {{запрос['voice']}}\\n")
    записать(запрос["out"])
    print(json.dumps({{"ok": True}}), flush=True)
''', encoding="utf-8")

    class Настройки:
        def get(self, ключ, запасное=None):
            return {"qc_tts_service_command": f"{sys.executable} {скрипт}",
                    "qc_tts_voice": "irina"}.get(ключ, запасное)

    описание = tts.движок(Настройки())
    assert описание["available"] and описание["service"] and описание["name"] == "своя служба синтеза"
    фразы = [tts.приготовить(т, Настройки(), tmp_path / "кеш")
             for т in ("Здравствуйте.", "Оцените разговор.", "Спасибо.")]
    assert all(ф.секунд > 0.3 for ф in фразы)
    строки = журнал.read_text().split("\n")[:-1]
    assert len(строки) == 3 and len({с.split()[0] for с in строки}) == 1   # один процесс
    assert all(с.endswith(" irina") for с in строки)


def _поддельный_coqui(каталог: Path, *, сломан: bool = False) -> Path:
    """Пакеты TTS.api и torch для службы и программа `tts` с нашим Python."""
    пакеты = каталог / "пакеты"
    (пакеты / "TTS").mkdir(parents=True)
    (пакеты / "TTS" / "__init__.py").write_text("", encoding="utf-8")
    (пакеты / "torch").mkdir()
    (пакеты / "torch" / "__init__.py").write_text(
        "class cuda:\n    @staticmethod\n    def is_available():\n        return False\n",
        encoding="utf-8")
    журнал = каталог / "coqui.log"
    (пакеты / "TTS" / "api.py").write_text(_ЗВУК_WAV + f'''
import os
{"raise ImportError('нет модели')" if сломан else ""}
class TTS:
    def __init__(self, имя):
        open({str(журнал)!r}, "a").write(f"load {{имя}} {{os.getpid()}}\\n")
        print("Downloading model…")             # библиотека печатает в stdout
    def to(self, устройство):
        return self
    def tts_to_file(self, text, file_path, speaker_wav=None, language=None):
        open({str(журнал)!r}, "a").write(f"say {{language}} {{os.path.basename(speaker_wav)}}\\n")
        записать(file_path)
''', encoding="utf-8")
    программы = каталог / "bin"
    программы.mkdir()
    # Запасной путь — сама программа `tts` на каждую фразу: пишет WAV в --out_path.
    (программы / "tts").write_text(f"#!{sys.executable}\n" + _ЗВУК_WAV + f'''
import sys
open({str(журнал)!r}, "a").write("cli\\n")
записать(sys.argv[sys.argv.index("--out_path") + 1])
''', encoding="utf-8")
    (программы / "tts").chmod(0o755)
    return пакеты


def _образец(кеш: Path) -> None:
    каталог = tts.каталог_образца(кеш)
    каталог.mkdir(parents=True)
    import wave

    with wave.open(str(каталог / "sample.wav"), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(22050)
        файл.writeframes(b"\x01\x00" * 22050 * 4)


@pytest.mark.parametrize("сломан", [False, True])
def test_xtts_службой_модель_грузится_один_раз(tmp_path, monkeypatch, без_служб, сломан):
    пакеты = _поддельный_coqui(tmp_path, сломан=сломан)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(пакеты))
    кеш = tmp_path / "qc" / "tts"
    _образец(кеш)

    class Настройки:
        def get(self, ключ, запасное=None):
            return запасное

    описание = tts.движок(Настройки(), tts.образец(кеш))
    assert описание["clone"] and описание["service"][0] == sys.executable
    assert описание["service"][1].endswith("tts_service.py")
    for текст in ("Здравствуйте.", "Оцените разговор."):
        assert tts.приготовить(текст, Настройки(), кеш).секунд > 0.3
    журнал = (tmp_path / "coqui.log").read_text().split("\n")[:-1]
    if сломан:
        # Служба не поднялась — фразы командой, и вторая попытка службы не мучает.
        assert журнал == ["cli", "cli"]
        assert tts.служба(описание["service"]).запусков == 1
    else:
        assert журнал[0].startswith("load tts_models/multilingual/multi-dataset/xtts_v2")
        assert журнал[1:] == ["say ru sample.wav", "say ru sample.wav"]
        assert sum(с.startswith("load") for с in журнал) == 1


# --- живой разговор двух людей ---------------------------------------------------

def test_рецепт_живого_разговора_собирается_по_адресу_приёма():
    from asrhub.telephony import dialplan_config as К

    assert "live" not in [ф["id"] for ф in К.собрать(К.параметры())["files"]]
    п = К.параметры(live="10.0.5.2:9092")
    файлы = {ф["id"]: ф for ф in К.собрать(п)["files"]}
    текст = файлы["live"]["text"]
    assert "ВНИМАНИЕ" in текст and "не проверено" in файлы["live"]["title"]
    assert "AudioSocket,${UUID()}\\,10.0.5.2:9092" in текст      # запятая данных экранирована
    assert "Set(SPYGROUP=asrhub${ASRHUB_LIVE})" in текст
    assert "ChanSpy(,qEg(asrhub${EXTEN}))" in текст
    for плохой in ("10.0.5.2", "host:0", "a b:9092", "x:99999", "x;y:1"):
        with pytest.raises(ValueError):
            К.параметры(live=плохой)


# --- найдено проверкой типов --------------------------------------------------------

def test_чтение_событий_ami_не_затенено_полем():
    """Метод `события` затенялся одноимённым полем (классы событий входа)."""
    import threading

    from asrhub.telephony.asterisk import AMIClient

    клиент = AMIClient("127.0.0.1", 5038, "u", "p", события="cdr,agent")
    assert клиент.события == "cdr,agent" and callable(клиент.читать_события)
    пакеты = iter([{"Event": "Cdr"}, {}, None])
    клиент.пакет = lambda timeout=1.0: next(пакеты) or {}   # type: ignore[method-assign]
    стоп, пришло = threading.Event(), []

    def событие(данные):
        пришло.append(данные)
        стоп.set()

    клиент.читать_события(стоп, on_event=событие, idle=0.01)
    assert пришло == [{"Event": "Cdr"}]


def test_предел_размера_с_дробью_пишется_как_есть():
    from asrhub.errors import FileTooLarge

    assert "предел 0.5 МБ" in FileTooLarge(1.2, 0.5).message
    assert "предел 2048 МБ" in FileTooLarge(3000, 2048).message
