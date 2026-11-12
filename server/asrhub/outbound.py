"""Выход наружу: проверка адреса, открыватель с проверкой при соединении и
пауза у приёмников, которые не отвечают.

Вынесено из `job_queue.py` (3.1.39): по этим правилам ходят наружу не
только уведомления очереди, но и CRM, приёмники мониторинга, ссылка на
запись в диалплане, телефон из интеграции и языковая модель. `job_queue`
по-прежнему отдаёт эти имена — для прежних импортов.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .errors import ConfigError
from .logging_setup import get_logger

log = get_logger("queue")


def check_outbound_url(url: str, allow_internal: bool = False) -> str:
    """Проверяет адрес, на который сервер пойдёт сам: обратный вызов, трекер.

    Адрес приходит из запроса или настроек и уходит в urlopen, поэтому без
    проверки сервер становится инструментом обращения к внутренней сети от
    своего имени: file:// читает диск, а http://169.254.169.254 достаёт
    учётные данные облака. Пропускаем только http и https и запрещаем
    адреса, которые указывают внутрь.

    Узел проверяется по тем адресам, в которые он на самом деле
    превращается. Раньше проверка смотрела только на запись вида
    «10.0.0.1»: `ipaddress` не понимает «127.1», «2130706433», «0x7f000001»
    и «017700000001», считал их доменными именами «проверять некому» — а
    сокет затем честно превращал их в 127.0.0.1. Доменное имя тоже
    превращается в адреса здесь же: имя, указывающее на 127.0.0.1, ничем
    не лучше самого 127.0.0.1.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"Адрес уведомления должен начинаться с http:// или https:// "
            f"(получено «{parsed.scheme or url[:20]}»).",
            hint="Другие схемы запрещены: через них сервер читал бы "
                 "собственные файлы вместо отправки уведомления.")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ConfigError("В адресе уведомления не указан узел.")

    if allow_internal:
        return url

    lowered = host.lower()
    if lowered in ("localhost", "localhost.localdomain") or lowered.endswith(".localhost"):
        raise ConfigError(
            "Адрес уведомления указывает на сам сервер.",
            hint="Если это намеренно, включите webhook_allow_internal.")
    for address in _адреса_узла(host):
        if _внутренний(address):
            raise ConfigError(
                f"Адрес уведомления {host} находится во внутренней сети ({address}).",
                hint="Если это намеренно, включите webhook_allow_internal.")
    return url


def _адреса_узла(host: str) -> list[Any]:
    """Во что превращается узел: запись адреса или имя, которое ещё надо узнать."""
    import ipaddress
    import socket

    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    # Старые записи IPv4, которые понимает сокет, но не `ipaddress`.
    try:
        return [ipaddress.IPv4Address(socket.inet_aton(host))]
    except (OSError, ValueError):
        pass
    try:
        ответы = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        # Имя не узнаётся сейчас — не узнается и при отправке: запрос не
        # уйдёт никуда, и запрещать тут нечего.
        return []
    адреса = []
    for ответ in ответы:
        try:
            адреса.append(ipaddress.ip_address(str(ответ[4][0]).split("%", 1)[0]))
        except ValueError:
            continue
    return адреса


def _внутренний(address: Any) -> bool:
    """Адрес смотрит внутрь: сам сервер, локальная сеть, служебные диапазоны.

    Наружу — только глобальные адреса (`is_global`). Набор отдельных
    признаков (частный, петля, служебный…) не покрывал 100.64.0.0/10 —
    общий адрес провайдера, где в том числе живут метаданные Alibaba Cloud
    (100.100.100.200), — и такие адреса считались внешними.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(not address.is_global or address.is_loopback or address.is_private
                or address.is_link_local or address.is_reserved or address.is_multicast
                or address.is_unspecified)


def _класс_с_проверкой(базовый: Any, допуск: Callable[[Any, int], None]) -> Any:
    """Класс соединения http.client, который сам узнаёт адрес и проверяет его.

    Проверка адреса и соединение узнавали имя каждый своё: между ними ответ
    DNS мог смениться (имя с TTL 0 — сначала внешний адрес, при соединении
    127.0.0.1 или 169.254.169.254), и запрос уходил внутрь, а поле
    webhook_status отвечало, дошёл ли он. Здесь имя узнаётся один раз, все
    его адреса проверяются, и соединение идёт на тот же, проверенный, адрес.
    Имя узла остаётся прежним в заголовке Host и в SNI: соединение HTTPS
    поверх этого сокета строит сам http.client.
    """
    import ipaddress
    import socket

    #: Знак «тайм-аут не задан» у http.client — свой у модуля socket.
    _ПО_УМОЛЧАНИЮ: Any = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", object())

    class Проверенное(базовый):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = self._проверенное_соединение

        def _проверенное_соединение(self, address: tuple[str, int],
                                    timeout: Any = _ПО_УМОЛЧАНИЮ,
                                    source_address: Any = None) -> Any:
            host, port = address
            ответы = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            for *_, адрес_сокета in ответы:
                допуск(ipaddress.ip_address(str(адрес_сокета[0]).split("%", 1)[0]), port)
            ошибка: OSError | None = None
            for семейство, тип, протокол, _, адрес_сокета in ответы:
                сокет = socket.socket(семейство, тип, протокол)
                try:
                    if timeout is not _ПО_УМОЛЧАНИЮ:
                        сокет.settimeout(timeout)
                    if source_address:
                        сокет.bind(source_address)
                    сокет.connect(адрес_сокета)
                    return сокет
                except OSError as exc:
                    ошибка = exc
                    сокет.close()
            raise ошибка or OSError(f"Не удалось соединиться с {host}")

    return Проверенное


def открыватель_наружу(allow_internal: bool = False,
                       проверка: Callable[[str], Any] | None = None) -> Any:
    """urllib-открыватель, который проверяет и каждое перенаправление.

    Обычный urlopen идёт по 301/302 куда скажут, превращая POST в GET: внешний
    приёмник, ответивший «302 → http://169.254.169.254/…», заставлял сервер
    сходить туда самому, а поле webhook_status работало оракулом. `проверка`
    заменяет проверку по умолчанию — у маршрута /process-call она своя.
    """
    import http.client
    import urllib.request

    def проверить(адрес: str) -> None:
        if проверка is not None:
            проверка(адрес)
        else:
            check_outbound_url(адрес, allow_internal)

    def допуск(адрес: Any, порт: int) -> None:
        # Тот же закон, что у проверки адреса, — но для адреса, на который
        # соединение пойдёт на самом деле (см. `_класс_с_проверкой`).
        запись = f"[{адрес}]" if getattr(адрес, "version", 4) == 6 else str(адрес)
        проверить(f"http://{запись}:{порт}/")

    class Проверенные(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            проверить(newurl)
            # POST остаётся POST с тем же телом и заголовками (смысл 307/308).
            # urllib на 301/302/303 превращал его в GET без тела, а ответ 200
            # финального адреса записывался как `ok:200`: адрес http://,
            # который прокси переводит на https://, или путь без завершающей
            # косой (APPEND_SLASH) — и приёмник получал пустой GET, а сервер
            # считал, что уведомление дошло.
            if req.get_method() == "POST" and code in (301, 302, 303, 307, 308):
                return urllib.request.Request(
                    newurl.replace(" ", "%20"), data=req.data,
                    headers=dict(req.headers), method="POST",
                    origin_req_host=req.origin_req_host, unverifiable=True)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def через_прокси(req: Any) -> bool:
        # Через прокси имя узнаёт прокси, а не мы: соединение идёт к нему, и
        # закрепить адрес назначения здесь нечем. Остаётся проверка адреса
        # до отправки — как и раньше.
        return bool(getattr(req, "_tunnel_host", None)) or req.has_proxy()

    class ПроверенныйHTTP(urllib.request.HTTPHandler):
        def http_open(self, req):  # noqa: ANN001
            класс = (http.client.HTTPConnection if через_прокси(req)
                     else _класс_с_проверкой(http.client.HTTPConnection, допуск))
            return self.do_open(класс, req)

    class ПроверенныйHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001
            класс = (http.client.HTTPSConnection if через_прокси(req)
                     else _класс_с_проверкой(http.client.HTTPSConnection, допуск))
            return self.do_open(класс, req, context=self._context)

    return urllib.request.build_opener(Проверенные, ПроверенныйHTTP, ПроверенныйHTTPS)


#: Приёмники уведомлений, не отвечающие несколько заходов подряд:
#: узел → [отказов подряд, пауза до]. Общие на процесс: очередь одна.
_УЗЛЫ_УВЕДОМЛЕНИЙ: dict[str, list[float]] = {}
_ЗАМОК_УЗЛОВ = threading.Lock()


def узел_на_паузе(узел: str) -> float:
    """До какого момента узел приёмника на паузе (0 — не на паузе)."""
    with _ЗАМОК_УЗЛОВ:
        запись = _УЗЛЫ_УВЕДОМЛЕНИЙ.get(узел)
        return запись[1] if запись and запись[1] > time.time() else 0.0


def узел_ответил(узел: str, удачно: bool, *, порог: int = 3) -> None:
    """Счёт заходов подряд без ответа; с порогового — пауза узла целиком."""
    with _ЗАМОК_УЗЛОВ:
        if удачно:
            _УЗЛЫ_УВЕДОМЛЕНИЙ.pop(узел, None)
            return
        запись = _УЗЛЫ_УВЕДОМЛЕНИЙ.setdefault(узел, [0.0, 0.0])
        запись[0] += 1
        if запись[0] >= порог:
            пауза = min(1800.0, 60.0 * 2 ** (запись[0] - порог))
            запись[1] = time.time() + пауза
            log.warning("Приёмник уведомлений %s не отвечает %d заходов подряд — "
                        "пауза %.0f с", узел, int(запись[0]), пауза)
        if len(_УЗЛЫ_УВЕДОМЛЕНИЙ) > 1024:
            for старый in list(_УЗЛЫ_УВЕДОМЛЕНИЙ)[:512]:
                _УЗЛЫ_УВЕДОМЛЕНИЙ.pop(старый, None)
