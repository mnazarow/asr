"""Учётные записи: вход по логину и паролю, сессии веб-интерфейса.

Зачем это отдельно от ключей доступа. Ключ — для программ: он один, живёт
в файле конфигурации, его удобно подставлять в curl и в phone_asr. Человеку
он неудобен: чтобы открыть интерфейс, надо было зайти на сервер и прочитать
api-key.txt. Учётная запись решает ровно эту задачу и ничего больше: люди
входят логином и паролем, программы продолжают ходить с ключом.

Что здесь важно с точки зрения безопасности:

* Пароль не хранится. Хранится результат scrypt с индивидуальной солью —
  функция намеренно медленная и требовательная к памяти, поэтому перебор по
  украденной базе стоит дорого. Сравнение — hmac.compare_digest, чтобы по
  времени ответа нельзя было угадывать хеш посимвольно.
* Сессия в базе хранится не токеном, а его sha256. Утёкшая база не даёт
  войти: из хеша токен не восстановить.
* Счётчик неудачных попыток и блокировка лежат в базе, а не в памяти
  процесса: экземпляров сервера может быть несколько, и перебор по очереди
  через каждый из них обошёл бы счётчик в памяти.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database
from .errors import ASRHubError, AuthError
from .logging_setup import get_logger

log = get_logger("accounts")

#: Учётная запись, которая создаётся при первом запуске.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"

#: Параметры scrypt. n=2^15 при r=8 требует около 32 МБ на проверку — это
#: заметно для перебора и незаметно для входа раз в несколько часов.
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32

#: Сколько неудачных попыток подряд до блокировки и на сколько.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_S = 900.0

#: Требования к паролю. Намеренно скромные: длина работает лучше правил про
#: спецсимволы, а невыполнимые требования люди обходят стикером на мониторе.
MIN_PASSWORD_LEN = 8

_USERNAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9._@-]{2,64}$")

ROLES = ("admin", "user", "readonly")


class AccountError(ASRHubError):
    """Ошибка работы с учётной записью."""

    code = "account_error"
    http_status = 400
    hint = "Проверьте имя пользователя и пароль."


class AccountNotFound(ASRHubError):
    """Учётная запись не найдена."""

    code = "account_not_found"
    http_status = 404
    hint = "Список учётных записей: GET /api/users"


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Возвращает строку вида scrypt$n$r$p$соль$хеш (base64 без выравнивания)."""
    if not isinstance(password, str) or not password:
        raise AccountError("Пароль не может быть пустым.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                            r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN,
                            maxmem=64 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Проверяет пароль. Ошибочный формат хеша — это «не подошёл», а не отказ.

    Иначе испорченная строка в базе превращалась бы в пятисотую ошибку и в
    невозможность войти вообще, вместо обычного «неверный пароль» с рабочим
    восстановлением через консоль.
    """
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n), r=int(r), p=int(p), dklen=len(expected),
                                maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str:
    """Пустая строка — пароль годится, иначе объяснение на русском."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Пароль короче {MIN_PASSWORD_LEN} знаков."
    if password.strip() != password:
        return "Пароль начинается или заканчивается пробелом — это почти всегда опечатка."
    return ""


def username_problem(username: str) -> str:
    if not _USERNAME_RE.match(username or ""):
        return ("Имя пользователя: от двух до шестидесяти четырёх знаков, "
                "буквы, цифры, точка, дефис, подчёркивание или @.")
    return ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# Учётные записи
# ---------------------------------------------------------------------------


@dataclass
class Account:
    id: str
    username: str
    display_name: str = ""
    role: str = "user"
    group: str = ""
    enabled: bool = True
    must_change_password: bool = False
    created_at: float = 0.0
    last_login: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "group": self.group,
            "enabled": self.enabled,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


def _account(row: Any) -> Account:
    return Account(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"] or ""),
        role=str(row["role"] or "user"),
        group=str(row["user_group"] or ""),
        enabled=bool(row["enabled"]),
        must_change_password=bool(row["must_change"]),
        created_at=float(row["created_at"] or 0),
        last_login=float(row["last_login"] or 0),
    )


class Accounts:
    """Учётные записи и сессии поверх базы."""

    def __init__(self, db: Database, session_ttl_hours: float = 168.0) -> None:
        self.db = db
        self.session_ttl_s = max(1.0, float(session_ttl_hours)) * 3600.0

    # --- записи ---------------------------------------------------------

    def list(self) -> list[Account]:
        rows = self.db.query("SELECT * FROM users ORDER BY username")
        return [_account(row) for row in rows]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def get(self, user_id: str) -> Account | None:
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", [user_id])
        return _account(row) if row else None

    def by_username(self, username: str) -> Account | None:
        # Имя нечувствительно к регистру: «Admin» и «admin» — один человек,
        # и заводить двоих с такими именами нельзя.
        row = self.db.query_one(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", [username])
        return _account(row) if row else None

    def create(self, username: str, password: str, *, role: str = "user",
               display_name: str = "", group: str = "",
               must_change_password: bool = False) -> Account:
        problem = username_problem(username) or password_problem(password)
        if problem:
            raise AccountError(problem)
        if role not in ROLES:
            raise AccountError(f"Неизвестная роль «{role}». Допустимые: {', '.join(ROLES)}.")
        if self.by_username(username):
            raise AccountError(f"Пользователь «{username}» уже есть.")
        now = time.time()
        user_id = uuid.uuid4().hex[:16]
        self.db.execute(
            "INSERT INTO users (id, username, password_hash, display_name, role, "
            "user_group, enabled, must_change, created_at, updated_at, last_login, "
            "failed_attempts, locked_until) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 0, 0)",
            [user_id, username, hash_password(password), display_name, role, group,
             1 if must_change_password else 0, now, now])
        log.info("Создана учётная запись «%s» с ролью %s", username, role)
        account = self.get(user_id)
        assert account is not None
        return account

    def update(self, user_id: str, **fields: Any) -> Account:
        allowed = {"display_name", "role", "user_group", "enabled", "must_change"}
        if "group" in fields:
            fields["user_group"] = fields.pop("group")
        if "must_change_password" in fields:
            fields["must_change"] = fields.pop("must_change_password")
        updates = {k: v for k, v in fields.items() if k in allowed}
        if "role" in updates and updates["role"] not in ROLES:
            raise AccountError(f"Неизвестная роль «{updates['role']}».")
        account = self.get(user_id)
        if account is None:
            raise AccountNotFound(f"Учётная запись {user_id} не найдена.")
        if not updates:
            return account
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        if "must_change" in updates:
            updates["must_change"] = 1 if updates["must_change"] else 0
        updates["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in updates)
        self.db.execute(f"UPDATE users SET {assignments} WHERE id = ?",
                        [*updates.values(), user_id])
        # Отключённая запись не должна доживать в открытых вкладках.
        if updates.get("enabled") == 0:
            self.drop_sessions(user_id)
        result = self.get(user_id)
        assert result is not None
        return result

    def set_password(self, user_id: str, password: str, *,
                     must_change: bool = False, keep_sessions: str = "") -> None:
        """Меняет пароль и закрывает все сессии, кроме указанной.

        Смена пароля обязана выкидывать чужие сессии: иначе увод пароля
        остаётся действительным ровно до истечения чужой куки, и сменить его
        «на всякий случай» бесполезно.
        """
        problem = password_problem(password)
        if problem:
            raise AccountError(problem)
        if self.get(user_id) is None:
            raise AccountNotFound(f"Учётная запись {user_id} не найдена.")
        self.db.execute(
            "UPDATE users SET password_hash = ?, must_change = ?, updated_at = ?, "
            "failed_attempts = 0, locked_until = 0 WHERE id = ?",
            [hash_password(password), 1 if must_change else 0, time.time(), user_id])
        self.drop_sessions(user_id, keep=keep_sessions)

    def delete(self, user_id: str) -> None:
        if self.get(user_id) is None:
            raise AccountNotFound(f"Учётная запись {user_id} не найдена.")
        self.drop_sessions(user_id)
        self.db.execute("DELETE FROM users WHERE id = ?", [user_id])

    def admin_count(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND enabled = 1")
        return int(row["n"]) if row else 0

    # --- вход -----------------------------------------------------------

    def authenticate(self, username: str, password: str) -> Account:
        """Проверяет пару логин/пароль. Любая неудача — одно и то же сообщение.

        Разные ответы на «нет такого пользователя» и «неверный пароль»
        позволяют собирать список существующих логинов простым перебором.
        """
        row = self.db.query_one(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", [username])
        now = time.time()
        if row is None:
            # Считаем хеш и для несуществующего пользователя: иначе ответ на
            # неизвестный логин приходит заметно быстрее, и это тоже способ
            # выяснить, какие логины существуют.
            hash_password(password)
            raise AuthError("Неверный логин или пароль.",
                            hint="Проверьте раскладку и регистр.")
        locked_until = float(row["locked_until"] or 0)
        if locked_until > now:
            raise AuthError(
                "Вход временно заблокирован после неудачных попыток.",
                hint=f"Повторите через {int((locked_until - now) / 60) + 1} мин.")
        if not bool(row["enabled"]):
            raise AuthError("Учётная запись отключена.",
                            hint="Обратитесь к администратору сервера.")
        if not verify_password(password, str(row["password_hash"])):
            attempts = int(row["failed_attempts"] or 0) + 1
            locked = now + LOCKOUT_S if attempts >= MAX_FAILED_ATTEMPTS else 0.0
            self.db.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                [attempts, locked, row["id"]])
            if locked:
                log.warning("Учётная запись «%s» заблокирована на %d мин: %d неудачных попыток",
                            row["username"], int(LOCKOUT_S / 60), attempts)
            raise AuthError("Неверный логин или пароль.",
                            hint="Проверьте раскладку и регистр.")
        self.db.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = 0, last_login = ? "
            "WHERE id = ?", [now, row["id"]])
        return _account(row)

    # --- сессии ---------------------------------------------------------

    def open_session(self, user_id: str, *, user_agent: str = "",
                     address: str = "") -> tuple[str, float]:
        """Заводит сессию и возвращает токен вместе со сроком годности."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + self.session_ttl_s
        self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, "
            "last_seen, user_agent, address) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [_token_hash(token), user_id, now, expires, now, user_agent[:200],
             address[:64]])
        self._prune()
        return token, expires

    def session_account(self, token: str) -> Account | None:
        """Возвращает владельца сессии, продлевая её срок."""
        if not token:
            return None
        row = self.db.query_one(
            "SELECT s.user_id AS user_id, s.expires_at AS expires_at, "
            "s.last_seen AS last_seen FROM sessions s WHERE s.token_hash = ?",
            [_token_hash(token)])
        if row is None:
            return None
        now = time.time()
        if float(row["expires_at"] or 0) <= now:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?",
                            [_token_hash(token)])
            return None
        account = self.get(str(row["user_id"]))
        if account is None or not account.enabled:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?",
                            [_token_hash(token)])
            return None
        # Срок продлеваем, но не чаще раза в пять минут: иначе каждый опрос
        # состояния очереди — а он идёт секундами — писал бы в базу.
        if now - float(row["last_seen"] or 0) > 300:
            self.db.execute(
                "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                [now, now + self.session_ttl_s, _token_hash(token)])
        return account

    def close_session(self, token: str) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?",
                            [_token_hash(token)])

    def drop_sessions(self, user_id: str, *, keep: str = "") -> None:
        if keep:
            self.db.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                [user_id, _token_hash(keep)])
        else:
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", [user_id])

    def _prune(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", [time.time()])

    # --- первый запуск --------------------------------------------------

    def ensure_default_admin(self) -> Account | None:
        """Заводит admin/admin123, если учётных записей нет ни одной.

        Возвращает созданную запись или None, если записи уже были. Пароль
        помечен как обязательный к смене: интерфейс не даст работать, пока
        его не поменяют, а в журнале это предупреждение.
        """
        if self.count() > 0:
            return None
        account = self.create(DEFAULT_USERNAME, DEFAULT_PASSWORD, role="admin",
                              display_name="Администратор",
                              must_change_password=True)
        log.warning("Создана учётная запись по умолчанию: %s / %s",
                    DEFAULT_USERNAME, DEFAULT_PASSWORD)
        log.warning("Смените пароль при первом входе — до этого сервер доступен всем, "
                    "кто знает эту пару.")
        return account

    def uses_default_password(self) -> bool:
        """Стоит ли где-то ещё пароль по умолчанию — для предупреждения в интерфейсе."""
        row = self.db.query_one(
            "SELECT password_hash FROM users WHERE username = ? COLLATE NOCASE",
            [DEFAULT_USERNAME])
        if row is None:
            return False
        return verify_password(DEFAULT_PASSWORD, str(row["password_hash"]))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
