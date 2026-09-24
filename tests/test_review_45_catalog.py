"""Заход 45: каталог параметров, пресеты и справочник метрик.

Сплошной обход каталога нашёл то, что по одному не видно: ссылки «см.
также» на несуществующие параметры, пример, который не проходит
собственную проверку, умолчания мимо шага ползунка, пресеты GigaAM с
параметрами Whisper и описания метрик, расходящиеся с расчётом.
"""
from __future__ import annotations

from asrhub import catalog
from asrhub.catalog import params as P
from asrhub.catalog import presets as PR
from asrhub.monitoring.catalog import METRICS_BY_NAME
from asrhub.monitoring.collector import СТАДИИ


def test_ссылки_каталога_ведут_на_существующие_параметры():
    ключи = {п.key for п in P.PARAMS}
    плохие = [(п.key, куда) for п in P.PARAMS for куда in п.see_also if куда not in ключи]
    плохие += [(п.key, куда) for п in P.PARAMS for куда in п.requires if куда not in ключи]
    assert not плохие, плохие


def test_каждый_пример_проходит_свою_проверку():
    """Пример «Строкой» у контекстов диалплана не проходил собственную проверку."""
    плохие = []
    for п in P.PARAMS:
        for пример in п.examples:
            ok, причина = P.validate_value(п.key, P.coerce_value(п.key, пример.value))
            if not ok:
                плохие.append((п.key, пример.title, причина))
    assert not плохие, плохие


def test_контексты_строкой_и_проверка_направлений():
    значение = P.coerce_value("telephony_contexts", "from-pstn=входящий, outbound=исходящий")
    assert значение == {"from-pstn": "входящий", "outbound": "исходящий"}
    assert list(значение) == ["from-pstn", "outbound"], "порядок правил важен"
    ok, причина = P.validate_value("telephony_contexts", {"from-trunk": "наружу"})
    assert not ok and "наружу" in причина


def test_умолчания_и_примеры_на_шаге_ползунка():
    """Ползунок с шагом 50 от единицы не может встать на 1000 — умолчание."""
    плохие = []
    for п in P.PARAMS:
        if п.type not in ("int", "float") or п.step is None or п.minimum is None:
            continue
        for откуда, значение in [("умолчание", п.default)] + [
                (пример.title, пример.value) for пример in п.examples]:
            if isinstance(значение, bool) or not isinstance(значение, (int, float)):
                continue
            доля = (значение - п.minimum) / п.step
            if abs(доля - round(доля)) > 1e-6:
                плохие.append((п.key, откуда, значение, п.minimum, п.step))
    assert not плохие, плохие


def test_пресет_задаёт_только_то_что_движок_читает():
    """«Широкий луч» у GigaAM был обещанием: параметр читают Whisper и NeMo."""
    плохие = []
    for пресет in PR.PRESETS:
        движок = пресет.values.get("engine")
        for ключ in пресет.values:
            spec = P.PARAMS_BY_KEY.get(ключ)
            assert spec is not None, (пресет.id, ключ)
            if spec.engines and движок not in spec.engines:
                плохие.append((пресет.id, ключ, движок))
    assert not плохие, плохие
    for пресет in PR.PRESETS:
        if пресет.values.get("engine") == "gigaam":
            assert "луч" not in пресет.description and "жадн" not in пресет.description


def test_умолчания_моделей_в_словаре_каталога():
    """У Canary стояло `task: asr` — слово движка, а не значение параметра."""
    плохие = []
    for модель in catalog.MODELS:
        for ключ, значение in (модель.default_params or {}).items():
            if ключ in P.PARAMS_BY_KEY:
                ok, причина = P.validate_value(ключ, P.coerce_value(ключ, значение))
                if not ok:
                    плохие.append((модель.id, ключ, причина))
    assert not плохие, плохие


def test_описания_метрик_совпадают_с_расчётом():
    доля = METRICS_BY_NAME["asrhub_low_confidence_share"]
    assert "задани" in доля.label.lower() and "0,75" in доля.description
    стадии = METRICS_BY_NAME["asrhub_stage_seconds"].description
    for стадия in СТАДИИ:
        assert стадия in стадии, стадия
    for имя in ("asrhub_content_compliance_avg", "asrhub_content_agent_score_avg"):
        assert "content_script" in METRICS_BY_NAME[имя].description


def test_экспорт_метрик_описан_так_как_работает():
    описание = P.PARAMS_BY_KEY["metrics_enabled"].description
    assert "/api/monitoring/metrics" in описание and "404" in описание
