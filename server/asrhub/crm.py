"""Обратная запись в CRM: amoCRM и Bitrix24.

Вебхуки у сервера есть давно: после распознавания он умеет постучаться по
адресу и прислать JSON со своими полями. Этого достаточно для интеграции,
которую кто-то напишет, — и недостаточно для продажи: заказчик спрашивает не
«есть ли у вас вебхук», а «сколько будет стоить, чтобы расшифровка и пересказ
падали в карточку сделки». Разница между этими двумя вопросами — вот этот
модуль.

Что здесь есть и чего намеренно нет:

* **Готовые тела запросов для двух CRM.** amoCRM принимает примечание к
  сущности; Bitrix24 — комментарий в ленту дела. Это самые простые из
  работающих способов: они не требуют заранее заведённых своих полей и
  видны человеку сразу.
* **Сопоставление своих полей.** Кому мало примечания, тот указывает
  «наше поле = их поле», и значения уходят ещё и в них — вторым запросом,
  правкой самой сущности: у amoCRM это `PATCH /api/v4/<сущности>/<id>`, у
  Bitrix24 — `crm.<сущность>.update`. В примечание и в комментарий ленты
  свои поля не пишутся ни там, ни там.
* **Одно примечание на запись.** Примечание свежей записи ждёт смыслового
  разбора (иначе оно уходит без пересказа, и дослать пересказ уже некуда),
  а отметка «отправлено» по заданию держит от повторов: пересчёт разбора,
  открытие карточки и повторное распознавание в CRM больше не пишут.
* **Нет OAuth.** У amoCRM это отдельная история с обновлением токена, и
  делать её вслепую, без рабочего аккаунта, — значит написать то, что
  никогда не проверялось. Токен долгоживущий даётся в настройках, а у
  Bitrix24 входящий вебхук и вовсе не требует токена отдельно: он в адресе.
* **Нет поиска сущности по номеру.** Идентификатор сделки берётся из полей
  звонка (`userfield`, `accountcode`) или из параметров задания. Догадываться,
  к какой сделке относится разговор, по номеру телефона — это отдельная
  работа с чужой моделью данных, и ошибаться в ней дорого: комментарий
  уйдёт не в ту карточку.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .content import masking
from .errors import ASRHubError, ConfigError
from .logging_setup import get_logger

log = get_logger("crm")

#: Что сервер умеет отдать в CRM. Ключ — наше имя поля, значение — подпись
#: по-русски: она идёт в примечание и в подсказки сопоставления.
ПОЛЯ: dict[str, str] = {
    "summary": "Пересказ разговора",
    "transcript": "Расшифровка",
    "reason": "Причина обращения",
    "outcome": "Исход",
    "sentiment": "Тональность",
    "sentiment_shift": "Сдвиг тональности",
    "score": "Балл оператора",
    "compliance": "Соответствие скрипту",
    "categories": "Категории",
    "actions": "Действия к исполнению",
    "duration_s": "Длительность, с",
    "agent": "Оператор",
    "phone": "Номер",
    "direction": "Направление",
    "link": "Ссылка на запись",
}

#: Поля, которые по умолчанию идут в примечание. Расшифровка не в их числе:
#: разговор на десять минут — это простыня, в карточке её никто не читает, а
#: место она занимает. Кому нужна — включает отдельной настройкой.
В_ПРИМЕЧАНИЕ = ("summary", "reason", "outcome", "sentiment", "score",
                "compliance", "categories", "actions", "duration_s",
                "agent", "direction", "link")

ВИДЫ = ("amocrm", "bitrix24", "custom")


class CRMError(ASRHubError):
    """CRM не приняла запись."""

    code = "crm_error"
    http_status = 502


@dataclass(frozen=True)
class Настройки:
    enabled: bool = False
    kind: str = "bitrix24"
    url: str = ""
    token: str = ""
    entity: str = "lead"
    fields: dict[str, str] = None            # type: ignore[assignment]
    send_transcript: bool = False
    mask_pii: bool = False
    timeout_s: float = 10.0

    @classmethod
    def из_настроек(cls, settings: Any) -> Настройки:
        if not settings:
            return cls(fields={})
        сырое = settings.get("crm_fields") or ""
        поля: dict[str, str] = {}
        if isinstance(сырое, dict):
            поля = {str(к): str(з) for к, з in сырое.items()}
        else:
            for строка in str(сырое).splitlines():
                чистая = строка.split("#", 1)[0].strip()
                if "=" in чистая:
                    левое, правое = чистая.split("=", 1)
                    if левое.strip():
                        поля[левое.strip()] = правое.strip()
        return cls(
            enabled=bool(settings.get("crm_enabled", False)),
            kind=str(settings.get("crm_kind") or "bitrix24").strip().lower(),
            url=str(settings.get("crm_url") or "").strip(),
            token=str(settings.get("crm_token") or "").strip(),
            entity=str(settings.get("crm_entity") or "lead").strip().lower(),
            fields=поля,
            send_transcript=bool(settings.get("crm_send_transcript", False)),
            mask_pii=bool(settings.get("crm_mask_pii", False)),
            timeout_s=float(settings.get("crm_timeout_s") or 10.0))

    def проблема(self) -> str:
        if not self.enabled:
            return ""
        if self.kind not in ВИДЫ:
            return f"Неизвестная CRM «{self.kind}». Известны: {', '.join(ВИДЫ)}."
        if not self.url:
            return "Не задан адрес CRM (crm_url)."
        if self.kind == "amocrm" and not self.token:
            return ("Для amoCRM нужен долгоживущий токен (crm_token): без него "
                    "она отвечает отказом на любой запрос.")
        return ""


def собрать(job: dict[str, Any], разбор: dict[str, Any] | None = None,
            звонок: dict[str, Any] | None = None, *,
            base_url: str = "", mask: bool = False) -> dict[str, Any]:
    """Складывает всё, что известно о разговоре, в один плоский словарь.

    Плоский — намеренно: сопоставление полей человек пишет строкой
    «summary = Пересказ», и путь вида `content.sentiment.score` в такой
    строке не напишешь.
    """
    разбор = разбор or {}
    звонок = звонок or {}
    тон = разбор.get("sentiment") or {}
    скрипт = разбор.get("compliance") or {}
    оценка = разбор.get("scorecard") or {}
    # Ответ языковой модели лежит своей таблицей, а не внутри разбора, и
    # приходит сюда отдельным ключом. Разбор без модели — обычное дело:
    # смысловой слой необязателен, и без него в карточку уйдёт всё
    # остальное, а не ничего.
    модель = разбор.get("llm") or {}
    категории = (разбор.get("categories") or {}).get("topics") or []

    текст = str(job.get("text") or "")
    пересказ = str(модель.get("summary") or "")
    if mask:
        текст = masking.mask_text(текст)
        пересказ = masking.mask_text(пересказ)

    данные: dict[str, Any] = {
        "summary": пересказ,
        "transcript": текст,
        "reason": str(модель.get("reason") or ""),
        "outcome": str(модель.get("outcome") or ""),
        "sentiment": тон.get("label") or "",
        "sentiment_shift": (тон.get("turn") or {}).get("shift"),
        "score": оценка.get("score"),
        "compliance": скрипт.get("score"),
        "categories": ", ".join(
            str(к.get("name") or к) if isinstance(к, dict) else str(к)
            for к in категории[:10]),
        "actions": "; ".join(
            str(д.get("text") or д) if isinstance(д, dict) else str(д)
            for д in _список(модель.get("actions"))[:10]),
        "duration_s": job.get("media_duration_s"),
        "agent": звонок.get("agent") or "",
        "phone": звонок.get("src") or "",
        "direction": звонок.get("direction") or "",
        "link": (f"{base_url.rstrip('/')}/#/jobs/{job.get('id')}"
                 if base_url else str(job.get("id") or "")),
    }
    if mask:
        данные["phone"] = masking.mask_text(str(данные["phone"]))
    return данные


def _список(значение: Any) -> list[Any]:
    """Список из чего угодно: база хранит действия строкой JSON."""
    if значение is None:
        return []
    if isinstance(значение, str):
        try:
            разобрано = json.loads(значение)
        except (TypeError, ValueError):
            return [значение] if значение.strip() else []
        return разобрано if isinstance(разобрано, list) else [разобрано]
    if isinstance(значение, list):
        return значение
    return [значение]


def примечание(данные: dict[str, Any], *, transcript: bool = False) -> str:
    """Текст примечания — то, что человек увидит в карточке.

    Пустые поля пропускаются: строка «Балл оператора: —» в карточке сделки
    не сообщает ничего, а место занимает наравне с полезными.
    """
    строки: list[str] = []
    for ключ in В_ПРИМЕЧАНИЕ:
        значение = данные.get(ключ)
        if значение in (None, "", [], {}):
            continue
        строки.append(f"{ПОЛЯ.get(ключ, ключ)}: {значение}")
    if transcript and данные.get("transcript"):
        строки.append("")
        строки.append(f"{ПОЛЯ['transcript']}:")
        строки.append(str(данные["transcript"]))
    return "\n".join(строки)


def _свои_поля(данные: dict[str, Any], настройки: Настройки) -> dict[str, Any]:
    """Значения для полей CRM по сопоставлению «наше = их»."""
    итог: dict[str, Any] = {}
    for наше, их in (настройки.fields or {}).items():
        if наше in данные and их:
            итог[их] = данные[наше]
    return итог


@dataclass(frozen=True)
class Запрос:
    """Один запрос к CRM: что, куда и с каким телом."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    #: Зачем этот запрос: «примечание» или «поля» — для ответа и журнала.
    назначение: str = "примечание"


def _значения_amo(свои: dict[str, Any]) -> list[dict[str, Any]]:
    """Свои поля в виде amoCRM: `[{field_id|field_code, values: [{value}]}]`.

    Номер поля — числом (`field_id`), всё остальное — кодом поля
    (`field_code`): у amoCRM есть оба способа сослаться на своё поле.
    Пустые значения не шлются: пустота затёрла бы то, что менеджер
    вписал руками.
    """
    итог: list[dict[str, Any]] = []
    for поле, значение in свои.items():
        if значение in (None, "", [], {}):
            continue
        ссылка: dict[str, Any] = ({"field_id": int(поле)} if str(поле).strip().isdigit()
                                  else {"field_code": str(поле).strip()})
        итог.append({**ссылка, "values": [{"value": значение}]})
    return итог


def запросы(данные: dict[str, Any], настройки: Настройки, *,
            entity_id: str = "") -> list[Запрос]:
    """Все запросы к CRM по одной записи — по порядку отправки.

    Ничего не отправляется: так эту часть можно проверить целиком, не
    поднимая CRM, и показать человеку в разделе настроек, что именно уйдёт.

    Свои поля — ВТОРЫМ запросом, правкой самой сущности. У amoCRM раньше
    уходил один объект `{"notes": …, "custom_fields_values": …}` на адрес
    примечаний, который принимает только массив примечаний: запись в CRM
    ломалась целиком, как только человек сопоставлял хоть одно своё поле.
    У Bitrix24 поля клались в комментарий ленты, где их молча не пишут.
    """
    беда = настройки.проблема()
    if беда:
        raise ConfigError(беда, hint="Раздел «Настройки» → «CRM».")
    текст = примечание(данные, transcript=настройки.send_transcript)
    свои = _свои_поля(данные, настройки)
    заголовки = {"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "ASRHub/3.0"}

    def тело(что: Any) -> bytes:
        return json.dumps(что, ensure_ascii=False).encode("utf-8")

    if настройки.kind == "amocrm":
        if not entity_id:
            raise CRMError(
                "Не указано, к какой сущности amoCRM добавлять примечание.",
                hint="Идентификатор берётся из поля звонка userfield или "
                     "accountcode; задайте его на АТС или передайте в задании.")
        сущность = {"lead": "leads", "contact": "contacts",
                    "company": "companies"}.get(настройки.entity, "leads")
        заголовки["Authorization"] = f"Bearer {настройки.token}"
        основа = f"{настройки.url.rstrip('/')}/api/v4/{сущность}/{entity_id}"
        итог = [Запрос("POST", f"{основа}/notes", dict(заголовки),
                       тело([{"note_type": "common", "params": {"text": текст}}]))]
        значения = _значения_amo(свои)
        if значения:
            итог.append(Запрос("PATCH", основа, dict(заголовки),
                               тело({"custom_fields_values": значения}), "поля"))
        return итог

    if настройки.kind == "bitrix24":
        # Входящий вебхук Bitrix24 несёт ключ прямо в адресе, поэтому токена
        # отдельно не нужно. Адрес выглядит как
        # https://фирма.bitrix24.ru/rest/1/КЛЮЧ/
        тип = {"lead": "lead", "deal": "deal", "contact": "contact",
               "company": "company"}.get(настройки.entity, "deal")
        основа = настройки.url.rstrip("/")
        поля: dict[str, Any] = {"ENTITY_TYPE": тип, "COMMENT": текст}
        if entity_id:
            поля["ENTITY_ID"] = entity_id
        итог = [Запрос("POST", f"{основа}/crm.timeline.comment.add.json",
                       dict(заголовки), тело({"fields": поля}))]
        значимые = {к: з for к, з in свои.items() if з not in (None, "", [], {})}
        if значимые and entity_id:
            итог.append(Запрос("POST", f"{основа}/crm.{тип}.update.json", dict(заголовки),
                               тело({"id": entity_id, "fields": значимые}), "поля"))
        return итог

    # «custom» — своё тело: всё, что есть, плюс сопоставленные имена. Так
    # подключается то, чего мы не знаем, без правки кода.
    тело_свой = {**данные, **свои}
    if entity_id:
        тело_свой["entity_id"] = entity_id
    if настройки.token:
        заголовки["Authorization"] = f"Bearer {настройки.token}"
    return [Запрос("POST", настройки.url, заголовки, тело(тело_свой))]


def запрос(данные: dict[str, Any], настройки: Настройки, *,
           entity_id: str = "") -> tuple[str, dict[str, str], bytes]:
    """Адрес, заголовки и тело ПЕРВОГО запроса — примечания (см. `запросы`)."""
    первый = запросы(данные, настройки, entity_id=entity_id)[0]
    return первый.url, первый.headers, первый.body


def _послать(запрос_crm: Запрос, настройки: Настройки,
             allow_internal: bool) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    from .job_queue import check_outbound_url, открыватель_наружу

    проверенный = check_outbound_url(запрос_crm.url, allow_internal)
    запрос_http = urllib.request.Request(проверенный, data=запрос_crm.body,
                                         headers=запрос_crm.headers,
                                         method=запрос_crm.method)
    try:
        with открыватель_наружу(allow_internal).open(
                запрос_http, timeout=max(1.0, настройки.timeout_s)) as ответ:
            кусок = ответ.read(2048).decode("utf-8", "replace")
            return {"status": int(ответ.status), "body": кусок}
    except urllib.error.HTTPError as exc:
        кусок = exc.read(2048).decode("utf-8", "replace") if exc.fp else ""
        raise CRMError(
            f"CRM ответила {exc.code}: {кусок[:200] or exc.reason}",
            hint="Проверьте адрес, токен и права учётной записи интеграции."
        ) from exc
    except Exception as exc:                                # noqa: BLE001
        raise CRMError(f"CRM недоступна: {exc}",
                       hint="Проверьте адрес и сеть.") from exc


def отправить(данные: dict[str, Any], настройки: Настройки, *,
              entity_id: str = "", allow_internal: bool = False) -> dict[str, Any]:
    """Шлёт запись в CRM. Возвращает код ответа и начало тела примечания.

    Одна попытка, без повторов: обратная запись в CRM — не то, что нужно
    доставить любой ценой. Повтор в чужую систему вслепую даёт дубли
    комментариев в карточке, а это заметнее и неприятнее пропуска.

    Примечание — первым запросом; его отказ поднимается ошибкой. Свои поля
    — вторым; их отказ ошибкой не поднимается: примечание уже в карточке, и
    «не ушло ничего» было бы неправдой. Он возвращается в `fields_error`.
    """
    список = запросы(данные, настройки, entity_id=entity_id)
    итог = _послать(список[0], настройки, allow_internal)
    for следующий in список[1:]:
        try:
            ответ = _послать(следующий, настройки, allow_internal)
            итог.setdefault("extra", []).append(
                {"purpose": следующий.назначение, "status": ответ["status"]})
        except CRMError as exc:
            log.warning("CRM приняла примечание, но не %s: %s",
                        следующий.назначение, exc.message)
            итог["fields_error"] = exc.message
    return итог


#: Поля звонка, в которых АТС обычно передаёт идентификатор сделки. Порядок —
#: от более частного к общему: `userfield` в Asterisk свободное и его чаще
#: всего и занимают под такие нужды.
ПОЛЯ_СДЕЛКИ = ("userfield", "accountcode")


def сущность_из(job: dict[str, Any], звонок: dict[str, Any] | None) -> str:
    """Идентификатор сделки: из полей звонка или из параметров задания.

    Пусто — отправлять некуда, и это не ошибка настройки, а обычное дело:
    запись, загруженная руками, ни к какой сделке не относится.
    """
    for поле in ПОЛЯ_СДЕЛКИ:
        значение = str((звонок or {}).get(поле) or "").strip()
        if значение:
            return значение
    параметры = job.get("params") or {}
    if isinstance(параметры, dict):
        for ключ in ("crm_entity_id", "entity_id", "deal_id", "lead_id"):
            значение = str(параметры.get(ключ) or "").strip()
            if значение:
                return значение
    return ""


def отправить_разбор(db: Any, settings: Any, job_id: str, *,
                     разбор: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Собирает всё о записи и шлёт в CRM — ОДИН раз. None — не отправлялось.

    Ошибки не поднимаются наружу: разбор записи не должен падать из-за того,
    что чужая система не отвечает. Всё, что случилось, уходит в журнал —
    и туда же уходит успех, потому что «комментарий не появился» разбирают по
    журналу, а не по памяти. Итог ложится отметкой на задание (`crm_status`):
    по ней отправка не повторяется и видна в карточке записи.
    """
    настройки = Настройки.из_настроек(settings)
    if not настройки.enabled:
        return None
    беда = настройки.проблема()
    if беда:
        log.warning("Обратная запись в CRM не настроена: %s", беда)
        return None
    задание = db.get_job(job_id)
    if not задание:
        return None
    звонок = db.call_for_job(job_id) if hasattr(db, "call_for_job") else None
    сделка = сущность_из(задание, звонок)
    if not сделка and настройки.kind != "custom":
        log.info("Запись %s не привязана к сделке — в CRM не отправляется", job_id)
        if hasattr(db, "crm_mark") and str(задание.get("crm_status") or "") in (
                "", db.CRM_ЖДЁТ_МОДЕЛЬ):
            db.crm_mark(job_id, db.CRM_БЕЗ_СДЕЛКИ)
        return None
    if hasattr(db, "crm_claim") and not db.crm_claim(job_id):
        log.info("Примечание по записи %s в CRM уже уходило — повторно не шлём", job_id)
        return None
    if разбор is None and hasattr(db, "get_content"):
        разбор = dict((db.get_content(job_id) or {}).get("detail") or {})
    модель = db.llm_get(job_id) if hasattr(db, "llm_get") else None
    полный = dict(разбор or {})
    if модель and not модель.get("error"):
        полный["llm"] = dict(модель)
    данные = собрать(задание, полный, звонок,
                     base_url=str(settings.get("public_url") or ""),
                     mask=настройки.mask_pii)
    try:
        ответ = отправить(
            данные, настройки, entity_id=сделка,
            allow_internal=bool(settings.get("webhook_allow_internal", False)))
    except ASRHubError as exc:
        log.warning("Запись %s не ушла в CRM: %s", job_id, exc, extra={"job_id": job_id})
        if hasattr(db, "crm_mark"):
            db.crm_mark(job_id, f"ошибка: {exc.message}")
        return {"status": 0, "error": str(exc)}
    log.info("Запись %s ушла в CRM (%s), сделка %s, ответ %s",
             job_id, настройки.kind, сделка, ответ.get("status"),
             extra={"job_id": job_id})
    if hasattr(db, "crm_mark"):
        db.crm_mark(job_id, db.CRM_ОТПРАВЛЕНО if not ответ.get("fields_error")
                    else f"{db.CRM_ОТПРАВЛЕНО}, поля не приняты")
    return ответ


#: Задачи модели, ради которых примечание стоит подождать: без них в нём
#: не будет пересказа, причины, исхода и действий.
_ЗАДАЧИ_ДЛЯ_ПРИМЕЧАНИЯ = frozenset({"summary", "outcome", "actions"})

#: Дольше этого примечание модель не ждёт: сервер модели лёг надолго,
#: очередь разбора приостановили — карточка сделки не должна оставаться
#: пустой до конца недели.
ЖДАТЬ_МОДЕЛЬ_С = 6 * 3600


def модель_будет(settings: Any) -> bool:
    """Будет ли по свежей записи смысловой разбор, нужный примечанию."""
    if str(settings.get("llm_backend") or "off") not in ("ollama", "openai", "stub"):
        return False
    if not settings.get("llm_auto", True):
        return False
    задачи = settings.get("llm_tasks")
    if задачи is None:
        return True
    return bool(_ЗАДАЧИ_ДЛЯ_ПРИМЕЧАНИЯ & {str(з) for з in (задачи or [])})


def после_разбора(db: Any, settings: Any, job_id: str,
                  разбор: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Свежая запись разобрана: отправить примечание сейчас или после модели.

    Только для свежих записей. Пересчёт разбора, открытие карточки и
    разбор архива сюда не приходят — раньше каждый из них слал в карточку
    сделки новое примечание.
    """
    настройки = Настройки.из_настроек(settings)
    if not настройки.enabled or настройки.проблема():
        return None
    if модель_будет(settings) and hasattr(db, "crm_mark"):
        # Отметка ставится, только если по записи ещё ничего не решали:
        # повторное распознавание уже отправленной записи не должно снова
        # поставить её в ожидание.
        db.crm_mark(job_id, db.CRM_ЖДЁТ_МОДЕЛЬ, только_если="")
        return None
    return отправить_разбор(db, settings, job_id, разбор=разбор)


def после_модели(db: Any, settings: Any, job_id: str) -> dict[str, Any] | None:
    """Модель закончила с записью (или бросила её): дослать, если ждали."""
    задание = db.get_job(job_id) if hasattr(db, "get_job") else None
    if not задание or str(задание.get("crm_status") or "") != getattr(
            db, "CRM_ЖДЁТ_МОДЕЛЬ", "ждёт модель"):
        return None
    return отправить_разбор(db, settings, job_id)


def досылка(db: Any, settings: Any, *, предел: int = 20) -> int:
    """Примечания, которые ждут модель слишком долго или уже зря.

    Зря — когда строки в очереди модели больше нет или она не ждёт и не
    идёт (сняли, отменили, запись ушла в ошибку): ждать больше нечего.
    Слишком долго — когда сервер модели лёг или очередь приостановлена.
    Возвращает, сколько примечаний отправлено.
    """
    if not hasattr(db, "crm_waiting"):
        return 0
    настройки = Настройки.из_настроек(settings)
    if not настройки.enabled or настройки.проблема():
        return 0
    сейчас = time.time()
    отправлено = 0
    for строка in db.crm_waiting(limit=предел):
        ждёт = str(строка.get("llm_state") or "") in (
            getattr(db, "LLMQ_ЖДЁТ", "ждёт"), getattr(db, "LLMQ_ИДЁТ", "идёт"))
        if ждёт and сейчас - float(строка.get("crm_at") or сейчас) < ЖДАТЬ_МОДЕЛЬ_С:
            continue
        if отправить_разбор(db, settings, str(строка["id"])) is not None:
            отправлено += 1
    return отправлено
