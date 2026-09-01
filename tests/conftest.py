"""Общие приспособления для тестов ASR Hub."""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный каталог данных на каждый тест."""
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    return tmp_path / "data"


@pytest.fixture(scope="session")
def sample_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Тестовая запись: тон, тишина, тон — чтобы VAD нашёл два участка."""
    path = tmp_path_factory.mktemp("audio") / "sample.wav"
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=320:duration=3",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-f", "lavfi", "-i", "sine=frequency=480:duration=2",
            "-filter_complex",
            "[0:a]atrim=0:3[a];[1:a]atrim=0:1.2[b];[2:a]atrim=0:2[c];[a][b][c]concat=n=3:v=0:a=1[o]",
            "-map", "[o]", "-ar", "16000", "-ac", "1", str(path),
        ], check=True, capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.SubprocessError):
        # ffmpeg недоступен — собираем WAV средствами стандартной библиотеки
        import math
        import struct

        rate = 16000
        frames = bytearray()
        for index in range(rate * 6):
            second = index / rate
            amplitude = 0.0 if 3.0 <= second < 4.2 else 0.4
            value = int(amplitude * 32767 * math.sin(2 * math.pi * 320 * second))
            frames += struct.pack("<h", value)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
    return path
