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
import os
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
#: Когда копия по расписанию в последний раз не удалась.
KV_BACKUP_FAILED = "maintenance.backup_failed_at"
#: Суточная уборка базы и файлов без задания.
KV_SWEEP = "maintenance.sweep_at"

#: После сбоя копии по расписанию — пауза до следующей попытки, секунд.
#: Отметку «копия снята» ставим только после успеха, и без паузы упавшая
#: копия повторялась бы каждые двадцать секунд — полным чтением базы.
ПОВТОР_КОПИИ_С = 3600.0

#: Аренда на снятие копии: на общей базе серверов несколько, и раньше от
#: двойной копии их берегла отметка, поставленная ДО попытки, — ценой того,
#: что упавшая копия повторялась только через сутки.
АРЕНДА_КОПИИ = "maintenance.backup_lease"
СРОК_АРЕНДЫ_КОПИИ_С = 6 * 3600.0

#: Файл без задания младше этого — не сирота, а загрузка, задание по
#: которой ещё не заведено (или заводится соседним сервером).
ВОЗРАСТ_СИРОТЫ_С = 86400.0

#: Сколько дней файлы без задания лежат в карантине `orphans/`, прежде
#: чем уйти насовсем. Карантин, а не удаление: «без задания» может значить
#: и «база не та» — восстановили чужую копию, перепутали каталог данных.
КАРАНТИН_ДНЕЙ = 14

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


def _можно_повторить_копию(db: Any) -> bool:
    """Прошла ли пауза после неудачной копии."""
    try:
        было = float(db.get_kv(KV_BACKUP_FAILED, 0) or 0)
    except (TypeError, ValueError):
        было = 0.0
    return time.time() - было >= ПОВТОР_КОПИИ_С


def backup_dir(settings: Any) -> Path:
    """Каталог для копий: заданный настройкой или «backups» рядом с данными."""
    задан = str(settings.get("backup_dir") or "").strip()
    return Path(задан) if задан else Path(settings.paths.data) / "backups"


def как_у_каталога(каталог: Path, *пути: Path) -> None:
    """Отдаёт файлы владельцу каталога данных, если работаем от root.

    Резервную копию и восстановление из командной строки запускают через
    sudo: каталог данных (0750) принадлежит пользователю службы, и `stop`
    требует root. Всё, что при этом создавал Python, оставалось root:
    восстановленная база открывалась службой только на чтение («attempt to
    write a readonly database» на первом же задании, хотя /health отвечал),
    а каталог копий, созданный первым `service.sh backup`, закрывал дорогу
    всем следующим копиям по расписанию. Пути, которых нет, пропускаются.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        свой = каталог.stat()
    except OSError:
        return
    if свой.st_uid == 0:
        return
    for путь in пути:
        try:
            if путь.exists():
                os.chown(путь, свой.st_uid, свой.st_gid)
        except OSError as exc:
            log.warning("Не удалось вернуть владельца %s: %s", путь, exc)


def make_backup(db: Any, settings: Any) -> Path | None:
    """Снимает копию базы и убирает лишние.

    Копирование идёт командой SQLite «.backup», а не `cp`: база работает в
    режиме WAL, рядом лежат файлы `-wal` и `-shm`, и обычная копия
    получается несогласованной — то есть выглядит как копия и не является
    ею. Ровно об этом предупреждает глава «Эксплуатация», и сервер обязан
    делать так же, как советует человеку.
    """
    каталог = backup_dir(settings)
    новый = not каталог.exists()
    try:
        каталог.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Каталог для резервных копий недоступен (%s): %s", каталог, exc)
        return None
    if новый:
        как_у_каталога(Path(settings.paths.data), каталог)

    # Подчистка ДО копирования, а не после. Каталог копий по умолчанию лежит
    # на той же файловой системе, что и база: если места хватает ровно на
    # `backup_keep` копий, очередная не помещалась, а старые не удалялись —
    # их удаляет строка после, до которой дело уже не доходило. С этого
    # момента свежих копий не появлялось вообще никогда, молча.
    # Ноль — «предела по числу нет», и это документированное значение
    # параметра. `int(значение or 7)` превращал его в семь и молча сносил
    # самые старые копии — те, ради которых предел и ставили в ноль.
    предел = _срок(settings, "backup_keep", 0)
    if предел > 0:
        _подчистить(каталог, предел, место_под_ещё_одну=True)

    # Имя несёт и экземпляр: на общей базе серверов несколько, и с точностью
    # до секунды два одновременных захода выбирали одно имя — на выходе
    # оставался один файл вместо двух и вдвое больше ввода-вывода. Тот же
    # случай — «asrctl backup» руками в ту же секунду, что и по расписанию.
    метка = time.strftime("%Y%m%d-%H%M%S")
    цель = каталог / f"asrhub-{метка}-{_кто()}.db"
    начало = time.time()
    try:
        # Копия базы — вся база разговоров: 0600, как и архивы копий.
        цель.touch(mode=0o600, exist_ok=True)
        os.chmod(цель, 0o600)
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

    как_у_каталога(Path(settings.paths.data), цель)
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
    # Дрейф уверенности — только когда есть что сказать: «без дрейфа» в
    # каждой сводке — это шум, который перестают читать.
    дрейф = отчёт.get("drift") or {}
    сдвинулись = [м for м in [дрейф.get("overall") or {}, *(дрейф.get("models") or [])]
                  if м.get("verdict") in ("warning", "critical")]
    if сдвинулись:
        готовое["drift"] = [{k: м.get(k) for k in ("key", "verdict", "shift_relative")}
                            | {"p": (м.get("ks") or {}).get("p"),
                               "median_before": (м.get("baseline") or {}).get("p50"),
                               "median_now": (м.get("current") or {}).get("p50")}
                            for м in сдвинулись]
    # Плохой звук — тоже только когда он есть: доля шумных и срезанных
    # записей и худшие источники. Это про вход, а не про модель, и
    # лечится на стороне телефонии, а не сервера.
    звук = отчёт.get("audio") or {}
    if звук.get("bad_audio_jobs"):
        готовое["bad_audio"] = {
            "jobs": звук.get("bad_audio_jobs"), "measured": звук.get("measured_jobs"),
            "share": звук.get("bad_audio_share"), "noisy": звук.get("noisy_jobs"),
            "clipped": звук.get("clipped_jobs"),
            "sources": [и for и in (звук.get("bad_audio_by_source") or []) if и.get("bad")][:3],
        }
    # Очередь проверки и согласие моделей — когда есть о чём сказать.
    if getattr(analytics, "db", None) is not None and settings.get("review_enabled", True):
        from .analytics import PERIODS  # noqa: PLC0415

        счёт = analytics.db.review_counts(
            since=time.time() - (PERIODS.get(срок) or 7 * 86400))
        if any(счёт.get(к) for к in ("pending", "done", "skipped")):
            готовое["review"] = счёт
    согласие = отчёт.get("agreement") or {}
    if согласие.get("verdict") in ("warning", "critical"):
        готовое["agreement"] = {k: согласие.get(k) for k in
                                ("verdict", "checks", "wer_avg", "previous_wer_avg", "growth")}
    # Смысловой слой: исходы и причины по ответам модели — когда они есть.
    if getattr(analytics, "db", None) is not None and \
            str(settings.get("llm_backend") or "off") != "off":
        from .analytics import PERIODS  # noqa: PLC0415
        from .llm import tasks as llm_tasks  # noqa: PLC0415

        свод_модели = analytics.db.llm_stats(
            llm_tasks.VERSION, time.time() - (PERIODS.get(срок) or 7 * 86400))
        ответы = [р for р in свод_модели["rows"] if not р.get("error")]
        if ответы:
            готовое["llm"] = {"records": свод_модели["total"], "analyzed": len(ответы),
                              **llm_tasks.summarize_for_digest(ответы)}
    метка = str(settings.get("consent_tag") or "").strip()
    if метка and getattr(analytics, "db", None) is not None:
        # Ноль — осмысленное значение: «предупреждать про любую запись без
        # метки». В Python он ложен, и `or 30` превращал его в месяц —
        # тот же дефект, ради которого тридцатью строками ниже написана
        # `retention_days` с длинным объяснением. Отчёт о согласиях и
        # строка в сводке показывали «нарушений нет», пока записи не
        # перевалят за тридцать дней.
        срок_дней = int(_срок(settings, "consent_days", 30))
        без = analytics.db.jobs_without_tag(
            метка, older_than=time.time() - срок_дней * 86400)
        готовое["consent_missing"] = len(без)
    готовое["text"] = digest_text(сводка, ошибки, очередь, содержание,
                                  suspicious=готовое.get("suspicious"),
                                  consent_missing=готовое.get("consent_missing"),
                                  drift=готовое.get("drift"),
                                  bad_audio=готовое.get("bad_audio"),
                                  review=готовое.get("review"),
                                  agreement=готовое.get("agreement"),
                                  llm=готовое.get("llm"))
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
        "trackers": категории.get("trackers") or [],
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
                consent_missing: int | None = None,
                drift: list[dict[str, Any]] | None = None,
                bad_audio: dict[str, Any] | None = None,
                review: dict[str, Any] | None = None,
                agreement: dict[str, Any] | None = None,
                llm: dict[str, Any] | None = None) -> str:
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
    if bad_audio and bad_audio.get("jobs"):
        источники = ", ".join(f"{и.get('key')} — {число((и.get('share') or 0) * 100, 0)} %"
                              for и in bad_audio.get("sources") or [])
        строки.append(
            f"Плохой звук на входе: {bad_audio['jobs']} из {bad_audio.get('measured') or 0} "
            f"записей ({число((bad_audio.get('share') or 0) * 100, 0)} %): "
            f"шумных {bad_audio.get('noisy') or 0}, с клиппингом {bad_audio.get('clipped') or 0}"
            + (f"; хуже всего {источники}" if источники else ""))
    if review:
        строки.append(f"Очередь ручной проверки: ожидают {review.get('pending') or 0}, "
                      f"проверено {review.get('done') or 0}, пропущено {review.get('skipped') or 0}")
    if agreement:
        уровень = "критично" if agreement.get("verdict") == "critical" else "предупреждение"
        строки.append(
            f"Согласие моделей: {уровень} — расхождение "
            f"{число((agreement.get('previous_wer_avg') or 0) * 100, 1)} % → "
            f"{число((agreement.get('wer_avg') or 0) * 100, 1)} % "
            f"по {agreement.get('checks') or 0} контрольным прогонам")
    for д in drift or []:
        уровень = "критично" if д.get("verdict") == "critical" else "предупреждение"
        кто = "по всем моделям" if д.get("key") == "all" else f"у модели {д.get('key')}"
        строки.append(f"Дрейф уверенности {кто}: {уровень} — медиана "
                      f"{число(д.get('median_before'), 3)} → {число(д.get('median_now'), 3)}, "
                      f"сдвиг {число((д.get('shift_relative') or 0) * 100, 1)} %, "
                      f"p = {число(д.get('p'), 3)}")

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
        if свод.get("agent_score") is not None:
            строки.append(f"Балл оператора: {число(свод['agent_score'], 0)} из 100"
                          + (f", нарушений — в {свод['violation_records']} записях"
                             if свод.get("violation_records") else ""))
        if свод.get("empathy") is not None:
            строки.append(f"Индекс эмпатии операторов: {число(свод['empathy'], 0)}")
        if свод.get("objections") and свод.get("objections_unhandled_share") is not None:
            строки.append(f"Возражений без отработки: {свод.get('objections_unhandled') or 0} "
                          f"из {свод['objections']} "
                          f"({число(свод['objections_unhandled_share'])} %)")
        трекеры = (содержание or {}).get("trackers") or []
        if трекеры:
            строки.append("Трекеры: " + ", ".join(
                f"{т.get('label')} — {т.get('hits')} в {т.get('records')} записях"
                for т in трекеры[:5]))
        for вывод in ((содержание or {}).get("findings") or [])[:3]:
            метка = {"warning": "!", "good": "+"}.get(вывод.get("severity"), "·")
            строки.append(f"  {метка} {вывод.get('text')}")
    if llm and llm.get("analyzed"):
        строки.append("")
        строки.append(f"По ответам языковой модели ({llm['analyzed']} из "
                      f"{llm.get('records') or 0} записей)")
        if llm.get("outcomes"):
            строки.append("Исходы: " + ", ".join(
                f"{и['key']} {число(и['share'], 0)} %" for и in llm["outcomes"][:5]))
        if llm.get("reasons"):
            строки.append("Причины обращений: " + ", ".join(
                f"{п['key']} {число(п['share'], 0)} %" for п in llm["reasons"][:5]))
        if llm.get("resolved_share") is not None:
            строки.append(f"Вопрос решён в разговоре: {число(llm['resolved_share'], 0)} %")
        if llm.get("actions"):
            строки.append(f"Действий к исполнению: {llm['actions']} в "
                          f"{llm.get('records_with_actions') or 0} записях")
    return "\n".join(строки)


def send_json(payload: dict[str, Any], url: str, *, what: str = "сводка") -> bool:
    """Отправляет JSON на заданный адрес; сбой — в журнал, не наружу."""
    тело = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        # Сборка запроса — тоже внутри: негодный адрес в настройках
        # («без схемы», опечатка) роняет уже конструктор, и без этого
        # служебный цикл писал бы в журнал ошибку каждые двадцать секунд.
        запрос = urllib.request.Request(                 # noqa: S310
            url, data=тело, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "ASR Hub"})
        with urllib.request.urlopen(запрос, timeout=DIGEST_TIMEOUT) as ответ:  # noqa: S310
            log.info("%s: отправлено на %s, код %s", what.capitalize(), url, ответ.status)
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Сбой отправки никогда не влияет на работу сервера: сводка и
        # трекеры — удобство, а не часть обработки заданий.
        log.warning("%s: отправить не удалось (%s): %s", what.capitalize(), url, exc)
        return False


def send_digest(digest: dict[str, Any], url: str) -> bool:
    """Отправляет сводку на заданный адрес."""
    return send_json(digest, url, what="сводка")


#: Отметки суточных заходов здоровья распознавания.
KV_REVIEW = "review_sampled_at"
#: Когда в последний раз набирали проверки качества работы операторов.
KV_QA = "qa_sampled_at"
KV_CONTROL = "control_sampled_at"

#: Когда последний раз обновляли справочник сотрудников.
KV_EMPLOYEES = "employees_synced_at"

#: Когда последний раз подчищали историю очереди к языковой модели.
KV_LLM_QUEUE = "llm_queue_pruned_at"


def _справочник(db: Any, settings: Any) -> dict[str, Any] | None:
    """Обновление справочника сотрудников по расписанию.

    Возвращает сводку импорта, `None` — если ходить ещё рано или не за чем.

    Сбой обновления намеренно не выходит наружу. Справочник — это удобство
    (имя вместо номера в отчёте), а не условие работы сервера, и падение
    служебного потока из-за недоступного портала остановило бы заодно
    резервные копии и уборку хранилища — то есть поменяло бы мелкое
    неудобство на настоящую аварию. Поэтому всё, что случилось, уходит в
    журнал и событием в базу: там его видно в разделе «Журнал», рядом с
    остальной жизнью сервера.
    """
    if not settings.get("employees_sync_enabled"):
        return None
    адрес = str(settings.get("employees_url") or "").strip()
    if not адрес:
        # Расписание включено, а адрес не задан — это не ошибка захода, но
        # и молчать нельзя: человек включил обновление и ждёт его.
        log.warning("Обновление справочника включено, но employees_url не задан")
        return None
    if not _пора(db, KV_EMPLOYEES, float(_срок(settings, "employees_sync_hours", 24))):
        return None
    db.set_kv(KV_EMPLOYEES, time.time())
    from . import employees as справочник_сотрудников  # noqa: PLC0415

    try:
        итог = справочник_сотрудников.импорт(
            db, url=адрес, source="справочник",
            deactivate_missing=bool(settings.get("employees_deactivate_missing", True)))
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Справочник сотрудников не обновлён: %s", exc)
        db.add_event(None, "employees_sync_failed",
                     f"Справочник не обновлён: {exc}", {"url": адрес})
        return {"error": str(exc), "url": адрес}
    db.add_event(None, "employees_synced",
                 f"Справочник обновлён: +{итог.get('added')} новых, "
                 f"{итог.get('updated')} изменено, {итог.get('deactivated')} уволено",
                 {"url": адрес, "rows": итог.get("rows"),
                  "skipped": итог.get("skipped")})
    return итог


def run_scheduled(db: Any, settings: Any, analytics: Any,
                  insights: Any = None, queue: Any = None) -> dict[str, Any]:
    """Один заход обслуживания. Вызывается служебным циклом.

    Возвращает, что было сделано, — чтобы вызывающий мог это записать, а
    тест проверить, не подглядывая в журнал.
    """
    from . import review  # noqa: PLC0415

    сделано: dict[str, Any] = {}
    # Здоровье распознавания — раз в сутки: очередь ручной проверки и
    # контрольные прогоны. Сбой одного не мешает другому и не мешает копии.
    if settings.get("review_enabled", True) and _пора(db, KV_REVIEW, 24.0):
        db.set_kv(KV_REVIEW, time.time())
        try:
            сделано["review"] = review.sample_review(db, settings)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Очередь проверки не пополнена: %s", exc)
    if str(settings.get("control_model") or "").strip() and queue is not None \
            and _пора(db, KV_CONTROL, 24.0):
        db.set_kv(KV_CONTROL, time.time())
        try:
            сделано["control"] = review.sample_control(db, settings, queue)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Контрольные прогоны не поставлены: %s", exc)
    # Контроль качества работы операторов — чаще, чем раз в сутки: порция
    # считается скользящим окном, и заход раз в час держит поток проверок
    # ровным. Пачка в двести проверок, поставленная в полночь, не делается
    # никогда — а десять в час разбираются по ходу дня.
    if settings.get("qa_enabled", False) and _пора(db, KV_QA, 1.0):
        db.set_kv(KV_QA, time.time())
        try:
            from . import qa  # noqa: PLC0415

            сделано["qa"] = len(qa.набрать(db, settings))
        except Exception as exc:                             # noqa: BLE001
            log.warning("Проверки качества не назначены: %s", exc)
    # Справочник сотрудников: раз в employees_sync_hours часов забрать
    # выгрузку по employees_url. Стоит до копии намеренно — чтобы свежий
    # справочник попал в неё же, а не оказался на сутки старше базы.
    справочник = _справочник(db, settings)
    if справочник is not None:
        сделано["employees"] = справочник
    # История очереди к языковой модели. По ней строится график раздела и
    # разбираются сбои, но не вечно: на сервере, разбирающем тысячу записей
    # в сутки, за год таблица станет больше самого архива разборов.
    if _пора(db, KV_LLM_QUEUE, 24.0):
        db.set_kv(KV_LLM_QUEUE, time.time())
        try:
            убрано = db.llmq_prune(_срок(settings, "llm_queue_keep_days", 14))
            if убрано:
                сделано["llm_queue"] = {"removed": убрано}
        except Exception as exc:                             # noqa: BLE001
            log.warning("История очереди разбора не подчищена: %s", exc)

    # Копии снимает модуль `backup`: он собирает архив с описью, настройками
    # и — по виду копии — базой. Расписание там же: раз в сутки в назначенное
    # время или по интервалу в часах, если он задан.
    from . import backup as резерв  # noqa: PLC0415

    if резерв.пора(db, settings, ключ=KV_BACKUP) and _можно_повторить_копию(db):
        # Отметка «снята» — только после успеха. Прежде она ставилась до
        # попытки, и упавшая копия (полный диск, чужие права на каталог)
        # повторялась лишь на следующие сутки: неделя сбоев — неделя без
        # копий. Двойную копию на общей базе теперь держит аренда.
        держатель = db.lease_take(АРЕНДА_КОПИИ, INSTANCE_ID, СРОК_АРЕНДЫ_КОПИИ_С)
        if держатель is None:
            try:
                копия = резерв.создать(db, settings,
                                       kind=str(settings.get("backup_kind") or "full"),
                                       comment="по расписанию")
                db.set_kv(KV_BACKUP, time.time())
                сделано["backup"] = копия.get("name")
            except Exception as exc:                         # noqa: BLE001
                # Сбой копии не должен ронять служебный заход: следом идут
                # уборка и сводка, и они к копии отношения не имеют.
                log.warning("Резервная копия не снята: %s", exc)
                db.set_kv(KV_BACKUP_FAILED, time.time())
                сделано["backup"] = None
                сделано["backup_error"] = str(exc)
            finally:
                db.lease_release(АРЕНДА_КОПИИ, INSTANCE_ID)

    # Суточная уборка: строки и файлы без задания, следы удалённых
    # разговоров в указателе и словаре форм. Раз в сутки, а не с часовой
    # уборкой по сроку: подметание таблиц стоит секунд под блокировкой.
    if _пора(db, KV_SWEEP, 24.0):
        db.set_kv(KV_SWEEP, time.time())
        try:
            сделано["sweep"] = db.sweep_orphans()
        except Exception as exc:                             # noqa: BLE001
            log.warning("Суточная уборка базы не прошла: %s", exc)
        try:
            сделано["orphan_files"] = подмести_файлы(db, settings)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Уборка файлов без задания не прошла: %s", exc)

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


def restore(path: Path, target: Path, settings: Any = None) -> str:
    """Возвращает базу из копии на место рабочей — сервер остановлен.

    Работу делает `backup.восстановить_из_файла`, тот же путь, что у
    восстановления из интерфейса: копия (файл базы или архив
    `*.asrhub.tar.gz`) проверяется ДО того, как трогать рабочую базу, пути
    заданий переписываются под этот каталог данных, прежняя база уходит
    рядом под именем `….before-restore-ДАТА` вместе со своим журналом —
    под именами, по которым SQLite его найдёт. Прежде `-wal` откладывался
    как «asrhub.db-wal.before-restore-…», и последние транзакции прежней
    базы при её возвращении терялись.

    Ошибки — `FileNotFoundError` и `ValueError` с объяснением: их печатает
    `service.sh restore`. Возвращает имя сохранённой прежней базы.
    """
    from . import backup as резерв  # noqa: PLC0415
    from .errors import ASRHubError  # noqa: PLC0415

    if not Path(path).exists():
        raise FileNotFoundError(f"Копия не найдена: {path}")
    настройки = settings if settings is not None else _НастройкиБазы(Path(target))
    try:
        итог = резерв.восстановить_из_файла(path, настройки)
    except ASRHubError as exc:
        текст = exc.message.rstrip(". ") + "."
        raise ValueError(f"{текст} {exc.hint}" if exc.hint else текст) from exc
    прежняя = str(итог.get("previous") or "")
    спутники = [Path(target).with_name(прежняя + хвост) for хвост in ("", "-wal", "-shm")] \
        if прежняя else []
    как_у_каталога(Path(target).parent, Path(target), *спутники)
    log.info("База восстановлена из %s; прежняя — %s", path, прежняя or "—")
    return прежняя


class _НастройкиБазы:
    """Настройки для восстановления, когда известен только путь базы."""

    def __init__(self, база: Path) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        self.paths = SimpleNamespace(db=база, data=база.parent, tmp=база.parent / "tmp")

    def get(self, ключ: str, по_умолчанию: Any = None) -> Any:
        return по_умолчанию


def подмести_файлы(db: Any, settings: Any, *, сейчас: float | None = None) -> dict[str, Any]:
    """Файлы загрузок и результатов, на которые не ссылается ни одно задание.

    Откуда они берутся: восстановление «на вчера» оставляет файлы заданий,
    появившихся после копии; перенос базы с другого сервера — файлы, пути к
    которым в базе вели в чужой каталог; сбой между записью файла и заведением
    задания. Удаление задания и уборка по сроку ходят по таблице заданий и
    таких файлов не видят никогда — они лежали вечно, а в них разговоры.

    Сироты не удаляются, а переезжают в карантин `orphans/<дата>/` в каталоге
    данных и уходят насовсем через `КАРАНТИН_ДНЕЙ`. Предохранитель: если
    сирот больше половины всех файлов, уборка не делает ничего и говорит об
    этом — так выглядит не мусор, а чужая база (не тот каталог данных, не та
    копия), и переносить в карантин весь архив нельзя.
    """
    момент = time.time() if сейчас is None else float(сейчас)
    загрузки = Path(settings.paths.uploads)
    результаты = Path(settings.paths.results)
    карантин = Path(settings.paths.data) / "orphans"
    файлы, каталоги, номера = db.job_file_refs()

    def своё(путь: Path) -> str:
        try:
            return str(путь.resolve())
        except OSError:
            return str(путь)

    кандидаты: list[tuple[str, Path]] = []
    всего = 0
    for вид, корень in (("uploads", загрузки), ("results", результаты)):
        if not корень.is_dir():
            continue
        for путь in корень.iterdir():
            всего += 1
            if вид == "uploads":
                имя = путь.name
                основа = имя.replace(".redacted", "", 1) if ".redacted" in имя else имя
                if своё(путь) in файлы or своё(путь.with_name(основа)) in файлы:
                    continue
            else:
                номер = путь.name[:-5] if путь.name.endswith(".prev") else путь.name
                if номер in номера or своё(путь) in каталоги:
                    continue
            try:
                возраст = момент - путь.stat().st_mtime
            except OSError:
                continue
            if возраст < ВОЗРАСТ_СИРОТЫ_С:
                continue
            кандидаты.append((вид, путь))

    итог: dict[str, Any] = {"orphans": len(кандидаты), "moved": 0, "expired": 0,
                            "total": всего}
    if кандидаты and len(кандидаты) > 20 and len(кандидаты) * 2 > всего:
        итог["skipped"] = (
            f"файлов без задания {len(кандидаты)} из {всего} — больше половины: так "
            "выглядит не мусор, а чужая база. Проверьте каталог данных и то, из "
            "какой копии восстанавливали; уборка файлы не трогала.")
        log.warning("Уборка файлов без задания пропущена: %s", итог["skipped"])
        try:
            db.add_event(None, "orphans_skipped", итог["skipped"])
        except Exception:                                    # noqa: BLE001
            pass
        return итог

    сегодня = карантин / time.strftime("%Y-%m-%d", time.localtime(момент))
    for вид, путь in кандидаты:
        куда = сегодня / вид / путь.name
        try:
            куда.parent.mkdir(parents=True, exist_ok=True)
            if куда.exists():
                куда = куда.with_name(f"{куда.name}.{int(момент)}")
            try:
                путь.replace(куда)
            except OSError:
                # Другой том (каталог загрузок вынесен ссылкой) — копией.
                shutil.move(str(путь), str(куда))
            итог["moved"] += 1
        except OSError as exc:
            log.warning("Файл без задания %s не убран в карантин: %s", путь, exc)
    if итог["moved"]:
        try:
            os.chmod(карантин, 0o700)
        except OSError:
            pass
        log.warning("Файлов без задания убрано в карантин: %d (%s); через %d дн. "
                    "они будут удалены", итог["moved"], сегодня, КАРАНТИН_ДНЕЙ)
        try:
            db.add_event(None, "orphans_moved",
                         f"Файлов без задания: {итог['moved']} — в карантине "
                         f"{сегодня.name}, удалятся через {КАРАНТИН_ДНЕЙ} дн.",
                         {"moved": итог["moved"], "dir": str(сегодня)})
        except Exception:                                    # noqa: BLE001
            pass

    # Срок карантина: каталоги называются датой, по ней и считаем.
    if карантин.is_dir():
        граница = time.strftime("%Y-%m-%d",
                                time.localtime(момент - КАРАНТИН_ДНЕЙ * 86400))
        for каталог in карантин.iterdir():
            if каталог.is_dir() and len(каталог.name) == 10 and каталог.name < граница:
                shutil.rmtree(каталог, ignore_errors=True)
                итог["expired"] += 1
    return итог


#: Срок хранения результатов из настроек.
#:
#: Ноль — «хранить бессрочно», и это документированное значение параметра
#: (`result_retention_days`, минимум 0). Прежнее `int(settings.get(...) or 30)`
#: превращало ноль в месяц, потому что ноль ложен: администратор просил
#: ничего не удалять, а часовая уборка сносила всё старше тридцати дней —
#: вместе с исходными файлами. Пустое значение и None — это «не задано»,
#: там месяц по умолчанию уместен.
def _срок(settings: Any, ключ: str, default: int = 30) -> int:
    """Числовая настройка, у которой ноль — значение, а не «не задано».

    В Python ноль ложен, поэтому `int(значение or 30)` молча превращает
    «хранить вечно» в месяц, а «предупреждать про любую запись» — в
    «предупреждать про записи старше месяца». Каталог у обоих параметров
    объявляет минимум 0 и объясняет, что он означает.
    """
    значение = settings.get(ключ)
    if значение is None or значение == "":
        return default
    try:
        return max(0, int(значение))
    except (TypeError, ValueError):
        return default


def retention_days(settings: Any, default: int = 30) -> int:
    return _срок(settings, "result_retention_days", default)
