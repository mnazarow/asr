"""Смысловой слой: клиент модели, подсказки, заглушка, фоновый поток, ручки.

Проверки идут на заглушке и на поддельном сервере модели — настоящую
модель на девять гигабайт набор тестов требовать не может. Проверяется
ровно то, что ломается: разбор ответа, закрытые списки, длинная запись,
кеш, ограничение одновременности, тайм-аут, уступка распознаванию,
сохранение и выдача.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from asrhub.llm import LLMError, tasks
from asrhub.llm.client import LLMClient
from asrhub.llm.worker import LLMWorker


class _Настройки:
    """Заглушка настроек: слой читает их через get()."""

    ПО_УМОЛЧАНИЮ = {
        "llm_backend": "stub", "llm_model": "stub", "llm_url": "http://127.0.0.1:11434",
        "llm_api_key": "", "llm_timeout_s": 5, "llm_max_concurrent": 1,
        "llm_context_chars": 12000, "llm_min_free_vram_gb": 0, "llm_auto": True,
        "llm_backfill": False, "llm_yield_to_queue": True,
        "llm_tasks": ["summary", "outcome", "actions"],
        "llm_reasons": ["вопрос по оплате", "статус заказа или доставки",
                        "техническая проблема", "жалоба", "другое"],
        "llm_outcomes": ["вопрос решён", "перезвонят или передано", "отказ клиента",
                         "не решено", "неясно"],
        "llm_trackers": [], "llm_scorecard": [],
        "content_script": [], "content_agent_speaker": "",
    }

    def __init__(self, **значения):
        self.значения = {**self.ПО_УМОЛЧАНИЮ, **значения}

    def get(self, ключ, по_умолчанию=None):
        return self.значения.get(ключ, по_умолчанию)


РАЗГОВОР = [
    {"speaker": "SPEAKER_00", "text": "Здравствуйте, компания Ромашка, меня зовут Анна."},
    {"speaker": "SPEAKER_01", "text": "Добрый день, у меня заказ не пришёл, что делать?"},
    {"speaker": "SPEAKER_00", "text": "Проверю статус доставки и перезвоню вам завтра."},
    {"speaker": "SPEAKER_01", "text": "Спасибо, жду."},
]


# ---------------------------------------------------------------------------
# Подсказки и разбор ответов
# ---------------------------------------------------------------------------


def test_the_transcript_is_built_from_replies_and_split_by_the_limit():
    """Разговор — строками «кто: реплика», с подписью оператора; длинный
    режется по репликам, а слишком длинная реплика — по знакам."""
    текст = tasks.transcript_of(РАЗГОВОР, "", agent_speaker="SPEAKER_00")
    assert текст.startswith("Сотрудник: Здравствуйте")
    assert "SPEAKER_01: Добрый день" in текст
    # Без разметки говорящих — сплошной текст задания.
    assert tasks.transcript_of([], "просто текст") == "просто текст"
    assert tasks.transcript_of([{"text": "  "}], "запас") == "запас"

    длинный = "\n".join(f"Сотрудник: реплика номер {i} про доставку" for i in range(200))
    куски = tasks.chunks_of(длинный, 1000)
    assert len(куски) > 1 and all(len(к) <= 1000 for к in куски)
    # Ни одна реплика не разорвана посередине.
    assert all(all(с.startswith("Сотрудник: реплика") for с in к.split("\n")) for к in куски)
    assert "\n".join(куски) == длинный
    assert tasks.chunks_of("коротко", 1000) == ["коротко"]
    # Реплика длиннее предела режется по знакам, а не теряется.
    огромная = "Сотрудник: " + "а" * 3000
    куски = tasks.chunks_of(огромная, 500)
    assert sum(len(к) for к in куски) >= 3000 and all(len(к) <= 500 for к in куски)


def test_json_is_taken_out_of_whatever_the_model_wrapped_it_in():
    assert tasks.parse_json('{"a": 1}') == {"a": 1}
    assert tasks.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert tasks.parse_json('Вот ответ: {"a": 1}. Готово.') == {"a": 1}
    for мусор in ("", "просто текст", "{сломано", "[1, 2]"):
        with pytest.raises(LLMError):
            tasks.parse_json(мусор)


def test_the_answer_is_forced_into_the_closed_lists_and_shapes():
    """Причина и исход — только из списка; мимо списка — запасной пункт;
    действия — список объектов с непустым «что»."""
    настройки = _Настройки(llm_tasks=["summary", "outcome", "actions"])

    class _Клиент:
        model = "поддельная"
        enabled = True

        def __init__(self, ответ):
            self.ответ = ответ
            self.вызовы = []

        def chat(self, system, user, **kw):
            self.вызовы.append((system, user, kw))
            return self.ответ

    # Модель ответила исходом не из списка и мусором в действиях.
    клиент = _Клиент(json.dumps({
        "summary": "Клиент не получил заказ.", "resolved": "может быть",
        "reason": "СТАТУС ЗАКАЗА ИЛИ ДОСТАВКИ", "outcome": "клиент подумает",
        "reason_quote": "заказ не пришёл",
        "actions": [{"what": "перезвонить", "who": "сотрудник", "when": "завтра"},
                    {"what": "", "who": "клиент"}, "мусор", 42]}, ensure_ascii=False))
    итог = tasks.analyze(клиент, text="", segments=РАЗГОВОР, settings=настройки)
    assert итог["reason"] == "статус заказа или доставки"      # регистр не важен
    assert итог["outcome"] == "неясно"                          # мимо списка → запасной
    assert "исход вне списка" in итог["warnings"]
    assert итог["resolved"] is None                             # не булево — значит неизвестно
    assert итог["actions"] == [{"what": "перезвонить", "who": "сотрудник",
                                "when": "завтра", "quote": ""}]
    assert итог["calls"] == 1 and итог["chunks"] == 1 and итог["latency_ms"] >= 0
    # Один вызов на три задачи: модель читает разговор один раз.
    assert len(клиент.вызовы) == 1 and "### задача: main" in клиент.вызовы[0][0]

    # Пустая расшифровка — не вызов модели, а честное «нечего разбирать».
    пустой = _Клиент("{}")
    пусто = tasks.analyze(пустой, text="", segments=[], settings=настройки)
    assert пусто["calls"] == 0 and "пустая расшифровка" in пусто["warnings"]


def test_a_long_recording_is_summarised_by_parts_first():
    """Длинная запись: вызов на кусок плюс основной по пересказам."""
    сегменты = [{"speaker": "SPEAKER_00", "text": f"реплика номер {i} про оплату счёта"}
                for i in range(120)]

    class _Клиент:
        model = "поддельная"

        def __init__(self):
            self.виды = []

        def chat(self, system, user, *, kind="chat", **kw):
            self.виды.append(kind)
            if kind == "chunk":
                return json.dumps({"summary": "часть пересказана"}, ensure_ascii=False)
            assert "часть пересказана" in user, "основной вызов должен идти по пересказам"
            return json.dumps({"summary": "итог", "outcome": "неясно"}, ensure_ascii=False)

    клиент = _Клиент()
    итог = tasks.analyze(клиент, text="", segments=сегменты,
                         settings=_Настройки(llm_context_chars=2000))
    assert итог["chunks"] > 1 and клиент.виды.count("chunk") == итог["chunks"]
    assert клиент.виды[-1] == "main" and итог["calls"] == итог["chunks"] + 1
    assert any("пересказ" in п for п in итог["warnings"])


def test_the_stub_answers_every_task_without_a_model():
    """Заглушка отвечает тем же JSON: резюме, исход, действия, трекеры,
    скоркарта — и по разговору, а не наугад."""
    настройки = _Настройки(
        llm_tasks=["summary", "outcome", "actions", "trackers", "scorecard"],
        llm_trackers=[{"id": "discount", "label": "Запрос скидки",
                       "description": "Клиент просит скидку или спрашивает про дешевле"},
                      {"id": "court", "label": "Угроза судом",
                       "description": "Клиент грозит судом или жалобой в надзор"}],
        llm_scorecard=[{"id": "greet", "question": "Сотрудник представился в начале?"}])
    клиент = LLMClient(настройки)
    сегменты = [*РАЗГОВОР, {"speaker": "SPEAKER_01",
                            "text": "А скидка на доставку возможна?"}]
    итог = tasks.analyze(клиент, text="", segments=сегменты, settings=настройки,
                         agent_speaker="SPEAKER_00")
    assert итог["summary"] and "Ромашка" in итог["summary"]
    assert итог["reason"] == "статус заказа или доставки"
    assert итог["outcome"] == "перезвонят или передано"
    assert [д["what"] for д in итог["actions"]] == [
        "Проверю статус доставки и перезвоню вам завтра."]
    assert итог["actions"][0]["when"] == "завтра" and итог["actions"][0]["who"] == "сотрудник"
    сработали = {т["id"]: т["fired"] for т in итог["trackers"]}
    assert сработали == {"discount": True, "court": False}
    assert next(т for т in итог["trackers"] if т["id"] == "discount")["quote"]
    assert итог["scorecard"][0]["answer"] in ("да", "нет")
    assert итог["calls"] == 3 and клиент.errors == 0

    # Вопросы скоркарты берутся из скрипта, если своих не задали.
    из_скрипта = _Настройки(llm_tasks=["scorecard"], llm_scorecard=[],
                            content_script=[{"id": "greet", "label": "Приветствие и имя"}])
    итог2 = tasks.analyze(LLMClient(из_скрипта), text="", segments=РАЗГОВОР,
                          settings=из_скрипта, script=из_скрипта.get("content_script"))
    assert [в["id"] for в in итог2["scorecard"]] == ["greet"]
    assert "скрипта" in итог2["scorecard"][0]["question"]


def test_the_digest_summary_counts_outcomes_reasons_and_actions():
    строки = [
        {"outcome": "решён", "reason": "оплата", "resolved": True,
         "actions": [{"what": "а"}, {"what": "б"}]},
        {"outcome": "решён", "reason": "доставка", "resolved": False, "actions": []},
        {"outcome": "не решён", "reason": "оплата", "resolved": None, "actions": None},
        {"outcome": None, "reason": None, "resolved": True, "actions": [{"what": "в"}]},
    ]
    свод = tasks.summarize_for_digest(строки)
    assert свод["outcomes"] == [{"key": "решён", "records": 2, "share": 66.7},
                                {"key": "не решён", "records": 1, "share": 33.3}]
    assert свод["reasons"][0] == {"key": "оплата", "records": 2, "share": 66.7}
    assert свод["resolved_share"] == pytest.approx(66.7)
    assert свод["actions"] == 3 and свод["records_with_actions"] == 2
    assert tasks.summarize_for_digest([])["resolved_share"] is None


# ---------------------------------------------------------------------------
# Клиент: кеш, одновременность, тайм-аут, память, проба
# ---------------------------------------------------------------------------


def test_the_client_caches_by_prompt_fingerprint(tmp_path):
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    клиент = LLMClient(_Настройки(), db)
    первый = клиент.chat("### задача: main\nсистема", "разговор", kind="main")
    второй = клиент.chat("### задача: main\nсистема", "разговор", kind="main")
    assert первый == второй
    assert клиент.calls == 1 and клиент.cache_hits == 1
    # Другая подсказка — другой отпечаток, снова вызов.
    клиент.chat("### задача: main\nсистема", "другой разговор", kind="main")
    assert клиент.calls == 2
    # Смена модели обесценивает кеш: ответ другой модели — не тот же ответ.
    клиент.settings.значения["llm_model"] = "другая"
    клиент.chat("### задача: main\nсистема", "разговор", kind="main")
    assert клиент.calls == 3 and клиент.cache_hits == 1
    # Мимо кеша — по требованию (проба сервера).
    клиент.chat("### задача: main\nсистема", "разговор", kind="main", use_cache=False)
    assert клиент.calls == 4


def test_the_client_limits_concurrency_and_reports_failures():
    """Больше одного вызова разом не идёт; сбой сети — LLMError с текстом,
    и он попадает в учёт и в состояние."""
    настройки = _Настройки(llm_backend="openai", llm_url="http://127.0.0.1:9",
                           llm_max_concurrent=1, llm_timeout_s=1)
    клиент = LLMClient(настройки)
    разом = []
    предел = []

    def медленный(*args, **kwargs):
        разом.append(1)
        предел.append(len(разом))
        time.sleep(0.15)
        разом.pop()
        return "{}"

    клиент._openai = медленный
    потоки = [threading.Thread(target=клиент.chat, args=("с", f"п{i}"),
                               kwargs={"use_cache": False}) for i in range(4)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join()
    assert max(предел) == 1, f"вызовы шли по {max(предел)} разом"
    assert клиент.calls == 4 and клиент.errors == 0

    # Настоящий вызов на закрытый порт: понятная ошибка, а не трассировка.
    свежий = LLMClient(настройки)
    with pytest.raises(LLMError) as ошибка:
        свежий.chat("с", "п", use_cache=False)
    assert "недоступен" in str(ошибка.value) or "не ответил" in str(ошибка.value)
    assert свежий.errors == 1 and свежий.status()["last_error"]
    assert свежий.status()["available"] is False


def test_the_client_waits_for_video_memory_and_probes_the_server():
    """Порог видеопамяти отменяет вызов; проба помнит ответ минуту."""
    настройки = _Настройки(llm_backend="openai", llm_min_free_vram_gb=8.0)
    клиент = LLMClient(настройки, hardware=lambda: 2.0)
    клиент._openai = lambda *a, **k: "{}"
    with pytest.raises(LLMError) as ошибка:
        клиент.chat("с", "п", use_cache=False)
    assert "видеопамяти" in str(ошибка.value) and клиент.errors == 0
    # Памяти хватило — вызов идёт.
    клиент._hardware = lambda: 12.0
    assert клиент.chat("с", "п", use_cache=False) == "{}"
    # Замер сломался — не повод не работать.
    клиент._hardware = lambda: 1 / 0
    assert клиент.chat("с", "п2", use_cache=False) == "{}"

    проб = []

    def проба():
        проб.append(1)
        return {"available": True, "models": ["m"], "model_known": True, "reason": None}

    клиент._probe_now = проба
    клиент.probe()
    клиент.probe()
    assert len(проб) == 1, "проба должна помниться"
    клиент.probe(fresh=True)
    assert len(проб) == 2
    # Выключенный слой не ходит в сеть вовсе.
    выкл = LLMClient(_Настройки(llm_backend="off"))
    assert выкл.probe() == {"available": False, "reason": "выключено"}
    assert not выкл.enabled
    with pytest.raises(LLMError):
        выкл.chat("с", "п")


def test_ollama_and_openai_requests_have_the_shape_each_server_expects(monkeypatch):
    """Тело запроса, путь и заголовки — те, что ждут Ollama и OpenAI."""
    записано = {}

    def поддельный_http(self, method, url, body, *, timeout):
        записано.update({"method": method, "url": url, "body": body, "timeout": timeout,
                         "key": self.settings.get("llm_api_key")})
        if url.endswith("/api/chat"):
            return {"message": {"content": '{"ok": 1}'}}
        if url.endswith("/v1/chat/completions"):
            return {"choices": [{"message": {"content": '{"ok": 2}'}}]}
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen3:14b"}]}
        return {"data": [{"id": "своя-модель"}]}

    monkeypatch.setattr(LLMClient, "_http", поддельный_http)
    оллама = LLMClient(_Настройки(llm_backend="ollama", llm_model="qwen3:14b"))
    assert оллама.chat("с", "п", use_cache=False) == '{"ok": 1}'
    assert записано["url"].endswith("/api/chat") and записано["body"]["format"] == "json"
    assert записано["body"]["stream"] is False and записано["body"]["keep_alive"]
    assert записано["body"]["options"]["temperature"] == 0
    assert [м["role"] for м in записано["body"]["messages"]] == ["system", "user"]
    assert оллама.probe(fresh=True)["model_known"] is True
    # Модель не скачана — проба это говорит и подсказывает команду.
    другая = LLMClient(_Настройки(llm_backend="ollama", llm_model="llama4:70b"))
    проба = другая.probe(fresh=True)
    assert проба["model_known"] is False and "ollama pull llama4:70b" in проба["reason"]

    совместимый = LLMClient(_Настройки(llm_backend="openai", llm_url="http://x:8000/",
                                       llm_model="своя-модель", llm_api_key="секрет"))
    assert совместимый.chat("с", "п", use_cache=False) == '{"ok": 2}'
    assert записано["url"] == "http://x:8000/v1/chat/completions"
    assert записано["body"]["response_format"] == {"type": "json_object"}
    assert записано["key"] == "секрет"
    assert совместимый.probe(fresh=True)["model_known"] is True
    # Пустой ответ — ошибка, а не пустое резюме в карточке.
    monkeypatch.setattr(LLMClient, "_http", lambda *a, **k: {"choices": []})
    with pytest.raises(LLMError):
        совместимый.chat("с", "п2", use_cache=False)


# ---------------------------------------------------------------------------
# Фоновый поток
# ---------------------------------------------------------------------------


def _архив(tmp_path, n: int = 3):
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for i in range(n):
        job_id = db.create_job({"id": f"l{i}", "filename": f"{i}.wav", "owner": "анна",
                                "model": "demo", "engine": "demo", "media_duration_s": 30.0})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (сейчас - i * 60, job_id))
        db.update_job(job_id, status="completed", finished_at=сейчас,
                      text="Здравствуйте, у меня заказ не пришёл. Перезвоню завтра.")
        db.save_segments(job_id, [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00",
                                   "text": "Здравствуйте, у меня заказ не пришёл."},
                                  {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01",
                                   "text": "Проверю и перезвоню завтра."}])
    return db


def test_the_worker_analyses_what_it_is_given_and_stores_the_answer(tmp_path):
    db = _архив(tmp_path)
    настройки = _Настройки()
    поток = LLMWorker(db, настройки, LLMClient(настройки, db))
    результат = поток.analyze_job("l0")
    assert результат["summary"] and результат["version"] == tasks.VERSION
    assert результат["outcome"] in настройки.get("llm_outcomes")
    assert результат["model"] == "stub" and результат["calls"] == 1
    assert isinstance(результат["actions"], list) and результат["error"] is None
    # Второй раз модель не спрашивается: ответ уже есть.
    поток.client.calls = 0
    снова = поток.analyze_job("l0")
    assert снова["created_at"] == результат["created_at"] and поток.client.calls == 0
    # force — спрашивается заново (ответ придёт из кеша подсказок).
    поток.analyze_job("l0", force=True)
    assert поток.client.calls + поток.client.cache_hits > 0
    # Ответ виден в базе и в списке ожидающих его больше нет.
    assert "l0" not in {з["id"] for з in db.llm_pending(tasks.VERSION, limit=10)}
    assert {з["id"] for з in db.llm_pending(tasks.VERSION, limit=10)} == {"l1", "l2"}
    # Смена версии подсказок возвращает запись в очередь.
    assert "l0" in {з["id"] for з in db.llm_pending(tasks.VERSION + 1, limit=10)}
    with pytest.raises(LLMError):
        поток.analyze_job("нет-такого")


def test_a_failing_model_is_recorded_and_does_not_loop(tmp_path):
    """Сбой модели кладётся в таблицу: запись не возвращается в очередь
    бесконечно, а в карточке видна причина."""
    db = _архив(tmp_path, n=1)
    настройки = _Настройки(llm_backend="openai", llm_url="http://127.0.0.1:9",
                           llm_timeout_s=1)
    клиент = LLMClient(настройки, db)
    клиент._openai = lambda *a, **k: (_ for _ in ()).throw(LLMError("сервер лёг"))
    поток = LLMWorker(db, настройки, клиент)
    with pytest.raises(LLMError):
        поток.analyze_job("l0")
    сохранено = db.llm_get("l0")
    assert сохранено["error"] == "сервер лёг" and сохранено["summary"] is None
    assert db.llm_pending(tasks.VERSION, limit=10) == []


def test_the_worker_yields_to_recognition_and_walks_the_archive(tmp_path):
    """Пока в очереди ждут задания — модель не вызывается; при разборе
    архива поток берёт записи сам."""
    db = _архив(tmp_path, n=3)
    настройки = _Настройки(llm_backfill=True)
    очередь = {"глубина": 5}
    поток = LLMWorker(db, настройки, LLMClient(настройки, db),
                      queue_state=lambda: очередь["глубина"])
    assert поток._queue_busy() is True
    поток.start()
    time.sleep(0.6)
    assert поток.done == 0, "разбор пошёл, пока очередь распознавания занята"
    очередь["глубина"] = 0
    крайний = time.time() + 20
    while time.time() < крайний and поток.done < 3:
        time.sleep(0.05)
    поток.stop()
    assert поток.done == 3 and поток.failed == 0
    assert db.llm_pending(tasks.VERSION, limit=10) == []
    состояние = поток.status()
    assert состояние["running"] is False and состояние["backfill"] is True

    # Без разбора архива поток берёт только то, что положили явно.
    настройки.значения["llm_backfill"] = False
    db.execute("DELETE FROM llm_results")
    поток2 = LLMWorker(db, настройки, LLMClient(настройки, db))
    поток2.enqueue("l1")
    поток2.start()
    крайний = time.time() + 20
    while time.time() < крайний and поток2.done < 1:
        time.sleep(0.05)
    time.sleep(0.4)
    поток2.stop()
    assert поток2.done == 1 and {з["id"] for з in db.llm_pending(tasks.VERSION, limit=10)} \
        == {"l0", "l2"}
    # Выключенный разбор новых записей ничего не ставит в очередь.
    настройки.значения["llm_auto"] = False
    поток2.enqueue("l0")
    assert поток2._pending.qsize() == 0


# ---------------------------------------------------------------------------
# Ручки, сводка, метрики
# ---------------------------------------------------------------------------


def test_the_layer_is_served_by_the_api_end_to_end(client, sample_wav: Path):
    """Состояние, проба, разбор записи, свод, отборы, сводка и метрики."""
    from asrhub.maintenance import build_digest

    состояние = client.app.state.hub
    состояние.settings.set("llm_backend", "stub")
    состояние.settings.set("llm_model", "stub")
    состояние.settings.values["llm_tasks"] = ["summary", "outcome", "actions"]

    with sample_wav.open("rb") as handle:
        job = client.post("/api/jobs", files={"file": ("смысл.wav", handle, "audio/wav")},
                          data={"settings": json.dumps({"model": "demo-simulator",
                                                        "engine": "demo",
                                                        "vad_backend": "energy"})}).json()
    for _ in range(80):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert job["status"] == "completed"

    состояние_слоя = client.get("/api/llm/status").json()
    assert состояние_слоя["backend"] == "stub" and состояние_слоя["available"] is True
    assert состояние_слоя["version"] == tasks.VERSION
    assert состояние_слоя["worker"]["running"] is True
    проба = client.post("/api/llm/test").json()
    assert проба["ok"] is True and проба["ms"] >= 0

    # Разбор по требованию: ответ приходит вместе с ним.
    ответ = client.post(f"/api/content/jobs/{job['id']}/llm").json()
    р = ответ["result"]
    assert р["summary"] and р["outcome"] in состояние.settings.get("llm_outcomes")
    assert р["model"] == "stub" and р["error"] is None
    assert client.get(f"/api/content/jobs/{job['id']}/llm").json()["result"]["summary"]
    assert client.get(f"/api/content/jobs/{job['id']}/llm").json()["stale"] is False

    свод = client.get("/api/content/llm?period=day").json()
    assert свод["analyzed"] == 1 and свод["records"] >= 1 and свод["enabled"] is True
    assert свод["outcomes"] and свод["outcomes"][0]["records"] == 1
    assert свод["coverage"] is not None and свод["errors"] == 0

    # Отборы в «Результатах» по ответу модели.
    исход = свод["outcomes"][0]["key"]
    отобрано = client.get(f"/api/jobs?content=outcome:{исход}&light=true").json()
    assert [з["id"] for з in отобрано["items"]] == [job["id"]]
    assert client.get("/api/jobs?content=outcome:нет-такого&light=true").json()["items"] == []
    если_действия = client.get("/api/jobs?content=llm_actions&light=true").json()
    assert isinstance(если_действия["items"], list)

    # Разбор архива ставит записи в очередь потоку.
    очередь = client.post("/api/llm/backfill", json={"limit": 10}).json()
    assert очередь["queued"] >= 0 and "worker" in очередь

    сводка = build_digest(состояние.analytics, состояние.settings, period="day")
    assert сводка["llm"]["analyzed"] == 1
    assert "По ответам языковой модели" in сводка["text"], сводка["text"]

    метрики = client.get("/api/monitoring/metrics").text
    assert "asrhub_llm_available 1" in метрики
    assert 'asrhub_llm_calls_total{status="ok"}' in метрики
    assert "asrhub_llm_coverage" in метрики

    # Выключенный слой: ручки отвечают отказом с подсказкой, а не молчат.
    состояние.settings.set("llm_backend", "off")
    assert client.post("/api/llm/test").status_code == 400
    assert client.post(f"/api/content/jobs/{job['id']}/llm").status_code == 400
    assert client.get("/api/llm/status").json()["enabled"] is False
    assert client.get("/api/content/llm?period=day").json()["enabled"] is False
    # Метрики слоя пропадают со следующего снимка: снимок живёт пять секунд.
    состояние.monitoring._cache_at = 0.0
    assert "asrhub_llm_available" not in client.get("/api/monitoring/metrics").text
