"""Заход 38: «Ollama вернул пустой ответ» в очереди к модели.

Настоящей модели здесь нет. Вместо неё — поддельный Ollama, который ведёт
себя так, как ведут себя настоящие семейства: рассуждающая модель тратит
предел на рассуждение, gpt-oss рассуждает всегда и понимает только уровни,
модель без рассуждения отвергает поле think, у третьей грамматика JSON
спорит с шаблоном. Каждая проверка — про то, что клиент делает с таким
ответом, а не про текст исходника.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from asrhub.llm import client as клиент_модуль
from asrhub.llm.client import LLMClient, LLMError

ОТВЕТ = '{"summary": "клиент спросил про оплату", "resolved": true}'


class _Настройки(dict):
    """Настройки слоя: читаются через get()."""

    def get(self, ключ, запасное=None):
        return super().get(ключ, запасное)


def _настройки(**поверх) -> _Настройки:
    основа = {"llm_backend": "ollama", "llm_model": "qwen3.8:27b",
              "llm_url": "http://127.0.0.1:11434", "llm_timeout_s": 5,
              "llm_max_concurrent": 1, "llm_context_chars": 12000}
    основа.update(поверх)
    return _Настройки(основа)


def _ответ(content: str = "", thinking: str = "", done_reason: str = "stop",
           eval_count: int | None = None, prompt: int = 900) -> dict[str, Any]:
    сообщение: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking:
        сообщение["thinking"] = thinking
    return {"message": сообщение, "done": True, "done_reason": done_reason,
            "eval_count": eval_count if eval_count is not None else max(1, len(content) // 3),
            "prompt_eval_count": prompt}


def _отказ_think(модель: str) -> LLMError:
    тело = f'{{"error":"\\"{модель}\\" does not support thinking"}}'
    ошибка = LLMError(f"Сервер модели ответил 400: {тело}")
    ошибка.status = 400
    ошибка.body = тело
    return ошибка


class _Ollama:
    """Поддельный Ollama. `поведение` — как отвечает модель на тело запроса."""

    def __init__(self, поведение):
        self.поведение = поведение
        self.запросы: list[dict[str, Any]] = []

    def __call__(self, клиент, method, url, body, *, timeout):
        self.запросы.append(json.loads(json.dumps(body)))
        return self.поведение(body)


def _предел(тело) -> int:
    return int((тело.get("options") or {}).get("num_predict") or 100000)


def рассуждающая(тело):
    """Как Qwen 3.x: рассуждает на 1500 токенов, пока не попросят не думать."""
    if тело.get("think") is False:
        return _ответ(ОТВЕТ)
    if _предел(тело) < 1600:
        return _ответ("", thinking="Разберём разговор. " * 300,
                      done_reason="length", eval_count=_предел(тело))
    return _ответ(ОТВЕТ, thinking="Короткое рассуждение.")


def долго_думает(тело):
    """Рассуждает на 6000 токенов — дольше любого запаса, — пока не попросят."""
    if тело.get("think") is False:
        return _ответ(ОТВЕТ)
    if _предел(тело) < 6100:
        return _ответ("", thinking="Разберём. " * 900, done_reason="length",
                      eval_count=_предел(тело))
    return _ответ(ОТВЕТ, thinking="Долгое рассуждение.")


def упрямая(тело):
    """Рассуждает на 2500 токенов, что бы ни просили."""
    if _предел(тело) < 2600:
        return _ответ("", thinking="analysis " * 400, done_reason="length",
                      eval_count=_предел(тело))
    return _ответ(ОТВЕТ, thinking="analysis")


def gpt_oss(тело):
    """Как gpt-oss: понимает только уровни, «low» — это 800 токенов."""
    сколько = 800 if тело.get("think") == "low" else 2500
    if _предел(тело) < сколько + 100:
        return _ответ("", thinking="analysis " * 300, done_reason="length",
                      eval_count=_предел(тело))
    return _ответ(ОТВЕТ, thinking="analysis")


def без_рассуждения_модель(тело):
    """Модель, которая рассуждать не умеет: поле think отвергает."""
    if "think" in тело:
        raise _отказ_think(тело["model"])
    return _ответ(ОТВЕТ)


def спорит_с_грамматикой(тело):
    """С форматом JSON замолкает, без него отвечает текстом с JSON внутри."""
    if тело.get("format") == "json":
        return _ответ("", eval_count=1)
    return _ответ("Вот ответ: " + ОТВЕТ)


def вечно_думает(тело):
    """Не отвечает никогда: всё уходит в рассуждение."""
    return _ответ("", thinking="…" * 500, done_reason="length",
                  eval_count=_предел(тело))


def не_влезла(тело):
    """Подсказка заняла всё окно, модель закончила, не начав."""
    return _ответ("", eval_count=0, prompt=тело["options"]["num_ctx"])


def _подменить(monkeypatch, ollama: _Ollama) -> None:
    """Подставляет поддельный Ollama на место сети клиента.

    Обычной функцией, а не связанным методом подделки: связанный метод,
    положенный в класс, не получает клиента первым аргументом.
    """
    def http(клиент, method, url, body, *, timeout):
        return ollama(клиент, method, url, body, timeout=timeout)

    monkeypatch.setattr(LLMClient, "_http", http)


def _клиент(monkeypatch, поведение, **настройки) -> tuple[LLMClient, _Ollama]:
    ollama = _Ollama(поведение)
    _подменить(monkeypatch, ollama)
    return LLMClient(_настройки(**настройки)), ollama


# --------------------------------------------------------------------------
# Сама причина: рассуждение съедало предел ответа
# --------------------------------------------------------------------------

def test_рассуждающая_модель_больше_не_отвечает_пустотой(monkeypatch):
    """Ровно случай из очереди: Qwen 3.x, предел 1200, пустой content.

    Прежний клиент не посылал think и читал только content — рассуждающая
    модель тратила весь предел на рассуждение, и очередь падала с «Ollama
    вернул пустой ответ» почти на каждой записи.
    """
    клиент, ollama = _клиент(monkeypatch, рассуждающая)
    assert клиент.chat("система", "разговор", use_cache=False) == ОТВЕТ
    assert len(ollama.запросы) == 1, "ответ должен прийти с первого раза"
    assert ollama.запросы[0]["think"] is False


def test_прежний_запрос_воспроизводит_ошибку():
    """Проверка поддельной модели: старое тело даёт ту самую пустоту.

    Без этого проверки выше доказывали бы только то, что подделка
    отвечает на новое тело, — а не то, что старое на ней ломалось.
    """
    старое_тело = {"model": "qwen3.8:27b", "stream": False,
                   "options": {"temperature": 0, "num_predict": 1200}}
    ответ = клиент_модуль.разобрать_ollama(рассуждающая(старое_тело))
    assert ответ.текст == ""
    assert ответ.почему_пусто == "рассуждение"


def test_gpt_oss_просят_коротко_а_не_false(monkeypatch):
    """gpt-oss рассуждение не выключает; «false» для неё ничего не значит."""
    клиент, ollama = _клиент(monkeypatch, gpt_oss, llm_model="gpt-oss:20b")
    assert клиент.chat("система", "разговор", use_cache=False) == ОТВЕТ
    assert ollama.запросы[0]["think"] == "low"
    # Под «low» прибавлен запас: 1200 на ответ плюс 2048 на рассуждение.
    assert ollama.запросы[0]["options"]["num_predict"] == 1200 + 2048


def test_модели_без_рассуждения_think_больше_не_шлётся(monkeypatch):
    """Отказ из-за think лечится запросом без него — и запоминается."""
    клиент, ollama = _клиент(monkeypatch, без_рассуждения_модель,
                             llm_model="gemma4:26b")
    assert клиент.chat("система", "первый", use_cache=False) == ОТВЕТ
    assert клиент.chat("система", "второй", use_cache=False) == ОТВЕТ
    assert "think" in ollama.запросы[0]
    assert "think" not in ollama.запросы[1]
    # Второй вызов не ловит тот же отказ заново: запросов три, а не четыре.
    assert len(ollama.запросы) == 3


def test_рассуждение_съело_предел_повтор_с_выключенным(monkeypatch):
    """«Как решит модель» — и модель решила думать дольше предела с запасом."""
    клиент, ollama = _клиент(monkeypatch, долго_думает, llm_think="model",
                             llm_max_tokens=512)
    assert клиент.chat("система", "разговор", use_cache=False) == ОТВЕТ
    первый, второй = ollama.запросы
    assert "think" not in первый
    # Под «как решит модель» запас прибавлен и с первого раза.
    assert первый["options"]["num_predict"] == 512 + 4096
    assert второй["think"] is False
    assert второй["options"]["num_predict"] > первый["options"]["num_predict"]


def test_упрямой_модели_помогает_запас(monkeypatch):
    """Модель рассуждает вопреки false — второй раз предел заметно больше."""
    клиент, ollama = _клиент(monkeypatch, упрямая)
    assert клиент.chat("система", "разговор", use_cache=False) == ОТВЕТ
    assert ollama.запросы[1]["options"]["num_predict"] >= 1200 + 4096


def test_грамматика_json_спорит_с_шаблоном(monkeypatch):
    """Модель замолкает с форматом JSON — второй раз без формата."""
    клиент, ollama = _клиент(monkeypatch, спорит_с_грамматикой,
                             llm_model="gemma4:12b")
    ответ = клиент.chat("система", "разговор", use_cache=False)
    assert "клиент спросил про оплату" in ответ
    assert ollama.запросы[0].get("format") == "json"
    assert "format" not in ollama.запросы[-1]


def test_повтор_один_а_не_бесконечный(monkeypatch):
    """Вызов модели — секунды, а очередь к ней одна."""
    клиент, ollama = _клиент(monkeypatch, вечно_думает)
    with pytest.raises(LLMError):
        клиент.chat("система", "разговор", use_cache=False)
    assert len(ollama.запросы) == 2


def test_пустой_ответ_называет_причину_с_числами(monkeypatch):
    """«Ollama вернул пустой ответ» без подробностей отправлял в журнал Ollama."""
    клиент, _ = _клиент(monkeypatch, вечно_думает)
    with pytest.raises(LLMError) as ошибка:
        клиент.chat("система", "разговор", use_cache=False)
    текст = str(ошибка.value)
    assert "рассужд" in текст
    assert "llm_max_tokens" in текст
    assert "второй попытки" in текст
    # Названо и число: сколько токенов ушло на рассуждение.
    assert any(ч.isdigit() for ч in текст)


def test_подсказка_не_влезла_в_окно_говорится_прямо(monkeypatch):
    клиент, _ = _клиент(monkeypatch, не_влезла)
    with pytest.raises(LLMError, match="не помещается в окно"):
        клиент.chat("система", "разговор", use_cache=False)


def test_пустой_ответ_не_ложится_в_кеш(monkeypatch):
    """Иначе «Разобрать заново» вечно возвращало бы ту же пустоту."""
    положено: list[str] = []

    class База:
        def llm_cache_get(self, ключ):
            return None

        def llm_cache_put(self, ключ, *args):
            положено.append(ключ)

    _подменить(monkeypatch, _Ollama(вечно_думает))
    клиент = LLMClient(_настройки(), db=База())
    with pytest.raises(LLMError):
        клиент.chat("система", "разговор")
    assert положено == []


# --------------------------------------------------------------------------
# Рассуждение прямо в тексте ответа
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("сырое", "ответ", "мысли_есть"), [
    ("<think>думаю</think>\n" + ОТВЕТ, ОТВЕТ, True),
    ("думаю про оплату</think>" + ОТВЕТ, ОТВЕТ, True),
    ("<think>думаю, и оборвалось на пре", "", True),
    (ОТВЕТ, ОТВЕТ, False),
])
def test_рассуждение_в_тексте_отрезается(сырое, ответ, мысли_есть):
    """Старые версии Ollama отдают рассуждение не полем, а в самом тексте."""
    текст, мысли = клиент_модуль.без_рассуждения(сырое)
    assert текст.strip() == ответ
    assert bool(мысли.strip()) is мысли_есть


def test_незакрытое_рассуждение_в_тексте_лечится_как_рассуждение(monkeypatch):
    """Рассуждение оборвалось на пределе прямо в content — это не ответ."""
    def оборвалось(тело):
        if тело.get("think") is False:
            return _ответ(ОТВЕТ)
        return _ответ("<think>думаю " * 50, done_reason="length",
                      eval_count=_предел(тело))

    клиент, ollama = _клиент(monkeypatch, оборвалось, llm_think="model")
    assert клиент.chat("система", "разговор", use_cache=False) == ОТВЕТ
    assert ollama.запросы[-1]["think"] is False


# --------------------------------------------------------------------------
# Окно контекста
# --------------------------------------------------------------------------

def test_окно_по_умолчанию_вмещает_кусок_и_ответ():
    """Двенадцать тысяч знаков — до пяти тысяч токенов; окна 4096 мало."""
    окно = клиент_модуль.окно_контекста(_настройки())
    assert окно == 8192
    нужно = (12000 + клиент_модуль.ЗАПАС_ПОДСКАЗКИ) / клиент_модуль.ЗНАКОВ_НА_ТОКЕН + 1200
    assert окно >= нужно


def test_окно_растёт_с_пределом_текста_и_рассуждением():
    малое = клиент_модуль.окно_контекста(_настройки())
    большое = клиент_модуль.окно_контекста(_настройки(llm_context_chars=60000))
    с_рассуждением = клиент_модуль.окно_контекста(_настройки(llm_think="high"))
    assert большое >= (60000 + 3000) / 2.5 + 1200
    assert с_рассуждением > малое
    # gpt-oss рассуждает всегда, и окно под это прибавлено само.
    assert клиент_модуль.окно_контекста(_настройки(), "gpt-oss:20b") > малое


def test_окно_не_меньше_восьми_тысяч_даже_при_маленьком_куске():
    """Под кусок окно считается точно, но основной вызов идёт по склейке.

    У длинного разговора основной вызов получает не кусок, а пересказы всех
    кусков подряд, и при маленьком пределе текста склейка выходит длиннее
    одного куска. Окно, подогнанное вплотную под кусок, её бы обрезало.
    """
    окно = клиент_модуль.окно_контекста(_настройки(llm_context_chars=2000))
    точно_под_кусок = (2000 + клиент_модуль.ЗАПАС_ПОДСКАЗКИ) / клиент_модуль.ЗНАКОВ_НА_ТОКЕН + 1200
    assert точно_под_кусок < 4096
    assert окно == 8192


def test_заданное_окно_главнее_подобранного():
    assert клиент_модуль.окно_контекста(_настройки(llm_num_ctx=32768)) == 32768


def test_окно_не_прыгает_от_записи_к_записи(monkeypatch):
    """Другое окно — перезагрузка весов: десятки секунд на каждом вызове."""
    клиент, ollama = _клиент(monkeypatch, рассуждающая)
    клиент.chat("система", "короткий", use_cache=False)
    клиент.chat("система", "длинный " * 3000, use_cache=False)
    окна = {з["options"]["num_ctx"] for з in ollama.запросы}
    assert окна == {8192}


def test_состояние_слоя_показывает_окно_и_рассуждение():
    клиент = LLMClient(_настройки(llm_backend="stub", llm_think="low"))
    данные = клиент.status()
    assert данные["think"] == "low"
    assert данные["max_tokens"] == 1200


# --------------------------------------------------------------------------
# OpenAI-совместимый сервер
# --------------------------------------------------------------------------

def test_совместимый_сервер_рассуждение_лечится_запасом(monkeypatch):
    """vLLM кладёт рассуждение в reasoning_content, Ollama /v1 — в reasoning."""
    запросы: list[dict[str, Any]] = []

    def сервер(клиент, method, url, body, *, timeout):
        запросы.append(body)
        if body["max_tokens"] < 3000:
            return {"choices": [{"finish_reason": "length", "message": {
                "content": "", "reasoning_content": "думаю " * 100}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": ОТВЕТ}}]}

    monkeypatch.setattr(LLMClient, "_http", сервер)
    клиент = LLMClient(_настройки(llm_backend="openai", llm_url="http://x:8000"))
    assert клиент.chat("система", "разговор", use_cache=False) == ОТВЕТ
    assert запросы[1]["max_tokens"] > запросы[0]["max_tokens"]


def test_совместимый_сервер_без_ответа_называет_причину(monkeypatch):
    monkeypatch.setattr(LLMClient, "_http", lambda *a, **k: {"choices": [{
        "finish_reason": "length", "message": {"content": "", "reasoning": "…"}}]})
    клиент = LLMClient(_настройки(llm_backend="openai", llm_url="http://x:8000"))
    with pytest.raises(LLMError, match="рассуждение"):
        клиент.chat("система", "разговор", use_cache=False)


# --------------------------------------------------------------------------
# Шлюз для чужих программ
# --------------------------------------------------------------------------

from asrhub.api import routes_llm_proxy as шлюз  # noqa: E402


class _КлиентШлюза:
    backend = "ollama"
    url = "http://127.0.0.1:11434"
    timeout = 5.0

    def __init__(self, поведение, **настройки):
        self.settings = _настройки(**настройки)
        self.ollama = _Ollama(поведение)

    def _http(self, method, url, body, *, timeout):
        return self.ollama(self, method, url, body, timeout=timeout)


def _запрос_шлюза(модель: str = "qwen3.8:27b", **сырое) -> dict[str, Any]:
    сообщения = [{"role": "user", "content": "Поздоровайся"}]
    return {"model": модель, "messages": сообщения,
            "сырое": {"model": модель, "messages": сообщения, **сырое}}


def test_шлюз_отдаёт_рассуждение_а_не_ошибку():
    """Пустой ответ с рассуждением — так отвечает и сам OpenAI, это не 502."""
    клиент = _КлиентШлюза(вечно_думает)
    текст, _usage, причина, мысли = шлюз._спросить(клиент, _запрос_шлюза())
    assert текст == ""
    assert причина == "length"
    assert мысли
    ответ = шлюз._ответ_openai(текст, _запрос_шлюза(), None, причина, рассуждение=мысли)
    сообщение = ответ["choices"][0]["message"]
    assert сообщение["reasoning_content"] == мысли
    assert ответ["choices"][0]["finish_reason"] == "length"


def test_шлюз_по_прежнему_ругается_на_полную_пустоту():
    клиент = _КлиентШлюза(lambda тело: _ответ("", eval_count=0))
    with pytest.raises(LLMError):
        шлюз._спросить(клиент, _запрос_шлюза())


@pytest.mark.parametrize(("модель", "просят", "ждём"), [
    ("qwen3.8:27b", "none", False),
    ("qwen3.8:27b", "high", True),
    ("gpt-oss:20b", "none", "low"),
    ("gpt-oss:20b", "high", "high"),
])
def test_шлюз_переводит_reasoning_effort(модель, просят, ждём):
    """Просьба OpenAI-клиента о глубине рассуждения не должна теряться."""
    тело = шлюз._тело_ollama(_запрос_шлюза(модель, reasoning_effort=просят),
                             поток=False)
    assert тело["think"] == ждём


def test_шлюз_без_просьбы_не_навязывает_рассуждение():
    """Настройка сервера — про разбор записей, а не про чужих клиентов."""
    тело = шлюз._тело_ollama(_запрос_шлюза(), поток=False)
    assert "think" not in тело


def test_шлюз_держит_то_же_окно_что_и_разбор():
    """Иначе шлюз и очередь гоняли бы модель туда-сюда с перезагрузкой."""
    клиент = _КлиентШлюза(рассуждающая)
    тело = шлюз._тело_ollama(_запрос_шлюза(), поток=False, клиент=клиент)
    assert тело["options"]["num_ctx"] == клиент_модуль.окно_контекста(
        клиент.settings, "qwen3.8:27b")


def test_шлюз_расширяет_окно_под_длинный_запрос():
    """Перезагрузка раз в долгий запрос дешевле молча обрезанной подсказки."""
    клиент = _КлиентШлюза(рассуждающая)
    запрос = _запрос_шлюза()
    запрос["messages"] = [{"role": "user", "content": "слово " * 20000}]
    тело = шлюз._тело_ollama(запрос, поток=False, клиент=клиент)
    assert тело["options"]["num_ctx"] >= 120000 / 2.5


def test_поток_шлюза_несёт_рассуждение_отдельными_кусками():
    """Минута тишины в потоке читается как умершее соединение."""
    строки = [
        json.dumps({"message": {"thinking": "Думаю."}, "done": False}).encode(),
        json.dumps({"message": {"content": "Привет"}, "done": False}).encode(),
        json.dumps({"message": {"content": ""}, "done": True,
                    "done_reason": "stop", "eval_count": 2,
                    "prompt_eval_count": 5}).encode(),
    ]
    куски = [json.loads(к[0][6:]) for к in шлюз._чанки_ollama(iter(строки), "ид", 1, "м")]
    дельты = [к["choices"][0]["delta"] for к in куски]
    assert {"reasoning_content": "Думаю."} in дельты
    assert {"content": "Привет"} in дельты


# --------------------------------------------------------------------------
# Прогрев при установке
# --------------------------------------------------------------------------

from asrhub.llm import provision  # noqa: E402


def _установщик(settings):
    объект = provision.Установщик.__new__(provision.Установщик)
    объект.settings = settings
    объект.client = None
    объект.записи = []
    объект._записать = объект.записи.append
    объект._шаг = lambda *a, **k: None
    return объект


def test_прогрев_ловит_рассуждающую_модель(monkeypatch):
    """Раньше: «Пробный ответ: » и «готово», а потом пустота на каждой записи."""
    ollama = _Ollama(рассуждающая)
    monkeypatch.setattr(provision, "_запрос",
                        lambda method, url, body=None, **k: ollama(None, method, url, body,
                                                                   timeout=0))
    установщик = _установщик(_настройки(llm_think="model"))
    установщик._прогрев("qwen3.8:27b", "http://127.0.0.1:11434")
    assert ollama.запросы[-1]["think"] is False
    assert any("Пробный ответ" in з and "оплату" in з for з in установщик.записи)


def test_прогрев_с_пустым_ответом_это_ошибка(monkeypatch):
    from asrhub.errors import ConfigError

    ollama = _Ollama(вечно_думает)
    monkeypatch.setattr(provision, "_запрос",
                        lambda method, url, body=None, **k: ollama(None, method, url, body,
                                                                   timeout=0))
    установщик = _установщик(_настройки())
    with pytest.raises(ConfigError, match="пустотой"):
        установщик._прогрев("qwen3.8:27b", "http://127.0.0.1:11434")


def test_прогрев_поднимает_модель_с_рабочим_окном(monkeypatch):
    """Прогрев с другим окном — модель, которую первый же разбор перезагрузит."""
    ollama = _Ollama(рассуждающая)
    monkeypatch.setattr(provision, "_запрос",
                        lambda method, url, body=None, **k: ollama(None, method, url, body,
                                                                   timeout=0))
    установщик = _установщик(_настройки())
    установщик._прогрев("qwen3.8:27b", "http://127.0.0.1:11434")
    assert ollama.запросы[0]["options"]["num_ctx"] == клиент_модуль.окно_контекста(
        установщик.settings, "qwen3.8:27b")


# --- Документация называет то, что человек видит ------------------------------

КОРЕНЬ = Path(__file__).resolve().parents[1]


def _сжать(текст: str) -> str:
    """Переносы строк и отступы — не часть подписи."""
    return re.sub(r"\s+", " ", текст)


def _что_видит_человек() -> str:
    """Все подписи, которые человек может увидеть: веб-интерфейс и вывод
    установщика с обновлением (у него свои разделы — «Что дальше»)."""
    файлы = [КОРЕНЬ / "server/asrhub/web/app.js", КОРЕНЬ / "server/asrhub/web/index.html"]
    файлы += sorted((КОРЕНЬ / "scripts").glob("*.sh"))
    файлы += sorted((КОРЕНЬ / "scripts").glob("*.ps1"))
    файлы += sorted((КОРЕНЬ / "scripts/lib").glob("*"))
    return _сжать("\n".join(ф.read_text(encoding="utf-8") for ф in файлы if ф.is_file()))


def test_кнопки_и_разделы_из_документации_есть_в_интерфейсе():
    """«Нажмите X в разделе Y» — только если X и Y так и подписаны.

    Глава захода 38 и справка по голосовой аналитике отправляли человека к
    кнопке «Повторить неудачные» в раздел «Очередь к модели», а в
    интерфейсе это «Повторить упавшие» в «Очереди LLM» — человек, которому
    только что сломалась очередь, искал бы кнопку, которой нет. Та же
    проверка нашла ещё две старые подписи: «Сохранить в файл» в описании
    настроек и раздел «Медленные задания» в аналитике.

    Кавычки-«лапки» внутри ёлочек в документации — типографика вложенной
    цитаты, в интерфейсе на их месте ёлочки: это одна и та же подпись.
    """
    интерфейс = _что_видит_человек()
    нет = []
    for путь in sorted((КОРЕНЬ / "docs").glob("*.md")):
        текст = _сжать(путь.read_text(encoding="utf-8"))
        for найдено in re.finditer(r"(?:кнопк\w*|в раздел[еу])\s+«([^»]{2,60})»", текст):
            подпись = найдено.group(1).replace("„", "«").replace("“", "»")
            if подпись not in интерфейс:
                нет.append(f"{путь.name}: «{подпись}»")
    assert not нет, "В интерфейсе нет таких подписей: " + "; ".join(нет)
