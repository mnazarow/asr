"""Запуск сервера: python -m asrhub [--host …] [--port …] [--config …]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


def _users_command(settings: object, args: object) -> int:
    """Обслуживание учётных записей из консоли: список и смена пароля.

    Работает на остановленном сервере тоже: база SQLite открывается той же
    библиотекой, и запись идёт в отдельной транзакции.
    """
    import getpass

    from .accounts import Accounts, password_problem
    from .db import Database

    db = Database(settings.paths.db)          # type: ignore[attr-defined]
    accounts = Accounts(db)
    try:
        if getattr(args, "list_users", False):
            rows = accounts.list()
            if not rows:
                sys.stdout.write("Учётных записей нет.\n")
                return 0
            width = max(len(a.username) for a in rows)
            for account in rows:
                marks = []
                if not account.enabled:
                    marks.append("отключена")
                if account.must_change_password:
                    marks.append("пароль не сменён")
                sys.stdout.write(
                    f"{account.username.ljust(width)}  {account.role:<9}"
                    f"{('  ' + ', '.join(marks)) if marks else ''}\n")
            return 0

        username = str(args.set_password)     # type: ignore[attr-defined]
        account = accounts.by_username(username)
        if account is None:
            sys.stderr.write(f"Нет учётной записи «{username}».\n")
            sys.stderr.write("Список: python -m asrhub --list-users\n")
            return 2
        # Пароль спрашиваем с клавиатуры, а не берём ключом: аргументы
        # командной строки видны в списке процессов и остаются в истории
        # оболочки.
        password = getpass.getpass("Новый пароль: ")
        if password != getpass.getpass("Ещё раз: "):
            sys.stderr.write("Пароли не совпадают.\n")
            return 2
        problem = password_problem(password)
        if problem:
            sys.stderr.write(problem + "\n")
            return 2
        accounts.set_password(account.id, password)
        sys.stdout.write(f"Пароль для «{account.username}» изменён. "
                         "Все открытые сессии закрыты.\n")
        return 0
    finally:
        db.close()


def _потоки_без_консоли(argv: list[str] | None) -> None:
    """Куда писать, когда консоли нет вовсе (pythonw.exe).

    Задача планировщика Windows запускает сервер через pythonw.exe: у
    python.exe при каждом входе в систему открывалось окно консоли, и
    закрыть его значило погасить сервер. Но у pythonw нет ни stdout, ни
    stderr — `sys.stdout` и `sys.stderr` равны None, и uvicorn падает на
    первой же строке настройки журнала (`sys.stdout.isatty()`), а сообщение
    об ошибке конфигурации пропадает бесследно. Направляем оба потока в
    logs/service.log каталога данных — туда же, куда пишет служба NSSM, и
    именно этот файл первым показывает `service.ps1 -Action logs`.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    import os
    from pathlib import Path

    доводы = list(sys.argv[1:] if argv is None else argv)
    конфигурация = ""
    for номер, довод in enumerate(доводы):
        if довод == "--config" and номер + 1 < len(доводы):
            конфигурация = доводы[номер + 1]
        elif довод.startswith("--config="):
            конфигурация = довод.split("=", 1)[1]
    if конфигурация:
        каталог = Path(конфигурация).expanduser().parent / "logs"
    else:
        from .config import default_data_dir

        каталог = default_data_dir() / "logs"
    try:
        каталог.mkdir(parents=True, exist_ok=True)
        поток = open(каталог / "service.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        поток = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = поток
    if sys.stderr is None:
        sys.stderr = поток


def main(argv: list[str] | None = None) -> int:
    _потоки_без_консоли(argv)
    parser = argparse.ArgumentParser(
        prog="asrhub", description="Сервер распознавания речи ASR Hub")
    parser.add_argument("--host", help="Адрес прослушивания (по умолчанию из конфигурации)")
    parser.add_argument("--port", type=int, help="Порт (по умолчанию из конфигурации)")
    parser.add_argument("--config", help="Путь к файлу конфигурации")
    parser.add_argument("--workers", type=int, help="Число одновременных заданий")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--no-queue", action="store_true",
                        help="Не запускать обработку очереди (только интерфейс)")
    parser.add_argument("--print-config", action="store_true",
                        help="Вывести пример config.yaml и выйти")
    parser.add_argument("--check", action="store_true",
                        help="Проверить окружение и выйти")
    parser.add_argument("--check-config", action="store_true",
                        help="Показать, какие значения конфигурации не приняты, и выйти")
    # Путь назад, когда пароль забыт. Без него единственный администратор,
    # потерявший пароль, остаётся снаружи навсегда: ключ доступа управлять
    # учётными записями не позволяет, а руками в базе — не вариант.
    parser.add_argument("--set-password", metavar="ЛОГИН",
                        help="Задать пароль учётной записи и выйти "
                             "(пароль спрашивается с клавиатуры)")
    parser.add_argument("--list-users", action="store_true",
                        help="Показать учётные записи и выйти")
    args = parser.parse_args(argv)

    from .config import generate_example_config, load

    if args.print_config:
        sys.stdout.write(generate_example_config())
        return 0

    try:
        # Проверка конфигурации ничего не создаёт: см. `ensure_key`.
        settings = load(args.config, ensure_key=not args.check_config)
    except Exception as exc:
        sys.stderr.write(f"Ошибка конфигурации: {exc}\n")
        return 2

    if args.workers:
        settings.set("max_concurrent_jobs", args.workers)
    if args.log_level:
        settings.set("log_level", args.log_level)

    # Отдельно от `--check`: там полсотни проверок окружения, а здесь один
    # вопрос — принял ли сервер файл конфигурации целиком. Его задаёт
    # обновление перед тем, как перезапускать службу, и человек после
    # правки файла. Ответ короткий и без цветных таблиц.
    if args.check_config:
        файл = settings.config_file
        print(f"Файл конфигурации: {файл or 'не используется, взяты умолчания'}")
        if not settings.problems:
            print("Все значения приняты.")
            return 0
        print(f"Не принято значений: {len(settings.problems)}. "
              "Вместо них работают умолчания:")
        for проблема in settings.problems:
            print(f"  • {проблема}")
        print("\nПоправьте записи в файле и перезапустите сервер.")
        return 1

    if args.check:
        from .doctor import run_checks

        return 0 if run_checks(settings) else 1

    if args.list_users or args.set_password:
        return _users_command(settings, args)

    # Адрес и порт кладём в настройки, а не только в uvicorn: из настроек их
    # читают стартовая запись в журнале и готовый фрагмент prometheus.yml,
    # который отдаёт /api/monitoring/config/prometheus-scrape. Раньше сервер,
    # запущенный как `-m asrhub --host 127.0.0.1 --port 8199`, писал в журнал
    # «запущен: 0.0.0.0:8080» и выдавал для сбора метрик адрес, по которому
    # его нет.
    if args.host:
        settings.set("server_host", args.host)
    if args.port:
        settings.set("server_port", int(args.port))
    host = str(settings.get("server_host") or "0.0.0.0")
    port = int(settings.get("server_port") or 8080)

    try:
        import uvicorn
    except ModuleNotFoundError:
        sys.stderr.write(
            "Не установлен uvicorn. Выполните: pip install 'uvicorn[standard]' fastapi\n")
        return 3

    from .api import create_app

    _временный_каталог(settings)
    app = create_app(settings, start_queue=not args.no_queue)
    _не_умирать_от_sighup()
    uvicorn.run(app, host=host, port=port,
                log_level=str(settings.get("log_level") or "info").lower(),
                access_log=False,
                # Без срока uvicorn ждёт незакрытые запросы бесконечно, и до
                # lifespan — возврата идущих заданий в очередь — дело не
                # доходило: медленное тело POST /api/jobs держало остановку,
                # пока systemd (45 с) или Docker (10 с) не присылали SIGKILL,
                # и каждое идущее задание теряло попытку.
                timeout_graceful_shutdown=ОСТАНОВКА_ЗАПРОСОВ_С)
    return 0


#: Сколько при остановке ждать незавершённые запросы, прежде чем закрыть их
#: и перейти к остановке очереди. Меньше срока остановки у systemd и Docker
#: с запасом на возврат заданий (см. `api.app.lifespan`).
ОСТАНОВКА_ЗАПРОСОВ_С = 5


def _временный_каталог(settings: Any) -> None:
    """Временные файлы процесса — в каталог временных файлов сервера.

    Тело загрузки Starlette складывает в SpooledTemporaryFile без каталога,
    то есть в системный /tmp: до max_upload_mb на запрос, а /tmp бывает
    tmpfs (это память) или разделом в сотню мегабайт. `temp_dir` при этом
    обещал «место промежуточных файлов». Здесь — то же, что для SQLite
    (`SQLITE_TMPDIR`): каталог сервера, если он есть и доступен на запись.
    Только в самом сервере — не при импорте пакета.
    """
    import tempfile  # noqa: PLC0415

    каталог = str(settings.get("temp_dir") or settings.paths.tmp)
    try:
        Path(каталог).mkdir(parents=True, exist_ok=True)
        if os.access(каталог, os.W_OK):
            tempfile.tempdir = каталог
    except OSError as exc:
        sys.stderr.write(f"Каталог временных файлов {каталог} недоступен: {exc}\n")


def _не_умирать_от_sighup() -> None:
    """SIGHUP больше не убивает сервер.

    Юнит systemd объявлял `ExecReload=kill -HUP`, а обработчика SIGHUP не
    было ни у нас, ни у uvicorn: `systemctl reload` (так делают средства
    автоматизации) убивал процесс без остановки очереди, а при
    Restart=on-failure гибель от SIGHUP считалась чистым выходом — служба
    не поднималась. Перечитывать настройки на ходу сервер не умеет, поэтому
    сигнал только записывается в журнал.
    """
    import logging  # noqa: PLC0415
    import signal  # noqa: PLC0415

    if not hasattr(signal, "SIGHUP"):
        return

    def обработать(_signum: int, _frame: Any) -> None:
        logging.getLogger("asrhub.app").warning(
            "Получен SIGHUP: перечитывать настройки на ходу сервер не умеет — "
            "изменения из config.yaml вступят в силу после перезапуска службы")

    try:
        signal.signal(signal.SIGHUP, обработать)
    except (ValueError, OSError):                       # не главный поток
        pass


if __name__ == "__main__":
    raise SystemExit(main())
