"""Снимки разделов для руководства — с тем меню и теми разделами, что есть.

Снимки снимались руками по ходу работы и устаревали: к осени на большинстве
было меню из 11 пунктов вместо 30. Этот сценарий снимает обзорные снимки
разделов заново на демонстрационном архиве (тот же наполнитель, что у
браузерных проверок) — запускайте его перед выпуском, а потом посмотрите
глазами, что изменилось.

    PYTHONPATH=server python3 docs/make_screenshots.py            # все
    PYTHONPATH=server python3 docs/make_screenshots.py 03-results  # один

Кроме обзорных снимков разделов — снимки состояний (`СОСТОЯНИЯ`):
открытая карточка записи и её вкладки, карточка модели, окно горячих
клавиш, вкладки «АТС», «Аналитики записей» и других разделов. Состояние
открывается вызовом той же функции интерфейса, что и щелчок (`window.__asrhub`),
а не поиском кнопки по тексту: так снимок не ломается от переименования.

Тема — светлая: так снимки читаются и на экране, и на бумаге (Word, PDF).
Не снимаются разделы, которым демонстрационного архива мало, чтобы снимок
что-то показал: «Очередь LLM» (нужна идущая очередь разбора), «Резервные
копии» (нужны копии) и экран входа (нужен стенд со входом).

Из `docs/build.sh`: `bash docs/build.sh --screenshots` — снимки, потом сборка.
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ / "tests"))
sys.path.insert(0, str(КОРЕНЬ / "server"))

#: Снимок → (раздел, снимать всю страницу).
СНИМКИ: dict[str, tuple[str, bool]] = {
    "01-transcribe": ("transcribe", True),
    "02-queue": ("queue", True),
    "03-results": ("results", False),
    "04-analytics": ("analytics", False),
    "05-models": ("models", False),
    "07-settings": ("settings", False),
    "08-system": ("system", False),
    "09-logs": ("logs", False),
    "10-help": ("help", False),
    "18-monitoring": ("monitoring", False),
    "28-content-summary": ("content", False),
    "71-trends": ("trends", False),
    "72-telephony": ("telephony", False),
    "74-pbx": ("pbx", False),
    "79-dashboard": ("dashboard", False),
    "81-rescan": ("rescan", False),
    "83-staff": ("staff", False),
    "85-dialplan": ("dialplan", False),
    "86-qc": ("qc", True),
    "87-manual": ("manual/32-quality-control", False),
}


#: Снимок состояния → (раздел, действие в странице — выражение JS, которое
#: открывает нужное состояние; `ЗАПИСЬ` подставляется идентификатором
#: готовой записи архива, `МОДЕЛЬ` — модели).
СОСТОЯНИЯ: dict[str, tuple[str, str]] = {
    "13-model-card": ("models", "window.__asrhub.showModel(МОДЕЛЬ)"),
    "14-job-card": ("results", "window.__asrhub.openJob(ЗАПИСЬ)"),
    "17-hotkeys": ("transcribe", "window.__asrhub.showHotkeys()"),
    "21-waveform": ("results", "window.__asrhub.openJob(ЗАПИСЬ)"),
    "29-content-breakdowns": ("content", "РАЗДЕЛ_ВКЛАДКА('groups')"),
    "30-content-topics": ("content", "РАЗДЕЛ_ВКЛАДКА('topics')"),
    "31-content-records": ("content", "РАЗДЕЛ_ВКЛАДКА('records')"),
    "56-content-agents": ("content", "РАЗДЕЛ_ВКЛАДКА('agents')"),
    "64-job-reference-tab": ("results", "window.__asrhub.openJob(ЗАПИСЬ, {tab: 'reference'})"),
    "69-job-llm": ("results", "window.__asrhub.openJob(ЗАПИСЬ, {tab: 'analysis'})"),
    "75-employees": ("employees", ""),
    "82-voice": ("voice", ""),
    "84-agents": ("pbx", "ВКЛАДКА_АТС('agents')"),
    "25-load-charts": ("pbx", "ВКЛАДКА_АТС('load')"),
}

#: Как переключить вкладку раздела: «АТС» и «Аналитика записей».
_ВКЛАДКИ = """
window.ВКЛАДКА_АТС = (имя) => { const к = document.querySelector(
  `#pbx-tabs button[data-tab="${имя}"]`); if (к) к.click(); };
window.РАЗДЕЛ_ВКЛАДКА = (имя) => { const к = document.querySelector(
  `#content-tabs button[data-tab="${имя}"]`); if (к) к.click(); };
"""


def _контроль_качества(хаб) -> None:
    """Группы и журнал опросов — чтобы раздел не был пустым."""
    вопросы = [{"text": "Оцените вежливость оператора от одного до пяти.", "min": 1, "max": 5},
               {"text": "Решён ли ваш вопрос? Один — да, два — нет.", "min": 1, "max": 2}]
    н = хаб.settings
    н.set("qc_sip_host", "pbx.example.local")
    н.set("qc_sip_user", "900")
    н.set("qc_tts_command", "ffmpeg -y -f lavfi -i sine=frequency=440:duration=0.5 {out}")
    н.set("qc_groups", [
        {"name": "Отдел продаж", "extensions": "101, 102, 110-119", "direction": "in",
         "min_talk_s": 20, "optout_digit": "0", "questions": вопросы,
         "greeting": "Здравствуйте! Вы недавно звонили нам. Ответьте на два коротких вопроса."},
        {"name": "Техподдержка", "extensions": "200-210", "direction": "any",
         "min_talk_s": 30, "questions": вопросы[:1]}])
    случай = random.Random(7)
    сейчас = time.time()
    for _ in range(60):
        группа, добавочный = случай.choice([("Отдел продаж", "101"), ("Отдел продаж", "112"),
                                           ("Техподдержка", "203")])
        итог = случай.choices(["пройден", "частично", "не дозвонились", "ждёт"], [60, 10, 20, 10])[0]
        ответы, оценка = [], None
        if итог in ("пройден", "частично"):
            for н_, в in enumerate(вопросы if группа == "Отдел продаж" else вопросы[:1]):
                цифра = None if (итог == "частично" and н_) else случай.choice(
                    range(в["min"], в["max"] + 1))
                ответы.append({**в, "digit": цифра})
            оценка = float(ответы[0]["digit"])
        когда = сейчас - случай.uniform(600, 20 * 86400)
        хаб.db.execute(
            "INSERT INTO qc_surveys (id, call_key, kind, group_name, ext, number, call_at, "
            "created_at, due_at, finished_at, status, attempts, answers, score, score_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], uuid.uuid4().hex, "опрос", группа, добавочный,
             f"7999555{случай.randint(0, 9999):04d}", когда, когда, когда + 300,
             None if итог == "ждёт" else когда + 340, итог, 1, json.dumps(ответы, ensure_ascii=False),
             оценка, None if оценка is None else (оценка - 1) / 4 * 100))


def main(отбор: list[str]) -> int:
    from drill_seed import Архив, браузер, поднять
    from playwright.sync_api import sync_playwright

    архив = Архив(Path(tempfile.mkdtemp()) / "data", заданий=96)
    _контроль_качества(архив.app.state.hub)
    адрес, остановить = поднять(архив.app)
    куда = КОРЕНЬ / "docs" / "images"
    try:
        with sync_playwright() as игра:
            движок = игра.chromium.launch(executable_path=браузер())
            страница = движок.new_page(viewport={"width": 1680, "height": 1020},
                                       color_scheme="light", device_scale_factor=2)
            страница.add_init_script("try { localStorage.setItem('asrhub_theme', 'light'); }"
                                     " catch (e) {}")
            for имя, (раздел, целиком) in СНИМКИ.items():
                if отбор and имя not in отбор:
                    continue
                страница.goto(f"{адрес}/#{раздел}", wait_until="networkidle")
                страница.wait_for_timeout(2000)
                страница.screenshot(path=str(куда / f"{имя}.png"), full_page=целиком)
                print(f"  {имя}.png ← {раздел}")
            хаб = архив.app.state.hub
            запись = next((с["id"] for с in хаб.db.query(
                "SELECT id FROM jobs WHERE status='completed' ORDER BY created_at DESC LIMIT 1")),
                "")
            модель = str(хаб.settings.get("model") or "demo-simulator")
            for имя, (раздел, действие) in СОСТОЯНИЯ.items():
                if отбор and имя not in отбор:
                    continue
                страница.goto(f"{адрес}/#{раздел}", wait_until="networkidle")
                страница.wait_for_timeout(1500)
                if действие:
                    страница.evaluate(_ВКЛАДКИ)
                    страница.evaluate(действие.replace("ЗАПИСЬ", json.dumps(запись))
                                      .replace("МОДЕЛЬ", json.dumps(модель)))
                    страница.wait_for_timeout(2000)
                страница.screenshot(path=str(куда / f"{имя}.png"))
                print(f"  {имя}.png ← {раздел}: {действие or 'раздел'}")
            движок.close()
    finally:
        остановить()
        архив.закрыть()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
