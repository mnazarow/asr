"""Запуск сервера: python -m asrhub [--host …] [--port …] [--config …]"""
from __future__ import annotations

import argparse
import sys


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


def main(argv: list[str] | None = None) -> int:
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
        settings = load(args.config)
    except Exception as exc:
        sys.stderr.write(f"Ошибка конфигурации: {exc}\n")
        return 2

    if args.workers:
        settings.set("max_concurrent_jobs", args.workers)
    if args.log_level:
        settings.set("log_level", args.log_level)

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

    app = create_app(settings, start_queue=not args.no_queue)
    uvicorn.run(app, host=host, port=port,
                log_level=str(settings.get("log_level") or "info").lower(),
                access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
