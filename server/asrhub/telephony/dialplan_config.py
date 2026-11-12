"""Готовые куски настройки Asterisk для приёма звонков из диалплана.

Раздел «Asterisk: диалплан» показывает не общие слова, а файлы, которые
можно скопировать на станцию как есть: подключаемый диалплан, пример
вызова из своих контекстов, строку для queues.conf, настройки сценария
отправки и команды проверки. Всё это собирается здесь — из адреса
сервера, станции, драйвера (chan_sip или chan_pjsip) и способа передачи
записи, — чтобы интерфейс, документация и проверки видели одно и то же.

Два способа передачи:

* `upload` — запись уходит файлом. Обработчик отбоя зовёт сценарий
  `asrhub-dialplan`; тот сразу уходит в фон, ждёт, пока MixMonitor закроет
  файл, и отправляет звонок с записью, а при недоступном сервере
  повторяет и копит очередь повтора. Годится всегда: серверу распознавания
  не нужен доступ к дискам станции.
* `path` — запись не передаётся: сервер читает её с общего диска (та же
  машина, NFS, SMB). Сообщает о звонке сама функция CURL() из обработчика
  отбоя — без оболочки и без сценария; запись, которая в этот момент ещё
  дописывается, сервер дозабирает сам.

Безопасность — главное, что здесь нельзя упростить. Всё, что приходит
снаружи (номер и имя звонящего, набранный номер, заголовки SIP), в
командную строку System() попадает только через FILTER(): иначе номер
вида «1;rm -rf /» выполнился бы на станции с правами Asterisk. В CURL()
оболочки нет, и там значения кодируются URIENCODE() — чтобы запятая или
амперсанд не разломали запрос.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Драйверы SIP, для которых собираются примеры.
ДРАЙВЕРЫ = {"pjsip": "chan_pjsip", "sip": "chan_sip"}

#: Способы передачи записи.
СПОСОБЫ = {"upload": "файлом через сценарий отправки",
           "path": "путём на общем диске (CURL)"}

#: Форматы записи MixMonitor. wav — несжатые 16 бит, 8 кГц: лучшая точность
#: распознавания при 128 кбит/с. WAV (он же wav49) — GSM в обёртке WAV, в
#: пять раз меньше, но распознаётся заметно хуже: GSM съедает согласные.
ФОРМАТЫ = {"wav": "wav — несжатый, лучшая точность",
           "WAV": "WAV (wav49) — GSM, в 5 раз меньше, точность ниже",
           "gsm": "gsm — сжатый, только если места совсем нет"}

#: Подкаталог записей внутри каталога MixMonitor: так записи ASR Hub не
#: смешиваются с теми, что станция пишет по своим правилам.
ПОДКАТАЛОГ = "asrhub"

#: Адрес приёма на сервере.
ПРИЁМ = "/api/telephony/dialplan/call"

#: Что безопасно подставлять в настройку как есть. Адрес сервера, станция и
#: каталог вводит человек в форме раздела, но в файлы для станции они
#: попадают текстом — и перевод строки в адресе дописал бы в диалплан
#: произвольную строку.
_АДРЕС = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~\-/]*)?$")
_СТАНЦИЯ = re.compile(r"^[A-Za-z0-9._\-]{0,64}$")
_КАТАЛОГ = re.compile(r"^/[A-Za-z0-9._\-/]{0,200}$")
_ИМЯ = re.compile(r"^[A-Za-z0-9._\-]{1,40}$")


@dataclass
class Параметры:
    """Что человек выбрал в разделе — всё уже проверенное."""

    driver: str = "pjsip"
    mode: str = "upload"
    url: str = "http://asr.example.ru:8081"
    station: str = ""
    base: str = "/var/spool/asterisk/monitor"
    format: str = "wav"
    key_in: str = "header"
    trunk: str = "provider"
    internal_len: int = 3
    #: Писать стороны раздельно: MixMonitor r()/t() — клиент и оператор в
    #: своих файлах. Сервер сводит их в стерео и знает «кто говорит» точно.
    tracks: bool = False
    #: Код «не записывать» посреди разговора (`features.conf`); пусто — без него.
    norec_code: str = ""
    #: Живой разговор двух людей в ASR Hub по AudioSocket: «узел:порт»
    #: приёма (audiosocket_host/port сервера); пусто — без него.
    live: str = ""


def параметры(**сырые: Any) -> Параметры:
    """Проверяет выбор человека; негодное значение — ValueError с объяснением."""
    п = Параметры()
    драйвер = str(сырые.get("driver") or п.driver).strip().lower()
    if драйвер in ("chan_pjsip",):
        драйвер = "pjsip"
    if драйвер in ("chan_sip",):
        драйвер = "sip"
    if драйвер not in ДРАЙВЕРЫ:
        raise ValueError("драйвер — pjsip или sip")
    п.driver = драйвер
    способ = str(сырые.get("mode") or п.mode).strip().lower()
    if способ not in СПОСОБЫ:
        raise ValueError("способ передачи — upload или path")
    п.mode = способ
    адрес = str(сырые.get("url") or п.url).strip().rstrip("/")
    if not _АДРЕС.match(адрес):
        raise ValueError("адрес сервера — вида http://сервер:8081 или https://asr.example.ru")
    п.url = адрес
    станция = str(сырые.get("station") or "").strip()
    if not _СТАНЦИЯ.match(станция):
        raise ValueError("идентификатор станции — латиница, цифры, точка, дефис")
    п.station = станция
    каталог = str(сырые.get("base") or п.base).strip().rstrip("/") or п.base
    if not _КАТАЛОГ.match(каталог) or ".." in каталог.split("/"):
        raise ValueError("каталог записей — абсолютный путь из латиницы, цифр, точек и дефисов")
    п.base = каталог
    формат = str(сырые.get("format") or п.format).strip()
    if формат not in ФОРМАТЫ:
        raise ValueError("формат записи — wav, WAV или gsm")
    п.format = формат
    ключ = str(сырые.get("key_in") or п.key_in).strip().lower()
    if ключ not in ("header", "query"):
        raise ValueError("ключ доступа — в заголовке (header) или в адресе (query)")
    п.key_in = ключ
    транк = str(сырые.get("trunk") or п.trunk).strip()
    if not _ИМЯ.match(транк):
        raise ValueError("имя транка — латиница, цифры, точка, дефис")
    п.trunk = транк
    try:
        длина = int(сырые.get("internal_len") or п.internal_len)
    except (TypeError, ValueError) as exc:
        raise ValueError("длина внутреннего номера — число от 2 до 6") from exc
    if not 2 <= длина <= 6:
        raise ValueError("длина внутреннего номера — число от 2 до 6")
    п.internal_len = длина
    п.tracks = str(сырые.get("tracks") or "").strip().lower() in ("1", "true", "yes", "on")
    код = str(сырые.get("norec_code") or "").strip()
    if код and not re.fullmatch(r"[0-9*#]{1,4}", код):
        raise ValueError("код «не записывать» — до четырёх знаков из 0–9, * и #")
    п.norec_code = код
    if п.tracks and п.mode != "upload":
        raise ValueError("раздельные дорожки — только при отправке файлом (upload)")
    живой = str(сырые.get("live") or "").strip()
    if живой and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}:\d{1,5}", живой):
        raise ValueError("приём живого разговора — узел:порт, например 10.0.5.2:9092")
    if живой and not 0 < int(живой.rsplit(":", 1)[1]) < 65536:
        raise ValueError("порт приёма живого разговора — от 1 до 65535")
    п.live = живой
    return п


def живой_разговор(п: Параметры) -> str:
    """Оба голоса разговора — в ASR Hub на лету: ChanSpy через Local-канал."""
    return f"""\
; Живой разговор двух людей — в ASR Hub по AudioSocket (Asterisk 16.6+).
;
; ВНИМАНИЕ: рецепт собран по документации Asterisk и на живой станции не
; проверялся. Сначала — пробный звонок на тестовом номере.
;
; AudioSocket() забирает звук канала себе, поэтому разговор слушает «шпион»:
; Local-канал, одна половина которого слушает разговор (ChanSpy — обе
; стороны вместе), а другая отдаёт услышанное в AudioSocket. Свой разговор
; шпион находит по группе SPYGROUP — чужие он не слышит.
;
; Вызов — на канале разговора до Dial() или Queue() (рядом с asrhub-record):
;   same => n,Gosub(asrhub-live,s,1)
; На сервере: audiosocket_port (и audiosocket_host — {п.live.rsplit(":", 1)[0]}),
; события call.live приходят в веб-интерфейс и по веб-сокету.

[asrhub-live]
exten => s,1,Set(ASRHUB_LIVE=${{FILTER(0-9,${{UNIQUEID}})}})
 same => n,Set(SPYGROUP=asrhub${{ASRHUB_LIVE}})
 ; a — не ждать ответа шпиона: разговор идёт своим чередом.
 same => n,Originate(Local/${{ASRHUB_LIVE}}@asrhub-live-spy/n,app,AudioSocket,${{UUID()}}\\,{п.live},,,a)
 same => n,Return()

[asrhub-live-spy]
; q — без объявлений, E — выйти, когда разговор кончился, g() — только свой.
exten => _X.,1,Answer()
 same => n,ChanSpy(,qEg(asrhub${{EXTEN}}))
 same => n,Hangup()
"""


# ---------------------------------------------------------------------------
# Различия драйверов
# ---------------------------------------------------------------------------

#: То, чем chan_sip и chan_pjsip различаются для этой задачи. Таблица общая
#: для раздела и документации: расхождение между ними — это ровно та ошибка,
#: из-за которой у человека «по инструкции не работает».
РАЗЛИЧИЯ = [
    ("Модуль и файл настройки", "chan_sip, sip.conf", "chan_pjsip, pjsip.conf"),
    ("Вызов сотрудника", "Dial(SIP/101)", "Dial(PJSIP/101); на все его телефоны — "
     "Dial(${PJSIP_DIAL_CONTACTS(101)})"),
    ("Вызов через транк", "Dial(SIP/provider/${EXTEN})", "Dial(PJSIP/${EXTEN}@provider)"),
    ("Кто звонит (имя абонента)", "${CHANNEL(peername)}", "${CHANNEL(endpoint)}"),
    ("Имя канала", "SIP/101-0000002a", "PJSIP/101-0000002a"),
    ("Кто ответил (после Dial())", "${CUT(CUT(DIALEDPEERNAME,/,2),-,1)}",
     "${CUT(CUT(DIALEDPEERNAME,/,2),-,1)} — то же самое"),
    ("Заголовок SIP входящего", "${SIP_HEADER(X-CRM-Id)}", "${PJSIP_HEADER(read,X-CRM-Id)}"),
    ("Адрес телефона", "${CHANNEL(peerip)}", "${CHANNEL(pjsip,remote_addr)}"),
    ("Участник очереди", "member => SIP/101", "member => PJSIP/101"),
    ("Где есть", "Asterisk 16–20 (устарел с 17-й, удалён в 21-й)",
     "Asterisk 12 и новее; единственный в 21+"),
]


def _агент(п: Параметры) -> str:
    """Строки диалплана, вычисляющие добавочный в обработчике отбоя."""
    свой = "${CHANNEL(endpoint)}" if п.driver == "pjsip" else "${CHANNEL(peername)}"
    имя = "оконечная точка PJSIP" if п.driver == "pjsip" else "абонент sip.conf"
    префикс = "PJSIP" if п.driver == "pjsip" else "SIP"
    return f"""\
 ; Добавочный сотрудника. Исходящий и внутренний — кто звонил ({имя}
 ; этого канала). Входящий — кто ответил: имя соединённого канала
 ; ({префикс}/101-0000002a → 101), а в очереди — участник, ответивший на звонок
 ; ({префикс}/101 или Local/101@from-queue → 101; нужен setinterfacevar=yes).
 same => n,Set(ASRHUB_AGENT={свой})
 same => n,ExecIf($["${{ASRHUB_DIRECTION}}" = "in"]?Set(ASRHUB_AGENT=${{CUT(CUT(DIALEDPEERNAME,/,2),-,1)}}))
 same => n,ExecIf($["${{MEMBERINTERFACE}}" != ""]?Set(ASRHUB_AGENT=${{CUT(CUT(MEMBERINTERFACE,/,2),@,1)}}))
"""


def _запись(п: Параметры) -> str:
    """Подпрограмма начала записи — общая для обоих способов."""
    return f"""\
; ---------------------------------------------------------------------------
; Начать запись разговора и подготовить его передачу в ASR Hub.
; Вызывайте ДО Dial() или Queue():
;   same => n,Gosub(asrhub-record,s,1(<направление>,<номер>[,<очередь>[,<метка>]]))
; направление — in (входящий), out (исходящий) или internal (внутренний);
; номер — набранный номер: ${{EXTEN}} вызывающего контекста (внутри
; подпрограммы ${{EXTEN}} уже «s»); очередь — имя очереди для Queue();
; метка — что угодно для поиска (номер сделки из заголовка SIP и т. п.).
; ---------------------------------------------------------------------------
[asrhub-record]
exten => s,1,NoOp(ASR Hub: запись ${{UNIQUEID}}, ${{ARG1}}, ${{ARG2}})
 ; Звонок уже пишется: он вернулся в контекст (перевод, Local-канал
 ; участника очереди). Вторая запись того же разговора не нужна.
 same => n,GotoIf($["${{ASRHUB_UID}}" != ""]?done)
 same => n,Set(__ASRHUB_UID=${{UNIQUEID}})
 same => n,Set(ASRHUB_DIRECTION=${{ARG1}})
 same => n,Set(ASRHUB_DST=${{ARG2}})
 same => n,Set(ASRHUB_QUEUE=${{ARG3}})
 same => n,Set(ASRHUB_TAG=${{ARG4}})
 same => n,Set(ASRHUB_STARTED=${{EPOCH}})
{_код_в_разговоре(п)} same => n,Set(ASRHUB_REL={ПОДКАТАЛОГ}/${{STRFTIME(${{EPOCH}},,%Y/%m/%d)}}/${{UNIQUEID}}.{п.format})
 ; «Не записывать»: согласие не дано (см. asrhub-norec) — звонок уйдёт в
 ; журнал без записи.
 same => n,GotoIf($["${{ASRHUB_RECORD}}" = "0"]?handler)
{_мониктор(п)} ; Передача — в обработчике отбоя: к нему уже известны длительность и исход.
 same => n(handler),Set(CHANNEL(hangup_handler_push)=asrhub-hangup,s,1)
 same => n(done),Return()

; ---------------------------------------------------------------------------
; «Не записывать этот звонок»: вызовите до asrhub-record (например, в голосовом
; меню «нажмите 1, если не согласны на запись»), а посреди разговора —
; чтобы остановить уже идущую запись. Звонок попадёт в журнал ASR Hub без
; записи с пометкой «не записывать», запись (если началась) не отправится.
;   same => n,Gosub(asrhub-norec,s,1)
; ---------------------------------------------------------------------------
[asrhub-norec]
exten => s,1,Set(__ASRHUB_RECORD=0)
 same => n,ExecIf($["${{ASRHUB_UID}}" != ""]?StopMixMonitor())
 same => n,Return()

; Метка звонка для поиска и меток задания (номер сделки, тема): из голосового
; меню или по заголовку SIP — до или во время разговора.
;   same => n,Gosub(asrhub-tag,s,1(<метка>))
[asrhub-tag]
exten => s,1,Set(__ASRHUB_TAG2=${{FILTER(0-9A-Za-z_.-,${{ARG1}})}})
 same => n,Return()
"""


def _код_в_разговоре(п: Параметры) -> str:
    """Включить код «не записывать» в этом разговоре (DYNAMIC_FEATURES)."""
    if not п.norec_code:
        return ""
    return (" ; Код «не записывать» в разговоре (features.conf, раздел applicationmap):\n"
            " ; у входящего его набирает оператор (callee), у исходящего — он же (caller).\n"
            " same => n,Set(__DYNAMIC_FEATURES=${DYNAMIC_FEATURES}#"
            "${IF($[\"${ARG1}\" = \"out\"]?asrhub_norec_out:asrhub_norec_in)})\n")


def коды_в_разговоре(п: Параметры) -> str:
    """Раздел applicationmap для features.conf: код «не записывать»."""
    return f"""\
; Код «не записывать» посреди разговора: оператор набирает {п.norec_code} — запись
; останавливается, звонок уходит в ASR Hub без неё с пометкой «не записывать».
; FreePBX: файл features_applicationmap_custom.conf; голый Asterisk — раздел
; [applicationmap] в features.conf. После правки: asterisk -rx "module reload features".
;
; MixMonitor идёт на канале, где вызван asrhub-record: у входящего это канал
; клиента (peer для оператора-callee), у исходящего — канал самого оператора.
[applicationmap]
asrhub_norec_in => {п.norec_code},peer/callee,Gosub(asrhub-norec,s,1)
asrhub_norec_out => {п.norec_code},self/caller,Gosub(asrhub-norec,s,1)
"""


def _мониктор(п: Параметры) -> str:
    """Строки MixMonitor: одной смесью или ещё и сторонами раздельно."""
    if not п.tracks:
        return (" ; b — писать только соединённый разговор: без гудков, музыки ожидания и\n"
                " ; голосового меню. Каталоги по датам MixMonitor создаёт сам.\n"
                " same => n,MixMonitor(${ASRHUB_BASE}/${ASRHUB_REL},b)\n")
    основа = f"{ПОДКАТАЛОГ}/${{STRFTIME(${{EPOCH}},,%Y/%m/%d)}}/${{UNIQUEID}}"
    return (f" same => n,Set(ASRHUB_REL_IN={основа}-in.{п.format})\n"
            f" same => n,Set(ASRHUB_REL_OUT={основа}-out.{п.format})\n"
            " ; b — только соединённый разговор; r() — что слышит станция (клиент),\n"
            " ; t() — что станция отдаёт в канал (оператор). Смесь пишется тоже:\n"
            " ; её слушают в карточке, если стороны потеряются.\n"
            " same => n,MixMonitor(${ASRHUB_BASE}/${ASRHUB_REL},b"
            "r(${ASRHUB_BASE}/${ASRHUB_REL_IN})t(${ASRHUB_BASE}/${ASRHUB_REL_OUT}))\n")


def _отбой_файлом(п: Параметры) -> str:
    return f"""\
; ---------------------------------------------------------------------------
; Обработчик отбоя: передать звонок сценарию отправки. Сценарий сразу уходит
; в фон — канал не ждёт ни сети, ни сервера, — дожидается, пока MixMonitor
; закроет файл, и отправляет запись. Сервер недоступен — повторяет, потом
; копит очередь повтора (asrhub-dialplan --resend).
;
; Всё, что приходит снаружи, — только через FILTER(): значения попадают в
; командную строку, и без фильтра номер звонящего «1;rm -rf /» выполнился бы
; на станции. Фильтр оставляет цифры, латиницу и «+._» — пробела, кавычек,
; «;», «&», «$» и «`» в аргументах не бывает. В идентификаторе звонка — ещё
; «-»: с systemname в asterisk.conf ${{UNIQUEID}} выглядит как
; «pbx-1-1700000000.12». «--.» в начале фильтра — диапазон от «-» до «.»:
; одиночный дефис FILTER() читает как знак диапазона.
; ---------------------------------------------------------------------------
[asrhub-hangup]
exten => s,1,NoOp(ASR Hub: отправка ${{ASRHUB_UID}})
{_агент(п)} same => n,System(${{ASRHUB_SEND}} uniqueid=${{FILTER(--.0-9A-Za-z_,${{ASRHUB_UID}})}} linkedid=${{FILTER(--.0-9A-Za-z_,${{CDR(linkedid)}})}} sequence=${{FILTER(0-9,${{CDR(sequence)}})}} direction=${{FILTER(a-z,${{ASRHUB_DIRECTION}})}} src=${{FILTER(0-9+,${{CALLERID(num)}})}} dst=${{FILTER(0-9+,${{ASRHUB_DST}})}} agent=${{FILTER(0-9A-Za-z_.,${{ASRHUB_AGENT}})}} queue=${{FILTER(0-9A-Za-z_.,${{ASRHUB_QUEUE}})}} userfield=${{FILTER(0-9A-Za-z_.,${{ASRHUB_TAG}})}} started_at=${{FILTER(0-9,${{ASRHUB_STARTED}})}} duration=${{FILTER(0-9,${{CDR(duration)}})}} billsec=${{FILTER(0-9,${{CDR(billsec)}})}} disposition=${{FILTER(A-Z,${{CDR(disposition)}})}} record=${{FILTER(0-9,${{ASRHUB_RECORD}})}} tag=${{FILTER(0-9A-Za-z_.,${{ASRHUB_TAG2}})}} file=${{ASRHUB_BASE}}/${{ASRHUB_REL}}{_дорожки_в_отбой(п)})
 same => n,Return()
"""


def _дорожки_в_отбой(п: Параметры) -> str:
    if not п.tracks:
        return ""
    return " file_in=${ASRHUB_BASE}/${ASRHUB_REL_IN} file_out=${ASRHUB_BASE}/${ASRHUB_REL_OUT}"


def _отбой_путём(п: Параметры) -> str:
    заголовок = (
        " ; Ключ — заголовком (Asterisk 16.7, 17.1, 13.30 и новее). Заголовки\n"
        " ; CURLOPT добавляются к каналу и не заменяются: ставим их один раз.\n"
        " same => n,Set(CURLOPT(httpheader)=X-API-Key: ${ASRHUB_KEY})\n"
        " ; Любой свой заголовок снимает Content-Type по умолчанию — возвращаем.\n"
        " same => n,Set(CURLOPT(httpheader)=Content-Type: application/x-www-form-urlencoded)\n"
        if п.key_in == "header" else
        " ; Ключ — в адресе (ASRHUB_URL): для Asterisk старше 16.7, где у\n"
        " ; CURLOPT нет httpheader. Адрес с ключом виден в «dialplan show globals».\n")
    return f"""\
; ---------------------------------------------------------------------------
; Обработчик отбоя: сообщить серверу о звонке функцией CURL(). Оболочки здесь
; нет, сценарий не нужен; значения кодируются URIENCODE(), чтобы запятая или
; «&» в номере не разломали запрос. Запись сервер читает сам с общего диска
; (путь — относительно ASRHUB_BASE) и, если она ещё дописывается,
; дозабирает её позже. Канал ждёт ответа сервера не дольше httptimeout.
; ---------------------------------------------------------------------------
[asrhub-hangup]
exten => s,1,NoOp(ASR Hub: передача ${{ASRHUB_UID}})
{_агент(п)} same => n,Set(CURLOPT(conntimeout)=3)
 same => n,Set(CURLOPT(httptimeout)=10)
{заголовок} same => n,Set(ASRHUB_ANSWER=${{CURL(${{ASRHUB_URL}},uniqueid=${{URIENCODE(${{ASRHUB_UID}})}}&direction=${{ASRHUB_DIRECTION}}&src=${{URIENCODE(${{CALLERID(num)}})}}&dst=${{URIENCODE(${{ASRHUB_DST}})}}&agent=${{URIENCODE(${{ASRHUB_AGENT}})}}&queue=${{URIENCODE(${{ASRHUB_QUEUE}})}}&userfield=${{URIENCODE(${{ASRHUB_TAG}})}}&started_at=${{ASRHUB_STARTED}}&duration=${{CDR(duration)}}&billsec=${{CDR(billsec)}}&disposition=${{URIENCODE(${{CDR(disposition)}})}}&record=${{ASRHUB_RECORD}}&tag=${{URIENCODE(${{ASRHUB_TAG2}})}}&recording=${{URIENCODE(${{ASRHUB_REL}})}})}})
 same => n,NoOp(ASR Hub ответил: ${{ASRHUB_ANSWER}})
 same => n,Return()
"""


def диалплан(п: Параметры, версия: str = "") -> str:
    """Подключаемый файл extensions_asrhub.conf целиком."""
    драйвер = ДРАЙВЕРЫ[п.driver]
    адрес = f"{п.url}{ПРИЁМ}"
    if п.station:
        адрес += f"?station={п.station}"
    if п.mode == "path" and п.key_in == "query":
        адрес += ("&" if "?" in адрес else "?") + "api_key=ah_ВАШ_КЛЮЧ"
    глобальные = [
        "[globals]",
        "; Каталог MixMonitor (astspooldir/monitor). Записи ASR Hub ложатся в",
        f"; подкаталог {ПОДКАТАЛОГ}/ГГГГ/ММ/ДД; права на запись — у пользователя asterisk.",
        f"ASRHUB_BASE={п.base}",
    ]
    if п.mode == "upload":
        глобальные += [
            "; Сценарий отправки (раздел «Asterisk: диалплан» → «Сценарий отправки»).",
            "; Адрес сервера, ключ и станция — в /etc/asrhub/dialplan.conf, не здесь:",
            "; диалплан виден любому, кто может открыть консоль Asterisk.",
            "ASRHUB_SEND=/usr/local/bin/asrhub-dialplan",
        ]
        отбой = _отбой_файлом(п)
    else:
        глобальные += ["; Куда сообщать о звонках."]
        глобальные += [f"ASRHUB_URL={адрес}"]
        if п.key_in == "header":
            глобальные += [
                "; Ключ доступа ASR Hub с правом записи — отдельный, только для станции.",
                "ASRHUB_KEY=ah_ВАШ_КЛЮЧ",
            ]
        отбой = _отбой_путём(п)
    шапка = f"""\
; ===========================================================================
; ASR Hub: запись разговоров и передача их на распознавание
; Драйвер: {драйвер}; запись передаётся {СПОСОБЫ[п.mode]}.
; Собрано разделом «Asterisk: диалплан»{f' ASR Hub {версия}' if версия else ''}.
;
; Куда положить: /etc/asterisk/extensions_asrhub.conf
; Подключить:    в конец /etc/asterisk/extensions.conf — строку
;                    #include extensions_asrhub.conf
;                затем в консоли Asterisk: dialplan reload
; Проверить:     asterisk -rx "dialplan show asrhub-record"
;                asterisk -rx "dialplan show asrhub-hangup"
; ===========================================================================
"""
    return шапка + "\n" + "\n".join(глобальные) + "\n\n" + _запись(п) + "\n" + отбой


def контексты(п: Параметры) -> str:
    """Пример вызова из своих контекстов — с синтаксисом выбранного драйвера."""
    t = "PJSIP" if п.driver == "pjsip" else "SIP"
    наружу = (f"Dial(PJSIP/${{EXTEN}}@{п.trunk},60,tT)" if п.driver == "pjsip"
              else f"Dial(SIP/{п.trunk}/${{EXTEN}},60,tT)")
    внутренний = "X" * п.internal_len
    заголовок = ("${PJSIP_HEADER(read,X-CRM-Id)}" if п.driver == "pjsip"
                 else "${SIP_HEADER(X-CRM-Id)}")
    return f"""\
; Пример: как включить запись в ваших контекстах ({ДРАЙВЕРЫ[п.driver]}).
; Имена контекстов, номера и транк «{п.trunk}» замените своими. Главное —
; строка Gosub(asrhub-record,…) перед Dial() или Queue().

[from-trunk]                      ; входящие с городских линий
exten => _X.,1,NoOp(Входящий на ${{EXTEN}} от ${{CALLERID(num)}})
 same => n,Gosub(asrhub-record,s,1(in,${{EXTEN}}))
 same => n,Dial({t}/101&{t}/102,30,tT)
 same => n,Hangup()

; Входящий в очередь: имя очереди — третьим аргументом, а метка из заголовка
; SIP (если провайдер или CRM её присылает) — четвёртым.
exten => 74950000000,1,Gosub(asrhub-record,s,1(in,${{EXTEN}},sales,{заголовок}))
 same => n,Queue(sales,tT)
 same => n,Hangup()

[from-internal]                   ; звонки сотрудников
exten => _{внутренний},1,Gosub(asrhub-record,s,1(internal,${{EXTEN}}))
 same => n,Dial({t}/${{EXTEN}},30,tT)
 same => n,Hangup()

exten => _8XXXXXXXXXX,1,Gosub(asrhub-record,s,1(out,${{EXTEN}}))
 same => n,{наружу}
 same => n,Hangup()
"""


def журнал_cel(п: Параметры) -> str:
    """cel.conf и cel_custom.conf: события перевода для сервера."""
    from .transfers import ОТОБРАЖЕНИЕ_CEL  # noqa: PLC0415

    return f"""\
; Граница перевода в разговоре с переводом — для станции без AMI: Asterisk
; пишет события перевода в журнал CEL, сервер читает его (поле станции
; «Журнал CEL»). Со станцией по AMI не нужно: события придут оттуда.
;
; /etc/asterisk/cel.conf — если CEL уже ведётся, добавьте эти события к своим
; (вход в мост и выход из него — окно участника, перевод — его граница):
;   [general]
;   enable = yes
;   events = BLINDTRANSFER,ATTENDEDTRANSFER,BRIDGE_ENTER,BRIDGE_EXIT
;
; /etc/asterisk/cel_custom.conf — журнал для ASR Hub. Файл ляжет в
; /var/log/asterisk/cel-custom/asrhub-cel.csv. После правки:
;   asterisk -rx "module reload cel_custom.so"
[mappings]
{ОТОБРАЖЕНИЕ_CEL}
"""


def очереди(п: Параметры) -> str:
    t = "PJSIP" if п.driver == "pjsip" else "SIP"
    return f"""\
; /etc/asterisk/queues.conf — чтобы в обработчике отбоя был известен
; оператор, ответивший в очереди, очередь должна ставить MEMBERINTERFACE.
[sales]
strategy = rrmemory
setinterfacevar = yes            ; без этого добавочный в очереди не узнать
member => {t}/101
member => {t}/102
"""


def настройки_сценария(п: Параметры) -> str:
    return f"""\
# /etc/asrhub/dialplan.conf — настройки сценария asrhub-dialplan.
# Права: владелец root, группа asterisk, 0640 — ключ не должен читаться всеми.
ASRHUB_URL={п.url}
ASRHUB_KEY=ah_ВАШ_КЛЮЧ
ASRHUB_STATION={п.station}
# upload — отправлять файл записи; path — только путь (общий диск с сервером).
ASRHUB_SEND_MODE=upload
# Каталог MixMonitor: от него считаются относительные пути (FreePBX).
ASRHUB_RECORDINGS={п.base}
# Сколько ждать, пока MixMonitor закроет файл, с.
ASRHUB_WAIT_S=120
# Попыток отправки при недоступном сервере (пауза растёт: 5, 15, 30, 60, 120 с).
ASRHUB_RETRIES=5
# Очередь повтора и журнал.
ASRHUB_SPOOL=/var/spool/asrhub-dialplan
ASRHUB_LOG=/var/log/asterisk/asrhub-dialplan.log
# Свой корневой сертификат для https (пусто — системные).
ASRHUB_CACERT=
"""


def установка(п: Параметры) -> str:
    станция = f"?station={п.station}" if п.station else ""
    return f"""\
# На станции, от root. Ключ — тот, что заведён для станции в разделе «Доступ».
t=$(mktemp) && curl -fsS -H 'X-API-Key: ah_ВАШ_КЛЮЧ' -o "$t" \\
     '{п.url}/api/telephony/dialplan/asrhub-dialplan{станция}' &&
  sh "$t" --install; rm -f "$t"         # /usr/local/bin, /etc/asrhub, очередь повтора
editor /etc/asrhub/dialplan.conf         # вписать ключ
asrhub-dialplan --test                   # адрес, ключ и станция — без записи в архив

# Дослать то, что не ушло, когда сервер был недоступен (раз в пять минут):
echo '*/5 * * * * asterisk /usr/local/bin/asrhub-dialplan --resend' > /etc/cron.d/asrhub-dialplan
"""


def проверка(п: Параметры) -> str:
    адрес = f"{п.url}{ПРИЁМ}"
    станция = f" -F station={п.station}" if п.station else ""
    return f"""\
# 1. Диалплан подключён и разобран:
asterisk -rx "dialplan reload"
asterisk -rx "dialplan show asrhub-record"
asterisk -rx "dialplan show asrhub-hangup"

# 2. Сервер принимает (пробный звонок, в архив не попадает):
curl -sS -H 'X-API-Key: ah_ВАШ_КЛЮЧ' -F dry_run=1 -F uniqueid=test.1 \\
     -F src=79161234567 -F dst=101 -F direction=in{станция} '{адрес}'

# 3. Позвоните на станцию и посмотрите, что ушло:
tail -f /var/log/asterisk/asrhub-dialplan.log
asterisk -rx "core set verbose 3"        # строки «ASR Hub: …» в консоли
"""


def freepbx(п: Параметры) -> str:
    return """\
# FreePBX, Issabel и другие сборки, где записью управляет модуль Call Recording.
# Свой MixMonitor там не нужен: включите запись звонков как обычно, а в
#   Settings → Advanced Settings → Post Call Recording Script
# впишите строку ниже (одной строкой, без кавычек). FreePBX вызывает её,
# когда MixMonitor закрыл файл, поэтому ready=1: ждать сценарию нечего.
/usr/local/bin/asrhub-dialplan ready=1 uniqueid=^{UNIQUEID} src=^{FILTER(0-9+,^{CALLERID(num)})} agent=^{FILTER(0-9A-Za-z_.,^{FROMEXTEN})} started_at=^{EPOCH} file=^{MIXMON_DIR}^{YEAR}/^{MONTH}/^{DAY}/^{CALLFILENAME}.^{MIXMON_FORMAT}

# Направление сценарий берёт из начала имени файла FreePBX: in- и q- —
# входящий, out- — исходящий, exten- — внутренний. Длительность сервер
# измерит по самому файлу. Оператор, ответивший в очереди, на момент
# начала записи FreePBX не известен — для очередей точнее способ с
# обработчиком отбоя. Проверено не на всех версиях FreePBX: после
# настройки сделайте пробный звонок и посмотрите журнал
# /var/log/asterisk/asrhub-dialplan.log.
"""


def собрать(п: Параметры, *, версия: str = "") -> dict[str, Any]:
    """Всё для раздела: файлы для станции, различия драйверов, советы."""
    файлы = [
        {"id": "dialplan", "title": "Диалплан: запись и передача",
         "path": "/etc/asterisk/extensions_asrhub.conf", "lang": "asterisk",
         "text": диалплан(п, версия)},
        {"id": "contexts", "title": "Пример: вызов из ваших контекстов",
         "path": "/etc/asterisk/extensions.conf", "lang": "asterisk",
         "text": контексты(п)},
        {"id": "queues", "title": "Очереди: кто ответил",
         "path": "/etc/asterisk/queues.conf", "lang": "asterisk", "text": очереди(п)},
        {"id": "cel", "title": "Переводы: журнал CEL (без AMI)",
         "path": "/etc/asterisk/cel_custom.conf", "lang": "asterisk", "text": журнал_cel(п)},
    ]
    if п.live:
        файлы.append({"id": "live", "title": "Живой разговор двух людей (не проверено)",
                      "path": "/etc/asterisk/extensions_asrhub_live.conf",
                      "lang": "asterisk", "text": живой_разговор(п)})
    if п.norec_code:
        файлы.append({"id": "features", "title": "Код «не записывать» в разговоре",
                      "path": "/etc/asterisk/features_applicationmap_custom.conf",
                      "lang": "asterisk", "text": коды_в_разговоре(п)})
    if п.mode == "upload":
        файлы += [
            {"id": "install", "title": "Установка сценария отправки",
             "path": "", "lang": "shell", "text": установка(п)},
            {"id": "script_conf", "title": "Настройки сценария отправки",
             "path": "/etc/asrhub/dialplan.conf", "lang": "shell",
             "text": настройки_сценария(п)},
            {"id": "freepbx", "title": "FreePBX: Post Call Recording Script",
             "path": "", "lang": "shell", "text": freepbx(п)},
        ]
    файлы.append({"id": "check", "title": "Проверка", "path": "", "lang": "shell",
                  "text": проверка(п)})
    советы = [
        "Заведите для станции отдельный ключ доступа с правом записи: его можно "
        "отозвать, не трогая остальных, а в журнале приёма видно, что звонки пришли "
        "именно им. Если звонков больше 120 в минуту — поднимите ключу предел "
        "частоты запросов.",
        "Записывайте в wav: несжатый звук 8 кГц распознаётся точнее GSM и mp3. "
        "Опция b у MixMonitor оставляет в записи только сам разговор — без гудков "
        "и музыки ожидания, и короткие «сорвавшиеся» звонки не занимают очередь.",
        "Gosub(asrhub-record,…) ставьте до Dial() и Queue() в каждом контексте, "
        "где начинается разговор, — входящем, исходящем и внутреннем. Повторный "
        "вход в подпрограмму (перевод, Local-канал очереди) второй записи не "
        "заводит.",
        "Для очередей включите setinterfacevar = yes в queues.conf — иначе "
        "добавочный оператора, ответившего в очереди, в обработчике отбоя не "
        "узнать, и разговор достанется номеру очереди.",
        "Пороги — у станции в разделе «АТС»: «Минимальная длительность», "
        "«Пропускать без ответа», владелец, приоритет, метки. Короткие и "
        "неотвеченные звонки остаются в журнале звонков с причиной пропуска.",
        "Не подставляйте в System() значения снаружи без FILTER(): номер и имя "
        "звонящего, набранный номер и заголовки SIP задаёт тот, кто звонит.",
        "Перед звонком сообщайте о записи (Playback с предупреждением в "
        "голосовом меню): этого требует закон о персональных данных.",
    ]
    if п.driver == "sip":
        советы.insert(0, "chan_sip устарел с Asterisk 17 и удалён в 21: на новых "
                         "станциях используйте chan_pjsip. Для приёма звонков "
                         "разницы почти нет — меняется только способ узнать "
                         "абонента канала (CHANNEL(peername) вместо CHANNEL(endpoint)).")
    if п.mode == "path":
        советы.insert(0, "Способ «путём» годится, когда сервер распознавания видит "
                         "каталог записей станции (та же машина, NFS, SMB). У станции "
                         "в разделе «АТС» задайте «Каталог записей» — путь, под "
                         f"которым {п.base} виден на сервере: принимаются только "
                         "файлы внутри него.")
    return {"driver": п.driver, "driver_label": ДРАЙВЕРЫ[п.driver], "mode": п.mode,
            "mode_label": СПОСОБЫ[п.mode], "url": п.url, "station": п.station,
            "endpoint": f"{п.url}{ПРИЁМ}", "files": файлы, "tips": советы,
            "differences": [{"what": что, "sip": sip, "pjsip": pjsip}
                            for что, sip, pjsip in РАЗЛИЧИЯ],
            "drivers": ДРАЙВЕРЫ, "modes": СПОСОБЫ, "formats": ФОРМАТЫ}


__all__ = ["ДРАЙВЕРЫ", "ПРИЁМ", "РАЗЛИЧИЯ", "СПОСОБЫ", "ФОРМАТЫ", "Параметры",
           "параметры", "собрать"]
