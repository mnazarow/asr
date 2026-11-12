"""Заход 68: сводный отчёт за длинное окно — сразу, свежий — фоном.

Квартал, год и всё время на большом архиве собираются секундами, и раздел
«Аналитика» ждал их на каждое открытие после любой новой записи. Теперь
отчёт длинного окна, собранный не раньше десяти минут назад, отдаётся
сразу с пометкой `stale`, а свежий собирается одним фоновым потоком.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture()
def аналитика(tmp_path: Path):
    from asrhub.analytics import Analytics
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    db.create_job({"filename": "первая.wav"})
    yield Analytics(db), db
    db.close()


def _дождаться_фона(а) -> None:
    for поток in list(а._собираются.values()):
        поток.join(timeout=30)


def test_длинное_окно_отдаётся_сразу_а_собирается_фоном(аналитика):
    а, db = аналитика
    первый = а.full_report("year")
    assert first_total(первый) == 1 and "stale" not in первый
    db.create_job({"filename": "вторая.wav"})                # поколение сменилось
    устаревший = а.full_report("year")
    assert устаревший["stale"]["refreshing"] is True
    assert first_total(устаревший) == 1                       # прежний — без ожидания
    # Десять открытий раздела — одна фоновая сборка.
    for _ in range(10):
        а.full_report("year")
    assert len(а._собираются) <= 1
    _дождаться_фона(а)
    свежий = а.full_report("year")
    assert "stale" not in свежий and first_total(свежий) == 2


def test_короткое_окно_собирается_сразу(аналитика):
    а, db = аналитика
    а.full_report("week")
    db.create_job({"filename": "вторая.wav"})
    отчёт = а.full_report("week")
    assert "stale" not in отчёт and first_total(отчёт) == 2
    assert а._собираются == {}


def test_слишком_старый_длинный_отчёт_собирается_сразу(аналитика, monkeypatch):
    а, db = аналитика
    а.full_report("all")
    db.create_job({"filename": "вторая.wav"})
    monkeypatch.setattr(type(а), "СРОК_УСТАРЕВШЕГО_С", 0.0)
    time.sleep(0.01)
    отчёт = а.full_report("all")
    assert "stale" not in отчёт and first_total(отчёт) == 2


def first_total(отчёт: dict) -> int:
    return int(отчёт["overview"]["jobs"]["total"])
