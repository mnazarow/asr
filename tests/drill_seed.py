"""Архив для проверок раскрытия: задания, разбор, ответы модели, звонки.

Не тест, а общий набор данных для `test_drill*.py`. Пёстрый нарочно: число
отчёта и список под ним сверяются на каждом разрезе, а на однообразных
данных любые два отбора дают один и тот же ответ, и сверка ничего не ловит.
"""
from __future__ import annotations

import time
from typing import Any

ТЕКСТЫ = (
    ("Здравствуйте, компания Ромашка, меня зовут Анна. Договор готов, "
     "отправлю сегодня до восемнадцати. Спасибо, всего доброго!", "хорошо"),
    ("Срок поставки сорван второй раз, я крайне недоволен. Отвратительное "
     "качество. Буду жаловаться в суд.", "плохо"),
    ("Добрый день, подскажите статус заявки на двести тысяч рублей. "
     "Ну, э-э, короче, надо посмотреть. Перезвоню завтра.", "средне"),
    ("Алло, это доставка? Мне обещали привезти вчера, и никого. "
     "Хорошо, жду до вечера, спасибо.", "доставка, срочно"),
)
МОДЕЛИ = (("demo-simulator", "demo"), ("whisper-large", "whisper"), ("", ""))
ВЛАДЕЛЬЦЫ = ("анна", "борис", "вера", "")
ИСТОЧНИКИ = ("web", "api", "", "telephony")
ЯЗЫКИ = ("ru", "en", "")


def наполнить(приложение: Any, *, заданий: int = 96, с_моделью: int = 6,
              сейчас: float | None = None) -> dict[str, Any]:
    """Наполняет базу приложения и отдаёт, что куда легло.

    Задания идут по одному в час назад от `сейчас`; статус, модель, владелец,
    источник и язык чередуются с разными периодами, чтобы разрезы не
    совпадали между собой.
    """
    from asrhub.content_index import ContentIndex

    состояние = приложение.state.hub
    db = состояние.db
    сейчас = сейчас or time.time()
    номера: list[str] = []
    for n in range(заданий):
        текст, метки = ТЕКСТЫ[n % len(ТЕКСТЫ)]
        модель, движок = МОДЕЛИ[n % len(МОДЕЛИ)]
        когда = сейчас - n * 3600 - 60
        расширение = ("wav", "mp3", "ogg", "")[n % 4]
        job_id = db.create_job({
            "id": f"dr{n:04d}",
            "filename": f"разговор-{n}" + (f".{расширение}" if расширение else ""),
            "owner": ВЛАДЕЛЬЦЫ[n % len(ВЛАДЕЛЬЦЫ)], "engine": движок, "model": модель,
            "language": ЯЗЫКИ[n % len(ЯЗЫКИ)], "source": ИСТОЧНИКИ[n % len(ИСТОЧНИКИ)],
            "tags": метки if n % 5 else "", "media_duration_s": 20.0 + (n * 37) % 900})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, job_id))
        if n % 11 == 3:
            db.update_job(job_id, status="failed", finished_at=когда + 5,
                          error_code=("audio_decode_failed", "model_load_failed")[n % 2],
                          error_message="сбой", retries=n % 3)
        elif n % 13 == 5:
            db.update_job(job_id, status="cancelled", finished_at=когда + 2,
                          cancelled_by="user")
        elif n % 17 == 7:
            pass                                             # осталось в очереди
        else:
            db.update_job(
                job_id, status="completed", text=текст, finished_at=когда + 30,
                words_count=len(текст.split()), segments_count=2,
                rtf=0.05 + (n % 9) * 0.03, processing_time_s=3.0 + n % 7,
                queue_time_s=float((n * 13) % 700),
                avg_confidence=0.6 + (n % 8) * 0.05, speakers_count=1 + n % 3,
                retries=1 if n % 10 == 0 else 0,
                snr_db=5.0 + (n % 6) * 5, clipping_share=0.02 if n % 9 == 0 else 0.0,
                loudness_lufs=-30.0 + n % 10, peak_memory_mb=500.0 + n,
                audio_prep_s=0.2, vad_s=0.1, inference_s=2.0 + n % 5,
                cached_from="dr0000" if n % 12 == 6 else "",
                quality_flags=("repeat", "", "silence,tempo", "")[n % 4])
            куски = [к for к in текст.split(". ") if к]
            db.save_segments(job_id, [
                {"start": i * 12.0, "end": i * 12.0 + 10.0, "text": к,
                 "speaker": f"SPEAKER_{i % 2:02d}"}
                for i, к in enumerate(куски)])
        номера.append(job_id)
    ContentIndex(db, состояние.settings).backfill_once(limit=заданий + 10)
    if с_моделью:
        # Смысловой слой на заглушке: модель не нужна, а ответы в базе есть.
        состояние.settings.set("llm_backend", "stub")
        состояние.settings.set("llm_model", "stub")
        готовые = [р["id"] for р in db.query(
            "SELECT id FROM jobs WHERE status='completed' ORDER BY created_at DESC LIMIT ?",
            (с_моделью,))]
        for job_id in готовые:
            состояние.llm_worker.analyze_job(job_id, force=True)
    звонки = _звонки(db, номера, сейчас)
    return {"jobs": номера, "calls": звонки, "now": сейчас}


def _звонки(db: Any, номера: list[str], сейчас: float) -> list[str]:
    """Журнал АТС: звонки с записью, пропущенные, отложенные, без ответа."""
    готовые = [р["id"] for р in db.query(
        "SELECT id FROM jobs WHERE status='completed' ORDER BY created_at DESC")]
    ключи: list[str] = []
    for n in range(60):
        ключ = f"ats{1 + n % 2}:{1700000000 + n}.{n}"
        отвечен = n % 4 != 1
        с_записью = отвечен and n % 3 == 0 and n // 3 < len(готовые)
        пропуск = "" if с_записью or n % 5 else ("короче 5 с", "отложен: нет файла")[n % 2]
        db.save_call(
            ключ, job_id=готовые[n // 3] if с_записью else None,
            owner=ВЛАДЕЛЬЦЫ[n % 3], skipped=пропуск,
            src=f"+7916555{n:04d}", dst=("100", "101", "102")[n % 3],
            direction=("входящий", "исходящий", "внутренний", "")[n % 4],
            queue=("sales", "support", "")[n % 3], agent=("101", "102", "")[n % 3],
            disposition="ANSWERED" if отвечен else ("NO ANSWER", "BUSY")[n % 2],
            duration=30 + n * 3, billsec=(20 + n * 2) if отвечен else 0,
            answered=отвечен, started_at=сейчас - n * 1800 - 30,
            station=f"ats{1 + n % 2}", context="from-trunk")
        ключи.append(ключ)
    return ключи


class Архив:
    """Приложение с наполненной базой — на модуль тестов, а не на каждый.

    Наполнение занимает секунды, а проверки только читают. Окружение
    возвращается как было: соседние модули ставят своё через monkeypatch.
    """

    def __init__(self, каталог: Any, *, auth: bool = False,
                 ключи: dict[str, dict[str, Any]] | None = None, **наполнение: Any) -> None:
        import os

        self._os = os
        self._прежние = {к: os.environ.get(к) for к in
                         ("ASRHUB_DATA_DIR", "ASRHUB_AUTH_ENABLED", "ASRHUB_MODEL",
                          "ASRHUB_ENGINE")}
        os.environ.update(ASRHUB_DATA_DIR=str(каталог),
                          ASRHUB_AUTH_ENABLED="true" if auth else "false",
                          ASRHUB_MODEL="demo-simulator", ASRHUB_ENGINE="demo")
        from asrhub.api.app import create_app
        from asrhub.config import load

        настройки = load()
        for ключ, сведения in (ключи or {}).items():
            настройки.api_keys[ключ] = {"enabled": True, **сведения}
        self.app = create_app(настройки, start_queue=False)
        self.данные = наполнить(self.app, **наполнение)
        self.db = self.app.state.hub.db

    def закрыть(self) -> None:
        for к, v in self._прежние.items():
            if v is None:
                self._os.environ.pop(к, None)
            else:
                self._os.environ[к] = v


def всего(клиент: Any, окно: dict[str, Any] | None = None, f: list[str] | None = None,
          base: str = "jobs", **прочее: Any) -> int:
    """Сколько записей в списке раскрытия — то, что сверяют с числом отчёта."""
    параметры: dict[str, Any] = {"base": base, "f": f or [], "limit": 1, **прочее}
    for граница in ("since", "until"):
        if (окно or {}).get(граница) is not None:
            параметры[граница] = окно[граница]
    ответ = клиент.get("/api/drill", params=параметры)
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert not тело["timed_out"] and not тело["total_capped"], тело
    return int(тело["total"])


def все_строки(клиент: Any, **параметры: Any) -> list[dict[str, Any]]:
    """Список целиком, страница за страницей по указателю."""
    строки: list[dict[str, Any]] = []
    указатель = ""
    for _ in range(500):
        ответ = клиент.get("/api/drill", params={**параметры, "cursor": указатель})
        assert ответ.status_code == 200, ответ.text
        тело = ответ.json()
        строки.extend(тело["items"])
        указатель = тело["next"] or ""
        if not указатель:
            return строки
    raise AssertionError("список не кончается")


# ---------------------------------------------------------------------------
# Браузерные проверки: настоящий сервер и вкладка, копящая ошибки
# ---------------------------------------------------------------------------

БРАУЗЕРЫ = ("/opt/pw-browsers/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome")


def браузер() -> str | None:
    import os
    from pathlib import Path

    for путь in (os.environ.get("PLAYWRIGHT_CHROMIUM", ""), *БРАУЗЕРЫ):
        if путь and Path(путь).exists():
            return путь
    return None


def поднять(приложение: Any) -> tuple[str, Any]:
    """Сервер на свободном порту; отдаёт адрес и «остановить»."""
    import socket
    import threading

    import uvicorn

    with socket.socket() as гнездо:
        гнездо.bind(("127.0.0.1", 0))
        порт = int(гнездо.getsockname()[1])
    служба = uvicorn.Server(uvicorn.Config(приложение, host="127.0.0.1", port=порт,
                                           log_level="warning"))
    поток = threading.Thread(target=служба.run, daemon=True)
    поток.start()
    for _ in range(200):
        if служба.started:
            break
        time.sleep(0.05)
    assert служба.started, "сервер не поднялся"

    def остановить() -> None:
        служба.should_exit = True
        поток.join(timeout=10)

    return f"http://127.0.0.1:{порт}", остановить


class Вкладка:
    """Вкладка браузера, которая копит ошибки консоли и отказы сервера."""

    def __init__(self, адрес: str, *, ширина: int = 1600, высота: int = 1000) -> None:
        from playwright.sync_api import sync_playwright

        self._игра = sync_playwright().start()
        self._браузер = self._игра.chromium.launch(executable_path=браузер())
        self.страница = self._браузер.new_page(viewport={"width": ширина, "height": высота})
        self.ошибки: list[str] = []
        self.запросы: list[str] = []
        self.адрес = адрес
        с = self.страница
        с.on("console", lambda m: self.ошибки.append(f"консоль: {m.text}")
             if m.type == "error" else None)
        с.on("pageerror", lambda e: self.ошибки.append(f"исключение: {e}"))
        с.on("response", lambda r: self.ошибки.append(f"ответ {r.status}: {r.url}")
             if r.status >= 500 else None)
        с.on("request", lambda r: self.запросы.append(r.url))

    def открыть(self, раздел: str) -> Any:
        self.страница.goto(f"{self.адрес}/#{раздел}", wait_until="networkidle")
        self.страница.wait_for_timeout(1200)
        return self.страница

    def раскрытий(self) -> int:
        """Сколько раз страница сходила за списком."""
        return sum(1 for адрес in self.запросы if "/api/drill?" in адрес)

    def чисто(self) -> None:
        настоящие = [о for о in self.ошибки if "audio" not in о and "400" not in о]
        assert not настоящие, "\n".join(настоящие)

    def закрыть(self) -> None:
        self._браузер.close()
        self._игра.stop()
