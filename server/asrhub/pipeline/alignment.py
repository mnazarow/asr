"""Принудительное выравнивание: уточнение границ слов по звуку.

Таймкоды, которые выдаёт распознаватель, — это оценка декодера: он сообщает,
на каком кадре, по его мнению, было слово. Принудительное выравнивание решает
другую задачу: текст уже известен, и нужно найти, где именно в сигнале звучит
каждое слово. Границы получаются заметно точнее, и на субтитрах, дубляже и
разборе спорных записей эта разница видна.

Два механизма:

* **MFA** (Montreal Forced Aligner) — акустическая модель плюс произносительный
  словарь. Для русского есть готовые `russian_mfa`. Ставится через conda и
  занимает 2–3 ГБ, зато не требует видеокарты и даёт лучшее качество границ.
* **WhisperX** — выравнивание нейросетевой моделью wav2vec2. Легче в установке,
  работает на видеокарте, качество границ чуть хуже MFA, но заметно лучше
  модельных таймкодов.

Оба механизма при любой неудаче возвращают исходные сегменты: выравнивание —
улучшение, а не обязательный этап, и ронять из-за него готовую расшифровку
неправильно.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import ASRHubError, BinaryMissing, DependencyMissing

log = logging.getLogger("asrhub.alignment")

# Знаки, которые словарь MFA не знает: перед выравниванием текст очищается,
# а после — возвращается из исходных сегментов.
_STRIP = '.,;:!?«»""''()[]—–-'


def available(backend: str) -> tuple[bool, str]:
    """Готов ли механизм выравнивания к работе."""
    if backend == "mfa":
        if shutil.which("mfa") is None:
            return False, ("Не найдена программа mfa. Установка: "
                           "bash scripts/models.sh install-engine mfa")
        return True, ""
    if backend == "whisperx":
        try:
            import whisperx  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, ("Не установлен пакет whisperx: "
                           "bash scripts/models.sh install-engine whisperx")
    return False, f"Неизвестный механизм выравнивания «{backend}»"


def align_segments(audio_path: Path, segments: list[Any], settings: Any) -> list[Any]:
    """Уточняет границы слов и сегментов. При любой неудаче возвращает исходные.

    Args:
        audio_path: подготовленный WAV 16 кГц моно.
        segments: сегменты после распознавания (объекты Segment).
        settings: настройки задания.

    Returns:
        Сегменты с уточнёнными границами и заполненным полем words.
    """
    backend = str(settings.get("alignment_backend") or "none")
    if backend in ("none", "", None) or not segments:
        return segments

    ok, reason = available(backend)
    if not ok:
        raise DependencyMissing("alignment", backend).with_hint(reason)

    if backend == "mfa":
        return _align_mfa(audio_path, segments, settings)
    if backend == "whisperx":
        return _align_whisperx(audio_path, segments, settings)
    return segments


# ---------------------------------------------------------------------------
# Montreal Forced Aligner
# ---------------------------------------------------------------------------

def _align_mfa(audio_path: Path, segments: list[Any], settings: Any) -> list[Any]:
    """Выравнивание через MFA: текст подаётся как транскрипт, назад — TextGrid."""
    try:
        from textgrid import TextGrid  # type: ignore
    except ModuleNotFoundError as exc:
        raise DependencyMissing("alignment", "textgrid", cause=exc).with_hint(
            "Установите разбор TextGrid: pip install textgrid") from exc

    transcript = " ".join(s.text for s in segments).strip()
    if not transcript:
        return segments

    dictionary = str(settings.get("alignment_dictionary") or "russian_mfa")
    acoustic = str(settings.get("alignment_acoustic_model") or "russian_mfa")
    beam = int(settings.get("alignment_retry_beam") or 80)
    jobs = int(settings.get("alignment_jobs") or 4)
    timeout = int(settings.get("alignment_timeout_s") or 900)

    with tempfile.TemporaryDirectory(prefix="asrhub-mfa-") as tmp:
        work = Path(tmp)
        corpus = work / "corpus"
        output = work / "result"
        corpus.mkdir(parents=True)
        output.mkdir(parents=True)

        # MFA ждёт пару файлов с одинаковым именем: звук и его расшифровку.
        shutil.copy(audio_path, corpus / "utt.wav")
        (corpus / "utt.txt").write_text(_clean_for_mfa(transcript), encoding="utf-8")

        command = ["mfa", "align", str(corpus), dictionary, acoustic, str(output),
                   "--clean", "--single_speaker",
                   "--retry_beam", str(beam), "--num_jobs", str(jobs)]
        log.debug("MFA: %s", " ".join(command))
        try:
            result = subprocess.run(command, check=False, capture_output=True,
                                    text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise BinaryMissing("mfa", "bash scripts/models.sh install-engine mfa") from exc
        except subprocess.TimeoutExpired as exc:
            raise ASRHubError(
                f"Выравнивание MFA не уложилось в {timeout} с.",
                hint="Увеличьте alignment_timeout_s или разбейте запись на части.") from exc

        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            raise ASRHubError(
                "MFA не смог выровнять запись: " + " ".join(tail),
                hint="Чаще всего причина — не загружены модель и словарь: "
                     f"mfa model download acoustic {acoustic} и "
                     f"mfa model download dictionary {dictionary}")

        grid_path = output / "utt.TextGrid"
        if not grid_path.exists():
            raise ASRHubError(
                "MFA завершился без результата: файл TextGrid не создан.",
                hint="Проверьте, что язык записи совпадает со словарём "
                     f"«{dictionary}».")

        words = _words_from_textgrid(TextGrid.fromFile(str(grid_path)))

    if not words:
        return segments
    return _redistribute(segments, words, settings)


def _clean_for_mfa(text: str) -> str:
    """Убирает знаки препинания: словарь MFA их не знает."""
    cleaned = "".join(" " if ch in _STRIP else ch for ch in text)
    return " ".join(cleaned.split()).lower()


def _words_from_textgrid(grid: Any) -> list[dict[str, Any]]:
    """Достаёт пословные интервалы из первого непустого яруса TextGrid."""
    for tier in getattr(grid, "tiers", []):
        intervals = getattr(tier, "intervals", None)
        if not intervals:
            continue
        words = [{"word": interval.mark.strip(),
                  "start": float(interval.minTime),
                  "end": float(interval.maxTime)}
                 for interval in intervals if interval.mark.strip()]
        if words:
            return words
    return []


# ---------------------------------------------------------------------------
# WhisperX
# ---------------------------------------------------------------------------

def _align_whisperx(audio_path: Path, segments: list[Any], settings: Any) -> list[Any]:
    """Выравнивание моделью wav2vec2 через whisperx."""
    import whisperx  # type: ignore

    device = str(settings.get("device") or "auto")
    if device == "auto":
        try:
            import torch  # type: ignore
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ModuleNotFoundError:
            device = "cpu"
    language = str(settings.get("language") or "ru")
    if language in ("auto", ""):
        language = next((s.language for s in segments if getattr(s, "language", None)), "ru")

    model, meta = whisperx.load_align_model(language_code=language, device=device)
    payload = [{"start": float(s.start), "end": float(s.end), "text": s.text}
               for s in segments]
    audio = whisperx.load_audio(str(audio_path))
    aligned = whisperx.align(payload, model, meta, audio, device,
                             return_char_alignments=False)

    words: list[dict[str, Any]] = []
    for item in aligned.get("segments", []):
        for word in item.get("words", []):
            if word.get("start") is None or word.get("end") is None:
                continue
            words.append({"word": str(word.get("word", "")).strip(),
                          "start": float(word["start"]),
                          "end": float(word["end"]),
                          "score": word.get("score")})
    if not words:
        return segments
    return _redistribute(segments, words, settings)


# ---------------------------------------------------------------------------
# Раскладка выровненных слов обратно по сегментам
# ---------------------------------------------------------------------------

def _redistribute(segments: list[Any], words: list[dict[str, Any]],
                  settings: Any) -> list[Any]:
    """Раздаёт выровненные слова по сегментам, сохраняя исходный текст.

    Выравниватель работает с очищенным текстом в нижнем регистре, поэтому его
    результат нельзя просто подставить вместо расшифровки: пропадут знаки
    препинания и заглавные буквы. Вместо этого берутся только границы, а слова
    остаются исходными — сопоставление идёт по порядку.
    """
    keep_text = bool(settings.get("alignment_keep_text", True))
    cursor = 0
    aligned_segments = []

    for segment in segments:
        source_words = segment.text.split()
        # Раскладка по счёту слов верна только пока выравниватель вернул
        # ровно столько же слов, сколько их в исходном тексте. Это не
        # гарантировано: MFA отбрасывает дефисы при подготовке текста
        # («из-за» становится двумя словами), а WhisperX выбрасывает слова,
        # для которых не нашёл границ. Расхождение в одно слово сдвигало
        # границы всех последующих сегментов, и сдвиг накапливался до конца
        # записи — субтитры разъезжались на секунды и минуты. Поэтому
        # опираемся на содержимое: ищем, где кончается этот сегмент.
        take = _match_length(source_words, words, cursor)
        chunk = words[cursor:cursor + take]
        cursor += take
        if not chunk:
            aligned_segments.append(segment)
            continue

        segment.words = [
            {
                "word": source_words[index] if keep_text and index < len(source_words)
                        else item["word"],
                "start": round(item["start"], 3),
                "end": round(item["end"], 3),
                **({"score": round(float(item["score"]), 4)}
                   if item.get("score") is not None else {}),
            }
            for index, item in enumerate(chunk)
        ]
        # Границы сегмента подтягиваем к фактическим границам его слов.
        segment.start = round(chunk[0]["start"], 3)
        segment.end = round(chunk[-1]["end"], 3)
        aligned_segments.append(segment)

    if cursor < len(words):
        log.debug("Выравнивание: осталось нераспределённых слов — %d",
                  len(words) - cursor)
    return _merge_close(aligned_segments, S.num(settings, "alignment_max_gap_s", 0.0))


def _normalize(word: str) -> str:
    """Слово в виде, пригодном для сравнения с выводом выравнивателя."""
    cleaned = "".join(ch for ch in word.lower() if ch.isalnum())
    return cleaned


def _match_length(source_words: list[str], aligned: list[dict[str, Any]],
                  cursor: int) -> int:
    """Сколько выровненных слов приходится на этот сегмент.

    Считаем по содержимому, а не по счёту. Два случая, из-за которых счёт
    расходится:

    * выравниватель разбил слово — MFA чистит текст и «из-за» превращается
      в «из» + «за»;
    * выравниватель слово пропустил — WhisperX выбрасывает те, для которых
      не нашёл границ.

    В первом случае забираем столько слов, сколько нужно, чтобы собрать
    исходное целиком. Во втором — не забираем ничего: если текущее
    выровненное слово подходит следующему слову сегмента, значит нынешнее
    просто потеряно, и отдавать ему чужую границу нельзя.
    """
    if not source_words:
        return 0
    remaining = len(aligned) - cursor
    if remaining <= 0:
        return 0

    taken = 0
    for index, raw in enumerate(source_words):
        target = _normalize(raw)
        if not target or cursor + taken >= len(aligned):
            continue

        # Собираем выровненные слова, пока не покроем исходное целиком.
        collected = ""
        used = 0
        while cursor + taken + used < len(aligned) and used < 8:
            piece = _normalize(str(aligned[cursor + taken + used].get("word", "")))
            if not piece:
                used += 1
                continue
            if not target.startswith(collected + piece):
                break
            collected += piece
            used += 1
            if collected == target:
                break
        if collected == target and used:
            taken += used
            continue

        # Совпадения нет. Смотрим вперёд: если текущее выровненное слово
        # подходит одному из ближайших следующих слов сегмента, значит это
        # слово выравниватель потерял — ничего не забираем.
        current = _normalize(str(aligned[cursor + taken].get("word", "")))
        lookahead = [_normalize(w) for w in source_words[index + 1:index + 4]]
        if current and any(nxt and nxt.startswith(current) for nxt in lookahead):
            continue

        # Иначе считаем, что выравниватель дал другое слово вместо этого:
        # берём одно и идём дальше.
        taken += 1

    return max(0, min(taken, remaining))


def _count_words(text: str) -> int:
    return len(text.split())


def _merge_close(segments: list[Any], max_gap: float) -> list[Any]:
    """Склеивает соседние сегменты, если пауза между ними короче max_gap.

    После выравнивания границы становятся точными, и запись нередко распадается
    на множество коротких кусков — по фразе на вдох. Ноль отключает склейку.
    """
    if max_gap <= 0 or len(segments) < 2:
        return segments

    merged = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        same_speaker = (previous.speaker or None) == (segment.speaker or None)
        if same_speaker and segment.start - previous.end <= max_gap:
            previous.text = f"{previous.text} {segment.text}".strip()
            previous.end = segment.end
            previous.words = list(previous.words) + list(segment.words)
            if previous.confidence is not None and segment.confidence is not None:
                previous.confidence = (previous.confidence + segment.confidence) / 2
        else:
            merged.append(segment)
    return merged
