"""Запуск сервера: python -m asrhub [--host …] [--port …] [--config …]"""
from __future__ import annotations

import argparse
import sys


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

    host = args.host or str(settings.get("server_host") or "0.0.0.0")
    port = int(args.port or settings.get("server_port") or 8080)

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
