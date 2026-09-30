"""Адаптер whisper.cpp — вызов внешнего бинарника whisper-cli.

Лучший вариант для macOS (Metal) и для серверов без GPU.
Значения VAD по умолчанию в whisper.cpp сильно отличаются от faster-whisper,
поэтому ASR Hub всегда передаёт их явно.

Веса — файлы `ggml-<вариант>.bin` из репозитория ggerganov/whisper.cpp на
Hugging Face; лежат они в `<каталог моделей>/whisper.cpp/`. Модель поиска
речи для ключа `--vad` — `ggml-silero-<версия>.bin` там же.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import BinaryMissing, EngineError, ModelNotDownloaded
from ..logging_setup import get_logger
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

log = get_logger("engines.whisper_cpp")

_BIN_CANDIDATES = ["whisper-cli", "whisper-cpp", "main"]

#: Откуда брать веса: те же файлы, что качает models/download-ggml-model.sh
#: из самого whisper.cpp.
АДРЕС_ВЕСОВ = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
#: Модель поиска речи для `--vad` — models/download-vad-model.sh.
АДРЕС_VAD = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/"
ФАЙЛ_VAD = "ggml-silero-v6.2.0.bin"
#: Первые байты файла ggml: 0x67676d6c в порядке little-endian. Прокси или
#: страница входа отдают HTML с кодом 200 — такой «файл модели» whisper-cli
#: потом отверг бы с невнятным «failed to load model».
_ПОДПИСЬ_GGML = b"lmgg"


def каталог_весов(models_dir: str | os.PathLike[str]) -> Path:
    """Где лежат веса whisper.cpp внутри каталога моделей."""
    return Path(models_dir or ".") / "whisper.cpp"


def файл_весов(models_dir: str | os.PathLike[str], source: str) -> Path | None:
    """Файл весов модели, если он на месте: сперва каталог моделей, потом
    каталог из ASRHUB_WHISPER_CPP_MODELS — туда кладут веса, скачанные
    сценарием самого whisper.cpp."""
    кандидаты = [каталог_весов(models_dir) / source]
    свой = os.environ.get("ASRHUB_WHISPER_CPP_MODELS")
    if свой:
        кандидаты.append(Path(свой) / source)
    for путь in кандидаты:
        if путь.is_file():
            return путь
    return None


def модель_vad(models_dir: str | os.PathLike[str]) -> Path | None:
    """Модель поиска речи для `--vad`.

    Раньше она бралась только из переменной ASRHUB_WHISPER_CPP_VAD, которую
    нигде не задавали: «Поиск речи» у whisper.cpp был включён в настройках
    и не делал ничего — по тишине модель по-прежнему сочиняла текст.
    """
    явная = os.environ.get("ASRHUB_WHISPER_CPP_VAD", "")
    if явная and Path(явная).is_file():
        return Path(явная)
    найденные = sorted(каталог_весов(models_dir).glob("ggml-silero-*.bin"))
    return найденные[-1] if найденные else None


def _скачать(адрес: str, цель: Path, *, token: str | None,
             progress: Callable[[float], None] | None) -> Path:
    """Файл по адресу — атомарно: сначала `.part`, потом переименование.

    Оборванная загрузка не оставляет на месте весов обрубок, который сервер
    принял бы за скачанную модель.
    """
    цель.parent.mkdir(parents=True, exist_ok=True)
    заголовки = {"User-Agent": "asrhub"}
    if token:
        заголовки["Authorization"] = f"Bearer {token}"
    частичный = цель.with_name(цель.name + ".part")
    try:
        запрос = urllib.request.Request(адрес, headers=заголовки)
        with urllib.request.urlopen(запрос, timeout=60) as ответ, \
                частичный.open("wb") as выход:
            try:
                всего = int(ответ.headers.get("Content-Length") or 0)
            except ValueError:
                всего = 0                        # длина неизвестна — хода не будет
            получено = 0
            while True:
                кусок = ответ.read(1 << 20)
                if not кусок:
                    break
                выход.write(кусок)
                получено += len(кусок)
                if progress is not None and всего:
                    progress(min(1.0, получено / всего))
        if всего and получено != всего:
            raise EngineError(
                f"Загрузка {цель.name} оборвалась: получено {получено} из {всего} байт.",
                hint="Повторите загрузку; при повторных обрывах проверьте сеть и прокси.")
        with частичный.open("rb") as проверка:
            if проверка.read(4) != _ПОДПИСЬ_GGML:
                raise EngineError(
                    f"Вместо весов {цель.name} пришло что-то другое.",
                    hint="Так бывает за прокси или страницей входа. Скачайте файл "
                         f"вручную ({адрес}) и положите в {цель.parent}.")
        os.replace(частичный, цель)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise EngineError(
                f"На Hugging Face нет файла {цель.name}.",
                hint=f"Адрес: {адрес}. Проверьте имя варианта в каталоге.") from exc
        if exc.code in (401, 403):
            raise EngineError(
                f"Hugging Face отказал в доступе к {цель.name} (код {exc.code}).",
                hint="Проверьте токен Hugging Face и доступ сервера к huggingface.co.") from exc
        raise EngineError(f"Hugging Face ответил кодом {exc.code} на {цель.name}.",
                          hint="Повторите позже.") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            http.client.HTTPException) as exc:
        # HTTPException — это обрыв посреди ответа (IncompleteRead и родня):
        # он не наследует ни URLError, ни OSError, и без него улетал бы мимо.
        причина = getattr(exc, "reason", exc)
        raise EngineError(
            f"Не удалось скачать {цель.name}: {причина}",
            hint="Проверьте доступ к huggingface.co. Если сервер закрыт, скачайте "
                 f"файл на другой машине ({адрес}) и положите в {цель.parent}.") from exc
    except OSError as exc:
        raise EngineError(f"Не удалось записать {цель}: {exc}",
                          hint="Проверьте место на диске и права на каталог моделей.") from exc
    finally:
        частичный.unlink(missing_ok=True)
    return цель


def download_weights(source: str, models_dir: str | os.PathLike[str], *,
                     force: bool = False, token: str | None = None,
                     progress: Callable[[float], None] | None = None) -> Path:
    """Скачивает веса модели whisper.cpp и заодно модель поиска речи.

    Раньше загрузка шла общей дорогой Hugging Face: источник «large-v3-turbo»
    считался репозиторием faster-whisper, и вместо файла ggml на диск ложились
    чужие веса CTranslate2 — а движок оставался «не загружен». Варианты с
    квантованием («…-q5_0») не находились вовсе.
    """
    цель = каталог_весов(models_dir) / source
    if force or not цель.is_file():
        _скачать(АДРЕС_ВЕСОВ + source, цель, token=token, progress=progress)
    # Модель поиска речи весит меньше мегабайта; без неё «Поиск речи» у
    # whisper.cpp не работает. Её отказ загрузку весов не отменяет.
    if модель_vad(models_dir) is None:
        try:
            _скачать(АДРЕС_VAD + ФАЙЛ_VAD, каталог_весов(models_dir) / ФАЙЛ_VAD,
                     token=token, progress=None)
        except EngineError as exc:
            log.warning("Модель поиска речи для whisper.cpp не скачана: %s", exc.message)
    return цель


def _служебный(текст: str) -> bool:
    """Служебные метки whisper.cpp: «[_BEG_]», «[_TT_123]», «[_EOT_]»…"""
    return текст.startswith("[_")


#: Знаки, которые прирастают к слову перед ними и к слову после них — как
#: в faster-whisper, чтобы слова двух движков выглядели одинаково.
_К_ПРЕДЫДУЩЕМУ = frozenset(".,!?:;…»)]}”、。，！？：")
_К_СЛЕДУЮЩЕМУ = frozenset("«([{“¿¡")
_КАВЫЧКИ = frozenset("\"'")


def _слова(tokens: list[Any]) -> list[dict[str, Any]]:
    """Слова из токенов whisper.cpp.

    Токен — кусок слова, а не слово: «Привет» приходит как « При» + «вет».
    Раньше каждый токен становился отдельным «словом» с обрезанным пробелом,
    и пословные таймкоды, субтитры и поиск получали обрывки. Новое слово
    начинается с токена, у которого впереди пробел; закрывающие знаки
    прирастают к слову перед ними, открывающие — к слову после них.
    """
    слова: list[dict[str, Any]] = []
    приставка = ""
    начало_приставки: float | None = None
    вероятности_приставки: list[float] = []
    for token in tokens or []:
        if not isinstance(token, dict):
            continue
        сырой = str(token.get("text") or "")
        текст = сырой.strip()
        if not текст or _служебный(сырой.lstrip()):
            continue
        пробел = сырой[:1].isspace()
        отметки = token.get("offsets") or {}
        try:
            начало: float | None = float(отметки["from"]) / 1000.0
            конец: float | None = float(отметки["to"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            начало = конец = None
        p = token.get("p")
        вероятность = [float(p)] if isinstance(p, (int, float)) else []

        # Кавычка без пробела впереди закрывает, с пробелом — открывает.
        закрывающий = all(с in _К_ПРЕДЫДУЩЕМУ or (с in _КАВЫЧКИ and not пробел)
                          for с in текст)
        открывающий = all(с in _К_СЛЕДУЮЩЕМУ or (с in _КАВЫЧКИ and пробел)
                          for с in текст)
        if закрывающий and слова and not приставка:
            слово = слова[-1]
            слово["word"] += текст
            if конец is not None:
                слово["end"] = конец
            слово["_p"].extend(вероятность)
            continue
        if открывающий:
            приставка += текст
            if начало_приставки is None:
                начало_приставки = начало
            вероятности_приставки.extend(вероятность)
            continue
        if слова and not пробел and not приставка:
            слово = слова[-1]
            слово["word"] += текст
            if конец is not None:
                слово["end"] = конец
                if слово["start"] is None:
                    слово["start"] = начало
            слово["_p"].extend(вероятность)
            continue
        слова.append({
            "word": приставка + текст,
            "start": начало_приставки if начало_приставки is not None else начало,
            "end": конец,
            "_p": вероятности_приставки + вероятность,
        })
        приставка, начало_приставки, вероятности_приставки = "", None, []
    if приставка and слова:
        # Открывающий знак в самом конце — дописываем к последнему слову,
        # а не теряем.
        слова[-1]["word"] += приставка
    итог = []
    for слово in слова:
        if слово["start"] is None or слово["end"] is None:
            continue
        вероятности = слово.pop("_p")
        итог.append({
            "word": слово["word"],
            "start": round(слово["start"], 3),
            "end": round(max(слово["start"], слово["end"]), 3),
            "confidence": round(sum(вероятности) / len(вероятности), 4)
            if вероятности else None,
        })
    return итог


def разобрать_вывод(data: dict[str, Any], *, пословно: bool,
                    длительность: float | None = None) -> list[Segment]:
    """Реплики из JSON, который пишет `whisper-cli -ojf`.

    Таймкоды не выходят за длину записи: последнее окно whisper дополняется
    тишиной до 30 секунд, и конец последней реплики иногда оказывается там.
    """
    язык = str((data.get("result") or {}).get("language") or "")
    предел = длительность if длительность and длительность > 0 else None

    def в_пределах(значение: float) -> float:
        return min(значение, предел) if предел is not None else значение

    реплики: list[Segment] = []
    for item in data.get("transcription") or []:
        if not isinstance(item, dict):
            continue
        текст = str(item.get("text") or "").strip()
        if not текст:
            continue
        отметки = item.get("offsets") or {}
        начало = в_пределах(float(отметки.get("from", 0)) / 1000.0)
        конец = max(начало, в_пределах(float(отметки.get("to", 0)) / 1000.0))
        токены = [t for t in item.get("tokens") or [] if isinstance(t, dict)]
        # Уверенность — по токенам речи: у служебных меток вероятности
        # копеечные, и с ними уверенность каждой реплики занижалась.
        вероятности = [float(t["p"]) for t in токены
                       if isinstance(t.get("p"), (int, float))
                       and not _служебный(str(t.get("text") or "").lstrip())]
        слова = []
        if пословно:
            for слово in _слова(токены):
                слово["start"] = в_пределах(слово["start"])
                слово["end"] = max(слово["start"], в_пределах(слово["end"]))
                слова.append(слово)
        реплики.append(Segment(
            start=round(начало, 3), end=round(конец, 3), text=текст,
            confidence=round(sum(вероятности) / len(вероятности), 4) if вероятности else None,
            language=язык, words=слова,
        ))
    return реплики


class WhisperCppEngine(Engine):
    id = "whisper_cpp"
    supports_word_timestamps = True
    outputs_punctuation = True

    @classmethod
    def _find_binary(cls) -> str | None:
        env = os.environ.get("ASRHUB_WHISPER_CPP")
        if env and Path(env).exists():
            return env
        for name in _BIN_CANDIDATES:
            found = shutil.which(name)
            if found:
                return found
        # Установщик собирает whisper.cpp в `<установка>/whisper.cpp`, рядом
        # с `server/`. Раньше этот каталог в списке не значился, и собранный
        # бинарник находился только через переменную из env.sh — а её не
        # видит сервер, запущенный не службой.
        установка = Path(__file__).resolve().parents[3]
        for base in (установка / "whisper.cpp/build/bin",
                     Path.home() / ".local/share/asrhub/whisper.cpp/build/bin",
                     Path("/opt/whisper.cpp/build/bin"),
                     Path("/usr/local/lib/whisper.cpp/build/bin")):
            for name in _BIN_CANDIDATES:
                candidate = base / name
                if candidate.exists():
                    return str(candidate)
        return None

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        if cls._find_binary():
            return True, ""
        return False, ("Не найден бинарник whisper-cli. Соберите whisper.cpp: "
                       "bash scripts/models.sh install-engine whisper_cpp")

    def _model_file(self, settings: dict[str, Any]) -> Path:
        найден = файл_весов(settings.get("models_dir") or ".", self.spec.source)
        if найден is None:
            raise ModelNotDownloaded(self.spec.id, self.spec.disk_mb)
        return найден

    def _load(self, settings: dict[str, Any]) -> Any:
        binary = self._find_binary()
        if not binary:
            raise BinaryMissing(
                "whisper-cli",
                "Соберите whisper.cpp: bash scripts/models.sh install-engine whisper_cpp")
        return {"binary": binary, "model": str(self._model_file(settings))}

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        binary = self._model["binary"]
        model_file = self._model["model"]

        with tempfile.TemporaryDirectory(prefix="whispercpp-",
                                         dir=settings.get("temp_dir") or None) as tmp:
            out_base = Path(tmp) / "out"
            cmd = [
                # -ojf, а не -oj: в коротком JSON нет токенов, а с ними —
                # пословные таймкоды и уверенность. Раньше стоял -oj, и слова
                # и уверенность у whisper.cpp были пустыми всегда.
                binary, "-m", model_file, "-f", str(audio_path),
                "-ojf", "-of", str(out_base),
                "-t", str(S.integer(settings, "cpu_threads", 0) or os.cpu_count() or 4),
                "-bs", str(int(settings.get("beam_size") or 5)),
                "-bo", str(int(settings.get("best_of") or 5)),
                "-tp", str(S.num(settings, "temperature", 0.0)),
                "-et", str(float(settings.get("compression_ratio_threshold") or 2.4)),
                "-lpt", str(S.num(settings, "logprob_threshold", -1.0)),
                "-nth", str(S.num(settings, "no_speech_threshold", 0.6)),
            ]
            language = self.language_for(settings)
            cmd += ["-l", language or "auto"]
            if str(settings.get("task")) == "translate":
                cmd.append("-tr")
            if not settings.get("condition_on_previous_text", False):
                cmd += ["-mc", "0"]
            if not settings.get("temperature_fallback", True):
                cmd.append("-nf")
            # Ключа -dtw здесь нет намеренно. whisper-cli знает наборы голов
            # только под именами «large.v3.turbo», а передавалось
            # «large-v3-turbo» — и на «unknown DTW preset» падало КАЖДОЕ
            # задание моделей large-v3 со словами (они включены по
            # умолчанию). К тому же при flash attention, включённой по
            # умолчанию, DTW whisper.cpp отключает сам, а поле t_dtw этот
            # разбор не читает: границы слов берутся из токенов.
            if not settings.get("flash_attention", True):
                cmd.append("-nfa")
            if self.resolve_device(settings) == "cpu":
                cmd.append("-ng")
            prompt = str(settings.get("initial_prompt") or "").strip()
            if prompt:
                cmd += ["--prompt", prompt]
                if settings.get("carry_initial_prompt"):
                    cmd.append("--carry-initial-prompt")
            предупреждения: list[str] = []
            if settings.get("vad_enabled", True):
                vad_model = модель_vad(settings.get("models_dir") or ".")
                if vad_model is None:
                    предупреждения.append(
                        "Поиск речи у whisper.cpp не работает: нет модели "
                        f"{ФАЙЛ_VAD} в {каталог_весов(settings.get('models_dir') or '.')}. "
                        "Скачайте модель заново — она загружается вместе с весами.")
                else:
                    cmd += ["--vad", "-vm", str(vad_model),
                            "-vt", str(float(settings.get("vad_threshold") or 0.5)),
                            "-vspd", str(S.integer(settings, "vad_min_speech_ms", 250)),
                            "-vsd", str(S.integer(settings, "vad_min_silence_ms", 500)),
                            "-vp", str(S.integer(settings, "vad_speech_pad_ms", 200))]

            self.report(progress, 0.1, "распознавание")
            try:
                res = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=S.integer(settings, "job_timeout_s", 7200) or None,
                                     check=False)
            except subprocess.TimeoutExpired as exc:
                raise EngineError("whisper.cpp не завершился за отведённое время.") from exc
            except OSError as exc:
                raise BinaryMissing("whisper-cli", str(exc)) from exc

            if res.returncode != 0:
                raise EngineError(
                    "whisper.cpp завершился с ошибкой.",
                    hint=(res.stderr or "").strip()[-800:] or "Проверьте файл модели и параметры.",
                    details={"returncode": res.returncode})

            json_path = out_base.with_suffix(".json")
            if not json_path.exists():
                raise EngineError("whisper.cpp не создал файл результата.",
                                  hint=(res.stderr or "")[-500:])
            try:
                # Токены, разрезавшие многобайтовый символ, whisper.cpp склеивает
                # не во всех версиях — битый байт не должен ронять задание.
                data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError) as exc:
                raise EngineError("whisper.cpp записал результат, который не читается.",
                                  hint=f"{type(exc).__name__}: {exc}") from exc

        try:
            длительность: float | None = probe(audio_path).duration_s
        except Exception:                                   # noqa: BLE001
            длительность = None
        segments = разобрать_вывод(
            data if isinstance(data, dict) else {},
            пословно=bool(settings.get("word_timestamps", True)),
            длительность=длительность)

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments,
            language=str(((data if isinstance(data, dict) else {}).get("result") or {})
                         .get("language") or ""),
            duration=длительность or (segments[-1].end if segments else 0.0),
            meta={"binary": binary, "model_file": model_file,
                  "warnings": предупреждения},
        )
