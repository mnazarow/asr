"""Заход 59: ИИ-аналитика из главы 35 — всё, что там было предложено.

Слой модели: трекеры, скоркарта и поля длинного разговора по частям,
пояснения, свои поля, веса и критичные пункты скоркарты, добавка к
подсказке, списки отделов, пороги отбора, окно архива, журнал смен
настройки. Поверх ответов: оценка оператора, договорённости, решение с
первого обращения, сверка NPS с опросом и драйверы оценки, риск ухода,
уведомления, сомнительное — человеку. Новое: золотой набор, вопрос к
архиву, поиск и темы по смыслу, план обучения, тренажёр, совет модели в
разговоре, задачи в CRM.
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from asrhub.llm import deadlines, insight, policy, tasks
from asrhub.llm.client import LLMClient

КОРЕНЬ = Path(__file__).resolve().parents[1]


class _Настройки:
    ПО_УМОЛЧАНИЮ = {
        "llm_enabled": True, "llm_backend": "stub", "llm_model": "stub",
        "llm_url": "", "llm_api_key": "", "llm_timeout_s": 30,
        "llm_context_chars": 12000, "llm_min_free_vram_gb": 0, "llm_auto": True,
        "llm_backfill": False, "llm_yield_to_queue": False,
        "llm_tasks": ["summary", "outcome", "actions", "trackers", "scorecard"],
        "llm_reasons": ["статус заказа или доставки", "вопрос по оплате", "другое"],
        "llm_outcomes": ["вопрос решён", "перезвонят или передано", "не решено", "неясно"],
        "llm_trackers": [], "llm_scorecard": [],
        "content_script": [], "content_agent_speaker": "",
    }

    def __init__(self, **значения):
        self.значения = {**self.ПО_УМОЛЧАНИЮ, **значения}

    def get(self, ключ, по_умолчанию=None):
        return self.значения.get(ключ, по_умолчанию)


class _Запоминающий(LLMClient):
    """Заглушка, которая запоминает подсказки."""

    def __init__(self, настройки):
        super().__init__(настройки)
        self.вызовы: list[tuple[str, str, str]] = []

    def chat(self, system, user, *, kind="chat", **k):
        self.вызовы.append((kind, system, user))
        return super().chat(system, user, kind=kind, **k)


# ---------------------------------------------------------------------------
# Сроки договорённостей
# ---------------------------------------------------------------------------

СРЕДА = dt.datetime(2026, 11, 11, 14, 0).timestamp()


@pytest.mark.parametrize(("текст", "ожидание"), [
    ("завтра", dt.datetime(2026, 11, 12, 23, 59, 59)),
    ("до пятницы", dt.datetime(2026, 11, 13, 23, 59, 59)),
    ("в течение трёх дней", dt.datetime(2026, 11, 14, 23, 59, 59)),
    ("через 2 часа", dt.datetime(2026, 11, 11, 16, 0)),
    ("к 15 ноября", dt.datetime(2026, 11, 15, 23, 59, 59)),
    ("в среду", dt.datetime(2026, 11, 18, 23, 59, 59)),
    ("до конца недели", dt.datetime(2026, 11, 15, 23, 59, 59)),
    ("сегодня", dt.datetime(2026, 11, 11, 23, 59, 59)),
])
def test_deadlines_in_words_become_moments(текст, ожидание):
    assert deadlines.срок(текст, СРЕДА) == ожидание.timestamp()


@pytest.mark.parametrize("текст", ["потом", "на днях", "верну средства", "среди прочего", "", None])
def test_vague_words_are_not_a_deadline(текст):
    assert deadlines.срок(текст, СРЕДА) is None


def test_only_callbacks_are_checked_by_the_call_journal():
    assert deadlines.про_звонок("Перезвоню вам завтра")
    assert deadlines.про_звонок("свяжемся по итогам проверки")
    assert not deadlines.про_звонок("Отправлю письмо с договором")


# ---------------------------------------------------------------------------
# Слой модели: длинный разговор, пояснения, поля, веса, добавка к подсказке
# ---------------------------------------------------------------------------

def _длинный() -> list[dict[str, Any]]:
    """Разговор на несколько частей; угроза судом — в середине третьей."""
    реплики = []
    for i in range(30):
        кто = "SPEAKER_00" if i % 2 == 0 else "SPEAKER_01"
        текст = (f"Реплика номер {i}: обсуждаем условия доставки и сроки по заказу, "
                 f"уточняем детали адреса и времени, всё спокойно и по делу.")
        if i == 13:
            текст = ("Если вы не вернёте деньги, я подам в суд и напишу жалобу "
                     "в надзор, это моё последнее слово.")
        if i == 1:
            текст = "Здравствуйте, мой номер заказа 774512, доставка не пришла."
        реплики.append({"speaker": кто, "text": текст})
    return реплики


ТРЕКЕР_СУДА = {"id": "court", "label": "Угроза судом",
               "description": "Клиент грозит подать в суд или жалобой в надзор"}


def test_long_call_trackers_are_asked_per_part_of_the_transcript():
    """По частям трекер находится и его цитата сверяется с текстом части;
    по пересказам частей та же угроза до основного вызова не доживает."""
    по_частям = _Настройки(llm_context_chars=1000, llm_trackers=[ТРЕКЕР_СУДА],
                           llm_tasks=["summary", "trackers"])
    клиент = _Запоминающий(по_частям)
    итог = tasks.analyze(клиент, text="", segments=_длинный(), settings=по_частям,
                         agent_speaker="SPEAKER_00")
    assert итог["chunks"] > 2
    виды = [в[0] for в in клиент.вызовы]
    assert виды.count("chunk") == итог["chunks"] and виды.count("part") == итог["chunks"]
    assert "trackers" not in виды
    суд = итог["trackers"][0]
    assert суд["fired"] and "суд" in суд["quote"]
    assert not any("цитата не найдена" in з for з in итог["warnings"])

    по_пересказам = _Настройки(llm_context_chars=1000, llm_trackers=[ТРЕКЕР_СУДА],
                               llm_tasks=["summary", "trackers"], llm_parts_mode="summaries")
    клиент2 = _Запоминающий(по_пересказам)
    итог2 = tasks.analyze(клиент2, text="", segments=_длинный(), settings=по_пересказам,
                          agent_speaker="SPEAKER_00")
    assert "part" not in [в[0] for в in клиент2.вызовы]
    assert not итог2["trackers"][0]["fired"]


def test_scorecard_parts_merge_no_wins_then_yes_then_na():
    вопросы = [{"id": "greet", "question": "Сотрудник назвал номер заказа?"},
               {"id": "court", "question": "Сотрудник отработал угрозу подать в суд?"}]
    настройки = _Настройки(llm_context_chars=1000, llm_scorecard=вопросы,
                           llm_tasks=["scorecard"])
    итог = tasks.analyze(LLMClient(настройки), text="", segments=_длинный(),
                         settings=настройки, agent_speaker="SPEAKER_00")
    ответы = {о["id"]: о for о in итог["scorecard"]}
    # «да» в одной части, «н/п» в остальных — итог «да», с пояснением.
    assert ответы["greet"]["answer"] == "да" and ответы["greet"]["why"]
    assert итог["score"] is not None


def test_weighted_scorecard_and_critical_items():
    вопросы = [{"id": "a", "question": "?", "weight": 3},
               {"id": "b", "question": "?", "weight": 1},
               {"id": "c", "question": "?", "critical": True}]
    балл, провал = tasks.балл_скоркарты(
        [{"id": "a", "answer": "да"}, {"id": "b", "answer": "нет"},
         {"id": "c", "answer": "н/п"}], вопросы)
    assert балл == 75.0 and провал == []
    балл, провал = tasks.балл_скоркарты(
        [{"id": "a", "answer": "да"}, {"id": "c", "answer": "нет"}], вопросы)
    assert балл == 0.0 and провал == ["c"]
    assert tasks.балл_скоркарты([{"id": "a", "answer": "н/п"}], вопросы) == (None, [])


def test_explanations_and_own_fields_in_the_main_call():
    настройки = _Настройки(llm_fields=[{"id": "order", "label": "Номер заказа",
                                        "description": "номер заказа клиента"}])
    разговор = [{"speaker": "SPEAKER_00", "text": "Слушаю вас."},
                {"speaker": "SPEAKER_01", "text": "Мой заказ 774512 не доставили, где доставка?"},
                {"speaker": "SPEAKER_00", "text": "Проверю статус доставки и перезвоню завтра."}]
    клиент = _Запоминающий(настройки)
    итог = tasks.analyze(клиент, text="", segments=разговор, settings=настройки,
                         agent_speaker="SPEAKER_00")
    assert итог["fields"] == {"order": "774512"}
    assert итог["reason_why"] and итог["outcome_why"]
    основной = next(в for в in клиент.вызовы if в[0] == "main")
    assert '"reason_why"' in основной[2] and "id «order»" in основной[2]
    # Без пояснений — их и не просят.
    тихо = _Настройки(llm_explain=False)
    клиент2 = _Запоминающий(тихо)
    итог2 = tasks.analyze(клиент2, text="", segments=разговор, settings=тихо,
                          agent_speaker="SPEAKER_00")
    assert "reason_why" not in итог2
    assert '"reason_why"' not in next(в for в in клиент2.вызовы if в[0] == "main")[2]


def test_prompt_extra_goes_to_the_system_prompt_and_changes_the_fingerprint():
    обычные = _Настройки()
    с_добавкой = _Настройки(llm_prompt_extra="Решённым считается только заявка с номером.")
    разговор = [{"speaker": "SPEAKER_00", "text": "Слушаю."},
                {"speaker": "SPEAKER_01", "text": "Где мой заказ?"}]
    к1, к2 = _Запоминающий(обычные), _Запоминающий(с_добавкой)
    tasks.analyze(к1, text="", segments=разговор, settings=обычные, agent_speaker="SPEAKER_00")
    tasks.analyze(к2, text="", segments=разговор, settings=с_добавкой, agent_speaker="SPEAKER_00")
    система = next(в[1] for в in к2.вызовы if в[0] == "main")
    assert "заявка с номером" in система and "настройка администратора" in система
    assert к1.cache_key("main", к1.вызовы[0][1], к1.вызовы[0][2]) != к2.cache_key(
        "main", к2.вызовы[0][1], к2.вызовы[0][2])
    assert policy.отпечаток(обычные, 2)[0] != policy.отпечаток(с_добавкой, 2)[0]


# ---------------------------------------------------------------------------
# Сервер со смысловым слоем на заглушке
# ---------------------------------------------------------------------------

@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    настройки = load()
    for ключ, значение in {
        "llm_backend": "stub", "llm_model": "stub", "llm_auto": False,
        "llm_yield_to_queue": False,
        "llm_tasks": ["summary", "outcome", "actions", "trackers", "scorecard"],
        "llm_reasons": ["статус заказа или доставки", "вопрос по оплате", "другое"],
        "llm_outcomes": ["вопрос решён", "перезвонят или передано", "не решено", "неясно"],
        "llm_trackers": [ТРЕКЕР_СУДА],
        "llm_scorecard": [{"id": "greet", "question": "Сотрудник поздоровался?"}],
    }.items():
        настройки.set(ключ, значение, source="api")
    with TestClient(create_app(настройки, start_queue=False)) as клиент:
        yield клиент, клиент.app.state.hub


ДОВОЛЕН = [
    {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00",
     "text": "Здравствуйте, служба доставки, меня зовут Анна."},
    {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01",
     "text": "Добрый день, заказ не пришёл, где доставка?"},
    {"start": 9.0, "end": 14.0, "speaker": "SPEAKER_00",
     "text": "Проверю статус доставки и перезвоню вам завтра."},
    {"start": 14.0, "end": 16.0, "speaker": "SPEAKER_01", "text": "Спасибо, жду."},
]

НЕДОВОЛЕН = [
    {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00", "text": "Слушаю."},
    {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01",
     "text": "Деньги за заказ так и не вернули, я подам в суд и напишу жалобу."},
    {"start": 9.0, "end": 14.0, "speaker": "SPEAKER_00", "text": "Не могу помочь."},
]


def _запись(hub, ид: str, реплики: list[dict[str, Any]], *, когда: float | None = None,
            номер: str = "", направление: str = "входящий", добавочный: str = "101",
            длительность: float = 60.0, **поля: Any) -> str:
    db = hub.db
    когда = когда if когда is not None else time.time() - 3600
    db.create_job({"id": ид, "filename": f"{ид}.wav", "owner": "anonymous",
                   "model": "demo", "engine": "demo", "media_duration_s": длительность,
                   **поля})
    db.update_job(ид, status="completed", finished_at=когда,
                  text=" ".join(р["text"] for р in реплики))
    db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, ид))
    db.save_segments(ид, реплики)
    if номер:
        src, dst = (номер, добавочный) if направление == "входящий" else (добавочный, номер)
        db.execute("INSERT INTO calls (uniqueid, job_id, src, dst, direction, agent, "
                   "started_at, answered, imported_at) VALUES (?,?,?,?,?,?,?,1,?)",
                   (f"u-{ид}", ид, src, dst, направление, добавочный, когда, когда))
    return ид


def _разобрать(hub, ид: str) -> dict[str, Any]:
    return hub.llm_worker.analyze_job(ид, force=True)


def test_new_columns_are_stored_and_returned(сервер):
    _, hub = сервер
    hub.settings.set("llm_fields", [{"id": "order", "label": "Номер заказа",
                                     "description": "номер заказа клиента"}], source="api")
    _запись(hub, "a1", [*ДОВОЛЕН[:1], {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01",
                                        "text": "Мой заказ 556677 не доставили."}])
    ответ = _разобрать(hub, "a1")
    assert ответ["version"] == tasks.VERSION == 2
    assert ответ["fields"] == {"order": "556677"}
    assert ответ["reason_why"] and ответ["score"] is not None
    assert ответ["scorecard"][0].get("why")


# ---------------------------------------------------------------------------
# Пороги отбора, сомнительная расшифровка, окно архива, списки отделов
# ---------------------------------------------------------------------------

def test_short_calls_are_not_queued_by_themselves(сервер):
    _, hub = сервер
    hub.settings.set("llm_auto", True, source="api")
    hub.settings.set("llm_min_duration_s", 15, source="api")
    _запись(hub, "short", ДОВОЛЕН, длительность=8.0)
    _запись(hub, "long", ДОВОЛЕН, длительность=60.0)
    hub.llm_worker.enqueue("short")
    hub.llm_worker.enqueue("long")
    в_очереди = {r["job_id"] for r in hub.db.query("SELECT job_id FROM llm_queue")}
    assert в_очереди == {"long"}
    assert any(e["kind"] == "llm" and "короче 15" in e["message"]
               for e in hub.db.query("SELECT kind, message FROM events WHERE job_id='short'"))
    ждут = {з["id"] for з in hub.db.llm_pending(tasks.VERSION, min_duration_s=15)}
    assert "short" not in ждут and "long" in ждут
    # По просьбе — разбирается.
    assert _разобрать(hub, "short")["summary"]


def test_suspicious_transcripts_are_marked_or_skipped(сервер):
    _, hub = сервер
    _запись(hub, "sus", ДОВОЛЕН)
    hub.db.execute("UPDATE jobs SET suspect_share=0.5, quality_flags='repeat' WHERE id='sus'")
    ответ = _разобрать(hub, "sus")
    assert any("по сомнительной расшифровке" in з for з in ответ["warnings"])
    hub.settings.set("llm_suspect_mode", "skip", source="api")
    assert policy.пропустить(hub.db.get_job("sus"), hub.settings).startswith("сомнительная")
    hub.db.execute("DELETE FROM llm_results WHERE job_id='sus'")
    assert "sus" not in {з["id"] for з in hub.db.llm_pending(
        tasks.VERSION, skip_suspect_share=0.3)}


def test_archive_window_and_daily_limit(сервер):
    _, hub = сервер
    db = hub.db
    for ид in ("arch1", "arch2", "fresh"):
        _запись(hub, ид, ДОВОЛЕН)
    db.llmq_put("arch1", kind="архив", priority=30)
    db.llmq_put("arch2", kind="архив", priority=30)
    db.llmq_put("fresh", kind="свежая запись", priority=50)
    сейчас_час = dt.datetime.now().hour
    hub.settings.set("llm_archive_hours", f"{(сейчас_час + 2) % 24}-{(сейчас_час + 3) % 24}",
                     source="api")
    assert not policy.в_окне_архива(hub.settings, time.time())
    assert hub.llm_worker._next() == "fresh"
    db.llmq_finish("fresh")
    assert hub.llm_worker._next() is None
    hub.settings.set("llm_archive_hours", "", source="api")
    hub.settings.set("llm_archive_daily", 1, source="api")
    assert hub.llm_worker._next() == "arch1"
    db.llmq_finish("arch1")
    assert hub.llm_worker._next() is None
    assert policy.в_окне_архива(_Настройки(llm_archive_hours="22-7"),
                                dt.datetime(2026, 1, 1, 23, 0).timestamp())
    assert not policy.в_окне_архива(_Настройки(llm_archive_hours="22-7"),
                                    dt.datetime(2026, 1, 1, 12, 0).timestamp())


def test_department_lists_are_used_for_the_agents_department(сервер, monkeypatch):
    _, hub = сервер
    hub.db.employee_save({"last_name": "Иванова", "first_name": "Анна", "department": "Продажи",
                          "phone_ext": "205"})
    hub.settings.set("llm_lists_by_department",
                     {"продажи": {"reasons": ["выбор тарифа", "другое"]}}, source="api")
    _запись(hub, "dep", ДОВОЛЕН, номер="+7 916 111-22-33", добавочный="205")
    подсказки: list[str] = []
    исходный = hub.llm.chat

    def ловушка(system, user, **k):
        подсказки.append(user)
        return исходный(system, user, **k)

    monkeypatch.setattr(hub.llm, "chat", ловушка)
    _разобрать(hub, "dep")
    основной = next(п for п in подсказки if "Причины обращения" in п)
    assert "выбор тарифа" in основной and "вопрос по оплате" not in основной
    assert policy.списки_отдела(hub.settings, "  ПРОДАЖИ ") == (["выбор тарифа", "другое"], None)


def test_settings_changes_are_logged_and_marked_on_trends(сервер):
    клиент, hub = сервер
    _запись(hub, "v1", ДОВОЛЕН)
    _разобрать(hub, "v1")
    первые = hub.db.llm_versions_list()
    assert len(первые) == 1 and первые[0]["version"] == tasks.VERSION
    _разобрать(hub, "v1")
    assert len(hub.db.llm_versions_list()) == 1
    hub.settings.set("llm_reasons", ["доставка", "другое"], source="api")
    _разобрать(hub, "v1")
    смены = hub.db.llm_versions_list()
    assert len(смены) == 2 and "причины" in смены[-1]["changes"]
    ряды = клиент.get("/api/trends", params={"metrics": "llm_analyzed", "period": "week"}).json()
    assert [м["kind"] for м in ряды["markers"]] == ["llm", "llm"]
    без = клиент.get("/api/trends", params={"metrics": "jobs", "period": "week"}).json()
    assert без.get("markers") == []
    assert клиент.get("/api/llm/versions").json()["items"][-1]["changes"]


# ---------------------------------------------------------------------------
# Уведомления: умный трекер, риск; сомнительное — человеку
# ---------------------------------------------------------------------------

@pytest.fixture()
def приёмник():
    принято: list[dict[str, Any]] = []

    class Обработчик(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            тело = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            принято.append(json.loads(тело.decode("utf-8")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    сервер = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Обработчик)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{сервер.server_address[1]}/hook", принято
    сервер.shutdown()


def _дождаться(условие, срок: float = 10.0) -> bool:
    конец = time.time() + срок
    while time.time() < конец:
        if условие():
            return True
        time.sleep(0.05)
    return False


def test_smart_tracker_and_risk_are_notified(сервер, приёмник):
    _, hub = сервер
    адрес, принято = приёмник
    hub.settings.set("tracker_url", адрес, source="api")
    hub.settings.set("webhook_allow_internal", True, source="api")
    hub.settings.set("llm_risk_threshold", 40, source="api")
    _запись(hub, "angry", НЕДОВОЛЕН)
    ответ = _разобрать(hub, "angry")
    assert next(т for т in ответ["trackers"] if т["id"] == "court")["fired"]
    assert _дождаться(lambda: {п["event"] for п in принято} >= {"tracker", "risk"})
    трекер = next(п for п in принято if п["event"] == "tracker")
    assert трекер["source"] == "llm" and трекер["category"] == "court" and трекер["quote"]
    риск = next(п for п in принято if п["event"] == "risk")
    assert риск["score"] >= 40 and any("трекер" in п for п in риск["why"])
    события = {e["kind"] for e in hub.db.query("SELECT kind FROM events WHERE job_id='angry'")}
    assert {"tracker", "risk"} <= события


def test_doubtful_answers_go_to_human_review(сервер, monkeypatch):
    _, hub = сервер
    hub.settings.set("qa_enabled", True, source="api")
    _запись(hub, "odd", ДОВОЛЕН)
    hub.db.execute("UPDATE jobs SET suspect_share=0.9 WHERE id='odd'")
    _разобрать(hub, "odd")
    проверки = hub.db.qa_list()
    assert len(проверки) == 1 and проверки[0]["reason"].startswith("сомнительный ответ модели")
    # Без контроля качества — не ставится.
    hub.settings.set("qa_enabled", False, source="api")
    _запись(hub, "odd2", ДОВОЛЕН)
    hub.db.execute("UPDATE jobs SET suspect_share=0.9 WHERE id='odd2'")
    _разобрать(hub, "odd2")
    assert len(hub.db.qa_list()) == 1


# ---------------------------------------------------------------------------
# Своды поверх ответов
# ---------------------------------------------------------------------------

def test_commitments_are_checked_by_the_call_journal(сервер):
    клиент, hub = сервер
    давно = time.time() - 5 * 86400
    _запись(hub, "c-done", ДОВОЛЕН, когда=давно, номер="+7 916 000-00-01")
    _запись(hub, "c-late", ДОВОЛЕН, когда=давно, номер="+7 916 000-00-02")
    _запись(hub, "c-wait", ДОВОЛЕН, когда=time.time() - 600, номер="+7 916 000-00-03")
    for ид in ("c-done", "c-late", "c-wait"):
        _разобрать(hub, ид)
    # Перезвон по первому номеру — на следующий день, исходящим.
    hub.db.execute("INSERT INTO calls (uniqueid, src, dst, direction, agent, started_at, "
                   "imported_at) VALUES ('cb1','101','89160000001','исходящий','101',?,?)",
                   (давно + 86400, давно + 86400))
    ответ = клиент.get("/api/llm/commitments", params={"period": "month"}).json()
    по_записи = {п["job_id"]: п for п in ответ["items"]}
    assert по_записи["c-done"]["status"] == "done" and по_записи["c-done"]["callback_at"]
    assert по_записи["c-late"]["status"] == "overdue"
    assert по_записи["c-wait"]["status"] == "waiting"
    assert ответ["counts"]["overdue"] == 1 and ответ["kept_share"] == 50.0
    assert ответ["items"][0]["status"] == "overdue"


def test_first_contact_resolution_looks_at_repeat_calls(сервер):
    клиент, hub = сервер
    давно = time.time() - 3 * 86400
    _запись(hub, "f1", ДОВОЛЕН, когда=давно, номер="+79161234567")
    _запись(hub, "f2", ДОВОЛЕН, когда=давно + 86400, номер="89161234567")
    _запись(hub, "f3", ДОВОЛЕН, когда=давно, номер="+79165550000")
    for ид in ("f1", "f2", "f3"):
        _разобрать(hub, ид)
    ответ = клиент.get("/api/llm/first-contact", params={"period": "week"}).json()
    assert ответ["resolved_by_model"] == 3 and ответ["reopened"] == 1
    assert ответ["items"][0]["job_id"] == "f1" and ответ["items"][0]["repeat_job_id"] == "f2"
    assert ответ["fcr_share"] < ответ["model_share"]


def test_risks_list_and_levels(сервер):
    клиент, hub = сервер
    _запись(hub, "r-ok", ДОВОЛЕН)
    _запись(hub, "r-bad", НЕДОВОЛЕН)
    _разобрать(hub, "r-ok")
    _разобрать(hub, "r-bad")
    hub.settings.set("llm_risk_threshold", 40, source="api")
    ответ = клиент.get("/api/llm/risks").json()
    assert [п["job_id"] for п in ответ["items"]][0] == "r-bad"
    assert ответ["items"][0]["level"] == "high" and "вопрос не решён" in ответ["items"][0]["why"]
    assert insight.риск({"resolved": True})[0] == 0


def test_operator_card_and_employees_carry_model_answers(сервер):
    клиент, hub = сервер
    _запись(hub, "o1", ДОВОЛЕН, номер="+79160000011", добавочный="301")
    _запись(hub, "o2", НЕДОВОЛЕН, номер="+79160000012", добавочный="302")
    for ид in ("o1", "o2"):
        hub.content.analyze_job(ид)
        _разобрать(hub, ид)
    карточка = клиент.get("/api/content/agents/301", params={"by": "agent"}).json()
    assert карточка["llm"]["agent"]["analyzed"] == 1
    assert карточка["llm"]["agent"]["resolved_share"] == 100.0
    assert карточка["llm"]["team"]["analyzed"] == 2
    люди = {п["key"]: п for п in клиент.get("/api/content/employees",
                                             params={"by": "agent"}).json()["items"]}
    assert люди["301"]["llm_analyzed"] == 1 and люди["302"]["llm_resolved_share"] == 0.0


def _опрос(hub, ид: str, процент: float) -> None:
    hub.db.execute("INSERT INTO qc_surveys (id, call_key, kind, job_id, created_at, "
                   "finished_at, status, score, score_pct) VALUES (?,?,?,?,?,?,?,?,?)",
                   (f"s-{ид}", f"k-{ид}", "опрос", ид, time.time(), time.time(), "оценил",
                    процент / 20, процент))


def test_survey_insights_calibrate_nps_and_find_drivers(сервер):
    клиент, hub = сервер
    for n in range(12):
        ид = f"q{n}"
        _запись(hub, ид, НЕДОВОЛЕН if n % 3 == 0 else ДОВОЛЕН)
        hub.content.analyze_job(ид)
        _разобрать(hub, ид)
        hub.db.execute("UPDATE content SET nps=6 WHERE job_id=?", (ид,))
        _опрос(hub, ид, 40.0 if n % 3 == 0 else 95.0)
    ответ = клиент.get("/api/qc/insights").json()
    сверка = ответ["calibration"]
    assert ответ["surveys"] == 12 and сверка["pairs"] == 12 and сверка["enough"]
    assert сверка["bias"] > 0 and 0 < сверка["suggested_shift"] <= 3
    худший = ответ["drivers"][0]
    assert худший["delta"] < 0 and худший["n"] >= 3
    assert any("не решён" in д["feature"] or "суд" in д["feature"].lower()
               for д in ответ["drivers"][:3])


def test_nps_shift_moves_the_predicted_score():
    from asrhub import content

    реплики = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "text": "Здравствуйте."},
               {"start": 3.0, "end": 8.0, "speaker": "SPEAKER_01",
                "text": "Спасибо большое, всё отлично, очень довольна, помогли быстро."}]
    без = content.analyze(text="", segments=реплики, agent_speaker="SPEAKER_00")
    с = content.analyze(text="", segments=реплики, agent_speaker="SPEAKER_00", nps_shift=-3.0)
    assert без["indices"]["nps"]["score"] is not None
    assert с["indices"]["nps"]["score"] < без["indices"]["nps"]["score"]


# ---------------------------------------------------------------------------
# Ручная проверка: ответы скоркарты человеком и согласие с моделью
# ---------------------------------------------------------------------------

def test_human_scorecard_answers_give_agreement_per_question(сервер):
    клиент, hub = сервер
    _запись(hub, "h1", ДОВОЛЕН)
    _разобрать(hub, "h1")
    модель = hub.db.llm_get("h1")["scorecard"][0]["answer"]
    ид = hub.db.qa_assign("h1", agent="101")
    человек = "нет" if модель == "да" else "да"
    ответ = клиент.put(f"/api/qa/{ид}", json={"score": 80, "scorecard": {"greet": человек}})
    assert ответ.status_code == 200, ответ.text
    свод = клиент.get("/api/qa").json()["stats"]["scorecard"]
    assert свод == [{"id": "greet", "n": 1, "agree": 0, "agree_share": 0.0,
                     "model_yes_human_no": int(модель == "да"),
                     "model_no_human_yes": int(модель == "нет")}]
    плохо = клиент.put(f"/api/qa/{hub.db.qa_assign('h1')}", json={"scorecard": {"greet": "может"}})
    assert плохо.status_code == 400


# ---------------------------------------------------------------------------
# Золотой набор
# ---------------------------------------------------------------------------

def test_golden_set_accuracy_and_runs_with_overrides(сервер):
    from asrhub.llm import golden

    клиент, hub = сервер
    _запись(hub, "g1", ДОВОЛЕН)
    _запись(hub, "g2", НЕДОВОЛЕН)
    for ид in ("g1", "g2"):
        _разобрать(hub, ид)
    ответ = клиент.put("/api/llm/golden/g1", json={
        "reason": "статус заказа или доставки", "resolved": True,
        "trackers": {"court": False}, "лишнее": 1})
    assert ответ.status_code == 200 and "лишнее" not in ответ.json()["answers"]
    клиент.put("/api/llm/golden/g2", json={"reason": "вопрос по оплате", "resolved": False,
                                           "trackers": {"court": True}})
    assert клиент.put("/api/llm/golden/g2", json={"нечто": 1}).status_code == 400
    набор = клиент.get("/api/llm/golden").json()
    assert набор["current"]["records"] == 2 and набор["current"]["accuracy"] is not None
    assert клиент.post("/api/llm/golden/run",
                       json={"overrides": {"crm_url": "x"}}).status_code == 400
    номер = клиент.post("/api/llm/golden/run", json={
        "label": "без добавки", "overrides": {"llm_prompt_extra": "Отвечай строго."}}).json()["id"]
    прогон = golden.ждать(hub.db, номер)
    assert прогон and прогон["status"] == "готово", прогон
    assert прогон["result"]["fields"]["resolved"]["n"] == 2
    assert прогон["overrides"] == {"llm_prompt_extra": "Отвечай строго."}
    # Подмена — только на прогон: настройки сервера те же.
    assert not hub.settings.get("llm_prompt_extra")
    assert клиент.delete("/api/llm/golden/g1").json()["deleted"]
    hub.db.delete_job("g2")
    assert hub.db.golden_list() == []


# ---------------------------------------------------------------------------
# Вопрос к архиву, поиск и темы по смыслу
# ---------------------------------------------------------------------------

def test_ask_the_archive_answers_with_record_links(сервер, monkeypatch):
    from asrhub.llm import ask

    клиент, hub = сервер
    for n in range(6):
        _запись(hub, f"ask{n}", ДОВОЛЕН if n % 2 else НЕДОВОЛЕН)
        _разобрать(hub, f"ask{n}")
    ответ = клиент.post("/api/llm/ask", json={"question": "почему клиенты пишут жалобу?",
                                               "period": "week"}).json()
    assert ответ["used"] == 6 and ответ["calls"] == 1 and ответ["answer"]
    assert {з["job_id"] for з in ответ["records"]} <= {f"ask{n}" for n in range(6)}
    assert ответ["records"]
    # Порциями: выписки по каждой, затем ответ.
    monkeypatch.setattr(ask, "ПОРЦИЯ", 300)
    ответ2 = клиент.post("/api/llm/ask", json={"question": "жалобу", "period": "week"}).json()
    assert ответ2["calls"] > 2 and ответ2["records"]
    assert клиент.post("/api/llm/ask", json={"question": " "}).status_code >= 400


def test_semantic_search_and_topics_by_meaning(сервер):
    клиент, hub = сервер
    assert клиент.get("/api/llm/search", params={"q": "суд"}).status_code == 400
    hub.settings.set("llm_embeddings", True, source="api")
    for n in range(6):
        _запись(hub, f"s{n}", НЕДОВОЛЕН if n < 5 else ДОВОЛЕН)
        _разобрать(hub, f"s{n}")
    assert hub.db.vectors_stats()["records"] == 6
    найдено = клиент.get("/api/llm/search", params={"q": "подам в суд жалобу"}).json()
    assert найдено["items"][0]["job_id"] in {f"s{n}" for n in range(5)}
    темы = клиент.get("/api/content/discover", params={"by": "meaning", "name": "false"}).json()
    assert темы["by"] == "meaning" and темы["topics"] and темы["topics"][0]["size"] >= 5
    hub.db.delete_job("s0")
    assert not hub.db.query("SELECT 1 FROM llm_vectors WHERE job_id='s0'")


# ---------------------------------------------------------------------------
# План обучения и тренажёр
# ---------------------------------------------------------------------------

def test_coaching_plan_is_made_and_kept(сервер):
    клиент, hub = сервер
    hub.settings.set("llm_scorecard", [{"id": "help", "question": "Сотрудник помог клиенту?"}],
                     source="api")
    _запись(hub, "p1", НЕДОВОЛЕН, номер="+79160000099", добавочный="401")
    _разобрать(hub, "p1")
    assert клиент.get("/api/content/agents/401/plan").json()["plan"] is None
    план = клиент.post("/api/content/agents/401/plan", params={"by": "agent"}).json()["plan"]
    assert план["records"] == 1 and (план["weaknesses"] or план["strengths"])
    assert all(set(п["records"]) <= {"p1"} for п in план["weaknesses"] + план["strengths"])
    assert клиент.get("/api/content/agents/401/plan").json()["plan"]["created_at"]


def test_trainer_plays_the_client_and_scores_the_operator(сервер):
    клиент, hub = сервер
    _запись(hub, "t1", ДОВОЛЕН)
    _разобрать(hub, "t1")
    assert клиент.get("/api/llm/trainer/scenarios").json()["items"][0]["job_id"] == "t1"
    сессия = клиент.post("/api/llm/trainer", json={"job_id": "t1"}).json()
    assert сессия["history"][0]["role"] == "client"
    сессия = клиент.post(f"/api/llm/trainer/{сессия['id']}/reply",
                         json={"text": "Здравствуйте, меня зовут Анна, сейчас проверю."}).json()
    assert [р["role"] for р in сессия["history"]] == ["client", "agent", "client"]
    итог = клиент.post(f"/api/llm/trainer/{сессия['id']}/finish").json()
    assert итог["done"] and итог["result"]["scorecard"][0]["answer"] in ("да", "нет", "н/п")
    assert клиент.post(f"/api/llm/trainer/{сессия['id']}/reply",
                       json={"text": "ещё"}).status_code == 400
    assert клиент.post("/api/llm/trainer", json={}).status_code >= 400


# ---------------------------------------------------------------------------
# Совет модели в разговоре
# ---------------------------------------------------------------------------

def test_live_advice_comes_with_the_next_reply():
    from asrhub.llm.advice import Советчик

    советчик = Советчик(LLMClient(_Настройки()), _Настройки(stream_assist_llm_interval_s=60))
    реплики = [{"speaker": "оператор", "text": "Это стоит пять тысяч."},
               {"speaker": "оператор", "text": "Клиент говорит, что дорого."}]
    assert советчик.предложить(реплики, "возражение без отработки", сейчас=100.0)
    assert not советчик.предложить(реплики, "возражение", сейчас=101.0)
    assert _дождаться(lambda: not советчик.занят)
    готовые = советчик.готовые()
    assert готовые and готовые[0]["kind"] == "advice" and готовые[0]["detail"]
    assert советчик.готовые() == []
    # Без повода — не чаще интервала и не меньше трёх новых реплик.
    assert not советчик.предложить(реплики, "", сейчас=120.0)


def test_stream_session_gets_an_advisor_only_when_asked():
    from asrhub.streaming import StreamSession

    клиент = LLMClient(_Настройки())
    с = StreamSession(None, {"stream_assist": True, "stream_assist_llm": True},
                      llm=клиент)
    assert с.advisor is not None
    без = StreamSession(None, {"stream_assist": True}, llm=клиент)
    assert без.advisor is None


# ---------------------------------------------------------------------------
# Задачи в CRM
# ---------------------------------------------------------------------------

def test_dated_commitments_become_crm_tasks():
    from asrhub import crm

    модель = {"actions": [{"what": "Перезвонить клиенту", "who": "сотрудник", "when": "завтра"},
                          {"what": "Прислать фото", "who": "клиент", "when": "сегодня"},
                          {"what": "Уточнить", "who": "сотрудник", "when": None}],
              "fields": {"order": "774512"}}
    данные = crm.собрать({"id": "j1", "created_at": СРЕДА}, {"llm": модель})
    assert [з["text"] for з in данные["tasks"]] == ["Перезвонить клиенту"]
    assert данные["tasks"][0]["due"] == dt.datetime(2026, 11, 12, 23, 59, 59).timestamp()
    assert данные["field_order"] == "774512"
    amo = crm.Настройки(enabled=True, kind="amocrm", url="https://x.amocrm.ru", token="t",
                        entity="lead", fields={}, tasks=True)
    запросы = crm.запросы(данные, amo, entity_id="55")
    задача = next(з for з in запросы if з.назначение == "задачи")
    тело = json.loads(задача.body)
    assert задача.url.endswith("/api/v4/tasks") and тело[0]["entity_id"] == 55
    assert тело[0]["complete_till"] == int(данные["tasks"][0]["due"])
    битрикс = crm.Настройки(enabled=True, kind="bitrix24", url="https://x.bitrix24.ru/rest/1/k/",
                            entity="deal", fields={}, tasks=True)
    дела = [з for з in crm.запросы(данные, битрикс, entity_id="7") if з.назначение == "задачи"]
    assert дела and json.loads(дела[0].body)["ownerTypeId"] == 2
    без_задач = crm.Настройки(enabled=True, kind="amocrm", url="https://x.amocrm.ru", token="t",
                              entity="lead", fields={})
    assert all(з.назначение != "задачи" for з in crm.запросы(данные, без_задач, entity_id="55"))
    свой = crm.Настройки(enabled=True, kind="custom", url="https://x/y", fields={})
    assert "tasks" not in json.loads(crm.запросы(данные, свой)[0].body)


# ---------------------------------------------------------------------------
# Документация
# ---------------------------------------------------------------------------

def test_chapter_35_is_rewritten_as_done():
    глава = (КОРЕНЬ / "docs" / "35-ai-analytics.md").read_text(encoding="utf-8")
    for маршрут in ("/api/llm/commitments", "/api/llm/ask", "/api/llm/search",
                    "/api/llm/golden", "/api/qc/insights", "/api/llm/trainer"):
        assert маршрут in глава, маршрут
    assert "## Как пользоваться" in глава
