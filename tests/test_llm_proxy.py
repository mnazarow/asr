"""Шлюз к языковой модели из сети: /api/llm/v1 в формате OpenAI.

Проверяется то, что ломается: выключатель (пока он выключен — 403), формат
ответа (клиент OpenAI разбирает его буквально), поток, ключ доступа в обоих
видах заголовка, общий с разбором записей предел одновременности, понятная
ошибка при лежащем сервере модели и пределы на размер тела.

Настоящей модели здесь нет и быть не может: вместо неё — поддельный сервер
на http.server, который отвечает и как Ollama, и как OpenAI-совместимый,
умеет задерживать ответ (для проверки одновременности) и считает, сколько
запросов пришло к нему разом.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import threading
import time
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Поддельный сервер модели
# ---------------------------------------------------------------------------

КУСКИ = ["Здрав", "ствуй", "те!"]
ОТВЕТ = "".join(КУСКИ)


class _Обработчик(http.server.BaseHTTPRequestHandler):
    """Ollama и OpenAI-совместимый сервер в одном лице."""

    # HTTP/1.0: ответ потоком заканчивается закрытием соединения, и не надо
    # ни длины заранее, ни разбиения на куски по правилам HTTP/1.1.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):                             # тишина в выводе тестов
        pass

    def _тело(self) -> dict:
        длина = int(self.headers.get("Content-Length") or 0)
        сырое = self.rfile.read(длина) if длина else b"{}"
        try:
            return json.loads(сырое or b"{}")
        except ValueError:
            return {}

    def _json(self, код: int, данные: dict) -> None:
        тело = json.dumps(данные, ensure_ascii=False).encode("utf-8")
        self.send_response(код)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(тело)))
        self.end_headers()
        self.wfile.write(тело)

    def _поток(self, строки: list[str], тип: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", тип)
        self.end_headers()
        for строка in строки:
            self.wfile.write(строка.encode("utf-8"))
            self.wfile.flush()

    def do_GET(self):                                         # noqa: N802
        self.server.запросы.append(("GET", self.path, None))
        if self.path.startswith("/api/tags"):
            self._json(200, {"models": [{"name": "qwen3:14b"}, {"name": "llama3:8b"}]})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list",
                             "data": [{"id": "своя-модель", "object": "model"}]})
        else:
            self._json(404, {"error": "нет такого адреса"})

    def do_POST(self):                                        # noqa: N802
        тело = self._тело()
        self.server.запросы.append(("POST", self.path, тело))
        with self.server.замок:
            self.server.разом += 1
            self.server.предел = max(self.server.предел, self.server.разом)
        try:
            if self.server.задержка:
                time.sleep(self.server.задержка)
            self._ответить(тело)
        finally:
            with self.server.замок:
                self.server.разом -= 1

    def _ответить(self, тело: dict) -> None:
        if self.server.падать:
            self._json(500, {"error": "модель упала"})
            return
        if self.path.startswith("/api/chat"):
            self._ollama_chat(тело)
        elif self.path.startswith("/v1/chat/completions"):
            self._openai_chat(тело)
        elif self.path.startswith("/api/embed"):
            self._json(200, {"embeddings": [[0.1, 0.2, 0.3]
                                            for _ in (тело.get("input") or [""])],
                             "prompt_eval_count": 4})
        elif self.path.startswith("/v1/embeddings"):
            вход = тело.get("input") or [""]
            self._json(200, {"object": "list",
                             "data": [{"object": "embedding", "index": н,
                                       "embedding": [0.1, 0.2, 0.3]}
                                      for н, _ in enumerate(вход)],
                             "usage": {"prompt_tokens": 4, "total_tokens": 4}})
        else:
            self._json(404, {"error": "нет такого адреса"})

    def _ollama_chat(self, тело: dict) -> None:
        модель = тело.get("model") or "?"
        if not тело.get("stream"):
            self._json(200, {"model": модель, "done": True, "done_reason": "stop",
                             "message": {"role": "assistant", "content": ОТВЕТ},
                             "prompt_eval_count": 7, "eval_count": 3})
            return
        if self.server.без_потока:
            # Сервер потоком не умеет и отвечает целиком, игнорируя stream.
            self._json(200, {"model": модель, "done": True, "done_reason": "stop",
                             "message": {"role": "assistant", "content": ОТВЕТ},
                             "prompt_eval_count": 7, "eval_count": 3})
            return
        строки = [json.dumps({"model": модель, "done": False,
                              "message": {"role": "assistant", "content": к}},
                             ensure_ascii=False) + "\n" for к in КУСКИ]
        строки.append(json.dumps({"model": модель, "done": True, "done_reason": "stop",
                                  "message": {"role": "assistant", "content": ""},
                                  "prompt_eval_count": 7, "eval_count": 3}) + "\n")
        self._поток(строки, "application/x-ndjson")

    def _openai_chat(self, тело: dict) -> None:
        модель = тело.get("model") or "?"
        if not тело.get("stream") or self.server.без_потока:
            self._json(200, {"id": "chatcmpl-поддельный", "object": "chat.completion",
                             "created": 1757500001, "model": модель,
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": ОТВЕТ}}],
                             "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                       "total_tokens": 10}})
            return
        строки = []
        for к in КУСКИ:
            строки.append("data: " + json.dumps(
                {"id": "chatcmpl-поддельный", "object": "chat.completion.chunk",
                 "created": 1757500001, "model": модель,
                 "choices": [{"index": 0, "delta": {"content": к}, "finish_reason": None}]},
                ensure_ascii=False) + "\n\n")
        строки.append("data: " + json.dumps(
            {"id": "chatcmpl-поддельный", "object": "chat.completion.chunk",
             "created": 1757500001, "model": модель,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}) + "\n\n")
        строки.append("data: [DONE]\n\n")
        self._поток(строки, "text/event-stream")


@contextlib.contextmanager
def поддельная_модель(*, задержка: float = 0.0, падать: bool = False,
                      без_потока: bool = False):
    """Поднимает поддельный сервер модели и отдаёт его адрес."""
    сервер = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Обработчик)
    сервер.задержка = задержка
    сервер.падать = падать
    сервер.без_потока = без_потока
    сервер.запросы = []
    сервер.разом = 0
    сервер.предел = 0
    сервер.замок = threading.Lock()
    сервер.daemon_threads = True
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        yield сервер, f"http://127.0.0.1:{сервер.server_address[1]}"
    finally:
        сервер.shutdown()
        сервер.server_close()
        поток.join(timeout=5)


def настроить(client: TestClient, **значения) -> None:
    """Настройки слоя прямо в значениях: часть из них уже проверена каталогом."""
    состояние = client.app.state.hub
    for ключ, значение in значения.items():
        состояние.settings.values[ключ] = значение


def куски_потока(текст: str) -> list[dict]:
    """Разбирает ответ SSE в список объектов JSON (без финального [DONE])."""
    ответы = []
    for строка in текст.splitlines():
        строка = строка.strip()
        if not строка.startswith("data:"):
            continue
        полезное = строка[5:].strip()
        if полезное == "[DONE]":
            break
        ответы.append(json.loads(полезное))
    return ответы


# ---------------------------------------------------------------------------
# Выключатель
# ---------------------------------------------------------------------------

def test_the_gateway_is_closed_until_it_is_switched_on(client):
    """Пока llm_network_enabled выключен, все три маршрута отвечают 403.

    Это главная защита всей затеи: сервер с настроенным смысловым слоем
    после обновления не должен начать раздавать модель всей сети молча.
    """
    настроить(client, llm_backend="stub", llm_model="stub", llm_network_enabled=False)

    чат = client.post("/api/llm/v1/chat/completions",
                      json={"messages": [{"role": "user", "content": "привет"}]})
    assert чат.status_code == 403, чат.text
    тело = чат.json()
    assert тело["code"] == "llm_network_disabled"
    assert "llm_network_enabled" in тело["hint"]
    # Чужой клиент читает error.message — без него человек видит голое
    # «Error code: 403» и не знает, что включать.
    assert "выключен" in тело["error"]["message"]

    assert client.get("/api/llm/v1/models").status_code == 403
    assert client.post("/api/llm/v1/embeddings", json={"input": "текст"}).status_code == 403

    # Включили — тот же запрос проходит (на заглушке, без сервера модели).
    настроить(client, llm_network_enabled=True)
    ответ = client.post("/api/llm/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "привет"}]})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["choices"][0]["message"]["content"]


def test_the_gateway_says_when_the_model_layer_itself_is_off(client):
    """Слой выключен целиком — 400 с подсказкой, а не пустой ответ."""
    настроить(client, llm_backend="off", llm_network_enabled=True)
    ответ = client.post("/api/llm/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "п"}]})
    assert ответ.status_code == 400
    assert "llm_backend" in ответ.json()["hint"]


# ---------------------------------------------------------------------------
# Проброс и формат
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_a_chat_is_proxied_and_comes_back_in_openai_shape(client, backend):
    """Запрос уходит на сервер модели, ответ приходит в формате OpenAI."""
    with поддельная_модель() as (сервер, адрес):
        настроить(client, llm_backend=backend, llm_url=адрес,
                  llm_model="qwen3:14b", llm_network_enabled=True)
        ответ = client.post("/api/llm/v1/chat/completions", json={
            "model": "qwen3:14b",
            "messages": [{"role": "system", "content": "Отвечай коротко."},
                         {"role": "user", "content": "Поздоровайся"}],
            "temperature": 0.2, "max_tokens": 64, "top_p": 0.9, "stop": ["\n\n"]})
    assert ответ.status_code == 200, ответ.text
    данные = ответ.json()
    assert данные["object"] == "chat.completion"
    assert данные["id"].startswith("chatcmpl-")
    assert данные["model"] == "qwen3:14b"
    assert isinstance(данные["created"], int)
    выбор = данные["choices"][0]
    assert выбор["index"] == 0 and выбор["finish_reason"] == "stop"
    assert выбор["message"] == {"role": "assistant", "content": ОТВЕТ}
    assert данные["usage"] == {"prompt_tokens": 7, "completion_tokens": 3,
                               "total_tokens": 10}

    метод, путь, тело = сервер.запросы[-1]
    assert метод == "POST"
    if backend == "ollama":
        assert путь == "/api/chat"
        # max_tokens у Ollama называется иначе — без перевода он пропал бы.
        assert тело["options"]["num_predict"] == 64
        assert тело["options"]["temperature"] == 0.2
        assert тело["options"]["top_p"] == 0.9
        assert тело["options"]["stop"] == ["\n\n"]
        assert тело["stream"] is False and тело["keep_alive"]
    else:
        assert путь == "/v1/chat/completions"
        # Совместимому серверу поля едут как пришли: понимает их он, не мы.
        assert тело["temperature"] == 0.2 and тело["max_tokens"] == 64
        assert тело["stop"] == ["\n\n"]
    assert [м["role"] for м in тело["messages"]] == ["system", "user"]


def test_the_model_name_defaults_to_the_one_in_settings(client):
    """Модель не назвали — берём ту, что настроена: клиенты OpenAI часто
    шлют своё имя или не шлют ничего вовсе."""
    with поддельная_модель() as (сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес,
                  llm_model="qwen3:14b", llm_network_enabled=True)
        ответ = client.post("/api/llm/v1/chat/completions",
                            json={"messages": [{"role": "user", "content": "п"}]})
    assert ответ.status_code == 200
    assert ответ.json()["model"] == "qwen3:14b"
    assert сервер.запросы[-1][2]["model"] == "qwen3:14b"


def test_a_broken_request_is_explained_in_russian(client):
    """Запрос без messages — понятный отказ, а не пятисотка."""
    настроить(client, llm_backend="stub", llm_model="stub", llm_network_enabled=True)
    пусто = client.post("/api/llm/v1/chat/completions", json={"model": "stub"})
    assert пусто.status_code == 400
    assert "messages" in пусто.json()["message"]

    не_объект = client.post("/api/llm/v1/chat/completions",
                            json={"messages": ["просто строка"]})
    assert не_объект.status_code == 400
    assert "не объект" in не_объект.json()["message"]

    пустые = client.post("/api/llm/v1/chat/completions",
                         json={"messages": [{"role": "user", "content": "   "}]})
    assert пустые.status_code == 400
    assert "пуст" in пустые.json()["message"]


def test_a_body_that_is_too_large_is_refused_before_it_is_read(client):
    """Предел размера тела: иначе один запрос занимает память сервера."""
    from asrhub.api import routes_llm_proxy as шлюз

    настроить(client, llm_backend="stub", llm_model="stub", llm_network_enabled=True)
    огромное = "а" * (шлюз.ПРЕДЕЛ_ТЕЛА + 1024)
    ответ = client.post("/api/llm/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": огромное}]})
    assert ответ.status_code == 413
    assert ответ.json()["code"] == "file_too_large"


# ---------------------------------------------------------------------------
# Поток
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_streaming_gives_chunks_and_closes_with_done(client, backend):
    """stream: true — куски data: {...} и финальный data: [DONE]."""
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend=backend, llm_url=адрес,
                  llm_model="qwen3:14b", llm_network_enabled=True)
        ответ = client.post("/api/llm/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "Поздоровайся"}], "stream": True})
    assert ответ.status_code == 200, ответ.text
    assert ответ.headers["content-type"].startswith("text/event-stream")
    assert ответ.text.rstrip().endswith("data: [DONE]")
    # Каждое событие SSE обязано кончаться пустой строкой: поток без
    # разделителей библиотека openai читает как один бесконечный кусок и не
    # показывает ничего до самого конца ответа.
    события = [с for с in ответ.text.split("\n\n") if с.strip()]
    assert len(события) == len(куски_потока(ответ.text)) + 1, ответ.text
    assert all(с.startswith("data:") for с in события), события

    куски = куски_потока(ответ.text)
    assert куски, ответ.text
    assert all(к["object"] == "chat.completion.chunk" for к in куски)
    собрано = "".join(str((к["choices"][0]["delta"] or {}).get("content") or "")
                      for к in куски)
    assert собрано == ОТВЕТ
    assert куски[-1]["choices"][0]["finish_reason"] == "stop"


def test_streaming_works_even_if_the_model_server_cannot_stream(client):
    """Сервер игнорирует stream и отвечает целиком — отдаём один кусок."""
    with поддельная_модель(без_потока=True) as (_сервер, адрес):
        настроить(client, llm_backend="openai", llm_url=адрес,
                  llm_model="своя-модель", llm_network_enabled=True)
        ответ = client.post("/api/llm/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "п"}], "stream": True})
    assert ответ.status_code == 200, ответ.text
    куски = куски_потока(ответ.text)
    собрано = "".join(str((к["choices"][0]["delta"] or {}).get("content") or "")
                      for к in куски)
    assert собрано == ОТВЕТ
    assert ответ.text.rstrip().endswith("data: [DONE]")


def test_streaming_on_the_stub_backend_needs_no_model_server(client):
    """Заглушка тоже отвечает потоком: так проверяют настройку до модели."""
    настроить(client, llm_backend="stub", llm_model="stub", llm_network_enabled=True)
    ответ = client.post("/api/llm/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Здравствуйте, у меня заказ не пришёл."}],
        "stream": True})
    assert ответ.status_code == 200
    куски = куски_потока(ответ.text)
    assert "".join(str((к["choices"][0]["delta"] or {}).get("content") or "")
                   for к in куски)
    assert ответ.text.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# Список моделей и векторы
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend,ожидаемое",
                         [("ollama", "qwen3:14b"), ("openai", "своя-модель")])
def test_the_model_list_is_in_openai_shape(client, backend, ожидаемое):
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend=backend, llm_url=адрес,
                  llm_model=ожидаемое, llm_network_enabled=True)
        ответ = client.get("/api/llm/v1/models")
    assert ответ.status_code == 200, ответ.text
    данные = ответ.json()
    assert данные["object"] == "list"
    assert данные["data"] and данные["data"][0]["object"] == "model"
    # Настроенная модель — первой: именно её отдаёт шлюз без явного имени.
    assert данные["data"][0]["id"] == ожидаемое
    assert all("created" in м and "owned_by" in м for м in данные["data"])


def test_embeddings_work_where_they_can_and_explain_where_they_cannot(client):
    with поддельная_модель() as (сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес,
                  llm_model="nomic-embed-text", llm_network_enabled=True)
        ответ = client.post("/api/llm/v1/embeddings",
                            json={"input": ["первый", "второй"]})
        assert ответ.status_code == 200, ответ.text
        данные = ответ.json()
        assert данные["object"] == "list" and len(данные["data"]) == 2
        assert данные["data"][0] == {"object": "embedding", "index": 0,
                                     "embedding": [0.1, 0.2, 0.3]}
        assert данные["data"][1]["index"] == 1
        assert сервер.запросы[-1][1].startswith("/api/embed")

        # Строкой вместо списка — так тоже шлют.
        одна = client.post("/api/llm/v1/embeddings", json={"input": "текст"})
        assert одна.status_code == 200 and len(одна.json()["data"]) == 1

        пусто = client.post("/api/llm/v1/embeddings", json={"model": "m"})
        assert пусто.status_code == 400 and "input" in пусто.json()["message"]

    # Заглушка векторов не считает — 501 с объяснением по-русски.
    настроить(client, llm_backend="stub", llm_model="stub")
    отказ = client.post("/api/llm/v1/embeddings", json={"input": "текст"})
    assert отказ.status_code == 501
    assert отказ.json()["code"] == "not_supported"
    assert "векторы" in отказ.json()["message"]


# ---------------------------------------------------------------------------
# Сбои сервера модели
# ---------------------------------------------------------------------------

def test_a_dead_model_server_is_reported_in_plain_russian(client):
    """Ни трассировки, ни пустого ответа: 502 с адресом и подсказкой."""
    настроить(client, llm_backend="ollama", llm_url="http://127.0.0.1:9",
              llm_model="qwen3:14b", llm_network_enabled=True)
    client.app.state.hub.settings.values["llm_timeout_s"] = 2

    ответ = client.post("/api/llm/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "п"}]})
    assert ответ.status_code == 502, ответ.text
    тело = ответ.json()
    assert тело["code"] == "llm_upstream_error"
    assert "недоступен" in тело["message"] or "не ответил" in тело["message"]
    assert "127.0.0.1:9" in тело["hint"]
    # Сбой попал в учёт слоя — состояние обязано его показать.
    assert client.get("/api/llm/status").json()["last_error"]

    # То же самое для списка моделей и для потока: код другой, молчания нет.
    assert client.get("/api/llm/v1/models").status_code == 502
    поток = client.post("/api/llm/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "п"}], "stream": True})
    assert поток.status_code == 502


def test_an_error_from_the_model_server_reaches_the_client(client):
    """Сервер модели ответил пятисоткой — говорим об этом, а не молчим."""
    with поддельная_модель(падать=True) as (_сервер, адрес):
        настроить(client, llm_backend="openai", llm_url=адрес,
                  llm_model="м", llm_network_enabled=True)
        ответ = client.post("/api/llm/v1/chat/completions",
                            json={"messages": [{"role": "user", "content": "п"}]})
    assert ответ.status_code == 502
    assert "500" in ответ.json()["message"]


# ---------------------------------------------------------------------------
# Одновременность: общая с разбором записей
# ---------------------------------------------------------------------------

def test_the_gateway_waits_for_the_very_same_slot_as_the_analysis(client):
    """Слот один на всех: занят разбором — запрос из сети ждёт и отвечает 503.

    Проверяется именно тождество объекта, а не «похожее поведение»: слот
    занимается тем самым семафором, который берёт `LLMClient.chat`. Если
    шлюз заведёт свой, предел «одновременных вызовов: 1» станет двойкой, и
    разбор записей начнёт делить видеокарту с сетью.
    """
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес, llm_model="м",
                  llm_network_enabled=True, llm_max_concurrent=1)
        client.app.state.hub.settings.values["llm_timeout_s"] = 1
        семафор = client.app.state.hub.llm._семафор()
        assert семафор.acquire(timeout=1), "слот должен быть свободен до проверки"
        try:
            занято = client.post("/api/llm/v1/chat/completions",
                                 json={"messages": [{"role": "user", "content": "п"}]})
        finally:
            семафор.release()
        assert занято.status_code == 503, занято.text
        assert занято.json()["code"] == "llm_busy"
        assert "занята" in занято.json()["message"]

        # Слот освободился — тот же запрос проходит.
        снова = client.post("/api/llm/v1/chat/completions",
                            json={"messages": [{"role": "user", "content": "п"}]})
        assert снова.status_code == 200, снова.text


def test_parallel_requests_never_exceed_the_limit(client):
    """Четыре запроса разом — на сервере модели их не больше предела."""
    with поддельная_модель(задержка=0.2) as (сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес, llm_model="м",
                  llm_network_enabled=True, llm_max_concurrent=1)
        client.app.state.hub.settings.values["llm_timeout_s"] = 30
        коды: list[int] = []
        замок = threading.Lock()

        def запросить():
            ответ = client.post("/api/llm/v1/chat/completions",
                                json={"messages": [{"role": "user", "content": "п"}]})
            with замок:
                коды.append(ответ.status_code)

        потоки = [threading.Thread(target=запросить) for _ in range(4)]
        for п in потоки:
            п.start()
        for п in потоки:
            п.join(timeout=60)
        assert коды == [200] * 4, коды
        assert сервер.предел == 1, f"к модели шло по {сервер.предел} запроса разом"


def test_a_stream_holds_the_slot_until_it_is_finished(client):
    """Пока идут куски, модель занята — слот освобождается только в конце."""
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес, llm_model="м",
                  llm_network_enabled=True, llm_max_concurrent=1)
        ответ = client.post("/api/llm/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "п"}], "stream": True})
        assert ответ.status_code == 200
        assert ответ.text.rstrip().endswith("data: [DONE]")
        # Поток дочитан — слот обязан вернуться, иначе следующий запрос
        # (и разбор записей) ждал бы освобождения вечно.
        семафор = client.app.state.hub.llm._семафор()
        assert семафор.acquire(timeout=2), "слот не освободился после потока"
        семафор.release()


def test_a_client_that_hangs_up_mid_stream_does_not_hold_the_slot(client):
    """Клиент бросил чтение на середине — слот всё равно возвращается.

    Это не редкость, а обычное дело: человек закрыл вкладку чата, программа
    отвалилась по своему тайм-ауту. Python закрывает недочитанный генератор
    изнутри, и если убирать за собой в том же месте, где отдаётся признак
    конца, уборка не выполнится — слот к видеокарте останется занятым
    навсегда, и разбор записей встанет после первого же такого обрыва.
    """
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес, llm_model="м",
                  llm_network_enabled=True, llm_max_concurrent=1)
        with client.stream("POST", "/api/llm/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "п"}],
                                 "stream": True}) as ответ:
            assert ответ.status_code == 200
            for _ in ответ.iter_lines():
                break                                  # бросаем на первом куске
        семафор = client.app.state.hub.llm._семафор()
        assert семафор.acquire(timeout=5), "слот не вернулся после обрыва"
        семафор.release()


# ---------------------------------------------------------------------------
# Учёт
# ---------------------------------------------------------------------------

def test_proxied_calls_are_counted_where_the_usual_ones_are(client):
    """Запрос из сети виден в тех же счётчиках, что и разбор записей."""
    до = client.get("/api/llm/status").json()["calls"]
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес, llm_model="м",
                  llm_network_enabled=True)
        for _ in range(3):
            assert client.post("/api/llm/v1/chat/completions",
                               json={"messages": [{"role": "user", "content": "п"}]}
                               ).status_code == 200
        состояние = client.get("/api/llm/status").json()
    assert состояние["calls"] == до + 3
    assert состояние["avg_ms"] is not None and состояние["last_ms"] is not None
    assert состояние["last_error"] is None

    счёт = client.app.state.llm_proxy.свод()
    assert счёт["requests"] >= 3
    # Токены считаются: сервер модели сказал, сколько их было.
    assert счёт["tokens_in"] >= 21 and счёт["tokens_out"] >= 9


def test_the_journal_gets_a_line_now_and_then_but_not_on_every_request(client):
    """События пишутся раз в N запросов и при сбое — журнал не засоряется."""
    from asrhub.api import routes_llm_proxy as шлюз

    состояние = client.app.state.hub
    with поддельная_модель() as (_сервер, адрес):
        настроить(client, llm_backend="ollama", llm_url=адрес, llm_model="м",
                  llm_network_enabled=True)
        client.post("/api/llm/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "п"}]})
        события = [с for с in состояние.db.get_events(limit=200)
                   if с["kind"] == "llm_proxy"]
        assert события == [], "на каждый запрос событие писать нельзя"

        # Доводим счётчик до порога — событие появляется одно, со сводкой.
        счёт = client.app.state.llm_proxy
        счёт._с_события = шлюз.СОБЫТИЕ_КАЖДЫЕ - 1
        client.post("/api/llm/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "п"}]})
    события = [с for с in состояние.db.get_events(limit=200) if с["kind"] == "llm_proxy"]
    assert len(события) == 1, события
    assert "запросов" in события[0]["message"]
    assert события[0]["data"]["requests"] >= 2

    # Сбой пишется сразу, не дожидаясь порога.
    настроить(client, llm_url="http://127.0.0.1:9")
    состояние.settings.values["llm_timeout_s"] = 2
    client.post("/api/llm/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "п"}]})
    сбои = [с for с in состояние.db.get_events(limit=200)
            if с["kind"] == "llm_proxy" and "сбой" in с["message"]]
    assert len(сбои) == 1, сбои


# ---------------------------------------------------------------------------
# Ключ доступа
# ---------------------------------------------------------------------------

@pytest.fixture()
def ключи(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Приложение со включённой аутентификацией и включённым шлюзом."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.api_keys["ah_alice"] = {"name": "Алиса", "role": "user", "enabled": True}
    settings.api_keys["ah_ro"] = {"name": "чтение", "role": "readonly", "enabled": True}
    settings.values["llm_backend"] = "stub"
    settings.values["llm_model"] = "stub"
    settings.values["llm_network_enabled"] = True
    app = create_app(settings, start_queue=False)
    with TestClient(app) as c:
        yield c


def test_the_key_is_required_and_bearer_is_accepted(ключи):
    """Ключ обязателен, и заголовок Bearer принимается наравне с X-API-Key.

    Клиенты OpenAI шлют ключ только так — `Authorization: Bearer <ключ>`.
    Отдельного разбора для этого не понадобилось: `deps.token_from` давно
    принимает и X-API-Key, и Bearer, и голый Authorization — одним местом
    на весь сервер. Проверка здесь стоит затем, чтобы это свойство не
    потерялось: расхождение между «кем тебя считает проверка» и «кем
    считает защита» — дыра, а не неудобство.
    """
    тело = {"messages": [{"role": "user", "content": "привет"}]}

    без_ключа = ключи.post("/api/llm/v1/chat/completions", json=тело)
    assert без_ключа.status_code == 401
    assert без_ключа.json()["code"] == "auth_error"

    чужой = ключи.post("/api/llm/v1/chat/completions", json=тело,
                       headers={"Authorization": "Bearer ah_no_such_key"})
    assert чужой.status_code == 401

    bearer = ключи.post("/api/llm/v1/chat/completions", json=тело,
                        headers={"Authorization": "Bearer ah_alice"})
    assert bearer.status_code == 200, bearer.text
    assert bearer.json()["choices"][0]["message"]["content"]

    заголовком = ключи.post("/api/llm/v1/chat/completions", json=тело,
                            headers={"X-API-Key": "ah_alice"})
    assert заголовком.status_code == 200

    # Ключ только на чтение моделью не пользуется: это запись видеокарты.
    только_чтение = ключи.post("/api/llm/v1/chat/completions", json=тело,
                               headers={"Authorization": "Bearer ah_ro"})
    assert только_чтение.status_code == 403

    assert ключи.get("/api/llm/v1/models",
                     headers={"Authorization": "Bearer ah_alice"}).status_code == 200
    assert ключи.get("/api/llm/v1/models").status_code == 401
