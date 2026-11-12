"""Служба синтеза по образцу голоса: модель XTTS загружается один раз.

Запускается сервером ASR Hub тем Python, в котором установлен Coqui TTS
(его берёт из первой строки программы `tts`), и живёт, пока идут опросы.
Модель XTTS грузится десятки секунд — раньше это происходило на каждую
фразу. Скрипт самостоятельный: пакет asrhub ему не нужен.

Обмен — строками JSON через стандартные ввод и вывод:

    → {"text": "…", "out": "/путь/фраза.wav", "sample": "/путь/образец.wav", "voice": ""}
    ← {"ok": true}            или   {"ok": false, "error": "…"}

Первой строкой служба сообщает о готовности: {"ready": true, "device": "cuda"}
(или {"ready": false, "error": "…"}, если модель не загрузилась). Своя
служба синтеза (настройка qc_tts_service_command) обязана говорить так же.
"""
from __future__ import annotations

import json
import os
import sys

МОДЕЛЬ = os.environ.get("ASRHUB_XTTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2")
ЯЗЫК = os.environ.get("ASRHUB_XTTS_LANGUAGE", "ru")


_обмен: dict = {}


def _отделить_обмен() -> None:
    """Свои ввод и вывод службы.

    Библиотека синтеза печатает в stdout и может спросить что-то через
    input(): её вывод уходит в stderr, а ввод — пустой, иначе она
    перемешала бы обмен строками JSON.
    """
    _обмен["вывод"] = os.fdopen(os.dup(1), "w", encoding="utf-8")
    _обмен["ввод"] = os.fdopen(os.dup(0), "r", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 — на всё время службы


def _ответ(данные: dict) -> None:
    _обмен["вывод"].write(json.dumps(данные, ensure_ascii=False) + "\n")
    _обмен["вывод"].flush()


def main() -> int:
    _отделить_обмен()
    # Лицензию модели XTTS (CPML, некоммерческая) служба за человека не
    # принимает: Coqui спрашивает согласие при первой загрузке, а у службы
    # нет клавиатуры. Согласие — переменная COQUI_TOS_AGREED=1 в окружении
    # сервера, которую администратор ставит сам, прочитав лицензию.
    try:
        import torch  # noqa: PLC0415
        from TTS.api import TTS  # noqa: PLC0415

        устройство = "cuda" if torch.cuda.is_available() else "cpu"
        модель = TTS(МОДЕЛЬ).to(устройство)
    except Exception as exc:                                # noqa: BLE001
        причина = f"{type(exc).__name__}: {exc}"
        if "EOF" in причина or "agree" in причина.lower():
            причина += (" — модель ждёт согласия с лицензией CPML: прочитайте её и задайте "
                        "COQUI_TOS_AGREED=1 в окружении сервера")
        _ответ({"ready": False, "error": причина[:500]})
        return 1
    _ответ({"ready": True, "device": устройство, "model": МОДЕЛЬ})
    for строка in _обмен["ввод"]:
        строка = строка.strip()
        if not строка:
            continue
        try:
            запрос = json.loads(строка)
            модель.tts_to_file(text=str(запрос["text"]), file_path=str(запрос["out"]),
                               speaker_wav=str(запрос.get("sample") or "") or None,
                               language=str(запрос.get("language") or ЯЗЫК))
            _ответ({"ok": True})
        except Exception as exc:                            # noqa: BLE001
            _ответ({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
