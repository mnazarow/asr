"""Двадцать девятый заход: сплошная проверка всего кода.

Проверки на дефекты, найденные сплошным чтением сервера — границ, базы,
очереди, конвейера, трендов и телефонии. Каждая написана так, чтобы падать
на прежнем поведении: ключ, обходящий маскирование заголовком без слова
«Bearer»; звонок, потерянный навсегда; результат соседа, снесённый
отставшим экземпляром; счётчики очереди проверки без разреза по владельцу.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from asrhub.db import Database


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def запись(db: Database, job_id: str, *, owner: str = "анна", text: str = "здравствуйте",
           **поля) -> str:
    db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": owner,
                   "model": "demo", "engine": "demo", "media_duration_s": 60.0, **поля})
    db.update_job(job_id, status="completed", finished_at=time.time(), text=text)
    return job_id


# ---------------------------------------------------------------------------
# Границы: кто есть кто
# ---------------------------------------------------------------------------

def test_ключ_без_слова_bearer_не_обходит_маскирование(data_dir, monkeypatch):
    """Проверка ключа и защита обязаны считать ключом одно и то же.

    `authenticate` принимал «Authorization: <ключ>» без схемы, а прослойка
    маскирования — только «Bearer <ключ>». Ключ, заведённый именно затем,
    чтобы не видеть персональных данных, получал полные телефоны и карты:
    достаточно убрать из заголовка одно слово.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_masked_k": {"name": "аналитик", "role": "user", "enabled": True,
                        "mask_pii": True},
    })
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as c:
        db = app.state.hub.db
        текст = "Мой телефон +7 916 123-45-67, почта ivan@example.com."
        запись(db, "pii29", owner="аналитик", text=текст)

        заголовки = {
            "X-API-Key": {"X-API-Key": "ah_masked_k"},
            "Bearer": {"Authorization": "Bearer ah_masked_k"},
            "без схемы": {"Authorization": "ah_masked_k"},
        }
        for как, шапка in заголовки.items():
            ответ = c.get("/api/jobs/pii29", headers=шапка)
            assert ответ.status_code == 200, (как, ответ.text)
            тело = ответ.json()
            assert "916 123-45-67" not in тело["text"], f"через «{как}» утекли данные"
            assert "[телефон]" in тело["text"], как
        # И в строке запроса тоже.
        тело = c.get("/api/jobs/pii29?api_key=ah_masked_k").json()
        assert "[телефон]" in тело["text"]


def test_разбор_ключа_один_на_всех():
    """Шесть мест разбирали заголовок сами, и пятое разошлось с остальными."""
    from asrhub.api.deps import token_from

    assert token_from("ah_1", None, None) == "ah_1"
    assert token_from(None, "Bearer ah_2", None) == "ah_2"
    assert token_from(None, "bearer ah_3", None) == "ah_3"
    assert token_from(None, "ah_4", None) == "ah_4", "голый заголовок — тоже ключ"
    assert token_from(None, None, "ah_5") == "ah_5"
    assert token_from("  ah_6  ", None, None) == "ah_6"
    assert token_from(None, None, None) == ""
    # Ключ из заголовка важнее ключа из строки запроса.
    assert token_from("ah_a", "Bearer ah_b", "ah_c") == "ah_a"


def test_вход_ограничен_по_частоте_до_подсчёта_хеша(data_dir, monkeypatch):
    """Единственный маршрут без ключа — и единственный, где считается scrypt.

    Без предела шестьдесят запросов с одного адреса поднимали потребление
    памяти с 89 МБ до 1,1 ГБ, а соседний /api/health отвечал четыре секунды
    вместо десятой доли. Блокировка учётной записи от этого не спасает:
    она привязана к существующему логину, а наплыв идёт по выдуманным.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.set("login_rate_limit", 3)
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as c:
        коды = [c.post("/api/auth/login",
                       json={"username": f"нет-{i}", "password": "пароль"}).status_code
                for i in range(8)]
        assert коды[:3] == [401, 401, 401], коды
        assert 429 in коды, f"предел частоты не сработал: {коды}"
        assert коды.count(429) >= 5, коды


def test_кука_сессии_живёт_столько_же_сколько_сессия(data_dir, monkeypatch):
    """Max-Age — длительность, а не метка времени.

    Сюда уходила абсолютная метка (1,79 млрд), и браузер получал куку со
    сроком годности 2083 год. Серверная сессия истекала честно, а токен
    оставался на диске десятилетиями и уходил при каждом запросе.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.set("session_ttl_hours", 168)
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as c:
        ответ = c.post("/api/auth/login",
                       json={"username": "admin", "password": "admin123"})
        assert ответ.status_code == 200, ответ.text
        кука = ответ.headers.get("set-cookie", "")
        assert "Max-Age=" in кука, кука
        срок = int(кука.split("Max-Age=")[1].split(";")[0])
        assert 160 * 3600 <= срок <= 168 * 3600 + 60, f"Max-Age={срок} с — это {срок/86400:.0f} суток"


def test_счётчик_неудачных_попыток_складывается_а_не_затирается(tmp_path: Path):
    """Read-modify-write со scrypt посередине — окно в сотню миллисекунд.

    Каждый параллельный поток читал «ноль», прибавлял единицу и записывал
    единицу: проверка «пять попыток» пропускала двести паролей за
    шестнадцать секунд, и усиление росло с числом потоков без предела.
    """
    import threading

    from asrhub.accounts import MAX_FAILED_ATTEMPTS, Accounts
    from asrhub.errors import AuthError

    db = база(tmp_path)
    acc = Accounts(db)
    acc.create(username="жертва", password="правильный-пароль-1", role="user")

    проверено = []
    замок = threading.Lock()

    def перебор() -> None:
        for _ in range(4):
            try:
                acc.authenticate("жертва", "неверный")
            except AuthError as exc:
                if "заблокирован" in str(exc):
                    continue
            with замок:
                проверено.append(1)

    потоки = [threading.Thread(target=перебор) for _ in range(8)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=60)

    строка = db.query_one("SELECT failed_attempts, locked_until FROM users "
                          "WHERE username='жертва'")
    assert int(строка["failed_attempts"]) >= MAX_FAILED_ATTEMPTS, \
        "счётчик затёрся: попытки не сложились"
    assert float(строка["locked_until"]) > time.time(), "блокировка не встала"
    # Без атомарного счёта сюда проходили все 32 попытки.
    assert len(проверено) < 32, f"проверено паролей: {len(проверено)} из 32"


# ---------------------------------------------------------------------------
# Телефония: звонок не должен теряться
# ---------------------------------------------------------------------------

def test_позиция_журнала_не_уезжает_на_чужой_кодировке(tmp_path: Path):
    """Имя звонящего из SIP-заголовка бывает в CP1251 — это не повод терять звонки.

    Файл читался текстом с `errors="replace"`, а позиция считалась как
    `len(строка.encode("utf-8"))`: каждый байт, не сложившийся в UTF-8,
    превращался в U+FFFD и кодировался обратно тремя байтами вместо
    одного. Позиция уезжала вперёд, следующий звонок пропадал целиком, и
    дрейф копился строка за строкой.
    """
    from asrhub.telephony.asterisk import читать_csv

    журнал = tmp_path / "Master.csv"
    н = time.strftime("%Y-%m-%d %H:%M:%S")

    def строка(uid: str, имя: bytes) -> bytes:
        поле = b'"' + имя + b'"'
        return (b'"","79161234567","101","from-trunk",' + поле +
                b',"SIP/t","SIP/101","Dial","x","' + н.encode() + b'","' +
                н.encode() + b'","' + н.encode() + b'",60,55,"ANSWERED","DOC","' +
                uid.encode() + b'",""\n')

    плохая = строка("2.1", "Иванов".encode("cp1251"))
    журнал.write_bytes(строка("1.1", b"Petrov") + плохая
                       + строка("3.1", b"Smith") + строка("4.1", b"Jones"))

    звонки, позиция = читать_csv(журнал, offset=0, limit=2)
    assert [з.uniqueid for з in звонки] == ["1.1", "2.1"]
    assert позиция == len(строка("1.1", b"Petrov")) + len(плохая), \
        "позиция считается по настоящим байтам, а не по знакам"
    ещё, конец = читать_csv(журнал, offset=позиция, limit=10)
    assert [з.uniqueid for з in ещё] == ["3.1", "4.1"], "звонок потерян дрейфом"
    assert конец == журнал.stat().st_size


def test_номер_в_имени_файла_ищется_отдельным_числом(tmp_path: Path):
    """Подстрока без границ подбирает чужую запись.

    В имени `out-79161234567-79995554433-20260910-101020.wav` находятся и
    «101», и «102» — оба внутри времени «101020». Звонок 101 → 102, у
    которого своей записи нет, получал чужой внешний разговор: тот уходил
    на распознавание и доставался владельцу, которому сопоставлен 101.
    """
    from asrhub.telephony.asterisk import Звонок, найти_запись

    каталог = tmp_path / "monitor"
    каталог.mkdir()
    (каталог / "out-79161234567-79995554433-20260910-101020.wav").write_bytes(b"RIFF")
    звонок = Звонок(uniqueid="1.1", src="101", dst="102", started_at=time.time())
    assert найти_запись(звонок, каталог, окно_дней=7) is None, "подобралась чужая запись"

    (каталог / "out-101-102-20260910-121314.wav").write_bytes(b"RIFF")
    найдено = найти_запись(звонок, каталог, окно_дней=7)
    assert найдено is not None and найдено.name == "out-101-102-20260910-121314.wav"


def test_ноль_в_настройках_телефонии_означает_ноль(tmp_path: Path):
    """`float(значение or 30)` врёт ровно на нуле, а каталог его разрешает."""
    from asrhub.telephony.importer import _число

    assert _число({"x": 0}, "x", 30) == 0.0, "ноль — это значение, а не «не задано»"
    assert _число({"x": 0.0}, "x", 30) == 0.0
    assert _число({}, "x", 30) == 30.0
    assert _число({"x": None}, "x", 30) == 30.0
    assert _число({"x": ""}, "x", 30) == 30.0
    assert _число({"x": "мусор"}, "x", 30) == 30.0
    assert _число({"x": 7}, "x", 30) == 7.0


def test_соединение_с_атс_переоткрывается_при_смене_пароля(tmp_path: Path):
    """Правка учётных данных должна доходить до станции без перезапуска."""
    from asrhub.db import Database
    from asrhub.telephony import Импортёр

    class Клиент:
        живые = []

        def __init__(self, host, port, username, secret, **_):
            self.секрет = secret
            self.закрыт = False
            Клиент.живые.append(self)

        def connect(self):
            return "ok"

        def close(self):
            self.закрыт = True

        def пакет(self, timeout=None):
            return {}

    import asrhub.telephony.importer as M
    настоящий, M.AMIClient = M.AMIClient, Клиент
    try:
        настройки = {"telephony_source": "ami", "telephony_host": "127.0.0.1",
                     "telephony_port": 5038, "telephony_username": "asrhub",
                     "telephony_secret": "старый"}
        имп = Импортёр(Database(tmp_path / "asrhub.db"), настройки, None)
        имп._из_ami(5)
        assert len(Клиент.живые) == 1 and Клиент.живые[0].секрет == "старый"
        имп._из_ami(5)
        assert len(Клиент.живые) == 1, "соединение держится между заходами"

        настройки["telephony_secret"] = "новый"
        имп._из_ami(5)
        assert len(Клиент.живые) == 2, "смена пароля не дошла до станции"
        assert Клиент.живые[0].закрыт, "старое соединение не закрыто"
        assert Клиент.живые[1].секрет == "новый"
    finally:
        M.AMIClient = настоящий


# ---------------------------------------------------------------------------
# Данные, которые считаются удалёнными
# ---------------------------------------------------------------------------

def test_разбор_удалённой_записи_не_дописывается_обратно(tmp_path: Path):
    """Фоновый разбор считается секунды, смысловой — минуты.

    За это время запись могут удалить: часовой уборкой по сроку, кнопкой в
    интерфейсе или удалением по требованию субъекта. Дописанные после
    удаления пересказ и разбор не убирал потом никто — и уборка, и удаление
    ходят по таблице заданий. То есть данные, которые считаются удалёнными,
    оставались в базе навсегда.
    """
    db = база(tmp_path)
    запись(db, "j1", text="иванов просил вернуть деньги за заказ")
    db.save_content("j1", {"version": 1, "computed_at": time.time(), "sentiment": -0.4},
                    terms=[{"stem": "деньг", "n": 2}])
    db.llm_save("j1", 1, summary="Иванов просил вернуть деньги", reason="возврат")
    assert db.llm_get("j1") is not None

    db.delete_job("j1")
    # Гонка: разбор, начатый до удаления, дописывает уже удалённую запись.
    db.save_content("j1", {"version": 1, "computed_at": time.time(), "sentiment": -0.4},
                    terms=[{"stem": "деньг", "n": 2}])
    db.llm_save("j1", 1, summary="Иванов просил вернуть деньги", reason="возврат")

    for таблица in ("content", "content_terms", "llm_results"):
        осталось = db.query_one(
            f"SELECT COUNT(*) n FROM {таблица} WHERE job_id='j1'")["n"]
        assert осталось == 0, f"{таблица}: разбор удалённой записи дописан"
    assert db.llm_get("j1") is None


def test_уборка_подметает_разбор_без_задания(tmp_path: Path):
    """На базе, пережившей прежние версии, такие строки уже лежат."""
    db = база(tmp_path)
    db.execute("INSERT INTO llm_results (job_id, version, created_at, summary) "
               "VALUES ('старьё', 1, 0, 'чужой пересказ')")
    db.execute("INSERT INTO segments (job_id, idx, start_s, end_s, text) "
               "VALUES ('старьё', 0, 0, 1, 'чужая реплика')")
    убрано = db.cleanup(results_days=30)
    assert убрано["orphans"] >= 2
    assert db.query_one("SELECT COUNT(*) n FROM llm_results")["n"] == 0
    assert db.query_one("SELECT COUNT(*) n FROM segments")["n"] == 0


def test_метка_согласия_находится_с_пробелами_и_метасимволами(tmp_path: Path):
    """Колонку меток заполняют трое, и все по-разному.

    Интерфейс кладёт набранное человеком, телефония склеивает свои метки
    через запятую, аналитика читает через split+strip. Побайтовое
    сравнение считало «срочно, согласие» записью БЕЗ согласия — и она
    попадала в список на уничтожение по 152-ФЗ.
    """
    db = база(tmp_path)
    метки = ["согласие", "согласие,срочно", "срочно, согласие", "согласие, срочно",
             "АТС, входящий, согласие", "", "несогласие"]
    for i, м in enumerate(метки):
        запись(db, f"t{i}", tags=м)
        db.execute("UPDATE jobs SET created_at=? WHERE id=?",
                   (time.time() - 100000, f"t{i}"))
    без = db.jobs_without_tag("согласие", older_than=time.time())
    assert без == ["t5", "t6"], f"в список на уничтожение попали помеченные: {без}"
    # Метасимвол LIKE в самой метке не должен означать «подходит всё».
    assert len(db.jobs_without_tag("%", older_than=time.time())) == len(метки)


def test_ноль_дней_согласия_означает_ноль():
    """`int(значение or 30)` врёт ровно на нуле, а каталог объявляет минимум 0."""
    from asrhub.maintenance import _срок

    assert _срок({"consent_days": 0}, "consent_days", 30) == 0
    assert _срок({"consent_days": 7}, "consent_days", 30) == 7
    assert _срок({}, "consent_days", 30) == 30
    assert _срок({"consent_days": ""}, "consent_days", 30) == 30
    assert _срок({"consent_days": "мусор"}, "consent_days", 30) == 30


# ---------------------------------------------------------------------------
# Разрез по владельцу
# ---------------------------------------------------------------------------

def test_счётчики_очереди_проверки_по_своим(tmp_path: Path):
    """В одном ответе были список из трёх своих записей и счётчик «ожидают 10»."""
    db = база(tmp_path)
    for i in range(10):
        запись(db, f"r{i}", owner="анна" if i == 0 else "борис")
        db.review_add(f"r{i}", reason="random")
    assert db.review_counts()["pending"] == 10
    assert db.review_counts(owner="анна")["pending"] == 1
    assert db.review_counts(owner="борис")["pending"] == 9


def test_покрытие_разбора_считается_по_своим(tmp_path: Path):
    """Отдел с одной разобранной записью видел «ожидают разбора 41»."""
    db = база(tmp_path)
    for i in range(10):
        запись(db, f"c{i}", owner="анна" if i == 0 else "борис")
    assert db.content_stats(1)["total"] == 10
    assert db.content_stats(1, owner="анна")["total"] == 1
    assert db.content_stats(1, owner="борис")["total"] == 9


def test_предел_поиска_не_съедают_чужие_совпадения(tmp_path: Path):
    """Отдел с одной старой записью не находил её среди свежих чужих.

    Предел в четыреста совпадений накладывался ДО разреза по владельцу и
    до листалки: поиск отвечал «ничего», при том что слово в разговоре
    было. Заодно это чинит листалку администратора — предел применялся
    раньше LIMIT/OFFSET, и девятая страница была последней даже там, где
    совпадений шестьсот.
    """
    db = база(tmp_path)
    сейчас = time.time()
    запись(db, "своя", owner="анна", text="договор о поставке")
    db.save_segments("своя", [{"start": 0, "end": 1, "text": "договор о поставке"}])
    db.execute("UPDATE jobs SET created_at=? WHERE id=?", (сейчас - 100000, "своя"))
    for i in range(db.SEARCH_LIMIT + 50):
        j = f"чужая{i}"
        запись(db, j, owner="борис", text="договор об оказании услуг")
        db.save_segments(j, [{"start": 0, "end": 1, "text": "договор об оказании услуг"}])
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (сейчас - i, j))
    if not db.fts_ready:
        pytest.skip("сборка SQLite без FTS5")
    найдено = db.list_jobs(search="договор", owner="анна", limit=50)
    assert [з["id"] for з in найдено] == ["своя"]
    assert db.count_jobs(search="договор", owner="анна") == 1
    assert db.count_jobs(search="договор", owner="борис") == db.SEARCH_LIMIT
    assert db.last_search_truncated, "обрезку надо признавать, а не скрывать"


# ---------------------------------------------------------------------------
# Очередь, конвейер, поток
# ---------------------------------------------------------------------------

def test_смена_числа_воркеров_не_плодит_потоки(data_dir):
    """«4 → 1 → 4» — две правки подряд или отказ от только что сделанной.

    Пометка на выход снималась до ветвления, а помеченный воркер уходит не
    сразу: индексы 1–3 оставались заняты живыми потоками, и для них
    заводились вторые, да ещё и затирался WorkerState. Три лишних потока ОС
    на каждую правку, и два потока на одно состояние.
    """
    import threading

    from asrhub.config import load
    from asrhub.engines import EngineRegistry
    from asrhub.job_queue import JobQueue

    настройки = load()
    настройки.set("max_concurrent_jobs", 4)
    очередь = JobQueue(Database(Path(настройки.paths.data) / "q.db"),
                       настройки, EngineRegistry())
    очередь.start()
    try:
        time.sleep(0.3)
        живых = lambda: sum(1 for t in threading.enumerate()          # noqa: E731
                            if t.name.startswith("asrhub-worker"))
        assert живых() == 4
        for _ in range(3):
            очередь.set_concurrency(1)
            очередь.set_concurrency(4)
            time.sleep(0.3)
        assert живых() == 4, f"утекли потоки: {живых()}"
        assert len(очередь._states) == 4
    finally:
        очередь.stop()


def test_готовый_результат_соседа_не_сносится(tmp_path: Path):
    """Проверка закрывала только «сосед ещё считает».

    Завершённое задание пишет instance_id=NULL и статус completed, поэтому
    условие оказывалось ложным и rmtree сносил каталог соседа: в базе
    задание оставалось completed с result_path, указывающим в пустоту, и
    все скачивания отвечали 404 навсегда.
    """
    from asrhub.config import load
    from asrhub.engines import EngineRegistry
    from asrhub.job_queue import JobQueue

    настройки = load()
    db = Database(tmp_path / "asrhub.db")
    очередь = JobQueue(db, настройки, EngineRegistry())
    каталог = tmp_path / "results" / "J"
    каталог.mkdir(parents=True)
    for имя in ("сосед.json", "сосед.srt", "сосед.txt"):
        (каталог / имя).write_text("готовая работа", encoding="utf-8")
    запись(db, "J")
    db.update_job("J", status="completed", instance_id=None,
                  result_path=str(каталог))

    очередь._discard_unless_taken(каталог, "J")
    assert каталог.is_dir(), "каталог готового задания снесён"
    assert len(list(каталог.iterdir())) == 3

    # А вот каталог незавершённого задания убрать можно и нужно.
    db.update_job("J", status="failed")
    очередь._discard_unless_taken(каталог, "J")
    assert not каталог.is_dir()


def test_выгрузка_json_не_портит_сам_результат():
    """`dict(result)` — мелкая копия: словари реплик остаются общими.

    `seg.pop("confidence")` вычищал их у оригинала, а оригинал — это
    `outcome.segments`, который сразу после выгрузки уходит в базу. То есть
    настройка «не включать уверенность в JSON» стирала уверенность и
    пословные тайминги из базы для всей записи.
    """
    from asrhub.pipeline import export

    результат = {"text": "а б", "segments": [
        {"start": 0.0, "end": 2.0, "text": "первый", "confidence": 0.91,
         "words": [{"word": "первый", "start": 0.0, "end": 1.0, "confidence": 0.9}]},
        {"start": 2.0, "end": 4.0, "text": "второй", "confidence": 0.44, "words": []},
    ]}
    тело = export.to_json(результат, {"include_confidence": False,
                                      "word_timestamps": False})
    assert результат["segments"][0]["confidence"] == 0.91, "оригинал испорчен"
    assert результат["segments"][0]["words"], "пословные тайминги стёрты из оригинала"
    assert "confidence" not in тело and '"words"' not in тело


def test_склейка_реплик_не_теряет_слова():
    """Условие «и там, и там» выбрасывало слова, когда один список пуст.

    А это обычное дело: движки часто не дают слов для реплики в одно слово.
    Оба параметра стоят по умолчанию.
    """
    from asrhub.pipeline.postprocess import merge_segments

    из = merge_segments([
        {"start": 0.0, "end": 0.4, "text": "да", "speaker": "S1", "words": []},
        {"start": 0.5, "end": 2.0, "text": "конечно поможем", "speaker": "S1",
         "words": [{"word": "конечно", "start": 0.5, "end": 1.2},
                   {"word": "поможем", "start": 1.2, "end": 2.0}]},
    ], min_duration=1.0, max_gap=0.5, max_duration=30.0)
    assert из[0]["text"] == "да конечно поможем"
    assert [с["word"] for с in из[0]["words"]] == ["конечно", "поможем"]


def test_слово_с_дефисом_не_сдвигает_тайминги_остатка():
    """MFA делит «из-за» надвое, а раздача шла по номеру.

    Каждое слово с дефисом сдвигало пословные границы всего остатка реплики
    на одно слово и дублировало последнее. В русском разговоре это «из-за»,
    «кто-то», «что-то», «какой-то», «по-моему» — на сорокаминутном звонке
    сотни раз.
    """
    from asrhub.pipeline.alignment import _слова_сегмента

    слова = _слова_сегмента(
        ["из-за", "дождя", "отменили"],
        [{"word": "из", "start": 0.10, "end": 0.30},
         {"word": "за", "start": 0.30, "end": 0.55},
         {"word": "дождя", "start": 0.60, "end": 1.10},
         {"word": "отменили", "start": 1.20, "end": 1.90}],
        keep_text=True)
    assert [(с["word"], с["start"], с["end"]) for с in слова] == [
        ("из-за", 0.1, 0.55), ("дождя", 0.6, 1.1), ("отменили", 1.2, 1.9)]


def test_сбой_распознавания_не_съедает_звук():
    """Тридцать секунд речи исчезали, и клиенту не уходило ни final, ни error.

    `_recognize` глотал любое исключение и отвечал пустой строкой, а хвост
    удалялся в любом случае. Человек видел текст в partial, а в итоговой
    расшифровке его не оказывалось; сессия заканчивалась штатным done.
    """
    from asrhub.errors import EngineError
    from asrhub.streaming import SAMPLE_RATE, StreamSession

    сессия = StreamSession.__new__(StreamSession)
    сессия._pcm = bytearray(b"\x01\x02" * int(SAMPLE_RATE * 20))
    сессия._committed_s = 0.0
    сессия._last_partial = ""
    сессия._final_text = ""
    сессия._first_text_at = None
    сессия._started = 0.0
    сессия._recognize = lambda pcm: (_ for _ in ()).throw(EngineError("нет видеопамяти"))

    было = len(сессия._pcm)
    события = сессия._commit()
    assert len(сессия._pcm) == было, "звук выброшен вместе с несостоявшимся текстом"
    assert сессия._committed_s == 0.0
    assert [е.type for е in события] == ["error"], "клиенту не сказали о сбое"


def test_итоговое_событие_потока_несёт_только_хвост():
    """Клиент складывает события final подряд — и показывал текст дважды."""
    from asrhub.streaming import SAMPLE_RATE, StreamSession

    сессия = StreamSession.__new__(StreamSession)
    сессия._final_text = "алло добрый день чем могу помочь"
    сессия._committed_s = 30.0
    сессия._first_text_at = 1.0
    сессия._started = 0.0
    сессия._native_bytes = int(SAMPLE_RATE * 2 * 34)
    сессия._pcm = bytearray()
    сессия._native = type("Н", (), {"finish": lambda s: "до свидания"})()

    события = сессия._finish_native()
    assert [е.text for е in события] == ["до свидания"]
    assert сессия._final_text.endswith("до свидания")


# ---------------------------------------------------------------------------
# Что уходит наружу
# ---------------------------------------------------------------------------

def test_путь_на_диске_не_уезжает_в_текст_ошибки():
    """`error_message` уходит в карточку, в список и на адрес, заданный клиентом.

    То есть раскладку хранилища — каталог загрузок, каталог моделей, схему
    именования — видел любой владелец ключа, а при желании и произвольный
    внешний адрес: загрузить битый файл и указать webhook_url. Соседний
    /api/system прячет ту же раскладку за правами администратора.
    """
    from asrhub.errors import classify_exception, без_путей

    assert без_путей("Файл не найден: /srv/asrhub/data/uploads/f7.wav") \
        == "Файл не найден: f7.wav"
    assert без_путей("ошибка в C:\\asrhub\\data\\битый.wav.") == "ошибка в битый.wav."
    assert без_путей("Не хватило памяти") == "Не хватило памяти"

    ошибка = classify_exception(
        FileNotFoundError(2, "нет", "/srv/asrhub/models/gigaam/v3/weights.ckpt"))
    assert "weights.ckpt" in ошибка.message
    assert "/srv/asrhub" not in ошибка.message


def test_заголовок_скачивания_без_переводов_строки():
    """Имя файла приходит снаружи, а \\r\\n в значении заголовка ломает ответ.

    На uvicorn это «Invalid HTTP header value»: пустой ответ, оборванное
    соединение и необработанная 500 при каждой попытке скачать задание. На
    сервере попроще — расщепление ответа.
    """
    from asrhub.api.routes_jobs import content_disposition

    for имя in ("зло\r\nX-Evil: 1.wav", 'с"кавычкой.wav', "", "обычный файл.wav"):
        заголовок = content_disposition(имя)
        assert "\r" not in заголовок and "\n" not in заголовок, имя
        assert заголовок.count('"') == 2, f"кавычка закрыла заголовок раньше: {имя}"


def test_метка_метрики_не_дописывает_чужие_строки():
    """Значение метки приходит из настроек задания, а `model` — enum без списка.

    Ключ с ролью user одним заданием мог дописать в InfluxDB подложную
    точку («свободного места ноль»), сломать имя в Graphite и развалить
    разбор csv кавычкой.
    """
    from asrhub.monitoring.collector import Sample
    from asrhub.monitoring.exporters import csv_table, graphite, influx_line

    злая = 'ok\nasrhub_disk_free_bytes,host=fake value=0 1'
    образцы = [Sample(name="asrhub_jobs_by_model", value=1.0,
                      labels={"engine": "demo", "model": злая})]
    строки = [с for с in influx_line(образцы).splitlines() if с.strip()]
    assert len(строки) == 1, f"в Influx уехало {len(строки)} строк вместо одной"
    строки = [с for с in graphite(образцы).splitlines() if с.strip()]
    assert len(строки) == 1 and строки[0].count(" ") == 2, строки

    таблица = csv_table([Sample(name="asrhub_jobs_by_model", value=1.0,
                                labels={"model": 'злой" ,модель'})])
    import csv as _csv
    строки = list(_csv.reader(таблица.splitlines()))
    assert len(строки) == 2 and len(строки[1]) == 5, строки


@pytest.mark.parametrize("маршрут", [
    "/api/logs?limit=-1",
    "/api/monitoring/alerts/history?limit=-1",
    "/api/models/recommended?limit=-1",
])
def test_предел_не_принимает_отрицательное(data_dir, маршрут):
    """`LIMIT -1` в SQLite снимает предел вовсе."""
    from asrhub.api import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(start_queue=False)) as c:
        assert c.get(маршрут).status_code == 422, маршрут


def test_пути_на_диске_не_уходят_в_карточке_задания(data_dir, monkeypatch):
    """/api/system прячет раскладку от неадминистратора — карточка отдавала её."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_adm": {"name": "админ", "role": "admin", "enabled": True},
        "ah_usr": {"name": "аня", "role": "user", "enabled": True},
    })
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as c:
        запись(app.state.hub.db, "p1", owner="аня")
        app.state.hub.db.update_job("p1", file_path="/srv/asrhub/data/uploads/x.wav",
                                    result_path="/srv/asrhub/data/results/p1")
        свой = c.get("/api/jobs/p1", headers={"X-API-Key": "ah_usr"}).json()
        assert "file_path" not in свой and "result_path" not in свой
        админ = c.get("/api/jobs/p1", headers={"X-API-Key": "ah_adm"}).json()
        assert админ["file_path"].endswith("x.wav"), "администратору пути нужны"


def test_средняя_длительность_делит_своё_на_своё(tmp_path: Path):
    """Числитель по всем заданиям, знаменатель по завершённым.

    Десять заданий в корзине, из них пять завершены — и средняя
    длительность выходила вдвое больше настоящей, а «быстрее реального
    времени» вдесятеро. Справка при этом честно обещала «считается по
    завершённым». Перекос тем сильнее, чем больше незавершённых и упавших.
    """
    from asrhub.trends import Trends

    db = база(tmp_path)
    когда = time.time() - 3600
    for i in range(10):
        db.create_job({"id": f"d{i}", "filename": "f.wav", "owner": "анна",
                       "model": "m", "engine": "e", "media_duration_s": 100.0})
        if i < 5:
            db.update_job(f"d{i}", status="completed", processing_time_s=10.0, text="т")
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, f"d{i}"))

    свод = Trends(db).series("day", bucket="hour",
                             metrics=["avg_duration", "speedup"],
                             compare=False, is_admin=True)
    значения = {}
    for ряд in свод["series"]:
        есть = [v for v in ряд["values"] if v is not None]
        значения[ряд["id"]] = есть[-1] if есть else None
    assert значения["avg_duration"] == 100.0, значения
    assert значения["speedup"] == 10.0, значения
