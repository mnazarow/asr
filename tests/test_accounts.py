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


# ---------------------------------------------------------------------------
# Токен Hugging Face в настройках
# ---------------------------------------------------------------------------


def _admin(client):
    """Входит администратором и снимает обязательную смену пароля."""
    _login(client)
    client.post("/api/auth/password", json={"current_password": DEFAULT_PASSWORD,
                                            "new_password": "пароль-настроек-2026"})


def test_hf_token_is_set_from_the_interface_and_never_shown_back(app_client, data_dir: Path):
    """Токен задаётся из интерфейса, но целиком наружу не отдаётся.

    До этого его можно было задать только при установке или строкой в
    config.yaml — то есть человеку с доступом к серверу. При этом показывать
    его в ответе нельзя: он уйдёт в кеш браузера и будет виден через плечо.
    """
    _admin(app_client)
    assert app_client.get("/api/settings/hf-token").json()["configured"] is False

    token = "hf_" + "a" * 30
    saved = app_client.put("/api/settings/hf-token", json={"token": token})
    assert saved.status_code == 200
    body = saved.json()
    assert body["configured"] is True
    assert body["preview"] == "hf_aaa…"
    assert body["length"] == len(token)
    assert token not in saved.text, "токен вернулся целиком"

    state = app_client.get("/api/settings/hf-token")
    assert token not in state.text
    assert state.json()["preview"] == "hf_aaa…"

    # И в общих настройках его тоже нет: их читает любой ключ, включая readonly.
    assert token not in app_client.get("/api/settings").text

    # Записан в конфигурацию, а не только в память процесса.
    config = data_dir / "config.yaml"
    assert config.exists()
    assert token in config.read_text(encoding="utf-8")
    assert oct(config.stat().st_mode)[-3:] in ("600", "640"), "файл с секретом читают все"


def test_hf_token_checks_the_shape_and_can_be_removed(app_client):
    """Мусор вместо токена отклоняется, пустая строка — это «убрать».

    Опечатка в токене иначе обнаружилась бы через полчаса на загрузке весов,
    и виноватой выглядела бы модель.
    """
    _admin(app_client)
    bad = app_client.put("/api/settings/hf-token", json={"token": "просто-текст"})
    assert bad.status_code == 400
    # Поля отказа доступны верхним уровнем — и одинаково у всех маршрутов.
    assert "hf_" in bad.json()["message"]
    assert bad.json()["code"] == "config_error"

    app_client.put("/api/settings/hf-token", json={"token": "hf_" + "b" * 30})
    assert app_client.get("/api/settings/hf-token").json()["configured"] is True

    cleared = app_client.put("/api/settings/hf-token", json={"token": ""})
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False


def test_hf_token_is_admin_only(app_client):
    """Токен открывает доступ к чужим весам — задавать его может админ."""
    _admin(app_client)
    app_client.post("/api/users", json={"username": "оператор", "password": "пароль-оператора",
                                        "role": "user", "must_change_password": False})
    app_client.post("/api/auth/logout")
    _login(app_client, "оператор", "пароль-оператора")

    assert app_client.get("/api/settings/hf-token").status_code == 403
    assert app_client.put("/api/settings/hf-token",
                          json={"token": "hf_" + "c" * 30}).status_code == 403


def test_access_section_exists_in_the_settings_view(repo_root: Path):
    """Раздел «Доступ» в настройках — там, где его ищут.

    Ключ доступа и токен лежали в разделе «Сервер», куда за настройками не
    ходят. Панель кнопок параметров в этом разделе скрыта: «Сбросить» там
    сбросил бы совсем не то, о чём думает человек.
    """
    app = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    assert "const ACCESS_GROUP = '_access';" in app
    assert "renderAccessSection" in app
    assert "'/api/settings/hf-token'" in app
    assert "state.paramGroup === ACCESS_GROUP ? 'hidden' : ''" in app, \
        "панель параметров показана там, где параметров нет"
    # Токен не должен запрашиваться у того, кто не администратор: иначе в
    # интерфейсе появится отказ там, где просто нечего показывать.
    assert "if (!isAdmin) {" in app


def test_error_body_has_one_shape_everywhere(app_client):
    """Отказ читается одинаково, откуда бы он ни пришёл.

    Ошибка, поднятая через error_response, уходила вложенной в `detail`, а
    та же ошибка из глубины — полями верхнего уровня. Клиент был обязан
    разбирать оба вида, и нигде об этом не говорилось. Теперь поля есть
    верхним уровнем всегда, а `detail` сохранён для тех, кто уже разбирает
    ответ так.
    """
    _admin(app_client)
    cases = [
        app_client.get("/api/jobs/такого-нет"),                       # error_response
        app_client.put("/api/settings/hf-token", json={"token": "мусор"}),
        app_client.post("/api/auth/login", json={"username": "нет", "password": "нет"}),
    ]
    for response in cases:
        body = response.json()
        assert response.status_code >= 400
        assert body.get("code"), f"нет поля code: {body}"
        assert body.get("message"), f"нет поля message: {body}"
        assert "retryable" in body, f"нет признака повторяемости: {body}"
        if "detail" in body:
            assert body["detail"]["code"] == body["code"], "вложенная копия разошлась"


def test_the_key_field_lives_in_one_place(repo_root: Path):
    """Поле для ключа было в двух разделах и показывало разное.

    В «Справке» стояло своё поле с тем же значением из localStorage: сохранив
    ключ в одном месте, человек видел в другом прежний — до перезагрузки
    страницы. Теперь ключ задаётся только в «Настройки → Доступ», а «Справка»
    туда ведёт.
    """
    app = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    assert "help-key-save" not in app and "help-key-clear" not in app, \
        "в справке осталось второе поле для ключа"
    assert "help-to-access" in app, "из справки нет пути к разделу «Доступ»"
    # Ключ кладётся в браузер ровно в двух местах, и оба обязательны: форма
    # «войти по ключу» на экране входа — туда попадают, ещё не войдя, — и
    # раздел «Доступ». Третье место означало бы, что поля снова разошлись.
    assert app.count("localStorage.setItem('asrhub_key'") == 2
