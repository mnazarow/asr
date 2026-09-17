"""Вход через каталог предприятия: LDAP и Active Directory.

Зачем это нужно. Учётные записи на сервере уже есть, и работают они хорошо
ровно до того разговора, где выясняется, что при увольнении человека его надо
отключить в пятнадцати системах, и речевая аналитика — шестнадцатая. Дальше
разговор идёт не про качество распознавания. Вход через каталог снимает
вопрос целиком: отключили в каталоге — доступа нет, и никто ничего не забыл.

Как это устроено здесь:

* **Пароль не хранится и не проверяется нами.** Сервер пробует
  присоединиться к каталогу под логином и паролем человека; удалось — значит
  пара верна. Это и есть весь способ проверки, и он намеренно не оставляет у
  нас ни хеша, ни самого пароля.
* **Роль приходит из групп.** Сопоставление «группа каталога → роль сервера»
  задаётся настройкой. Человек из группы администраторов получает роль
  администратора при каждом входе, и снятие его из группы отнимает права на
  следующем входе — без похода в раздел учётных записей.
* **Местная учётная запись всё равно заводится.** Ей принадлежат владелец
  заданий, подразделение и всё остальное, что ссылается на пользователя;
  пароля у неё нет. Иначе первый же разрыв связи с каталогом означал бы, что
  у половины архива пропал владелец.
* **Местные записи проверяются первыми.** Администратор, заведённый при
  установке, обязан входить и тогда, когда каталог недоступен, — иначе
  сервер запирается вместе с ним.

Пакет `ldap3` — необязательная зависимость: без него настройка просто
недоступна, и сервер говорит об этом прямо, а не отказывает во входе молча.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import ASRHubError, AuthError, ConfigError
from .logging_setup import get_logger

log = get_logger("ldap")

#: Отбор пользователя по умолчанию: имя учётной записи в Active Directory или
#: uid в обычном LDAP. Так один и тот же отбор работает и там и там.
ОТБОР_ПО_УМОЛЧАНИЮ = "(|(sAMAccountName={username})(uid={username}))"

#: Атрибуты, которые забираются у найденной записи. Больше не нужно: всё, что
#: сервер умеет хранить о человеке, — это имя и группы.
АТРИБУТЫ = ("cn", "displayName", "mail", "memberOf", "sAMAccountName", "uid")

#: Нулевой байт в логине не бывает ни у кого и обрывает строку на полпути в
#: половине библиотек. Остальные особые знаки LDAP — `(`, `)`, `*`, `\` —
#: ЗДЕСЬ НЕ ПЕРЕЧИСЛЕНЫ НАМЕРЕННО, и это осознанная правка.
#:
#: Сначала они отвергались вместе с нулевым байтом: логин со скобкой выглядит
#: как попытка подмены отбора. Но, во-первых, скобки в имени учётной записи
#: Active Directory разрешает — «o(brien» отвергался бы ни за что; во-вторых и
#: главных, отвергать их значило иметь две защиты от одного и того же, из
#: которых работает только первая. Экранирование по RFC 4515 при таком отборе
#: не получало ни одного опасного знака НИКОГДА — то есть было мёртвым кодом,
#: и проверка на него проходила бы даже после того, как его убрали.
#: Правильная защита здесь одна — экранирование, и теперь она единственная.
ОПАСНЫЕ = re.compile(r"[\x00]")


class LDAPError(ASRHubError):
    """Каталог недоступен или отвечает не так, как ожидалось."""

    code = "ldap_error"
    http_status = 502


@dataclass(frozen=True)
class Настройки:
    """То, чем задаётся связь с каталогом."""

    enabled: bool = False
    url: str = ""
    base_dn: str = ""
    bind_template: str = ""
    user_filter: str = ОТБОР_ПО_УМОЛЧАНИЮ
    group_map: dict[str, str] = field(default_factory=dict)
    default_role: str = ""
    start_tls: bool = False
    timeout_s: float = 5.0

    @classmethod
    def из_настроек(cls, settings: Any) -> Настройки:
        if not settings:
            return cls()
        карта = settings.get("auth_ldap_group_map") or {}
        if isinstance(карта, str):
            # «группа = роль» построчно — так настройку пишут руками чаще,
            # чем словарём JSON.
            карта = dict(_пары(карта))
        if isinstance(карта, list):
            карта = dict(_пары("\n".join(str(с) for с in карта)))
        return cls(
            enabled=bool(settings.get("auth_ldap_enabled", False)),
            url=str(settings.get("auth_ldap_url") or "").strip(),
            base_dn=str(settings.get("auth_ldap_base_dn") or "").strip(),
            bind_template=str(settings.get("auth_ldap_bind_template") or "").strip(),
            user_filter=(str(settings.get("auth_ldap_user_filter") or "").strip()
                         or ОТБОР_ПО_УМОЛЧАНИЮ),
            group_map={str(к).strip().lower(): str(з).strip()
                       for к, з in dict(карта).items() if str(к).strip()},
            default_role=str(settings.get("auth_ldap_default_role") or "").strip(),
            start_tls=bool(settings.get("auth_ldap_start_tls", False)),
            timeout_s=float(settings.get("auth_ldap_timeout_s") or 5.0))

    def проблема(self) -> str:
        """Пустая строка — настройка годится, иначе объяснение по-русски."""
        if not self.enabled:
            return ""
        if not self.url:
            return "Не задан адрес каталога (auth_ldap_url)."
        if not self.bind_template:
            return ("Не задан шаблон имени для присоединения "
                    "(auth_ldap_bind_template), например «{username}@example.ru».")
        if "{username}" not in self.bind_template:
            return ("В шаблоне имени нет места для логина: добавьте "
                    "«{username}», иначе все войдут под одним и тем же именем.")
        return ""


@dataclass(frozen=True)
class Человек:
    """Что каталог рассказал о вошедшем."""

    username: str
    display_name: str = ""
    groups: tuple[str, ...] = ()
    role: str = ""


def _пары(текст: str) -> list[tuple[str, str]]:
    """Разбирает «группа = роль» построчно, пропуская пустое и комментарии."""
    пары: list[tuple[str, str]] = []
    for строка in str(текст or "").splitlines():
        чистая = строка.split("#", 1)[0].strip()
        if not чистая:
            continue
        for разделитель in ("=", ":", "→"):
            if разделитель in чистая:
                левое, правое = чистая.split(разделитель, 1)
                пары.append((левое.strip(), правое.strip()))
                break
    return пары


def экранировать(логин: str) -> str:
    """Готовит логин к подстановке в отбор LDAP (RFC 4515).

    Без этого логин «*)(objectClass=*» превращает отбор «найди этого
    пользователя» в «найди любого» — и дальше сервер берёт первого попавшегося
    и считает, что вошёл именно он.
    """
    # Обратная косая заменяется первой: иначе она попала бы ещё раз в
    # заменах следующих знаков и удвоилась бы.
    замены = (("\\", "\\5c"), ("*", "\\2a"), ("(", "\\28"),
              (")", "\\29"), ("\x00", "\\00"))
    итог = str(логин or "")
    for знак, замена in замены:
        итог = итог.replace(знак, замена)
    return итог


def роль_по_группам(группы: tuple[str, ...], настройки: Настройки) -> str:
    """Роль сервера по группам каталога; пусто — вход не разрешён.

    Когда человек состоит сразу в нескольких сопоставленных группах, берётся
    самая сильная роль: иначе порядок, в котором каталог перечислил группы,
    решал бы, администратор человек или нет.
    """
    сила = {"readonly": 1, "user": 2, "admin": 3}
    лучшая = ""
    for группа in группы:
        имя = str(группа or "").strip().lower()
        роль = настройки.group_map.get(имя)
        if роль is None:
            # Группа приходит различающимся именем целиком
            # («CN=asr-admins,OU=Groups,DC=…»), а в настройке её пишут просто
            # «asr-admins». Сверяем ещё и по первой части.
            первая = имя.split(",", 1)[0]
            if первая.startswith("cn="):
                роль = настройки.group_map.get(первая[3:])
        if роль and сила.get(роль, 0) > сила.get(лучшая, 0):
            лучшая = роль
    return лучшая or настройки.default_role


def _ldap3():
    try:
        import ldap3  # type: ignore
    except ImportError as exc:                              # pragma: no cover
        raise ConfigError(
            "Вход через каталог включён, но пакет ldap3 не установлен.",
            hint="Поставьте его в окружение сервера: "
                 "/opt/asrhub/venv/bin/pip install ldap3 — или выключите "
                 "настройку «Вход через каталог».") from exc
    return ldap3


def соединение(настройки: Настройки, имя: str, пароль: str):
    """Присоединение к каталогу под указанным именем. Отдельно — ради подмены.

    Проверки ходят через ту же дверь, что и рабочий код: подменяется эта
    функция, а не логика входа. Иначе проверялась бы заглушка, а не то, что
    работает на сервере.
    """
    ldap3 = _ldap3()
    сервер = ldap3.Server(настройки.url, get_info=ldap3.NONE,
                          connect_timeout=max(1.0, настройки.timeout_s))
    связь = ldap3.Connection(сервер, user=имя, password=пароль,
                             auto_bind=False, raise_exceptions=False,
                             receive_timeout=max(1.0, настройки.timeout_s))
    if настройки.start_tls:
        связь.start_tls()
    if not связь.bind():
        return None
    return связь


def войти(настройки: Настройки, username: str, password: str) -> Человек | None:
    """Проверяет пару в каталоге и возвращает, что о человеке известно.

    None — каталог не принял пару. Исключение — каталог недоступен или
    настроен неверно: это разные случаи, и второй нельзя выдавать за
    «неверный пароль», иначе упавший контроллер домена выглядит как
    забывчивость всех сотрудников сразу.
    """
    if not настройки.enabled:
        return None
    беда = настройки.проблема()
    if беда:
        raise ConfigError(беда, hint="Раздел «Настройки» → «Доступ».")
    if not username or not password:
        return None
    if ОПАСНЫЕ.search(username):
        log.warning("Вход через каталог: в логине «%s» нулевой байт",
                    username[:64].replace("\x00", "?"))
        return None

    имя_для_связи = настройки.bind_template.replace("{username}", username)
    try:
        связь = соединение(настройки, имя_для_связи, password)
    except AuthError:
        raise
    except ASRHubError:
        raise
    except Exception as exc:                                # noqa: BLE001
        raise LDAPError(
            f"Каталог {настройки.url} недоступен: {exc}",
            hint="Проверьте адрес, сеть и сертификат. Пока каталог молчит, "
                 "входить можно только местными учётными записями.") from exc
    if связь is None:
        return None

    группы: tuple[str, ...] = ()
    показать = ""
    try:
        if настройки.base_dn:
            отбор = настройки.user_filter.replace("{username}", экранировать(username))
            связь.search(настройки.base_dn, отбор, attributes=list(АТРИБУТЫ))
            записи = list(getattr(связь, "entries", ()) or ())
            if записи:
                первая = записи[0]
                группы = tuple(str(з) for з in _значения(первая, "memberOf"))
                показать = (_одно(первая, "displayName") or _одно(первая, "cn") or "")
    except Exception as exc:                                # noqa: BLE001
        # Присоединиться удалось — значит пара верна. Не сумели прочитать
        # группы: это повод для роли по умолчанию и записи в журнал, а не для
        # отказа во входе.
        log.warning("Группы пользователя «%s» не прочитаны: %s", username, exc)
    finally:
        with_unbind = getattr(связь, "unbind", None)
        if callable(with_unbind):
            try:
                with_unbind()
            except Exception:                               # noqa: BLE001
                pass

    роль = роль_по_группам(группы, настройки)
    if not роль:
        log.info("Вход через каталог: «%s» не состоит ни в одной сопоставленной "
                 "группе и роли по умолчанию нет", username)
        return None
    return Человек(username=username, display_name=показать, groups=группы, role=роль)


def _значения(запись: Any, имя: str) -> list[Any]:
    значение = getattr(запись, имя, None)
    if значение is None:
        return []
    сырое = getattr(значение, "values", значение)
    if isinstance(сырое, (list, tuple)):
        return list(сырое)
    return [сырое]


def _одно(запись: Any, имя: str) -> str:
    значения = _значения(запись, имя)
    return str(значения[0]) if значения else ""
