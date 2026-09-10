"""Обслуживание по расписанию: резервная копия базы и сводка о работе.

Оба дела до сих пор лежали на администраторе. Копия базы — строкой в `cron`
из главы «Эксплуатация»; сводка не существовала вовсе, и о том, как шли дела
за неделю, можно было узнать, только открыв раздел аналитики самому.
Отчёта, который никто не открывает, не существует.

Расписание здесь простое, без выражений `cron`: «раз в N часов, считая от
предыдущего раза». Сервер перезапускают, и привязка к календарю потребовала
бы помнить, что уже сделано; отметка «когда в последний раз» лежит в базе и
переживает перезапуск сама.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .instance import INSTANCE_ID
from .logging_setup import get_logger

log = get_logger("maintenance")

#: Ключи отметок в таблице настроек: когда обслуживание выполнялось.
KV_BACKUP = "maintenance.backup_at"
KV_DIGEST = "maintenance.digest_at"

#: Сколько ждать ответа от адреса сводки. Дольше держать служебный поток
#: незачем: сводка — не то, ради чего стоит задерживать уборку хранилища.
DIGEST_TIMEOUT = 20.0


def _пора(db: Any, ключ: str, часов: float) -> bool:
    """Наступил ли срок очередного захода.

    Первый заход после запуска не делается сразу: сервер только поднялся,
    показывать в сводке ещё нечего, а копию базы разумнее снять, когда
    работа уже идёт. Поэтому отсутствующая отметка ставится «сейчас», и
    первый настоящий заход случится через положенный срок.
    """
    if часов <= 0:
        return False
    было = db.get_kv(ключ)
    сейчас = time.time()
    if not было:
        db.set_kv(ключ, сейчас)
        return False
    try:
        прошло = сейчас - float(было)
    except (TypeError, ValueError):
        db.set_kv(ключ, сейчас)
        return False
    return прошло >= часов * 3600


def backup_dir(settings: Any) -> Path:
    """Каталог для копий: заданный настройкой или «backups» рядом с данными."""
    задан = str(settings.get("backup_dir") or "").strip()
    return Path(задан) if задан else Path(settings.paths.data) / "backups"


def make_backup(db: Any, settings: Any) -> Path | None:
    """Снимает копию базы и убирает лишние.

    Копирование идёт командой SQLite «.backup», а не `cp`: база работает в
    режиме WAL, рядом лежат файлы `-wal` и `-shm`, и обычная копия
    получается несогласованной — то есть выглядит как копия и не является
    ею. Ровно об этом предупреждает глава «Эксплуатация», и сервер обязан
    делать так же, как советует человеку.
    """
    каталог = backup_dir(settings)
    try:
        каталог.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Каталог для резервных копий недоступен (%s): %s", каталог, exc)
        return None

    # Подчистка ДО копирования, а не после. Каталог копий по умолчанию лежит
    # на той же файловой системе, что и база: если места хватает ровно на
    # `backup_keep` копий, очередная не помещалась, а старые не удалялись —
    # их удаляет строка после, до которой дело уже не доходило. С этого
    # момента свежих копий не появлялось вообще никогда, молча.
    _подчистить(каталог, int(settings.get("backup_keep") or 7), место_под_ещё_одну=True)

    # Имя несёт и экземпляр: на общей базе серверов несколько, и с точностью
    # до секунды два одновременных захода выбирали одно имя — на выходе
    # оставался один файл вместо двух и вдвое больше ввода-вывода. Тот же
    # случай — «asrctl backup» руками в ту же секунду, что и по расписанию.
    метка = time.strftime("%Y%m%d-%H%M%S")
    цель = каталог / f"asrhub-{метка}-{_кто()}.db"
    начало = time.time()
    try:
        приёмник = sqlite3.connect(str(цель))
        try:
            with приёмник:
                db.conn.backup(приёмник)
        finally:
            приёмник.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("Резервная копия не удалась: %s", exc)
        цель.unlink(missing_ok=True)
        return None

    размер = цель.stat().st_size
    log.info("Резервная копия: %s, %.1f МБ, за %.1f с",
             цель, размер / 1024 / 1024, time.time() - начало)
    return цель


def _кто() -> str:
    """Короткая метка экземпляра для имени файла копии."""
    from .phone_compat import safe_path_key  # noqa: PLC0415

    return safe_path_key(INSTANCE_ID)[:24] or "экземпляр"


def _подчистить(каталог: Path, держать: int, *,
                место_под_ещё_одну: bool = False) -> None:
    """Удаляет копии сверх заданного числа, начиная со старых.

    `место_под_ещё_одну` вычитает единицу: перед снятием новой копии в
    каталоге должно остаться `держать - 1` штук, иначе после успешного
    захода их станет на одну больше заявленного.
    """
    предел = max(1, держать)
    if место_под_ещё_одну:
        предел = max(0, предел - 1)
    try:
        копии = sorted(каталог.glob("asrhub-*.db"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        log.warning("Не удалось перечислить копии в %s: %s", каталог, exc)
        return
    for лишняя in копии[предел:]:
        try:
            лишняя.unlink()
            log.info("Удалена старая копия: %s", лишняя.name)
        except OSError as exc:
            log.warning("Старую копию %s удалить не удалось: %s", лишняя.name, exc)


def build_digest(analytics: Any, settings: Any, *, period: str = "",
                 insights: Any = None) -> dict[str, Any]:
    """Сводка о работе сервера — то же, что в разделе «Аналитика».

    Собирается по всем заданиям, без разреза по владельцу: сводка уходит
    тому, кто отвечает за сервер целиком.

    `insights` — свод по содержанию записей. Если он передан и разбор
    включён, к сводке добавляется раздел о самих разговорах: тональность,
    доля отрицательных, тревожные упоминания, обещания без срока и готовые
    выводы. Именно эти строки и читают: «сервер обработал 4000 записей» —
    это про сервер, а «каждый третий разговор отрицательный» — про дело.
    """
    срок = period or str(settings.get("digest_period") or "week")
    отчёт = analytics.full_report(срок, owner=None)
    сводка = отчёт.get("overview") or {}
    ошибки = отчёт.get("errors") or {}
    очередь = (отчёт.get("queue") or {}).get("overall") or {}
    надёжность = отчёт.get("reliability") or {}
    готовое: dict[str, Any] = {
        "kind": "asrhub.digest",
        "period": срок,
        "generated_at": time.time(),
        # Кто прислал: на общей базе серверов несколько, и сводка без
        # имени отправителя не отвечает на первый же вопрос получателя.
        "instance": INSTANCE_ID,
        "jobs": сводка.get("jobs"),
        "volume": сводка.get("volume"),
        "performance": сводка.get("performance"),
        "quality": сводка.get("quality"),
        "failure_rate": ошибки.get("failure_rate"),
        "top_errors": (ошибки.get("by_code") or [])[:5],
        "queue_wait": {k: очередь.get(k) for k in ("count", "p50", "p95", "max")},
        "first_attempt_rate": надёжность.get("first_attempt_rate"),
        "models": (отчёт.get("models") or [])[:10],
    }
    содержание = _digest_content(insights, settings, срок)
    if содержание:
        готовое["content"] = содержание
    # Подозрительные расшифровки и записи без метки согласия — это про
    # сервер и про порядок, а не про разговоры, поэтому они здесь, а не в
    # разделе о содержании.
    подозрительные = отчёт.get("suspicious") or {}
    if подозрительные.get("assessed"):
        готовое["suspicious"] = {k: подозрительные.get(k) for k in
                                 ("assessed", "flagged", "flagged_share",
                                  "suspect_share")}
    метка = str(settings.get("consent_tag") or "").strip()
    if метка and getattr(analytics, "db", None) is not None:
        срок_дней = int(settings.get("consent_days") or 30)
        без = analytics.db.jobs_without_tag(
            метка, older_than=time.time() - срок_дней * 86400)
        готовое["consent_missing"] = len(без)
    готовое["text"] = digest_text(сводка, ошибки, очередь, содержание,
                                  suspicious=готовое.get("suspicious"),
                                  consent_missing=готовое.get("consent_missing"))
    return готовое


def _digest_content(insights: Any, settings: Any,
                    срок: str) -> dict[str, Any] | None:
    """Раздел сводки о содержании разговоров.

    Берём разделы по отдельности, а не полный отчёт: тому нужны все восемь
    разрезов, все темы и все связи — восемь секунд на архиве в сто тысяч
    записей ради четырёх строк в чате. Здесь хватает свода, выводов и трёх
    списков «что послушать».

    Сбой этого раздела не должен уносить всю сводку: показатели сервера
    полезны и без него, а разбор содержания — надстройка. Раньше сводки
    вообще не было — весь блок целиком в обёртке по этой же причине.
    """
    if insights is None or not bool(settings.get("content_analysis", True)):
        return None
    if not bool(settings.get("digest_content", True)):
        return None
    try:
        свод = insights.summary(срок)
        if not свод.get("records"):
            return None
        категории = insights.categories(срок)
        выводы = insights.findings(срок, категории=категории)
        послушать = {вид: insights.records(вид, срок, limit=3)
                     for вид in ("negative", "alerts", "open_commitments")}
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Раздел содержания в сводку не попал: %s", exc)
        return None
    return {
        "summary": свод,
        "findings": выводы,
        # Категории — верхние пять с долей и изменением: «о чём звонили»
        # в трёх строках чата, без правил и примеров.
        "categories": [
            {к: з.get(к) for к in ("id", "label", "kind", "records", "share",
                                   "previous", "share_previous", "delta")}
            for з in (категории.get("items") or []) if з.get("records")][:5],
        "uncategorized": категории.get("uncategorized"),
        "coverage": insights.index.status() if insights.index else {},
        "highlights": {вид: [
            {к: з.get(к) for к in ("job_id", "filename", "owner", "sentiment",
                                   "sentiment_label", "alerts", "commitments",
                                   "commitments_dated", "created_at")}
            for з in (данные.get("items") or [])]
            for вид, данные in послушать.items()},
    }


def digest_text(сводка: dict[str, Any], ошибки: dict[str, Any],
                очередь: dict[str, Any],
                содержание: dict[str, Any] | None = None, *,
                suspicious: dict[str, Any] | None = None,
                consent_missing: int | None = None) -> str:
    """Та же сводка словами.

    Приёмник входящих сообщений в мессенджере показывает поле `text` и
    ничего не знает про остальные: без этой строки в чат приходил бы
    свёрнутый JSON, который никто не разворачивает.
    """
    задания = сводка.get("jobs") or {}
    объём = сводка.get("volume") or {}
    скорость = сводка.get("performance") or {}
    качество = сводка.get("quality") or {}

    def число(значение: Any, знаков: int = 1) -> str:
        try:
            return f"{float(значение):.{знаков}f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return "—"

    строки = [
        f"ASR Hub, сводка за период «{сводка.get('period') or '—'}»",
        f"Заданий: {задания.get('total') or 0}, "
        f"готово {задания.get('completed') or 0}, "
        f"ошибок {задания.get('failed') or 0}",
        f"Звука обработано: {число(объём.get('audio_hours'))} ч, "
        f"слов {объём.get('words') or 0}",
        f"Скорость: RTF {число((скорость.get('rtf') or {}).get('avg'), 3)} "
        f"(в {число(скорость.get('speedup'))} раза быстрее реального времени)",
    ]
    уверенность = (качество.get("confidence") or {}).get("avg")
    if уверенность is not None:
        строки.append(f"Средняя уверенность: {число(уверенность, 3)}")
    ошибка_wer = (качество.get("wer") or {}).get("avg")
    if ошибка_wer is not None:
        строки.append(f"WER на заданиях с эталоном: {число(ошибка_wer, 3)}")
    if очередь.get("p95") is not None:
        строки.append(f"Ожидание в очереди: p50 {число(очередь.get('p50'))} с, "
                      f"p95 {число(очередь.get('p95'))} с")
    верхние = (ошибки.get("by_code") or [])[:3]
    if верхние:
        перечень = ", ".join(f"{o.get('code')} ×{o.get('count')}" for o in верхние)
        строки.append(f"Чаще всего падало: {перечень}")

    if suspicious and suspicious.get("flagged"):
        строки.append(f"Подозрительных расшифровок: {suspicious['flagged']} из "
                      f"{suspicious['assessed']} ({число(suspicious.get('flagged_share'))} %)")
    if consent_missing:
        строки.append(f"Записей без метки согласия старше срока: {consent_missing}")

    # Про разговоры — отдельным блоком и после показателей сервера: читают
    # сводку сверху вниз, а «сервер жив» — это условие, при котором вторая
    # половина вообще имеет смысл.
    свод = (содержание or {}).get("summary") or {}
    if свод.get("records"):
        строки.append("")
        строки.append(f"О чём говорили ({свод['records']} разобранных записей)")
        доля = свод.get("negative_share")
        if доля is not None:
            строки.append(f"Отрицательных разговоров: {число(доля)} % "
                          f"({свод.get('negative') or 0} из {свод.get('scored') or 0})")
        if свод.get("alert_records"):
            строки.append(f"С упоминанием суда, жалоб и огласки: "
                          f"{свод['alert_records']}")
        if свод.get("commitments_open"):
            строки.append(f"Обещаний без названного срока: "
                          f"{свод['commitments_open']} из "
                          f"{свод.get('commitments') or 0}")
        скрипт = свод.get("compliance")
        if скрипт is not None:
            строки.append(f"Скрипт разговора соблюдён на {число(скрипт * 100, 0)} %")
        if свод.get("frustrated"):
            строки.append(f"С признаками раздражения клиента: {свод['frustrated']} "
                          f"({число(свод.get('frustrated_share'))} %)")
        if свод.get("repeat"):
            строки.append(f"Повторных обращений по нерешённому вопросу: {свод['repeat']} "
                          f"({число(свод.get('repeat_share'))} %)")
        if свод.get("profanity_agent_records"):
            строки.append(f"Нецензурная лексика у сотрудника: "
                          f"{свод['profanity_agent_records']} записей")
        категории = (содержание or {}).get("categories") or []
        if категории:
            части = []
            for к in категории:
                сдвиг = к.get("delta")
                знак = (f" ({'+' if сдвиг > 0 else '−'}{число(abs(сдвиг))} п.п.)"
                        if сдвиг else "")
                части.append(f"{к.get('label')} {число(к.get('share'))} %{знак}")
            строки.append("О чём звонили: " + ", ".join(части))
        без = (содержание or {}).get("uncategorized") or {}
        if без.get("records"):
            строки.append(f"Без категории: {без['records']} записей "
                          f"({число(без.get('share'))} %)")
        for вывод in ((содержание or {}).get("findings") or [])[:3]:
            метка = {"warning": "!", "good": "+"}.get(вывод.get("severity"), "·")
            строки.append(f"  {метка} {вывод.get('text')}")
    return "\n".join(строки)


def send_digest(digest: dict[str, Any], url: str) -> bool:
    """Отправляет сводку на заданный адрес."""
    тело = json.dumps(digest, ensure_ascii=False).encode("utf-8")
    try:
        # Сборка запроса — тоже внутри: негодный адрес в настройках
        # («без схемы», опечатка) роняет уже конструктор, и без этого
        # служебный цикл писал бы в журнал ошибку каждые двадцать секунд.
        запрос = urllib.request.Request(                 # noqa: S310
            url, data=тело, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "ASR Hub"})
        with urllib.request.urlopen(запрос, timeout=DIGEST_TIMEOUT) as ответ:  # noqa: S310
            log.info("Сводка отправлена на %s: код %s", url, ответ.status)
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Сбой отправки никогда не влияет на работу сервера: сводка — это
        # удобство, а не часть обработки заданий.
        log.warning("Сводку отправить не удалось (%s): %s", url, exc)
        return False


def run_scheduled(db: Any, settings: Any, analytics: Any,
                  insights: Any = None) -> dict[str, Any]:
    """Один заход обслуживания. Вызывается служебным циклом.

    Возвращает, что было сделано, — чтобы вызывающий мог это записать, а
    тест проверить, не подглядывая в журнал.
    """
    сделано: dict[str, Any] = {}
    if _пора(db, KV_BACKUP, float(settings.get("backup_interval_hours") or 0)):
        db.set_kv(KV_BACKUP, time.time())
        копия = make_backup(db, settings)
        сделано["backup"] = str(копия) if копия else None

    адрес = str(settings.get("digest_url") or "").strip()
    if адрес and _пора(db, KV_DIGEST, float(settings.get("digest_interval_hours") or 0)):
        db.set_kv(KV_DIGEST, time.time())
        try:
            сводка = build_digest(analytics, settings, insights=insights)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Сводку собрать не удалось: %s", exc)
        else:
            сделано["digest"] = send_digest(сводка, адрес)
    return сделано


def restore(path: Path, target: Path) -> None:
    """Возвращает базу из копии на место рабочей.

    Рабочую базу не удаляем, а переименовываем: восстановление из не той
    копии — обычная ошибка, и она должна быть обратимой. Файлы `-wal` и
    `-shm` от прежней базы обязаны уйти вместе с ней, иначе SQLite
    достроит по ним состояние, которого в восстановленной копии нет.
    """
    if not path.exists():
        raise FileNotFoundError(f"Копия не найдена: {path}")
    # Проверяем ДО того, как трогать рабочую базу. Восстановление из битого
    # файла, обнаруженное после подмены, оставляет человека вообще без
    # базы — с двумя нерабочими файлами вместо одного.
    try:
        with sqlite3.connect(str(path)) as проверка:
            итог = проверка.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise ValueError(
            f"Файл «{path}» не похож на базу ASR Hub: {exc}. "
            "Проверьте, что указана копия, а не архив или журнал.") from exc
    if итог != "ok":
        raise ValueError(f"Копия повреждена, восстановление отменено: {итог}")

    метка = time.strftime("%Y%m%d-%H%M%S")
    if target.exists():
        target.rename(target.with_suffix(f".db.before-restore-{метка}"))
    for хвост in ("-wal", "-shm"):
        спутник = Path(str(target) + хвост)
        if спутник.exists():
            спутник.rename(Path(str(спутник) + f".before-restore-{метка}"))
    shutil.copy2(path, target)
    log.info("База восстановлена из %s", path)
