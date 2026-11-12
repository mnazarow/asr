"""Каталог настраиваемых параметров ASR Hub.

Каждый параметр описан один раз и используется:
* веб-интерфейсом — для генерации формы с подсказками и примерами;
* валидатором конфигурации — для проверки диапазонов и типов;
* генератором документации — для построения справочника параметров;
* движками — для трансляции в их собственные имена (поле ``aliases``).

Поле ``impact`` описывает, что произойдёт при увеличении значения:
``up`` — растёт, ``down`` — падает, ``neutral`` — не меняется.
"""
from __future__ import annotations

import importlib
import json
import math
from typing import Any

from ...logging_setup import get_logger
from ._base import GROUPS, PARAMS, Ex, P, _p  # noqa: F401

log = get_logger(__name__)

#: Группы по файлу на группу. Порядок здесь — порядок параметров в форме
#: настроек и в справочнике (глава 04): файл регистрирует свои параметры
#: в общем списке `PARAMS` при импорте.
ФАЙЛЫ_ГРУПП = ("model", "decoding", "whisper", "vad", "audio", "batching",
                "diarization", "postprocess", "output", "queue", "streaming", "server",
                "runtime", "content", "llm", "telephony", "employees")
for _файл in ФАЙЛЫ_ГРУПП:
    importlib.import_module(f"{__name__}.{_файл}")
del _файл

# ===========================================================================
# Индексы и утилиты
# ===========================================================================

PARAMS_BY_KEY: dict[str, P] = {p.key: p for p in PARAMS}
GROUPS_BY_ID: dict[str, dict[str, str]] = {g["id"]: g for g in GROUPS}


def get_param(key: str) -> P | None:
    return PARAMS_BY_KEY.get(key)


def params_for_group(group: str) -> list[P]:
    return [p for p in PARAMS if p.group == group]


def params_for_engine(engine_id: str) -> list[P]:
    """Параметры, применимые к движку: универсальные плюс специфичные для него."""
    return [p for p in PARAMS if not p.engines or engine_id in p.engines]


def defaults() -> dict[str, object]:
    return {p.key: p.default for p in PARAMS}


def translate_key(key: str, engine_id: str) -> str:
    """Имя параметра в терминах конкретного движка."""
    spec = PARAMS_BY_KEY.get(key)
    if spec is None:
        return key
    return spec.aliases.get(engine_id, key)


def _проверить_категории(value: object) -> list[str]:
    """Категории — со сверкой правил: набор с ошибкой в базу не попадает.

    Проверка типа «это список» здесь недостаточна. Правило с незакрытой
    скобкой прошло бы, а всплыло бы через сутки в фоновом разборе архива —
    молча, у категории, которая с этого момента не совпадает ни с чем.
    """
    from ...content.categories import validate  # noqa: PLC0415

    return validate(value)


def _проверить_скрипт(value: object) -> list[str]:
    """Скрипт разговора — тем же порядком, что и категории: список пунктов-
    объектов, приметы списком, правило через движок правил, проверка из
    известных. Без этого строка вместо пункта ложилась в базу и роняла
    разбор каждой новой записи."""
    from ...content.compliance import validate  # noqa: PLC0415

    return validate(value)


def _конечное(значение: object) -> bool:
    """Число, и не бесконечность и не NaN."""
    if isinstance(значение, bool):
        return False
    try:
        return math.isfinite(float(значение))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False


def _проверить_приёмники(value: object) -> list[str]:
    """Приёмники метрик: список объектов с известным kind и адресом.

    Раньше тип был «list», которого не знали ни приведение, ни проверка, —
    проходило что угодно. Строка в config.yaml (а интерфейс рисовал поле
    простым текстом) доходила до разбора приёмников, и сервер не поднимался:
    AttributeError в `Target.from_dict` посреди запуска.
    """
    from ...monitoring.pushers import KINDS  # noqa: PLC0415

    if not isinstance(value, list):
        return ['ожидается список приёмников: [{"kind": "influxdb", "url": "http://…"}]']
    ошибки: list[str] = []
    имена: set[str] = set()
    for номер, приёмник in enumerate(value, 1):
        if not isinstance(приёмник, dict):
            ошибки.append(f"приёмник {номер}: ожидается объект с полями kind и url")
            continue
        вид = str(приёмник.get("kind") or "")
        if вид not in KINDS:
            ошибки.append(f"приёмник {номер}: неизвестный kind «{вид}» — "
                          f"допустимы {', '.join(KINDS)}")
        if not str(приёмник.get("url") or "").strip():
            ошибки.append(f"приёмник {номер}: не задан url")
        for поле in ("interval_s", "timeout_s"):
            if поле in приёмник and not _конечное(приёмник[поле]):
                ошибки.append(f"приёмник {номер}: {поле} должно быть числом")
        if приёмник.get("headers") is not None and not isinstance(приёмник["headers"], dict):
            ошибки.append(f"приёмник {номер}: headers — объект «заголовок: значение»")
        elif any(any(ord(з) < 32 or ord(з) == 127 for з in str(з_).strip())
                 for з_ in (приёмник.get("headers") or {}).values()):
            # Перевод строки в токене http.client отвергает, вписывая токен
            # в текст ошибки, а её видят все, кто видит /targets.
            ошибки.append(f"приёмник {номер}: в значении заголовка управляющие знаки "
                          "(перевод строки, табуляция)")
        # Имя — ключ приёмника, по умолчанию это вид: два influxdb без name
        # схлопывались в один, и работал только последний — молча.
        имя = str(приёмник.get("name") or вид)
        if имя in имена:
            ошибки.append(f"приёмник {номер}: имя «{имя}» уже занято — задайте "
                          "приёмнику своё name")
        имена.add(имя)
    return ошибки


def _проверить_правила(value: object) -> list[str]:
    """Правила оповещения: метрика из каталога, направление, порог, важность.

    Без проверки опечатка давала правило, которое молчит всегда (метрики с
    таким именем нет), «Above» читалось как «below», а «Critical» не
    считалось ни критической тревогой, ни предупреждением.
    """
    from ...monitoring.catalog import METRICS_BY_NAME  # noqa: PLC0415

    if not isinstance(value, list):
        return ['ожидается список правил: [{"metric": …, "direction": "above", "threshold": …}]']
    ошибки: list[str] = []
    for номер, правило in enumerate(value, 1):
        if not isinstance(правило, dict):
            ошибки.append(f"правило {номер}: ожидается объект с полями metric и threshold")
            continue
        метрика = str(правило.get("metric") or "").strip()
        if not метрика:
            ошибки.append(f"правило {номер}: не задана metric")
        elif метрика not in METRICS_BY_NAME:
            ошибки.append(f"правило {номер}: метрики «{метрика}» нет в каталоге — "
                          "список: GET /api/monitoring/catalog")
        if str(правило.get("direction", "above")).strip().lower() not in ("above", "below"):
            ошибки.append(f"правило {номер}: direction — above или below")
        if str(правило.get("severity", "warning")).strip().lower() not in ("warning", "critical"):
            ошибки.append(f"правило {номер}: severity — warning или critical")
        if not _конечное(правило.get("threshold")):
            ошибки.append(f"правило {номер}: threshold должен быть числом")
        if "for_seconds" in правило and not (
                _конечное(правило["for_seconds"]) and float(правило["for_seconds"]) >= 0):
            ошибки.append(f"правило {номер}: for_seconds — неотрицательное число секунд")
    return ошибки


#: Проверки значений сверх типа — по ключу параметра.
def _проверить_syslog(значение: Any) -> list[str]:
    """Адрес копии журнала доступа: udp://, tcp:// или путь к сокету."""
    from ...audit import проверить_адрес_syslog  # noqa: PLC0415

    return проверить_адрес_syslog(значение)


def _проверить_сети(значение: Any) -> list[str]:
    """Список адресов и сетей через запятую — каждая запись разбирается."""
    import ipaddress  # noqa: PLC0415

    ошибки = []
    for часть in str(значение or "").split(","):
        запись = часть.strip()
        if not запись:
            continue
        try:
            ipaddress.ip_network(запись, strict=False)
        except ValueError:
            ошибки.append(f"«{запись}» — не адрес и не сеть (пример: 10.0.0.0/8)")
    return ошибки


def _контексты_из_строки(текст: str) -> Any:
    """«from-trunk=входящий, from-internal=исходящий» → словарь с порядком.

    Рекомендация параметра обещает запись строкой, а проверка её не
    принимала: «ожидается объект JSON» — про пример из самого каталога.
    """
    from ...telephony.asterisk import правила_контекстов  # noqa: PLC0415

    пары = правила_контекстов(текст)
    return dict(пары) if пары else текст


def _проверить_контексты(value: object) -> list[str]:
    """Направления в правилах контекстов — только из трёх известных."""
    from ...telephony.asterisk import правила_контекстов  # noqa: PLC0415

    try:
        пары = правила_контекстов(value)
    except (TypeError, ValueError):
        return ["ожидается «контекст → направление»"]
    return [f"направление «{куда}» у контекста «{контекст}» не бывает — только "
            "входящий, исходящий, внутренний"
            for контекст, куда in пары
            if куда not in ("входящий", "исходящий", "внутренний")]


def _проверить_станции(value: object) -> list[str]:
    """Список станций: поля, которые не годятся ни в каком виде.

    Общий PUT /api/settings не проверял станции вовсе: `owner_map` строкой
    без пар принимался, раздел «АТС» отвечал 500, а сервер после перезапуска
    не поднимался. Проверка — только типов (`проверить_типы`): она же
    действует при чтении config.yaml, где негодное значение заменяется
    пустым списком станций целиком.
    """
    from ...telephony.stations import проверить_типы  # noqa: PLC0415

    return проверить_типы(value)


def _проверить_карту_владельцев(value: object) -> list[str]:
    """«Номер → владелец»: владелец — строка, а не объект или список."""
    if isinstance(value, dict) and any(isinstance(з, (dict, list)) for з in value.values()):
        return ["владелец задаётся строкой: {\"101\": \"sales\"}"]
    if isinstance(value, list):
        return ["ожидается объект «номер → владелец»: {\"101\": \"sales\"}"]
    return []


def _проверить_внешний_адрес(value: Any) -> list[str]:
    """Внешний адрес — пусто или http(s)://хост[:порт][/путь], без пробелов."""
    адрес = str(value or "").strip()
    if not адрес:
        return []
    схема, _, остаток = адрес.partition("://")
    хост = остаток.split("/", 1)[0]
    if схема.lower() not in ("http", "https") or not хост or any(с.isspace() for с in адрес):
        return ["ожидается адрес вида https://asr.example.ru"]
    return []


def _проверить_резервную(value: Any) -> list[str]:
    """Резервная модель — пусто или модель из каталога.

    Опечатка в имени всплывала бы только в тот день, когда основная модель
    откажет: перехода не случилось бы, и задание упало бы, хотя резервная
    «настроена».
    """
    имя = str(value or "").strip()
    if not имя:
        return []
    from ..models import get_model, suggest_models  # noqa: PLC0415

    spec = get_model(имя)
    if spec is not None:
        # Вспомогательная или неисполняемая модель задание не распознает:
        # она принималась, ставила движок, качала веса и падала.
        return [] if spec.runnable else [
            f"модель «{имя}» в задание не берётся: {spec.role_note}"]
    похожие = suggest_models(имя)
    return [f"модели «{имя}» нет в каталоге"
            + (f"; похожие: {', '.join(похожие)}" if похожие else "")]


def _проверить_модель(value: Any) -> list[str]:
    """Модель сервера — из каталога и исполняемая. Пустое имя — тоже ошибка."""
    имя = str(value or "").strip()
    if not имя:
        return ["модель не указана"]
    return _проверить_резервную(имя)


def _проверить_движок(value: Any) -> list[str]:
    """Движок сервера — «auto» или движок каталога."""
    имя = str(value or "").strip()
    from ..engines import ENGINES_BY_ID  # noqa: PLC0415

    if not имя or имя == "auto" or имя in ENGINES_BY_ID:
        return []
    return [f"движка «{имя}» нет; возможные: auto, " + ", ".join(sorted(ENGINES_BY_ID))]


#: Проверки, которые годятся только для настроек СЕРВЕРА — config.yaml,
#: переменных окружения и правки из интерфейса. Опечатка в модели сервера
#: иначе принималась молча, и падало каждое задание: model_not_found на всём
#: потоке вместо одной красной строки при запуске. В общую проверку
#: (`validate_value`) они не входят: ею же повторно разбираются сохранённые
#: параметры заданий, а модель старого задания могла уйти из каталога — и
#: тогда не скачать бы его результат и не перейти на резервную модель.
_ПРОВЕРКИ_СЕРВЕРА = {"model": _проверить_модель, "engine": _проверить_движок}


def _проверить_словарь(value: object) -> list[str]:
    """Словарь замен: строка → строка, выражения «re:» — разбираемые и не опасные."""
    from ...pipeline.postprocess import проверить_словарь  # noqa: PLC0415

    return проверить_словарь(value)


def _проверить_время(value: object) -> list[str]:
    """Время суток «ЧЧ:ММ» — 00:00…23:59."""
    import re  # noqa: PLC0415

    найдено = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value or "").strip())
    return [] if найдено else ["ожидается время «ЧЧ:ММ», например 00:01 или 13:00"]


def _время_из_числа(значение: Any) -> Any:
    """`backup_time: 13:00` без кавычек YAML 1.1 читает как 780 (минуты) — назад в «13:00»."""
    if isinstance(значение, int) and not isinstance(значение, bool) and 0 <= значение < 24 * 60:
        return f"{значение // 60:02d}:{значение % 60:02d}"
    return значение


def _проверить_группы_контроля(value: object) -> list[str]:
    from ...qc.survey import проверить_группы  # noqa: PLC0415

    return проверить_группы(value)


def _проверить_часы_обзвона(value: object) -> list[str]:
    from ...qc.survey import разобрать_часы  # noqa: PLC0415

    try:
        разобрать_часы(value)
    except ValueError as exc:
        return [str(exc)]
    return []


def _проверить_пояс_обзвона(value: object) -> list[str]:
    from ...telephony.stations import разобрать_пояс  # noqa: PLC0415

    try:
        разобрать_пояс(str(value or ""))
    except ValueError as exc:
        return [str(exc)]
    return []


_ПРОВЕРКИ = {"content_categories": _проверить_категории,
             "glossary": _проверить_словарь,
             "content_script": _проверить_скрипт,
             "backup_time": _проверить_время,
             "monitoring_targets": _проверить_приёмники,
             "monitoring_rules": _проверить_правила,
             "trusted_proxies": _проверить_сети,
             "metrics_allowed_networks": _проверить_сети,
             "audit_syslog": _проверить_syslog,
             "telephony_contexts": _проверить_контексты,
             "telephony_stations": _проверить_станции,
             "telephony_owner_map": _проверить_карту_владельцев,
             "model_fallback": _проверить_резервную,
             "public_url": _проверить_внешний_адрес,
             "qc_groups": _проверить_группы_контроля,
             "qc_hours": _проверить_часы_обзвона,
             "qc_timezone": _проверить_пояс_обзвона}

#: Разбор строки для параметров-объектов, у которых строковая запись —
#: обещанная форма, а не ошибка (см. `coerce_value`).
_ИЗ_СТРОКИ = {"telephony_contexts": _контексты_из_строки}


#: Как записывают «да» и «нет» руками и в переменных окружения.
_ДА = frozenset({"да", "yes", "true", "on", "1", "вкл", "включено", "истина"})
_НЕТ = frozenset({"нет", "no", "false", "off", "0", "выкл", "выключено", "ложь"})


def _пусто_для(spec: P) -> Any:
    """Чем заменить пустую запись: пустым списком или пустым объектом."""
    return {} if isinstance(spec.default, dict) else []


def _строкой(value: Any) -> str:
    """Число — в ту запись, которой его написал бы человек: 5, а не 5.0."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


#: Текстовые параметры «по одному на строку»: список YAML склеивается
#: переводом строки, а не запятой (см. `coerce_value`).
_ПОСТРОЧНЫЕ = frozenset({"hallucination_phrases", "filler_words", "auth_ldap_group_map",
                         "crm_fields", "initial_prompt"})

#: Параметры «что = чему» по строке на пару: объект YAML — тоже годная запись.
_ПАРЫ_СТРОКАМИ = frozenset({"auth_ldap_group_map", "crm_fields"})


#: Значения перечислений, которых больше нет, и чем их заменить.
#:
#: Три варианта стояли в справочнике, показывались в интерфейсе, проходили
#: проверку — и не делали ничего: кода под ними не было ни строчки.
#: `demucs` не попадал в цепочку фильтров ffmpeg, `vosk-punct` и `runorm`
#: не совпадали ни с одной веткой выбора обработчика, и функция молча
#: возвращала «обработчика нет». Человек выбирал извлечение вокала,
#: получал запись без обработки и нигде не видел почему; хуже того, при
#: явно выбранном варианте сервер умеет предупреждать, что надстройка
#: недоступна, — но эти три до предупреждения не доходили вовсе.
#:
#: Варианты убраны из справочника, а сохранённые значения переводятся
#: сюда: у кого в config.yaml лежит `vosk-punct`, тот получит `auto` и
#: строку в журнале, а не отказ сервера при запуске.
УСТАРЕВШИЕ_ЗНАЧЕНИЯ: dict[str, dict[str, str]] = {
    "audio_denoise": {"demucs": "none"},
    "punctuation_model": {"vosk-punct": "auto"},
    "itn_backend": {"runorm": "auto"},
}


def coerce_value(key: str, value: Any) -> Any:
    """Приводит значение к типу параметра, если запись читается однозначно.

    Нужно ради обновлений. Тип параметра со временем расширяется: было одно
    число — стало «несколько через запятую». Файл конфигурации на сервере
    при этом остаётся прежним, и строгая проверка останавливает сервер на
    файле, который вчера работал. Так и случилось с «длинами внутренних
    номеров»: в config.yaml лежало ``5``, параметр стал строкой, и сервер
    после обновления не поднялся — с сообщением «ожидается строка» про
    строку, которую человек не писал.

    Приводится только то, что нельзя прочитать двояко: ``5`` → ``«5»``,
    ``«5»`` → ``5``, ``«да»`` → ``True``, ``«a, b»`` → ``[«a», «b»]``,
    ``«[]»`` → ``[]``, пустая запись — в пустое значение своего типа.
    Всё остальное возвращается нетронутым и уходит в :func:`validate_value`,
    которая объяснит человеку, что не так.

    Угадывать здесь нельзя: ``«2»`` для параметра-числа — это два, а вот
    «похожее» имя модели или «почти подходящее» значение перечисления
    исправлять молча — значит запустить сервер не с теми настройками и
    ничего об этом не сказать.
    """
    spec = PARAMS_BY_KEY.get(key)
    if spec is None:
        return value
    t = spec.type

    замены = УСТАРЕВШИЕ_ЗНАЧЕНИЯ.get(key)
    if замены and isinstance(value, str) and value in замены:
        log.warning("Значение «%s» параметра «%s» больше не поддерживается — "
                    "берётся «%s». Кода под прежним вариантом не было, и "
                    "обработка всё это время не выполнялась.",
                    value, spec.label, замены[value])
        return замены[value]

    if t == "bool":
        if isinstance(value, bool):
            return value
        # ``isinstance(True, int)`` истинно, поэтому сначала bool, потом int.
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            низ = value.strip().lower()
            if низ in _ДА:
                return True
            if низ in _НЕТ:
                return False
        return value

    if t == "int":
        # `bool` — разновидность `int`, поэтому «да» проходит здесь
        # нетронутым: единицей оно не станет, об этом скажет проверка.
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else value
        if isinstance(value, str):
            текст = value.strip()
            try:
                return int(текст)
            except ValueError:
                pass
            try:
                дробное = float(текст)
            except ValueError:
                return value
            return int(дробное) if дробное.is_integer() else дробное
        return value

    if t == "float":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value
        return value

    if t in ("str", "text"):
        if isinstance(value, str):
            return value
        if key == "backup_time":
            # «25:99» и «ночью» тоже принимались, а копия молча шла в 00:01:
            # проверка — в `_проверить_время`, а здесь только запись YAML.
            value = _время_из_числа(value)
            if isinstance(value, str):
                return value
        if value is None:
            return ""                          # пустой ключ в YAML — это «не задано»
        if isinstance(value, bool):
            return value                       # «да» — не строка «True»
        if isinstance(value, (int, float)):
            return _строкой(value)
        # Список там, где ждут перечисление через запятую. Записать «3, 4, 6»
        # списком YAML — естественное движение руки, а смысл ровно тот же.
        # У параметров «по строке на запись» склейка — переводом строки:
        # запятая склеивала две пары карты групп LDAP в одну
        # («asr-admins = admin, asr-users = user» — роль не распознавалась, и
        # вход по каталогу закрывался всем), две фразы-галлюцинации — в одну.
        if isinstance(value, (list, tuple)) and all(
                isinstance(ч, (str, int, float)) and not isinstance(ч, bool) for ч in value):
            знак = "\n" if key in _ПОСТРОЧНЫЕ else ", "
            return знак.join(_строкой(ч) for ч in value)
        # Пары «что = чему» объектом YAML — та же запись, что строками.
        if key in _ПАРЫ_СТРОКАМИ and isinstance(value, dict) and all(
                isinstance(к, str) and isinstance(з, (str, int, float))
                and not isinstance(з, bool) for к, з in value.items()):
            return "\n".join(f"{к} = {_строкой(з)}" for к, з in value.items())
        return value

    if t == "multi":
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if value is None:
            return []
        if isinstance(value, str):
            текст = value.strip()
            if not текст:
                return []
            if текст.startswith("["):
                try:
                    разобрано = json.loads(текст)
                except ValueError:
                    разобрано = None
                if isinstance(разобрано, list):
                    return разобрано
            return [ч.strip() for ч in текст.replace(";", ",").split(",") if ч.strip()]
        if isinstance(value, (int, float, bool)):
            return [value]                     # одно значение записали без списка
        return value

    if t == "json":
        if isinstance(value, (dict, list)):
            return value
        if value is None:
            return _пусто_для(spec)
        if isinstance(value, str):
            текст = value.strip()
            if not текст:
                return _пусто_для(spec)
            try:
                разобрано = json.loads(текст)
            except ValueError:
                разбор = _ИЗ_СТРОКИ.get(key)
                return разбор(текст) if разбор is not None else value
            if isinstance(разобрано, (dict, list)):
                return разобрано
        return value

    if t == "enum" and spec.options:
        допустимые = [o["value"] for o in spec.options]
        if value in допустимые:
            return value
        # Значение записано числом там, где в каталоге строка (``2`` вместо
        # ``«2»``), или наоборот. Сверяем по записи, а не «на похожесть».
        как_текст = _строкой(value).strip().lower()
        for допустимое in допустимые:
            if _строкой(допустимое).strip().lower() == как_текст:
                return допустимое
        return value

    return value


def coerce_all(values: dict[str, Any]) -> dict[str, Any]:
    """То же для набора значений. Неизвестные ключи не трогает."""
    return {ключ: coerce_value(ключ, значение) for ключ, значение in values.items()}


#: Параметры, убранные из каталога, — с причиной. Они ничего не делали, а в
#: config.yaml прежних версий могли остаться: такие строки пропускаются с
#: записью в журнал, а не красной полосой «неизвестный параметр».
УБРАННЫЕ: dict[str, str] = {
    "streaming_latency_ms": "задержку потока задаёт шаг окна stream_window_s, "
                            "а у Vosk с настоящим потоком — сам движок",
    "streaming_chunk_ms": "размер куска звука выбирает клиент потока, сервер принимает любой",
}


def validate_value(key: str, value: object) -> tuple[bool, str]:
    """Проверка одного значения. Возвращает (успех, сообщение об ошибке)."""
    spec = PARAMS_BY_KEY.get(key)
    if spec is None:
        return False, f"Неизвестный параметр: {key}"
    t = spec.type
    try:
        if t == "bool":
            if not isinstance(value, bool):
                return False, f"«{spec.label}»: ожидается да/нет"
        elif t == "int":
            # «да» — не единица, 2.5 — не целое: приведение оставляет такие
            # записи как есть именно затем, чтобы здесь о них сказали. А
            # int(2.5) молча давал 2, и бесконечность роняла запуск целиком
            # OverflowError, которую не ловили.
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                return False, f"«{spec.label}»: ожидается целое число"
            iv = int(value)  # type: ignore[arg-type,call-overload]
            if spec.minimum is not None and iv < spec.minimum:
                return False, f"«{spec.label}»: минимум {int(spec.minimum)}"
            if spec.maximum is not None and iv > spec.maximum:
                return False, f"«{spec.label}»: максимум {int(spec.maximum)}"
        elif t == "float":
            if isinstance(value, bool):
                return False, f"«{spec.label}»: ожидается число"
            fv = float(value)  # type: ignore[arg-type]
            # NaN проходит любые границы — сравнение с ним всегда ложно, — и
            # проверки места и готовности с ним вечно «в норме».
            if not math.isfinite(fv):
                return False, f"«{spec.label}»: ожидается конечное число"
            if spec.minimum is not None and fv < spec.minimum:
                return False, f"«{spec.label}»: минимум {spec.minimum}"
            if spec.maximum is not None and fv > spec.maximum:
                return False, f"«{spec.label}»: максимум {spec.maximum}"
        elif t == "enum":
            if spec.options:
                allowed = {o["value"] for o in spec.options}
                if value not in allowed:
                    return False, (f"«{spec.label}»: допустимые значения — "
                                   f"{', '.join(str(a) for a in sorted(allowed, key=str))}")
            else:
                # Перечень без списка — значения берутся из каталога моделей
                # или движков, и проверяет их своя функция.
                проверка = _ПРОВЕРКИ.get(key)
                ошибки = проверка(value) if проверка is not None else []
                if ошибки:
                    return False, f"«{spec.label}»: " + "; ".join(ошибки[:5])
        elif t == "multi":
            if not isinstance(value, (list, tuple)):
                return False, f"«{spec.label}»: ожидается список"
            if spec.options:
                allowed = {o["value"] for o in spec.options}
                bad = [v for v in value if v not in allowed]
                if bad:
                    return False, f"«{spec.label}»: неизвестные значения — {', '.join(map(str, bad))}"
        elif t == "json":
            if not isinstance(value, (dict, list)):
                return False, f"«{spec.label}»: ожидается объект JSON"
            # Вид — тот же, что у умолчания: словарь замен списком, список
            # причин объектом, карта владельцев списком принимались с 200 и
            # всплывали потом — посреди постобработки, разбора или опроса АТС.
            if isinstance(spec.default, dict) and not isinstance(value, dict):
                return False, f"«{spec.label}»: ожидается объект {{…}}, а не список"
            if isinstance(spec.default, list) and not isinstance(value, list):
                return False, f"«{spec.label}»: ожидается список […], а не объект"
            проверка = _ПРОВЕРКИ.get(key)
            if проверка is not None:
                ошибки = проверка(value)
                if ошибки:
                    return False, f"«{spec.label}»: " + "; ".join(ошибки[:5])
        elif t in ("str", "text"):
            if not isinstance(value, str):
                return False, f"«{spec.label}»: ожидается строка"
            проверка = _ПРОВЕРКИ.get(key)
            if проверка is not None:
                ошибки = проверка(value)
                if ошибки:
                    return False, f"«{spec.label}»: " + "; ".join(ошибки[:5])
    except (TypeError, ValueError, OverflowError):
        return False, f"«{spec.label}»: значение «{value}» неверного типа"
    return True, ""


def validate_server_value(key: str, value: object) -> tuple[bool, str]:
    """`validate_value` плюс проверки, уместные только для настроек сервера."""
    ok, msg = validate_value(key, value)
    if not ok:
        return ok, msg
    проверка = _ПРОВЕРКИ_СЕРВЕРА.get(key)
    ошибки = проверка(value) if проверка is not None else []
    if ошибки:
        spec = PARAMS_BY_KEY.get(key)
        return False, f"«{spec.label if spec else key}»: " + "; ".join(ошибки[:5])
    return True, ""


def validate_all(values: dict[str, object], *, server: bool = False) -> list[str]:
    """Проверяет набор значений, возвращает список сообщений об ошибках.

    `server` — проверки настроек сервера (`validate_server_value`): ими
    PUT /api/settings проверяет весь набор ДО первого изменения. Прежде
    модель и движок отвергал уже `Settings.set` посреди цикла, и ответ 400
    приходил, когда часть значений была применена.
    """
    проверка = validate_server_value if server else validate_value
    errors: list[str] = []
    for key, value in values.items():
        ok, msg = проверка(key, value)
        if not ok:
            errors.append(msg)
    return errors


def stats() -> dict[str, int]:
    return {
        "total": len(PARAMS),
        "groups": len(GROUPS),
        "advanced": sum(1 for p in PARAMS if p.advanced),
        "with_examples": sum(1 for p in PARAMS if p.examples),
        "examples_total": sum(len(p.examples) for p in PARAMS),
    }
