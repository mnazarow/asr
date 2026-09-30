"""Разделение записи по говорящим.

Порядок выбора: встроенная в модель разметка → pyannote → NVIDIA Sortformer →
простая кластеризация по паузам. Последний вариант работает без зависимостей
и даёт грубое, но полезное приближение для диалогов двух человек.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..engines.base import Segment, device_for
from ..errors import DependencyMissing, GatedModelError
from ..logging_setup import get_logger
from ..monitoring.collector import RUNTIME
from . import model_cache

log = get_logger("diarization")


def diarize_segments(audio_path: Path, segments: list[Segment],
                     settings: dict[str, Any],
                     warnings: list[str] | None = None) -> list[Segment]:
    """Расставляет говорящих. Возвращает те же сегменты с полем speaker.

    Если выбранный механизм не сработал, применяется разбивка по паузам —
    но об этом обязательно сообщается: в журнал и в список предупреждений
    задания. Раньше подмена была молчаливой, и в протоколе совещания реплики,
    расставленные по паузе в 1,2 секунды, выглядели ровно так же, как
    настоящая диаризация, — отличить их было нельзя ни в интерфейсе, ни в
    выгрузке.
    """
    backend = str(settings.get("diarization_backend") or "auto")
    order = {
        "auto": ["pyannote", "sortformer", "pauses"],
        "pyannote": ["pyannote", "pauses"],
        "sortformer": ["sortformer", "pauses"],
        "channels": ["pauses"],
        "builtin": ["pauses"],
    }.get(backend, ["pauses"])

    def fallback(reason: str) -> list[Segment]:
        if reason:
            note = (f"Диаризация «{backend}» не выполнена ({reason}). "
                    "Говорящие расставлены по паузам — это грубая оценка, "
                    "а не разделение по голосам.")
            log.warning("%s", note)
            if warnings is not None:
                warnings.append(note)
            RUNTIME.inc("asrhub_errors_total", labels={"code": "diarization_fallback",
                                                       "retryable": "true"})
        return _by_pauses(segments, settings)

    # «По каналам» и «встроенная в модель» своего механизма здесь не имеют:
    # говорящих расставляют раздельная обработка каналов и сама модель. Раз
    # дошли сюда без говорящих — этого не случилось, и разбивка по паузам
    # идёт ВМЕСТО выбранного, а не как выбранное. Раньше она шла молча, и
    # протокол, расставленный по паузам, выглядел как разделение по каналам.
    failure = {
        "channels": "каналов для разделения нет: запись одноканальная или "
                    "в «Сведении каналов» выбран один канал",
        "builtin": f"модель «{settings.get('model') or '?'}» не расставляет "
                   "говорящих сама",
    }.get(backend, "")
    for name in order:
        try:
            if name == "pyannote":
                turns = _pyannote(audio_path, settings)
            elif name == "sortformer":
                turns = _sortformer(audio_path, settings)
            else:
                # Дошли до разбивки по паузам: причина — отказ механизмов
                # выше или, для «channels» и «builtin», то, что говорящих
                # не расставили ни каналы, ни модель.
                return fallback(failure)
        except (DependencyMissing, GatedModelError):
            if backend not in ("auto",):
                raise
            continue
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            log.warning("Диаризация «%s» не удалась: %s", name, exc)
            continue
        if turns:
            return _assign(segments, turns,
                           пословно=bool(settings.get("diarization_assign_words", True)))
        failure = failure or "механизм не нашёл ни одного говорящего"
    return fallback(failure)


def _pyannote(audio_path: Path, settings: dict[str, Any]) -> list[tuple[float, float, str]]:
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ModuleNotFoundError as exc:
        raise DependencyMissing("pyannote", "pyannote.audio", cause=exc) from exc

    token = str(settings.get("hf_token") or os.environ.get("HF_TOKEN", "")
                or os.environ.get("HUGGING_FACE_HUB_TOKEN", ""))
    if not token:
        raise GatedModelError("pyannote/speaker-diarization-community-1",
                              "https://huggingface.co/pyannote/speaker-diarization-community-1")

    model_name = str(settings.get("diarization_model")
                     or "pyannote/speaker-diarization-community-1")
    # «auto» — это видеокарта, если она есть. Раньше на карту переносили
    # только при явном «cuda», а умолчание «auto» оставляло pyannote считать
    # на процессоре — в разы дольше самого распознавания.
    device = device_for(settings)

    def загрузить() -> Any:
        pipeline = _из_хаба(Pipeline, model_name, token)
        if pipeline is None:
            raise GatedModelError(model_name, f"https://huggingface.co/{model_name}")
        if device.startswith("cuda"):
            try:
                import torch  # type: ignore

                pipeline.to(torch.device(device))
            except Exception as exc:                        # noqa: BLE001
                log.warning("Диаризация остаётся на процессоре: %s", exc)
        return pipeline

    # Конвейер грузится один раз и живёт в кеше, как модели распознавания:
    # раньше — заново на каждое задание, секунды и гигабайт видеопамяти.
    # В ключе класс конвейера: у 3.x и 4.x они разные.
    запись = model_cache.взять(("pyannote", id(Pipeline), model_name, device), загрузить)

    kwargs: dict[str, Any] = {}
    num = S.integer(settings, "diarization_num_speakers", 0)
    if num:
        kwargs["num_speakers"] = num
    else:
        kwargs["min_speakers"] = int(settings.get("diarization_min_speakers") or 1)
        kwargs["max_speakers"] = int(settings.get("diarization_max_speakers") or 8)

    with запись.замок:
        итог = запись.модель(str(audio_path), **kwargs)
    # pyannote.audio 4 отдаёт не разметку, а набор разметок. Для расшифровки
    # нужна «исключающая» — без наложенных реплик: каждому слову один
    # говорящий. В 3.x ответ — сама разметка.
    annotation = (getattr(итог, "exclusive_speaker_diarization", None)
                  or getattr(итог, "speaker_diarization", None) or итог)
    return [(float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)]


def _из_хаба(Pipeline: Any, model_name: str, token: str) -> Any:
    """`Pipeline.from_pretrained` с токеном — под обе ветки pyannote.audio.

    В 4.x параметр называется `token`, а `use_auth_token` убран: вызов
    падал с TypeError, и диаризация через pyannote не работала никогда —
    конвейер уходил к Sortformer или к разбивке по паузам. Ставится же по
    требованию «pyannote.audio>=3.1» именно 4.x, а модель по умолчанию
    speaker-diarization-community-1 без неё и не грузится. В 3.x — наоборот.
    """
    import inspect  # noqa: PLC0415

    try:
        параметры = inspect.signature(Pipeline.from_pretrained).parameters
    except (TypeError, ValueError):
        параметры = {}
    if "token" in параметры or not параметры:
        try:
            return Pipeline.from_pretrained(model_name, token=token)
        except TypeError as exc:
            if "token" not in str(exc):
                raise
    return Pipeline.from_pretrained(model_name, use_auth_token=token)


def _sortformer(audio_path: Path, settings: dict[str, Any]) -> list[tuple[float, float, str]]:
    try:
        from nemo.collections.asr.models import SortformerEncLabelModel  # type: ignore
    except ModuleNotFoundError as exc:
        raise DependencyMissing("nemo", "nemo-toolkit-asr", cause=exc) from exc

    имя = str(settings.get("diarization_model") or "nvidia/diar_streaming_sortformer_4spk-v2")

    def загрузить() -> Any:
        model = SortformerEncLabelModel.from_pretrained(имя)
        model.eval()
        return model

    запись = model_cache.взять(("sortformer", id(SortformerEncLabelModel), имя), загрузить)
    with запись.замок:
        predictions = запись.модель.diarize(audio=str(audio_path), batch_size=1)
    turns: list[tuple[float, float, str]] = []
    for item in predictions or []:
        for entry in (item if isinstance(item, (list, tuple)) else [item]):
            parts = str(entry).split()
            if len(parts) >= 3:
                try:
                    turns.append((float(parts[0]), float(parts[1]), parts[2]))
                except ValueError:
                    continue
    return turns


def _assign(segments: list[Segment], turns: list[tuple[float, float, str]], *,
            пословно: bool = True) -> list[Segment]:
    """Присваивает сегменту говорящего с максимальным перекрытием по времени.

    `пословно` (параметр `diarization_assign_words`) — говорящий ещё и у
    каждого слова, а реплика, внутри которой он сменился, режется на части
    (см. `разделить_по_словам`). Раньше слова размечались всегда, а реплика
    не резалась никогда: перебившего собеседника реплика отдавала целиком
    тому, кто говорил дольше, и параметр ничего не менял.
    """
    mapping: dict[str, str] = {}

    def имя(label: str) -> str:
        # Слову может достаться говорящий, которому не досталось ни одной
        # реплики целиком. Раньше у такого слова оставалась сырая метка
        # движка («SPEAKER_02») рядом с «Говорящий 1» у реплик.
        if label not in mapping:
            mapping[label] = f"Говорящий {len(mapping) + 1}"
        return mapping[label]

    for segment in segments:
        best_label, best_overlap = None, 0.0
        for start, end, label in turns:
            overlap = min(segment.end, end) - max(segment.start, start)
            if overlap > best_overlap:
                best_overlap, best_label = overlap, label
        if best_label is not None:
            segment.speaker = имя(best_label)
        if not пословно:
            continue
        for word in segment.words:
            wstart = float(word.get("start", segment.start))
            for start, end, label in turns:
                if start <= wstart < end:
                    word["speaker"] = имя(label)
                    break
    if not пословно:
        return segments
    return _по_порядку(разделить_по_словам(segments))


def _по_порядку(segments: list[Segment]) -> list[Segment]:
    """Номера говорящих — по порядку первого появления в записи.

    Имена раздаются по ходу разметки, а пословная идёт после реплик: тот,
    кто только перебивал, получал номер после всех, даже заговорив первым.
    """
    номера: dict[str, str] = {}
    for segment in sorted(segments, key=lambda s: s.start):
        for метка in [segment.speaker, *(с.get("speaker") for с in segment.words or [])]:
            if метка and метка not in номера:
                номера[метка] = f"Говорящий {len(номера) + 1}"
    for segment in segments:
        if segment.speaker:
            segment.speaker = номера.get(segment.speaker, segment.speaker)
        for слово in segment.words or []:
            if слово.get("speaker"):
                слово["speaker"] = номера.get(слово["speaker"], слово["speaker"])
    return segments


#: Меньше этого — не смена говорящего, а шум на границе реплик: разметка
#: голосов на стыке двух реплик уверенно ошибается на слово-другое, и
#: резать по каждому такому слову значило бы плодить обрывки.
МИН_СЛОВ_ЧАСТИ = 2
МИН_СЕКУНД_ЧАСТИ = 0.6


def разделить_по_словам(segments: list[Segment]) -> list[Segment]:
    """Режет реплику там, где по словам сменился говорящий.

    Реплика движка — отрезок звука между паузами, а не высказывание одного
    человека: перебивание, короткое «да-да» собеседника, ответ без паузы
    попадают в неё целиком. Когда говорящий известен у каждого слова,
    реплика делится на части по сменам — у каждой свой говорящий, свои
    слова и время. Короткие пробеги (меньше двух слов и 0,6 секунды)
    приклеиваются к соседям. Реплики без пословной разметки не трогаются.
    """
    итог: list[Segment] = []
    for segment in segments:
        слова = list(segment.words or [])
        if len(слова) < 2 or not all(с.get("speaker") for с in слова):
            итог.append(segment)
            continue
        части = _пробеги(слова)
        if len(части) < 2:
            if segment.speaker != части[0][0]:
                segment.speaker = части[0][0]
            итог.append(segment)
            continue
        for говорящий, свои in части:
            уверенность = [float(с["confidence"]) for с in свои
                           if isinstance(с.get("confidence"), (int, float))]
            итог.append(Segment(
                start=float(свои[0].get("start", segment.start)),
                end=float(свои[-1].get("end", segment.end)),
                text=" ".join(str(с.get("word") or "").strip() for с in свои).strip(),
                speaker=говорящий,
                confidence=(round(sum(уверенность) / len(уверенность), 4)
                            if уверенность else segment.confidence),
                no_speech_prob=segment.no_speech_prob,
                compression_ratio=segment.compression_ratio,
                temperature=segment.temperature,
                language=segment.language,
                words=свои))
    return итог


def _пробеги(слова: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Подряд идущие слова одного говорящего; короткие — к соседу."""
    части: list[tuple[str, list[dict[str, Any]]]] = []
    for слово in слова:
        if части and части[-1][0] == слово["speaker"]:
            части[-1][1].append(слово)
        else:
            части.append((str(слово["speaker"]), [слово]))

    def короткий(свои: list[dict[str, Any]]) -> bool:
        длина = float(свои[-1].get("end", 0) or 0) - float(свои[0].get("start", 0) or 0)
        return len(свои) < МИН_СЛОВ_ЧАСТИ and длина < МИН_СЕКУНД_ЧАСТИ

    # Короткий пробег уходит к соседу — предыдущему, а у первого к
    # следующему. Говорящего части это не меняет: он — у длинного соседа.
    изменилось = True
    while изменилось and len(части) > 1:
        изменилось = False
        for индекс, (_, свои) in enumerate(части):
            if not короткий(свои):
                continue
            if индекс > 0:
                части[индекс - 1][1].extend(свои)
            else:
                части[1] = (части[1][0], свои + части[1][1])
            del части[индекс]
            изменилось = True
            break
        # Соседи одного говорящего после склейки — одна часть.
        склеенные: list[tuple[str, list[dict[str, Any]]]] = []
        for говорящий, свои in части:
            if склеенные and склеенные[-1][0] == говорящий:
                склеенные[-1][1].extend(свои)
            else:
                склеенные.append((говорящий, свои))
        части = склеенные
    return части


def _by_pauses(segments: list[Segment], settings: dict[str, Any]) -> list[Segment]:
    """Грубое разделение по длинным паузам между репликами.

    Не заменяет настоящую диаризацию, но на поочерёдном диалоге двух человек
    даёт разумный результат и не требует ни одной зависимости.
    """
    speakers = max(2, S.integer(settings, "diarization_num_speakers", 2))
    gap_threshold = float(settings.get("diarization_pause_s") or 1.2)
    current = 0
    previous_end = None
    for segment in segments:
        if previous_end is not None and segment.start - previous_end >= gap_threshold:
            current = (current + 1) % speakers
        segment.speaker = f"Говорящий {current + 1}"
        previous_end = segment.end
    log.info("Использовано приближённое разделение по паузам (%d говорящих)", speakers)
    return segments
