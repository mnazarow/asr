"""Вход по логину и паролю: учётные записи, сессии, восстановление доступа.

Проверяется не «форма нарисовалась», а свойства, ради которых всё делалось:
пароль нельзя достать из базы, перебор упирается в блокировку, пароль по
умолчанию не даёт работать до смены, ключи доступа продолжают работать
рядом, и потерянный пароль восстанавливается из консоли.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from asrhub.accounts import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    LOCKOUT_S,
    MAX_FAILED_ATTEMPTS,
    Accounts,
    hash_password,
    password_problem,
    verify_password,
)
from asrhub.db import Database
from asrhub.errors import ASRHubError


@pytest.fixture()
def accounts(tmp_path: Path) -> Accounts:
    return Accounts(Database(tmp_path / "asrhub.db"))


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------


def test_password_is_not_stored_and_each_hash_is_unique():
    """В базе лежит не пароль и даже не его прямой отпечаток.

    Соль у каждой записи своя, поэтому одинаковые пароли дают разные строки:
    по украденной базе нельзя ни прочитать пароль, ни увидеть, что у двоих
    он одинаковый, ни посчитать радужную таблицу один раз на всех.
    """
    first = hash_password("одинаковый-пароль")
    second = hash_password("одинаковый-пароль")
    assert first != second
    assert "одинаковый-пароль" not in first
    assert first.startswith("scrypt$")
    assert verify_password("одинаковый-пароль", first)
    assert verify_password("одинаковый-пароль", second)
    assert not verify_password("другой-пароль", first)


def test_broken_hash_means_wrong_password_not_a_crash():
    """Испорченная строка в базе — это «не подошёл», а не пятисотая ошибка.

    Иначе одна битая запись превращалась бы в невозможность войти вообще, и
    восстановление через консоль тоже не работало бы.
    """
    for broken in ("", "мусор", "scrypt$нет$полей", "bcrypt$1$2$3$4$5",
                   "scrypt$16384$8$1$неверная-соль$хеш"):
        assert verify_password("любой", broken) is False


def test_short_password_is_refused(accounts: Accounts):
    assert password_problem("семьзнак") == ""          # ровно восемь
    assert "короче" in password_problem("семь")
    with pytest.raises(ASRHubError):
        accounts.create("человек", "коротк")


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


def test_default_admin_appears_once_and_demands_a_change(accounts: Accounts):
    """При первом запуске заводится admin/admin123 с обязательной сменой."""
    created = accounts.ensure_default_admin()
    assert created is not None
    assert created.username == DEFAULT_USERNAME
    assert created.role == "admin"
    assert created.must_change_password
    assert accounts.uses_default_password()

    # Повторный запуск ничего не создаёт и, главное, не возвращает пароль по
    # умолчанию тому, кто его уже сменил.
    accounts.set_password(created.id, "выбранный-пароль")
    assert accounts.ensure_default_admin() is None
    assert not accounts.uses_default_password()
    assert accounts.count() == 1


def test_unknown_login_and_wrong_password_answer_the_same(accounts: Accounts):
    """Разные ответы позволяли бы собрать список логинов перебором."""
    accounts.create("существующий", "верный-пароль-1")
    messages = []
    for username, password in (("существующий", "неверный"), ("выдуманный", "неверный")):
        with pytest.raises(ASRHubError) as info:
            accounts.authenticate(username, password)
        messages.append(info.value.message)
    assert messages[0] == messages[1]


def test_bruteforce_runs_into_a_lockout(accounts: Accounts):
    """После нескольких неудач вход закрывается, и верный пароль тоже.

    Иначе пароль из восьми знаков перебирается по сети за разумное время, а
    счётчик в памяти процесса обходится через соседний экземпляр сервера —
    поэтому он в базе.
    """
    account = accounts.create("жертва", "настоящий-пароль")
    for _ in range(MAX_FAILED_ATTEMPTS):
        with pytest.raises(ASRHubError):
            accounts.authenticate("жертва", "подбор")

    with pytest.raises(ASRHubError) as info:
        accounts.authenticate("жертва", "настоящий-пароль")
    assert "заблокирован" in info.value.message

    # Блокировка временная и снимается сама.
    accounts.db.execute("UPDATE users SET locked_until = ? WHERE id = ?",
                        [time.time() - 1, account.id])
    assert accounts.authenticate("жертва", "настоящий-пароль").username == "жертва"
    # Успешный вход обнуляет счётчик: иначе пять опечаток за месяц работы
    # закрывали бы вход навсегда.
    row = accounts.db.query_one("SELECT failed_attempts FROM users WHERE id = ?",
                                [account.id])
    assert int(row["failed_attempts"]) == 0
    assert LOCKOUT_S > 0


def test_disabled_account_cannot_log_in_and_loses_its_sessions(accounts: Accounts):
    account = accounts.create("уволенный", "пароль-уволенного")
    token, _ = accounts.open_session(account.id)
    assert accounts.session_account(token) is not None

    accounts.update(account.id, enabled=False)
    assert accounts.session_account(token) is None, "открытая вкладка продолжала работать"
    with pytest.raises(ASRHubError):
        accounts.authenticate("уволенный", "пароль-уволенного")


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------


def test_session_token_is_not_stored_as_is(accounts: Accounts):
    """В базе лежит sha256 токена: утёкшая база не даёт войти."""
    account = accounts.create("человек", "пароль-человека")
    token, _ = accounts.open_session(account.id)
    rows = accounts.db.query("SELECT token_hash FROM sessions")
    assert len(rows) == 1
    assert token not in str(rows[0]["token_hash"])
    assert len(str(rows[0]["token_hash"])) == 64


def test_expired_session_stops_working(accounts: Accounts):
    account = accounts.create("человек", "пароль-человека")
    token, _ = accounts.open_session(account.id)
    accounts.db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?",
                        [time.time() - 1, account.id])
    assert accounts.session_account(token) is None
    # Просроченная запись не копится в базе.
    assert accounts.db.query("SELECT * FROM sessions") == []


def test_changing_the_password_closes_other_sessions(accounts: Accounts):
    """Смена пароля обязана выкидывать чужие сессии.

    Иначе увод пароля действует до истечения чужой куки, и сменить пароль
    «на всякий случай» бесполезно — а именно это человек и делает первым.
    """
    account = accounts.create("человек", "старый-пароль-1")
    mine, _ = accounts.open_session(account.id)
    stolen, _ = accounts.open_session(account.id)

    accounts.set_password(account.id, "новый-пароль-1", keep_sessions=mine)
    assert accounts.session_account(mine) is not None, "выкинуло того, кто менял"
    assert accounts.session_account(stolen) is None, "чужая сессия пережила смену пароля"


# ---------------------------------------------------------------------------
# Через сервер
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    with TestClient(create_app(start_queue=False)) as client:
        yield client


def _login(client, username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_default_password_blocks_work_until_changed(app_client):
    """С паролем по умолчанию сервер не даёт работать, а не просто ворчит.

    Пароль admin123 известен всем, у кого есть эта программа. Предупреждение
    в углу такой сервер не защищает: работать до смены нельзя.
    """
    assert app_client.get("/api/jobs").status_code == 401

    response = _login(app_client)
    assert response.status_code == 200
    assert response.json()["must_change_password"] is True

    blocked = app_client.get("/api/jobs")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"

    # Сменить пароль при этом можно — иначе выхода из положения не было бы.
    assert app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD,
        "new_password": "рабочий-пароль-2026"}).status_code == 200
    assert app_client.get("/api/jobs").status_code == 200
    assert app_client.get("/api/auth/me").json()["must_change_password"] is False


def test_the_default_password_cannot_be_set_again(app_client):
    _login(app_client)
    response = app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD})
    assert response.status_code == 400


def test_api_key_keeps_working_next_to_accounts(app_client, data_dir: Path):
    """Учётные записи не должны ломать то, что уже настроено.

    Ключами ходят phone_asr, asrctl и чужие интеграции: если бы вход по
    паролю подменял их, обновление сервера остановило бы работу.
    """
    key = (data_dir / "api-key.txt").read_text(encoding="utf-8").strip()
    assert app_client.get("/api/jobs", headers={"X-API-Key": key}).status_code == 200
    who = app_client.get("/api/auth/me", headers={"X-API-Key": key}).json()
    assert who["kind"] == "key"

    # И даже когда в браузере открыта сессия, запрос с ключом работает от
    # ключа: программа не должна наследовать чужие права из куки.
    _login(app_client)
    app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD, "new_password": "пароль-для-ключа"})
    assert app_client.get("/api/auth/me",
                          headers={"X-API-Key": key}).json()["kind"] == "key"


def test_logout_closes_the_session(app_client):
    _login(app_client)
    app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD, "new_password": "пароль-для-выхода"})
    assert app_client.get("/api/jobs").status_code == 200
    assert app_client.post("/api/auth/logout").status_code == 200
    assert app_client.get("/api/jobs").status_code == 401


def test_post_from_another_page_is_refused(app_client):
    """Кука уходит на сервер сама — чужая страница не должна ею пользоваться.

    SameSite=Lax закрывает это в браузере, здесь второй рубеж: заголовок
    Origin с чужого адреса на изменяющем запросе.
    """
    _login(app_client)
    app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD, "new_password": "пароль-происхождения"})

    alien = app_client.post("/api/maintenance/unload-models",
                            headers={"Origin": "https://chuzhoy.example"})
    assert alien.status_code == 403
    assert "чужой страницы" in alien.json()["message"]

    own = app_client.post("/api/maintenance/unload-models",
                          headers={"Origin": "http://testserver"})
    assert own.status_code == 200


def test_last_administrator_cannot_lock_everyone_out(app_client):
    """Разжаловать или отключить последнего администратора нельзя.

    Иначе сервером станет некому управлять, и возвращать доступ придётся
    из консоли — а человек, сделавший это, обычно не знает, что так можно.
    """
    _login(app_client)
    app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD, "new_password": "пароль-админа-1"})
    users = app_client.get("/api/users").json()["users"]
    admin = [u for u in users if u["username"] == DEFAULT_USERNAME][0]

    assert app_client.patch(f"/api/users/{admin['id']}",
                            json={"role": "user"}).status_code == 403
    assert app_client.patch(f"/api/users/{admin['id']}",
                            json={"enabled": False}).status_code == 403
    assert app_client.delete(f"/api/users/{admin['id']}").status_code == 403

    # Со вторым администратором — можно.
    app_client.post("/api/users", json={"username": "второй", "password": "пароль-второго",
                                        "role": "admin"})
    assert app_client.patch(f"/api/users/{admin['id']}",
                            json={"role": "user"}).status_code == 200


def test_ordinary_user_cannot_manage_accounts(app_client):
    _login(app_client)
    app_client.post("/api/auth/password", json={
        "current_password": DEFAULT_PASSWORD, "new_password": "пароль-админа-2"})
    app_client.post("/api/users", json={"username": "простой", "password": "пароль-простого",
                                        "role": "user", "must_change_password": False})
    app_client.post("/api/auth/logout")

    _login(app_client, "простой", "пароль-простого")
    assert app_client.get("/api/users").status_code == 403
    assert app_client.post("/api/users", json={"username": "самозванец",
                                               "password": "пароль-самозванца"}).status_code == 403


# ---------------------------------------------------------------------------
# Восстановление доступа
# ---------------------------------------------------------------------------


def test_password_can_be_reset_from_the_console(tmp_path: Path, repo_root: Path):
    """Забытый пароль единственного администратора — не тупик.

    Ключ доступа управлять учётными записями не позволяет, поэтому без
    консольной команды человек оставался бы снаружи навсегда.
    """
    data = tmp_path / "данные"
    data.mkdir()
    accounts = Accounts(Database(data / "asrhub.db"))
    accounts.ensure_default_admin()

    env = {"ASRHUB_DATA_DIR": str(data), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(repo_root / "server")}
    listing = subprocess.run([sys.executable, "-m", "asrhub", "--list-users"],
                             capture_output=True, text=True, env=env, timeout=120)
    assert listing.returncode == 0, listing.stderr
    assert DEFAULT_USERNAME in listing.stdout

    reset = subprocess.run([sys.executable, "-m", "asrhub", "--set-password",
                            DEFAULT_USERNAME],
                           input="восстановленный-пароль\nвосстановленный-пароль\n",
                           capture_output=True, text=True, env=env, timeout=120)
    assert reset.returncode == 0, reset.stderr + reset.stdout
    assert "изменён" in reset.stdout

    fresh = Accounts(Database(data / "asrhub.db"))
    assert fresh.authenticate(DEFAULT_USERNAME, "восстановленный-пароль")
    with pytest.raises(ASRHubError):
        fresh.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD)

    # Пароль не должен уходить в аргументы: они видны в списке процессов.
    source = (repo_root / "server" / "asrhub" / "__main__.py").read_text(encoding="utf-8")
    assert "getpass" in source
