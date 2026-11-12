"""Состояния задания очереди — одним местом.

Вынесено из `job_queue.py`: состояния нужны и уведомлениям, и
обслуживанию, и отчётам; `job_queue` по-прежнему отдаёт эти имена.
"""
from __future__ import annotations

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"
STATUS_RETRY = "retry"
#: Запись обработана, а слов в ней нет: молчит, или модель не нашла ни
#: одного. Итог, а не отказ — в ошибки и в долю отказов не идёт.
STATUS_NO_SPEECH = "no_speech"
#: Запись короче порога `audio_min_duration_s` (или пустая) — не
#: распознаётся вовсе. Тоже итог, а не отказ.
STATUS_SHORT = "short"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY, STATUS_PAUSED)
#: Итоги без расшифровки — отдельные категории, не ошибки.
EMPTY_STATUSES = (STATUS_NO_SPEECH, STATUS_SHORT)
#: Все конечные состояния: дальше задание само никуда не двинется.
FINISHED_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, *EMPTY_STATUSES)
