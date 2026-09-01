"""Мониторинг ASR Hub: сбор, выгрузка, оповещения, пробы, отправка наружу.

Собран отдельным пакетом, а не добавлен в аналитику, потому что решает другую
задачу. Аналитика отвечает на вопрос «как мы поработали за неделю» и живёт в
базе; мониторинг отвечает на вопрос «что происходит прямо сейчас» и должен
отдавать ответ за доли секунды, не нагружая базу тяжёлыми запросами.
"""
from .alerts import AlertEngine, AlertState, Rule, default_rules
from .catalog import GROUPS, METRICS, METRICS_BY_NAME, MetricSpec, Threshold, stats
from .collector import RUNTIME, Collector, Runtime, Sample
from .probes import liveness, overall, readiness, startup
from .pushers import KINDS, PushManager, Target
from .service import MonitoringService

__all__ = [
    "AlertEngine", "AlertState", "Rule", "default_rules",
    "GROUPS", "METRICS", "METRICS_BY_NAME", "MetricSpec", "Threshold", "stats",
    "RUNTIME", "Collector", "Runtime", "Sample",
    "liveness", "overall", "readiness", "startup",
    "KINDS", "PushManager", "Target",
    "MonitoringService",
]
