"""Заход 37: то, что осталось после обзора рынка, и разошедшиеся зависимости.

Проверки здесь ходят по поведению, а не по тексту исходника: мутация в коде
обязана ронять проверку. Там, где настоящая модель недоступна (сеть ONNX,
токенизатор), подставляется заглушка, повторяющая её договор, — но считает по
ней всё равно рабочий код.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import time
import types
from pathlib import Path

import numpy as np
import pytest
from asrhub import audit
from asrhub.content import speech
from asrhub.db import Database
from asrhub.pipeline import postprocess

# В пакете `asrhub.content` имя `analyze` занято функцией, а нужен модуль.
content_analyze = importlib.import_module("asrhub.content.analyze")

КОРЕНЬ = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
нужен_bash = pytest.mark.skipif(BASH is None, reason="нужен bash")


# --------------------------------------------------------------------------
# Пунктуация ONNX без optimum
# --------------------------------------------------------------------------

class _Кодировка(dict):
    """То, что возвращает быстрый токенизатор: входы сети плюс word_ids()."""

    def __init__(self, входы: dict[str, np.ndarray], номера: list[int | None]):
        super().__init__(входы)
        self._номера = list(номера)

    def word_ids(self, номер: int = 0) -> list[int | None]:
        return list(self._номера)


class _Сессия:
    """Заглушка onnxruntime: отдаёт заранее заданные логиты и помнит входы."""

    def __init__(self, логиты: np.ndarray, входы: tuple[str, ...] = ("input_ids",)):
        self._логиты = логиты
        self._входы = входы
        self.полученные: dict[str, np.ndarray] = {}

    def get_inputs(self):
        return [types.SimpleNamespace(name=имя) for имя in self._входы]

    def run(self, выходы, входы):
        self.полученные = dict(входы)
        return [np.asarray([self._логиты])]


МЕТКИ = {0: "O", 1: "UPPER_PERIOD", 2: "LOWER_COMMA", 3: "UPPER_TOTAL_O"}


def _токенизатор(номера: list[int | None], длина: int):
    """Токенизатор, который всегда даёт одну и ту же разбивку на частицы."""
    def вызов(слова, **_):
        входы = {"input_ids": np.zeros((1, длина), dtype=np.int64)}
        return _Кодировка(входы, номера)
    return вызов


def test_метка_слова_берётся_у_первой_частицы():
    """Слово из двух частиц размечается по первой, вторая не влияет.

    Это договор `aggregation_strategy="first"` у transformers. Если брать
    последнюю или максимум, «привет» получит запятую вместо точки.
    """
    # Частицы: [CLS] прив ет мир [SEP] — слово 0 разбито надвое.
    номера = [None, 0, 0, 1, None]
    логиты = np.zeros((5, 4))
    логиты[1, 1] = 5.0   # первая частица слова 0 — UPPER_PERIOD
    логиты[2, 2] = 9.0   # вторая частица того же слова — LOWER_COMMA, не в счёт
    логиты[3, 0] = 5.0   # слово 1 — без знака
    труба = postprocess._ТрубаONNX(_токенизатор(номера, 5), _Сессия(логиты), МЕТКИ)

    assert труба("привет мир") == [
        {"word": "привет", "entity_group": "UPPER_PERIOD"},
        {"word": "мир", "entity_group": "O"},
    ]
    assert postprocess._apply_rupunct(труба, "привет мир") == "Привет. мир"


def test_хвост_за_пределами_окна_возвращается_целым():
    """Слова, не поместившиеся в 512 позиций, выходят без знаков, но целыми.

    Токенизатор здесь «видит» только первое слово. Молча потерять остальные —
    это исчезнувший хвост реплики, а он заметнее любой пропущенной запятой.
    """
    номера = [None, 0, None]
    логиты = np.zeros((3, 4))
    логиты[1, 1] = 5.0
    труба = postprocess._ТрубаONNX(_токенизатор(номера, 3), _Сессия(логиты), МЕТКИ)

    размечено = труба("раз два три")
    assert [з["word"] for з in размечено] == ["раз", "два", "три"]
    assert [з["entity_group"] for з in размечено] == ["UPPER_PERIOD", "O", "O"]


def test_длинный_текст_идёт_окнами_и_не_теряет_слов():
    """Больше ОКНО_СЛОВ слов — несколько прогонов, порядок и состав целы."""
    всего = postprocess.ОКНО_СЛОВ * 2 + 7
    слова = [f"с{н}" for н in range(всего)]
    номера = [None] + list(range(postprocess.ОКНО_СЛОВ)) + [None]
    логиты = np.zeros((postprocess.ОКНО_СЛОВ + 2, 4))
    сессия = _Сессия(логиты)
    труба = postprocess._ТрубаONNX(
        _токенизатор(номера, postprocess.ОКНО_СЛОВ + 2), сессия, МЕТКИ)

    размечено = труба(" ".join(слова))
    assert [з["word"] for з in размечено] == слова


def test_недостающие_входы_сети_заполняются_нулями():
    """Сеть из BERT просит token_type_ids, которых токенизатор не дал."""
    номера = [None, 0, None]
    логиты = np.zeros((3, 4))
    сессия = _Сессия(логиты, входы=("input_ids", "token_type_ids", "attention_mask"))
    труба = postprocess._ТрубаONNX(_токенизатор(номера, 3), сессия, МЕТКИ)

    труба("раз")
    assert set(сессия.полученные) >= {"input_ids", "token_type_ids"}
    assert сессия.полученные["token_type_ids"].shape == (1, 3)
    assert not сессия.полученные["token_type_ids"].any()


def test_медленный_токенизатор_отвергается_с_объяснением():
    """Без word_ids() метку не отнести к слову — сказать прямо, а не гадать."""
    class МедленныйТокенизатор:
        def __call__(self, слова, **_):
            return {"input_ids": np.zeros((1, 3), dtype=np.int64)}

    труба = postprocess._ТрубаONNX(МедленныйТокенизатор(), _Сессия(np.zeros((3, 4))),
                                   МЕТКИ)
    with pytest.raises(RuntimeError, match="word_ids"):
        труба("раз два")


def test_пустой_текст_не_идёт_в_сеть():
    сессия = _Сессия(np.zeros((3, 4)))
    труба = postprocess._ТрубаONNX(_токенизатор([None], 1), сессия, МЕТКИ)
    assert труба("   ") == []
    assert сессия.полученные == {}


# --------------------------------------------------------------------------
# Откуда берётся файл сети
# --------------------------------------------------------------------------

def test_квантованная_сеть_предпочитается_обычной(tmp_path: Path):
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"0")
    (tmp_path / "onnx" / "model_quantized.onnx").write_bytes(b"0")
    assert postprocess._файл_onnx(str(tmp_path)).endswith("model_quantized.onnx")


def test_путь_прямо_к_файлу_принимается(tmp_path: Path):
    файл = tmp_path / "своя.onnx"
    файл.write_bytes(b"0")
    assert postprocess._файл_onnx(str(файл)) == str(файл)


def test_папка_без_onnx_объясняет_чего_не_хватило(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="model.onnx"):
        postprocess._файл_onnx(str(tmp_path))


def test_любой_onnx_в_папке_лучше_отказа(tmp_path: Path):
    """Веса, выгруженные под другим именем, тоже годятся."""
    (tmp_path / "rupunct-int8.onnx").write_bytes(b"0")
    assert postprocess._файл_onnx(str(tmp_path)).endswith("rupunct-int8.onnx")


# --------------------------------------------------------------------------
# Зависимости
# --------------------------------------------------------------------------

def test_optimum_не_требуется_ни_одним_списком_зависимостей():
    """optimum-onnx прибит к transformers<4.58, а движкам нужен свежий.

    На рабочем сервере это разошлось в первый же день после обновления:
    «optimum-onnx 0.1.0 has requirement transformers<4.58.0, but you have
    transformers 5.17.0». Проверка держит дверь закрытой.
    """
    виноватые = []
    for файл in sorted((КОРЕНЬ / "requirements").rglob("*.txt")):
        for номер, строка in enumerate(файл.read_text(encoding="utf-8").splitlines(), 1):
            голая = строка.split("#", 1)[0].strip().lower()
            if голая.startswith("optimum"):
                виноватые.append(f"{файл.relative_to(КОРЕНЬ)}:{номер}: {строка.strip()}")
    assert not виноватые, "\n".join(виноватые)


def test_пунктуация_onnx_обходится_без_optimum():
    """Ни один import модуля не ведёт в optimum — разбором дерева, не поиском.

    Поиск подстроки прошёл бы и на закомментированном импорте, и споткнулся бы
    о слово в комментарии. Здесь перечисляются настоящие import-узлы, включая
    те, что спрятаны внутри функций.
    """
    import ast

    файл = КОРЕНЬ / "server" / "asrhub" / "pipeline" / "postprocess.py"
    дерево = ast.parse(файл.read_text(encoding="utf-8"))
    откуда: set[str] = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Import):
            откуда.update(имя.name for имя in узел.names)
        elif isinstance(узел, ast.ImportFrom) and узел.module:
            откуда.add(узел.module)
    корни = {имя.split(".", 1)[0] for имя in откуда}
    assert "optimum" not in корни, sorted(откуда)
    assert "onnxruntime" in корни


# --------------------------------------------------------------------------
# Обновление: лишние пакеты от прежней версии
# --------------------------------------------------------------------------


def _прогнать(script: str):
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          env={**os.environ, "ASRHUB_QUIET": "0"}, timeout=120)


def _поддельный_pip(куда: Path, зависимые: dict[str, str]) -> Path:
    """pip, который на `show ПАКЕТ` отвечает заданным «Required-by»."""
    строки = "\n".join(
        f'    {имя}) echo "Required-by: {кто}" ;;' for имя, кто in зависимые.items())
    pip = куда / "pip"
    pip.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "show" ]; then\n'
        '  case "$2" in\n'
        f"{строки}\n"
        '    *) echo "Required-by:" ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n", encoding="utf-8")
    pip.chmod(0o755)
    return pip


@нужен_bash
def test_имена_требований_нормализуются_как_у_pip(tmp_path: Path):
    """«optimum[onnxruntime]>=1.20» и «Nemo_Toolkit[asr]» — это optimum и nemo-toolkit."""
    (tmp_path / "a.txt").write_text(
        "# комментарий\noptimum[onnxruntime]>=1.20\nNemo_Toolkit[asr]\n\n",
        encoding="utf-8")
    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; required_packages "{tmp_path}"')
    assert итог.stdout.split() == ["nemo-toolkit", "optimum"], итог.stdout


@нужен_bash
def test_лишний_пакет_опознаётся(tmp_path: Path):
    """Ровно случай с рабочего сервера: optimum остался, требований на него нет."""
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("onnxruntime>=1.18\n", encoding="utf-8")
    pip = _поддельный_pip(tmp_path, {})
    жалоба = ("optimum-onnx 0.1.0 has requirement transformers<4.58.0,>=4.36, "
              "but you have transformers 5.17.0.")
    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'orphan_packages_in "{жалоба}" "{tmp_path}/req" "{pip}"')
    assert итог.stdout.strip() == "optimum-onnx"


@нужен_bash
def test_спутник_нужного_пакета_лишним_не_считается(tmp_path: Path):
    """«nemo-toolkit-asr» — это семья требуемого «nemo_toolkit», трогать нельзя."""
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("nemo_toolkit[asr]>=2.0\n", encoding="utf-8")
    pip = _поддельный_pip(tmp_path, {})
    жалоба = ("nemo-toolkit-asr 2.1 has requirement protobuf~=5.29.5, "
              "but you have protobuf 7.36.1.")
    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'orphan_packages_in "{жалоба}" "{tmp_path}/req" "{pip}"')
    assert итог.stdout.strip() == ""


@нужен_bash
def test_пакет_за_которым_кто_то_стоит_не_предлагается_к_сносу(tmp_path: Path):
    """tokenizers нигде не перечислен, но живёт как спутник transformers."""
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("transformers>=5\n", encoding="utf-8")
    pip = _поддельный_pip(tmp_path, {"tokenizers": "transformers"})
    жалоба = ("tokenizers 0.20 has requirement huggingface-hub<1.0, "
              "but you have huggingface-hub 1.2.0.")
    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'orphan_packages_in "{жалоба}" "{tmp_path}/req" "{pip}"')
    assert итог.stdout.strip() == ""


@нужен_bash
def test_сводка_о_зависимостях_доводит_подсказку_до_человека(tmp_path: Path):
    """Проверка связки: жалоба pip check доходит до команды удаления.

    Без этого подсказку можно написать и не позвать — чек-лист обновления
    останется ровно таким, каким он пришёл с рабочего сервера: «версии не
    сходятся», и ни слова о том, что с этим делать.
    """
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("onnxruntime>=1.18\n", encoding="utf-8")
    pip = tmp_path / "pip"
    pip.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "check" ]; then\n'
        '  echo "optimum-onnx 0.1.0 has requirement transformers<4.58.0,>=4.36,'
        ' but you have transformers 5.17.0."\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "show" ]; then echo "Required-by:"; fi\n'
        "exit 0\n", encoding="utf-8")
    pip.chmod(0o755)

    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'check_dependency_health "{pip}" "{tmp_path}/req"')
    вывод = итог.stdout + итог.stderr
    assert "не сходятся" in вывод
    assert "uninstall -y optimum-onnx" in вывод


# --------------------------------------------------------------------------
# Пороги паузы, тишины и перебивания
# --------------------------------------------------------------------------


РАЗГОВОР = [
    {"start": 0.0, "end": 2.0, "speaker": "A", "text": "здравствуйте"},
    # разрыв 2.5 с — пауза по умолчанию, но не длинная
    {"start": 4.5, "end": 6.0, "speaker": "B", "text": "да"},
    # разрыв 5.0 с — и пауза, и длинная, и заметная тишина
    {"start": 11.0, "end": 12.0, "speaker": "A", "text": "минуту"},
    # наложение 0.2 с — меньше порога перебивания
    {"start": 11.8, "end": 13.0, "speaker": "B", "text": "я жду"},
    # разрыв 1.2 с — по умолчанию это время ответа, а не пауза
    {"start": 14.2, "end": 15.0, "speaker": "A", "text": "готово"},
]


def test_длинные_паузы_считаются_по_своей_границе():
    """Обычных пауз две, длинных одна — счётчики не подменяют друг друга."""
    речь = speech.analyze(РАЗГОВОР, 15.0)
    assert речь["pauses"] == 2
    assert речь["long_pauses"] == 1
    assert речь["pause_threshold_s"] == 2.0
    assert речь["long_pause_threshold_s"] == 4.0


def test_пороги_из_настроек_меняют_счёт():
    """Опущенные границы дают больше пауз, перебиваний и мёртвого эфира."""
    обычно = speech.analyze(РАЗГОВОР, 15.0)
    свои = speech.Пороги(пауза=1.0, длинная=2.0, тишина=2.0, перебивание=-0.1)
    речь = speech.analyze(РАЗГОВОР, 15.0, свои)
    # Разрыв в 1,2 с по умолчанию — время ответа, с порогом в секунду — пауза.
    assert обычно["pauses"] == 2 and речь["pauses"] == 3
    assert речь["long_pauses"] == 2          # обе долгие паузы теперь длинные
    assert речь["interruptions"] == 1        # наложение 0,2 с стало перебиванием
    assert речь["dead_air_s"] > обычно["dead_air_s"]


def test_осознанный_ноль_в_пороге_не_подменяется():
    """«Считать паузой любой разрыв» — законная настройка, а не «не задано».

    Прежний приём `float(значение or 2.0)` вернул бы здесь двойку, и человек,
    выставивший ноль, получил бы прежние числа без единого слова о том, что
    его настройку не применили.
    """
    пороги = speech.Пороги.из_настроек({"content_pause_s": 0,
                                        "content_dead_air_s": 0.0})
    assert пороги.пауза == 0.0
    assert пороги.тишина == 0.0


def test_порог_перебивания_в_настройке_положительный():
    """Человек задаёт наложение в секундах, внутри это отрицательный разрыв."""
    пороги = speech.Пороги.из_настроек({"content_interruption_s": 0.5})
    assert пороги.перебивание == -0.5
    # Знак в настройке не должен ничего ломать: «-0,5» — то же самое.
    assert speech.Пороги.из_настроек({"content_interruption_s": -0.5}).перебивание == -0.5


def test_пустые_настройки_дают_прежние_числа():
    """Появление настроек не меняет ничей архив."""
    assert speech.Пороги.из_настроек(None) == speech.Пороги()
    assert speech.Пороги.из_настроек({}) == speech.Пороги()
    assert speech.Пороги().пауза == speech.ПОРОГ_ПАУЗЫ
    assert speech.Пороги().длинная == speech.ПОРОГ_ДЛИННОЙ_ПАУЗЫ


def test_пороги_доходят_до_разбора_записи():
    """content.analyze передаёт границы дальше, а не считает по своим."""
    свои = speech.Пороги(пауза=1.0, длинная=2.0, тишина=2.0, перебивание=-0.1)
    разбор = content_analyze.analyze(text="", segments=РАЗГОВОР, duration_s=15.0,
                                     thresholds=свои)
    assert разбор["speech"]["long_pauses"] == 2


def test_длинные_паузы_попадают_в_свод():
    """Без колонки в своде показатель не попал бы ни в разрезы, ни в выгрузку."""
    разбор = content_analyze.analyze(text="", segments=РАЗГОВОР, duration_s=15.0)
    свод, _ = content_analyze.features(разбор)
    assert свод["long_pauses"] == 1


def test_версия_разбора_поднята_ради_пересчёта_архива():
    """Новый показатель без новой версии не появился бы у старых записей."""
    assert content_analyze.VERSION >= 7


def test_колонка_длинных_пауз_появляется_в_старой_базе(tmp_path: Path):
    """База прошлой версии догоняется, а не падает на «no such column»."""
    import sqlite3

    from asrhub.db import Database

    файл = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(файл)
    conn.execute("CREATE TABLE content (job_id TEXT PRIMARY KEY, version INTEGER, "
                 "computed_at REAL, pauses INTEGER)")
    conn.commit()
    conn.close()

    Database(файл).close()

    conn = sqlite3.connect(файл)
    колонки = {строка[1] for строка in conn.execute("PRAGMA table_info(content)")}
    conn.close()
    assert "long_pauses" in колонки


# --------------------------------------------------------------------------
# Показатели контакт-центра по журналу звонков
# --------------------------------------------------------------------------


НАЧАЛО = 1_700_000_000.0


def _база(tmp_path: Path, звонки) -> Database:
    db = Database(tmp_path / "kpi.sqlite3")
    for uid, src, напр, отв, длит, разг, старт in звонки:
        db.execute(
            "INSERT INTO calls (uniqueid, src, direction, answered, duration, "
            "billsec, started_at, station, pbx_uid) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, src, напр, отв, длит, разг, старт, "s", uid.split(":")[-1]))
    return db


ЗВОНКИ = [
    # ждал 10 с, разговор 20 с
    ("s:1", "111", "входящий", 1, 30, 20, НАЧАЛО),
    # ждал 2 с
    ("s:2", "222", "входящий", 1, 12, 10, НАЧАЛО + 60),
    # не ответили
    ("s:3", "333", "входящий", 0, 25, 0, НАЧАЛО + 120),
    # тот же номер через час — повторное обращение
    ("s:4", "111", "входящий", 1, 15, 12, НАЧАЛО + 3600),
    ("s:5", "444", "исходящий", 1, 40, 35, НАЧАЛО + 180),
]


def test_показатели_контакт_центра_считаются_по_журналу(tmp_path: Path):
    db = _база(tmp_path, ЗВОНКИ)
    k = db.call_kpi(service_level_s=5.0, repeat_window_days=7)
    assert k["inbound"] == 4 and k["abandoned"] == 1
    assert k["abandoned_share"] == 0.25
    # Среднее время разговора — по отвеченным (20+10+12+35)/4.
    assert k["aht_s"] == pytest.approx(19.2, abs=0.1)
    # Ждали 10, 2 и 3 секунды: быстрее пяти — два вызова из трёх.
    assert k["wait_avg_s"] == 5.0
    assert k["service_level"] == pytest.approx(2 / 3, abs=0.001)
    # Из трёх отвеченных входящих один номер позвонил снова.
    assert k["fcr"] == pytest.approx(2 / 3, abs=0.001)
    assert k["fcr_base"] == 3
    db.close()


def test_звонки_без_номера_не_считаются_решёнными(tmp_path: Path):
    """Пустой `src` — нет ключа повторного обращения, а не «решено».

    Молча зачесть такие звонки в решённые — значит завышать показатель ровно
    на долю станций, которые номер не передают.
    """
    db = _база(tmp_path, [
        ("s:1", "", "входящий", 1, 10, 10, НАЧАЛО),
        ("s:2", "", "входящий", 1, 10, 10, НАЧАЛО + 60),
    ])
    k = db.call_kpi(repeat_window_days=7)
    assert k["fcr_base"] == 0
    assert k["fcr"] is None
    db.close()


def test_повторный_звонок_за_границей_периода_всё_равно_считается(tmp_path: Path):
    """Отчёт за день, перезвонили на третий — обращение не было решённым."""
    db = _база(tmp_path, [
        ("s:1", "111", "входящий", 1, 10, 10, НАЧАЛО),
        ("s:2", "111", "входящий", 1, 10, 10, НАЧАЛО + 2 * 86400),
    ])
    только_первый = db.call_kpi(since=НАЧАЛО, until=НАЧАЛО + 3600,
                                repeat_window_days=7)
    assert только_первый["fcr_base"] == 1
    assert только_первый["fcr"] == 0.0
    db.close()


def test_окно_повторного_обращения_ограничивает_счёт(tmp_path: Path):
    """Звонок через два дня при окне в сутки повторным не считается."""
    db = _база(tmp_path, [
        ("s:1", "111", "входящий", 1, 10, 10, НАЧАЛО),
        ("s:2", "111", "входящий", 1, 10, 10, НАЧАЛО + 2 * 86400),
    ])
    assert db.call_kpi(since=НАЧАЛО, until=НАЧАЛО + 3600,
                       repeat_window_days=1)["fcr"] == 1.0
    db.close()


def test_нулевое_окно_выключает_показатель(tmp_path: Path):
    db = _база(tmp_path, ЗВОНКИ)
    k = db.call_kpi(repeat_window_days=0)
    assert k["fcr"] is None and k["fcr_base"] == 0
    db.close()


def test_пустой_период_даёт_прочерк_а_не_ноль(tmp_path: Path):
    """Ноль процентов потерянных и «не по чему считать» — разные ответы.

    Показатель, которого не из чего посчитать, обязан быть None: иначе пустой
    период рисуется как идеальная работа — «потеряно 0 %, уровень
    обслуживания 0 %», и по этой картинке принимают решения.
    """
    db = _база(tmp_path, [])
    k = db.call_kpi()
    assert k["abandoned_share"] is None
    assert k["aht_s"] is None
    assert k["service_level"] is None
    assert k["fcr"] is None
    db.close()


def test_уровень_обслуживания_считается_от_отвеченных(tmp_path: Path):
    """Потерянный вызов не должен занижать уровень обслуживания дважды.

    Он уже посчитан в «потерянных»; если делить ещё и уровень обслуживания на
    все входящие, один и тот же провал войдёт в отчёт двумя числами.
    """
    db = _база(tmp_path, [
        ("s:1", "111", "входящий", 1, 11, 10, НАЧАЛО),   # ждал 1 с
        ("s:2", "222", "входящий", 0, 30, 0, НАЧАЛО + 60),
    ])
    assert db.call_kpi(service_level_s=5.0)["service_level"] == 1.0
    db.close()


def test_показатели_доезжают_до_раздела_атс(client):
    """Раздел «АТС» получает их тем же ответом, что и всё остальное."""
    ответ = client.get("/api/telephony/overview?period=week")
    assert ответ.status_code == 200
    kpi = ответ.json().get("kpi")
    assert kpi is not None
    assert "service_level" in kpi and "fcr" in kpi


def test_отраслевой_чек_лист_закрыт_разбором_речи():
    """Каждый показатель, который глава о рынке объявляет считающимся, есть.

    Глава «Рынок речевой аналитики» перечисляет отраслевой чек-лист и говорит,
    что из него считается у нас. Первая её редакция утверждала обратное —
    что паузы, «говорил/слушал» и перебивания не считаются, — и это было
    неправдой. Проверка держит главу и код вместе: исчезнет показатель —
    упадёт она, а не доверие к документу.
    """
    речь = speech.analyze(РАЗГОВОР, 15.0)
    речь["sides"] = speech.sides(речь, "A")
    for ключ in ("silence_share", "pauses", "long_pauses", "longest_pause_s",
                 "dead_air_s", "interruptions", "overlap_s", "switches",
                 "wpm", "monologue"):
        assert ключ in речь, ключ
    for ключ in ("talk_share", "monologue_s", "customer_story_s",
                 "reply_delay_s", "tempo_ratio"):
        assert ключ in речь["sides"], ключ


def test_глава_о_рынке_не_отрицает_своих_показателей():
    """Прямая защита от возврата неверного утверждения в текст главы."""
    глава = (КОРЕНЬ / "docs" / "28-market.md").read_text(encoding="utf-8")
    assert "Не считаются: число пауз" not in глава
    assert "Считается по журналу звонков" in глава


# --------------------------------------------------------------------------
# Журнал доступа
# --------------------------------------------------------------------------

def test_журнал_пишет_изменения_и_молчит_о_списках():
    """Каждый GET списка в журнале — это журнал, который перестают открывать.

    Раздел аналитики делает десятки запросов на один взгляд человека.
    Записываются изменения; из чтений — только те, что выносят данные наружу,
    и только когда это включено настройкой.
    """
    assert audit.записывать("POST", "/api/jobs", reads=False)
    assert audit.записывать("DELETE", "/api/users/x", reads=False)
    assert not audit.записывать("GET", "/api/analytics/summary", reads=True)
    assert not audit.записывать("GET", "/api/jobs/x/media", reads=False)
    assert audit.записывать("GET", "/api/jobs/x/media", reads=True)
    assert audit.записывать("GET", "/api/analytics/export.xlsx", reads=True)
    # Статика и корень интерфейса — не действия с данными.
    assert not audit.записывать("GET", "/index.html", reads=True)
    assert not audit.записывать("POST", "/ws", reads=True)


def test_действие_называется_по_русски():
    assert audit.описать("DELETE", "/api/jobs/abc") == "удалил: записи"
    assert audit.описать("POST", "/api/settings") == "изменил: настройки"
    assert audit.описать("GET", "/api/jobs/abc/media") == "скачал: записи"
    assert audit.описать("GET", "/api/analytics/export.csv") == "выгрузил: аналитика"
    assert audit.описать("POST", "/api/auth/login") == "вход"


def test_адрес_берётся_из_за_прокси():
    """За nginx у каждого запроса адрес самого nginx — и журнал бесполезен."""
    класс = type("Клиент", (), {"host": "127.0.0.1"})
    заголовки = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
    assert audit.адрес(заголовки, класс()) == "203.0.113.9"
    assert audit.адрес({"x-real-ip": "203.0.113.7"}, класс()) == "203.0.113.7"
    assert audit.адрес({}, класс()) == "127.0.0.1"


def test_журнал_записывает_действие_через_сервер(client):
    """Проверка связки: запрос прошёл — строка появилась."""
    было = client.get("/api/audit").json()["total"]
    client.post("/api/settings", json={"values": {"beam_size": 5}})
    стало = client.get("/api/audit").json()
    assert стало["total"] > было
    свежая = стало["items"][0]
    assert свежая["method"] == "POST"
    assert "настройки" in свежая["action"]


def test_чтения_в_журнал_не_попадают_по_умолчанию(client):
    client.get("/api/jobs")
    client.get("/api/analytics/summary")
    строки = client.get("/api/audit").json()["items"]
    assert not [с for с in строки if с["method"] == "GET" and "/api/audit" not in с["path"]]


def test_старые_строки_журнала_убираются_по_сроку(tmp_path: Path):
    db = Database(tmp_path / "audit.sqlite3")
    import time as _time

    db.audit_add(action="вход", actor="anna")
    db.execute("UPDATE audit SET ts=?", (_time.time() - 400 * 86400,))
    db.audit_add(action="вход", actor="boris")
    убрано = db.cleanup(results_days=0, metrics_days=0, events_days=0, audit_days=365)
    assert убрано["audit"] == 1
    assert db.audit_list()["total"] == 1
    db.close()


def test_нулевой_срок_хранит_журнал_вечно(tmp_path: Path):
    """Ноль — «хранить вечно», а не «не задано»."""
    db = Database(tmp_path / "audit2.sqlite3")
    import time as _time

    db.audit_add(action="вход", actor="anna")
    db.execute("UPDATE audit SET ts=?", (_time.time() - 4000 * 86400,))
    убрано = db.cleanup(results_days=0, metrics_days=0, events_days=0, audit_days=0)
    assert убрано["audit"] == 0
    assert db.audit_list()["total"] == 1
    db.close()


def test_отбор_только_отказов(tmp_path: Path):
    db = Database(tmp_path / "audit3.sqlite3")
    db.audit_add(action="вход", actor="anna", status=200)
    db.audit_add(action="вход", actor="anna", status=401)
    db.audit_add(action="вход", actor="boris", status=403)
    assert db.audit_list(failed_only=True)["total"] == 2
    assert db.audit_list(actor="boris")["total"] == 1
    assert db.audit_list(query="вход")["total"] == 3
    db.close()


# --------------------------------------------------------------------------
# Вход через каталог предприятия
# --------------------------------------------------------------------------

from asrhub import ldap_auth  # noqa: E402


class _ЗаписьКаталога:
    def __init__(self, группы, имя="Иванов Иван"):
        self.memberOf = list(группы)
        self.displayName = [имя]


class _Связь:
    def __init__(self, записи):
        self.entries = list(записи)
        self.отвязан = False

    def search(self, *args, **kwargs):
        return True

    def unbind(self):
        self.отвязан = True


def _каталог(**поверх) -> ldap_auth.Настройки:
    основа = {"auth_ldap_enabled": True, "auth_ldap_url": "ldaps://dc",
              "auth_ldap_bind_template": "{username}@example.ru",
              "auth_ldap_base_dn": "dc=example,dc=ru",
              "auth_ldap_group_map": "asr-admins = admin\nasr-users = user"}
    основа.update(поверх)
    return ldap_auth.Настройки.из_настроек(основа)


def test_роль_берётся_самая_сильная():
    """Порядок групп в ответе каталога не должен решать, админ человек или нет."""
    н = _каталог()
    группы = ("CN=asr-users,OU=G,DC=example,DC=ru",
              "CN=asr-admins,OU=G,DC=example,DC=ru")
    assert ldap_auth.роль_по_группам(группы, н) == "admin"
    assert ldap_auth.роль_по_группам(tuple(reversed(группы)), н) == "admin"


def test_несопоставленная_группа_не_даёт_прав():
    н = _каталог()
    assert ldap_auth.роль_по_группам(("CN=бухгалтерия,OU=G,DC=x",), н) == ""
    с_умолчанием = _каталог(auth_ldap_default_role="readonly")
    assert ldap_auth.роль_по_группам(("CN=бухгалтерия,OU=G,DC=x",), с_умолчанием) == "readonly"


def test_логин_не_может_подменить_отбор():
    """«*)(objectClass=*» превращает «найди этого» в «найди любого»."""
    assert ldap_auth.экранировать("*)(objectClass=*") == r"\2a\29\28objectClass=\2a"
    assert ldap_auth.экранировать("\\x") == r"\5cx"
    assert ldap_auth.экранировать("обычный") == "обычный"


def test_шаблон_без_места_для_логина_отвергается():
    """Иначе все входят под одним и тем же именем."""
    плохой = _каталог(auth_ldap_bind_template="служебный@example.ru")
    assert "{username}" in плохой.проблема()
    assert _каталог().проблема() == ""
    assert "адрес" in _каталог(auth_ldap_url="").проблема()


def test_недоступный_каталог_это_не_неверный_пароль(monkeypatch):
    """Упавший контроллер домена не должен выглядеть забывчивостью всех сразу."""
    def падает(*args, **kwargs):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(ldap_auth, "соединение", падает)
    with pytest.raises(ldap_auth.LDAPError):
        ldap_auth.войти(_каталог(), "ivanov", "секрет")


def test_каталог_отказал_значит_пара_неверна(monkeypatch):
    monkeypatch.setattr(ldap_auth, "соединение", lambda *a, **k: None)
    assert ldap_auth.войти(_каталог(), "ivanov", "не тот") is None


def test_группы_прочитаны_и_связь_закрыта(monkeypatch):
    связь = _Связь([_ЗаписьКаталога(["CN=asr-admins,OU=G,DC=example,DC=ru"])])
    monkeypatch.setattr(ldap_auth, "соединение", lambda *a, **k: связь)
    человек = ldap_auth.войти(_каталог(), "ivanov", "секрет")
    assert человек is not None
    assert человек.role == "admin"
    assert человек.display_name == "Иванов Иван"
    assert связь.отвязан, "соединение с каталогом осталось открытым"


def _сервер_с_каталогом(monkeypatch, tmp_path: Path, связь_для):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "1")
    monkeypatch.setattr(ldap_auth, "соединение", связь_для)
    app = create_app(load(), start_queue=False)
    for ключ, значение in {
            "auth_ldap_enabled": True, "auth_ldap_url": "ldaps://dc",
            "auth_ldap_bind_template": "{username}@example.ru",
            "auth_ldap_base_dn": "dc=example,dc=ru",
            "auth_ldap_group_map": "asr-admins = admin\nasr-users = user"}.items():
        app.state.hub.settings.set(ключ, значение)
    return app, TestClient(app)


def test_вход_из_каталога_заводит_учётную_запись(monkeypatch, tmp_path: Path):
    связь = _Связь([_ЗаписьКаталога(["CN=asr-admins,OU=G,DC=example,DC=ru"])])
    app, c = _сервер_с_каталогом(
        monkeypatch, tmp_path,
        lambda н, имя, пароль: связь if (имя == "ivanov@example.ru"
                                         and пароль == "секрет") else None)
    ответ = c.post("/api/auth/login", json={"username": "ivanov", "password": "секрет"})
    assert ответ.status_code == 200
    учётная = ответ.json()["user"]
    assert учётная["role"] == "admin"
    assert учётная["source"] == "ldap"
    assert учётная["display_name"] == "Иванов Иван"


def test_роль_переписывается_каталогом_при_каждом_входе(monkeypatch, tmp_path: Path):
    """Снятие из группы администраторов отнимает права на следующем входе."""
    состояние = {"группы": ["CN=asr-admins,OU=G,DC=example,DC=ru"]}

    def связь_для(н, имя, пароль):
        if имя != "ivanov@example.ru" or пароль != "секрет":
            return None
        return _Связь([_ЗаписьКаталога(состояние["группы"])])

    app, c = _сервер_с_каталогом(monkeypatch, tmp_path, связь_для)
    первый = c.post("/api/auth/login", json={"username": "ivanov", "password": "секрет"})
    assert первый.json()["user"]["role"] == "admin"
    состояние["группы"] = ["CN=asr-users,OU=G,DC=example,DC=ru"]
    второй = c.post("/api/auth/login", json={"username": "ivanov", "password": "секрет"})
    assert второй.json()["user"]["role"] == "user"


def test_запись_из_каталога_не_запирается_счётчиком_неудач(monkeypatch, tmp_path: Path):
    """Её пароль здесь не проверяется — счётчик рос бы от чужих попыток.

    Без этого разделения каждый вход такого человека сперва проваливал бы
    местную проверку, и после нескольких входов подряд сервер запирал бы того,
    кто всё делал правильно.
    """
    связь = _Связь([_ЗаписьКаталога(["CN=asr-users,OU=G,DC=example,DC=ru"])])
    app, c = _сервер_с_каталогом(
        monkeypatch, tmp_path,
        lambda н, имя, пароль: связь if пароль == "секрет" else None)
    assert c.post("/api/auth/login",
                  json={"username": "ivanov", "password": "секрет"}).status_code == 200
    for _ in range(8):
        c.post("/api/auth/login", json={"username": "ivanov", "password": "мимо"})
    снова = c.post("/api/auth/login", json={"username": "ivanov", "password": "секрет"})
    assert снова.status_code == 200, "учётную запись из каталога заперли"


def test_местный_администратор_входит_при_живом_каталоге(monkeypatch, tmp_path: Path):
    """Своя запись проверяется своим паролем и в каталог не ходит вовсе."""
    ходили = {"в каталог": 0}

    def связь_для(н, имя, пароль):
        ходили["в каталог"] += 1
        return None

    app, c = _сервер_с_каталогом(monkeypatch, tmp_path, связь_для)
    ответ = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert ответ.status_code == 200
    assert ходили["в каталог"] == 0


def test_без_сопоставленной_группы_не_пускают(monkeypatch, tmp_path: Path):
    связь = _Связь([_ЗаписьКаталога(["CN=бухгалтерия,OU=G,DC=example,DC=ru"])])
    app, c = _сервер_с_каталогом(monkeypatch, tmp_path, lambda *a, **k: связь)
    ответ = c.post("/api/auth/login", json={"username": "petrov", "password": "секрет"})
    assert ответ.status_code == 401


def test_отбор_каталога_получает_экранированный_логин(monkeypatch):
    """Не «функция умеет экранировать», а «вход ею пользуется».

    Проверка самой функции проходит и тогда, когда её забыли позвать; отбор,
    ушедший в каталог, — единственное, что здесь важно.
    """
    ушло = {}

    class Подслушивающая(_Связь):
        def search(self, база, отбор, **kwargs):
            ушло["отбор"] = отбор
            return True

    связь = Подслушивающая([_ЗаписьКаталога(["CN=asr-users,OU=G,DC=x"])])
    monkeypatch.setattr(ldap_auth, "соединение", lambda *a, **k: связь)
    ldap_auth.войти(_каталог(), "иванов)(uid=admin", "секрет")
    assert ушло["отбор"].count("(") == ушло["отбор"].count(")")
    assert "uid=admin" not in ушло["отбор"].replace(r"\29\28uid=admin", "")
    assert r"\29\28" in ушло["отбор"]


def test_неизвестный_столбец_не_сдвигает_остальные(tmp_path: Path):
    """Выборка признаков держит позиции запрошенного списка.

    Найдено при добавлении «длинных пауз»: `content_sample` молча выбрасывала
    столбец, которого не знает, а тот, кто её заказал, продолжал считать по
    номерам СВОЕГО списка. Номера съезжали, и «паузы» начинали означать «самую
    долгую паузу» — не ошибка, не исключение, а неверные числа в отчёте.
    """
    db = Database(tmp_path / "c.sqlite3")
    db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
               "VALUES ('j1', ?, ?, 'completed')", (НАЧАЛО, НАЧАЛО))
    db.save_content("j1", {"version": 7, "pauses": 3, "longest_pause_s": 9.5})

    прямая = db.content_sample(["pauses", "longest_pause_s"])
    assert прямая == [(3, 9.5)]

    # Посередине — столбец, которого в выборке нет вовсе.
    с_дыркой = db.content_sample(["pauses", "такого-нет", "longest_pause_s"])
    assert с_дыркой == [(3, None, 9.5)]
    db.close()


def test_длинные_паузы_доступны_разрезам(tmp_path: Path):
    """Показатель без места в CONTENT_NUMERIC не попал бы ни в нормы, ни в связи."""
    db = Database(tmp_path / "c2.sqlite3")
    db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
               "VALUES ('j1', ?, ?, 'completed')", (НАЧАЛО, НАЧАЛО))
    db.save_content("j1", {"version": 7, "pauses": 3, "long_pauses": 1})
    assert db.content_sample(["long_pauses"]) == [(1,)]
    db.close()


# --------------------------------------------------------------------------
# Редакция звука
# --------------------------------------------------------------------------

from asrhub.pipeline import redact  # noqa: E402

# Номер, проходящий проверку Луна: на выдуманном `masking` ничего не находит,
# и проверка молча проверяла бы пустоту.
КАРТА = "4276 3800 1234 5679"
РЕПЛИКА_С_КАРТОЙ = {
    "start": 0.0, "end": 5.0, "speaker": "A",
    "text": f"номер карты {КАРТА} записал",
    "words": [
        {"word": "номер", "start": 0.0, "end": 0.5},
        {"word": "карты", "start": 0.5, "end": 1.0},
        {"word": "4276", "start": 1.0, "end": 1.6},
        {"word": "3800", "start": 1.6, "end": 2.2},
        {"word": "1234", "start": 2.2, "end": 2.8},
        {"word": "5679", "start": 2.8, "end": 3.4},
        {"word": "записал", "start": 3.5, "end": 4.2},
    ],
}


def test_границы_берутся_у_слов_а_не_у_реплики():
    """Заглушить реплику целиком — потерять полминуты из-за одного номера."""
    интервалы = redact.найти_интервалы([РЕПЛИКА_С_КАРТОЙ], padding_ms=0)
    assert len(интервалы) == 1
    и = интервалы[0]
    assert и.kind == "card"
    assert not и.estimated
    assert и.start == pytest.approx(1.0, abs=0.01)
    assert и.end == pytest.approx(3.4, abs=0.01)


def test_без_таймкодов_границы_оцениваются_по_тексту():
    без_слов = {к: з for к, з in РЕПЛИКА_С_КАРТОЙ.items() if к != "words"}
    интервалы = redact.найти_интервалы([без_слов], estimated_padding_ms=0)
    assert len(интервалы) == 1
    assert интервалы[0].estimated
    # Оценка, но осмысленная: номер в середине реплики, а не с нулевой секунды.
    assert 0.5 < интервалы[0].start < 3.0
    assert интервалы[0].end > интервалы[0].start


def test_оценке_даётся_запас_больше():
    без_слов = {к: з for к, з in РЕПЛИКА_С_КАРТОЙ.items() if к != "words"}
    точный = redact.найти_интервалы([РЕПЛИКА_С_КАРТОЙ])[0]
    оценка = redact.найти_интервалы([без_слов])[0]
    точная_ширина = точный.end - точный.start
    # Запас у оценки больше: промах на слог опаснее лишней четверти секунды.
    assert (оценка.end - оценка.start) - (точный.end - точный.start) > 0.3
    assert точная_ширина > 0


def test_пересекающиеся_куски_сливаются():
    """Два номера подряд — один фильтр ffmpeg, а не два на одно место."""
    интервалы = redact._слить([
        redact.Интервал(1.0, 2.0, "card"),
        redact.Интервал(1.8, 3.0, "phone"),
        redact.Интервал(5.0, 6.0, "email"),
    ])
    assert len(интервалы) == 2
    assert интервалы[0].start == 1.0 and интервалы[0].end == 3.0
    assert "card" in интервалы[0].kind and "phone" in интервалы[0].kind


def test_выбранные_виды_ограничивают_поиск():
    реплика = dict(РЕПЛИКА_С_КАРТОЙ)
    реплика["text"] = f"{реплика['text']} и почта ivan@mail.ru"
    все = redact.найти_интервалы([реплика])
    только_почта = redact.найти_интервалы([реплика], kinds=("email",))
    assert len(все) > len(только_почта)
    assert {и.kind for и in только_почта} == {"email"}


def test_находка_без_таймкодов_не_пропадает():
    """Слова кончились на середине реплики — остальное всё равно заглушается.

    Первая версия отдавала интервалы только по тем находкам, которым достались
    слова, а прочие теряла молча: почта после номера карты оставалась звучать
    в «отредактированной» записи. Для редакции персональных данных это худший
    вид ошибки — она выглядит как успех.
    """
    реплика = dict(РЕПЛИКА_С_КАРТОЙ)
    реплика["text"] = f"{реплика['text']} и почта ivan@mail.ru"
    виды = {и.kind for и in redact.найти_интервалы([реплика])}
    assert виды == {"card", "email"}
    почта = [и for и in redact.найти_интервалы([реплика]) if и.kind == "email"][0]
    assert почта.estimated, "у почты таймкодов не было — это оценка"


def test_ничего_не_нашлось_файл_не_делается(tmp_path: Path):
    """Копия без единой правки только занимала бы место."""
    итог = redact.заглушить(tmp_path / "нет.wav", tmp_path / "out.wav",
                            [{"start": 0.0, "end": 1.0, "text": "добрый день"}])
    assert итог.path is None
    assert итог.intervals == []
    assert not (tmp_path / "out.wav").exists()


def test_писк_не_делает_тише_всю_запись():
    """amix без normalize=0 срезает шесть децибел со всей записи.

    Замер показал ровно это: −21 дБ до редакции и −27 после, причём в тех
    местах, которых редакция не касалась. Ошибка неочевидная: файл получается,
    ffmpeg молчит, и заметно только на слух рядом с оригиналом.
    """
    команда = redact.построить_команду(
        Path("a.wav"), Path("b.wav"), [redact.Интервал(1.0, 2.0, "card")],
        mode="beep")
    строка = " ".join(команда)
    assert "normalize=0" in строка


def test_тишина_и_писк_дают_разные_команды():
    интервалы = [redact.Интервал(1.0, 2.0, "card")]
    тишина = " ".join(redact.построить_команду(Path("a"), Path("b"), интервалы,
                                               mode="silence"))
    писк = " ".join(redact.построить_команду(Path("a"), Path("b"), интервалы,
                                             mode="beep"))
    assert "sine=frequency=" not in тишина
    assert "sine=frequency=1000" in писк
    assert "between(t,1.000,2.000)" in тишина


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_заглушенный_кусок_звучит_иначе_чем_исходный(tmp_path: Path):
    """Настоящий ffmpeg на настоящем файле: тишина глушит, писк — нет.

    Проверка идёт по звуку, а не по команде: команда может быть правильной на
    вид и не делать ничего.
    """
    источник = tmp_path / "src.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=5:sample_rate=16000", str(источник)],
        check=True, timeout=60)

    def громкость(файл: Path, от: float, до: float) -> float:
        итог = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(файл),
             "-af", f"atrim={от}:{до},volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
        for строка in итог.stderr.splitlines():
            if "mean_volume:" in строка:
                значение = строка.split("mean_volume:")[1].split("dB")[0].strip()
                return float("-inf") if значение == "-inf" else float(значение)
        raise AssertionError("ffmpeg не сказал mean_volume")

    тихо = redact.заглушить(источник, tmp_path / "тихо.wav", [РЕПЛИКА_С_КАРТОЙ],
                            mode="silence")
    assert тихо.path is not None
    # Не ровно −inf: ffmpeg оставляет округление последнего бита.
    assert громкость(тихо.path, 1.5, 3.0) < -60
    assert громкость(тихо.path, 4.5, 5.0) == pytest.approx(-21.1, abs=1.0)

    писк = redact.заглушить(источник, tmp_path / "писк.wav", [РЕПЛИКА_С_КАРТОЙ],
                            mode="beep")
    assert писк.path is not None
    # Писк слышен на уровне речи — не тише её и не тишина.
    assert громкость(писк.path, 1.5, 3.0) == pytest.approx(-21.1, abs=3.0)
    # И вне заглушенного куска запись не стала тише ни на децибел.
    assert громкость(писк.path, 4.5, 5.0) == pytest.approx(
        громкость(источник, 4.5, 5.0), abs=0.5)


# --------------------------------------------------------------------------
# Обратная запись в CRM
# --------------------------------------------------------------------------

from asrhub import crm  # noqa: E402

РАЗБОР_ДЛЯ_CRM = {
    "sentiment": {"label": "нейтральная", "turn": {"shift": 0.2}},
    "compliance": {"score": 0.9},
    "scorecard": {"score": 82},
    "categories": {"topics": [{"name": "оплата"}]},
    "llm": {"summary": "клиент просил счёт", "reason": "счёт",
            "outcome": "решено", "actions": [{"text": "выставить счёт"}]},
}


def _crm(**поверх) -> crm.Настройки:
    основа = {"crm_enabled": True, "crm_kind": "bitrix24",
              "crm_url": "https://firma.bitrix24.ru/rest/1/abc/",
              "crm_entity": "deal"}
    основа.update(поверх)
    return crm.Настройки.из_настроек(основа)


def test_примечание_не_показывает_пустые_поля():
    """«Балл оператора: —» не сообщает ничего, а место занимает."""
    данные = crm.собрать({"id": "j1", "text": "текст"}, {})
    текст = crm.примечание(данные)
    assert "Балл оператора" not in текст
    assert "Тональность" not in текст


def test_расшифровка_в_примечание_по_просьбе():
    данные = crm.собрать({"id": "j1", "text": "весь разговор"}, РАЗБОР_ДЛЯ_CRM)
    assert "весь разговор" not in crm.примечание(данные)
    assert "весь разговор" in crm.примечание(данные, transcript=True)


def test_bitrix24_собирает_свой_метод():
    данные = crm.собрать({"id": "j1"}, РАЗБОР_ДЛЯ_CRM)
    адрес, заголовки, тело = crm.запрос(данные, _crm(), entity_id="42")
    assert адрес.endswith("/crm.timeline.comment.add.json")
    разобрано = json.loads(тело)
    assert разобрано["fields"]["ENTITY_TYPE"] == "deal"
    assert разобрано["fields"]["ENTITY_ID"] == "42"
    assert "клиент просил счёт" in разобрано["fields"]["COMMENT"]
    assert "Authorization" not in заголовки, "у Bitrix24 ключ в адресе"


def test_amocrm_требует_токен_и_сущность():
    данные = crm.собрать({"id": "j1"}, РАЗБОР_ДЛЯ_CRM)
    без_токена = _crm(crm_kind="amocrm", crm_url="https://f.amocrm.ru")
    with pytest.raises(Exception, match="токен"):
        crm.запрос(данные, без_токена, entity_id="42")
    с_токеном = _crm(crm_kind="amocrm", crm_url="https://f.amocrm.ru",
                     crm_token="t0ken", crm_entity="lead")
    with pytest.raises(crm.CRMError, match="сущност"):
        crm.запрос(данные, с_токеном)
    адрес, заголовки, тело = crm.запрос(данные, с_токеном, entity_id="42")
    assert адрес == "https://f.amocrm.ru/api/v4/leads/42/notes"
    assert заголовки["Authorization"] == "Bearer t0ken"
    assert json.loads(тело)[0]["note_type"] == "common"


def test_свои_поля_уходят_отдельно():
    данные = crm.собрать({"id": "j1"}, РАЗБОР_ДЛЯ_CRM)
    настройки = _crm(crm_fields="score = UF_CRM_BALL\nsentiment=UF_CRM_TON")
    _, _, тело = crm.запрос(данные, настройки, entity_id="42")
    поля = json.loads(тело)["fields"]
    assert поля["UF_CRM_BALL"] == 82
    assert поля["UF_CRM_TON"] == "нейтральная"


def test_обезличивание_доходит_до_тела_запроса():
    """Номер карты, продиктованный вслух, не должен уезжать в облачную CRM."""
    задание = {"id": "j1", "text": f"мой номер {КАРТА}"}
    как_есть = crm.собрать(задание, РАЗБОР_ДЛЯ_CRM, mask=False)
    скрыто = crm.собрать(задание, РАЗБОР_ДЛЯ_CRM, mask=True)
    assert КАРТА in как_есть["transcript"]
    assert КАРТА not in скрыто["transcript"]
    assert "[карта]" in скрыто["transcript"]


def test_идентификатор_сделки_берётся_из_полей_звонка():
    """По номеру телефона не угадываем: цена ошибки — чужая карточка."""
    assert crm.сущность_из({}, {"userfield": "4242"}) == "4242"
    assert crm.сущность_из({}, {"accountcode": "77"}) == "77"
    assert crm.сущность_из({"params": {"deal_id": "9"}}, {}) == "9"
    assert crm.сущность_из({}, {"src": "+79001234567"}) == ""


def test_без_сделки_в_crm_не_отправляется(tmp_path: Path):
    """Запись, загруженная руками, ни к какой сделке не относится."""
    db = Database(tmp_path / "crm.sqlite3")
    db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
               "VALUES ('j1', ?, ?, 'completed')", (НАЧАЛО, НАЧАЛО))
    настройки = {"crm_enabled": True, "crm_kind": "bitrix24",
                 "crm_url": "https://firma.bitrix24.ru/rest/1/abc/"}
    assert crm.отправить_разбор(db, настройки, "j1") is None
    db.close()


def test_действия_разбираются_из_строки_базы():
    """В базе действия лежат строкой JSON, а не списком."""
    данные = crm.собрать({"id": "j1"}, {"llm": {"actions": '[{"text": "счёт"}]'}})
    assert данные["actions"] == "счёт"


def test_проверка_показывает_запрос_и_ничего_не_шлёт(client):
    """Комментарий, ушедший не в ту карточку, из ленты уже не убрать."""
    настройки = client.app.state.hub.settings
    for ключ, значение in {"crm_enabled": True, "crm_kind": "bitrix24",
                           "crm_url": "https://firma.bitrix24.ru/rest/1/abc/",
                           "crm_entity": "deal"}.items():
        настройки.set(ключ, значение)
    ответ = client.post("/api/crm/test")
    assert ответ.status_code == 200
    тело = ответ.json()
    assert тело["sent"] is False
    assert тело["url"].endswith("crm.timeline.comment.add.json")
    assert "Пересказ разговора" in тело["note"]


# --------------------------------------------------------------------------
# Отраслевые наборы категорий
# --------------------------------------------------------------------------

from asrhub.content import categories as категории_модуль  # noqa: E402


def test_отраслевых_наборов_восемь_и_все_разбираются():
    """Правило, которое не компилируется, — это молча пропавшая категория."""
    assert len(категории_модуль.НАБОРЫ) >= 8
    for имя, набор in категории_модуль.НАБОРЫ.items():
        собрано = категории_модуль.с_набором(имя)
        ошибки = категории_модуль.validate(собрано)
        assert not ошибки, f"{имя}: {ошибки}"
        assert len(собрано) == len(категории_модуль.ГОТОВЫЕ) + len(набор["categories"])


def test_неизвестная_отрасль_даёт_общий_набор():
    assert (категории_модуль.с_набором("нет-такой")
            == list(категории_модуль.ГОТОВЫЕ))
    assert категории_модуль.с_набором("") == list(категории_модуль.ГОТОВЫЕ)


def test_совпадающие_имена_разводятся():
    """Отраслевая категория не должна затирать общую с тем же именем."""
    основа = [{"id": "budget", "label": "Своя", "kind": "topic", "who": "any",
               "rule": "слово"}]
    слито = категории_модуль.с_набором("sales", основа)
    имена = [к["id"] for к in слито]
    assert len(имена) == len(set(имена)), имена
    assert "budget" in имена and "budget_sales" in имена


def test_банковский_набор_ловит_своё():
    """Проверка по делу: правила должны срабатывать на настоящих репликах."""
    набор = категории_модуль.compile(категории_модуль.с_набором("banking"))
    реплики = [
        {"start": 0.0, "end": 3.0, "speaker": "A", "text": "мне заблокировали карту"},
        {"start": 3.0, "end": 6.0, "speaker": "B",
         "text": "назовите код из смс, это служба безопасности"},
    ]
    итог = категории_модуль.apply(реплики, набор)
    сработали = {с["category"] for с in категории_модуль.for_db(итог)}
    assert "card_block" in сработали
    assert "fraud_signal" in сработали


def test_свой_набор_важнее_отраслевого(tmp_path: Path, monkeypatch):
    """Правила, написанные руками, не получают довеска, о котором не просили."""
    from asrhub.config import load
    from asrhub.content_index import ContentIndex

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path))
    настройки = load()
    настройки.set("content_category_preset", "banking")
    db = Database(tmp_path / "ci.sqlite3")
    индекс = ContentIndex(db=db, settings=настройки)
    с_отраслью = len(индекс.categories())
    assert с_отраслью > len(категории_модуль.ГОТОВЫЕ)

    настройки.set("content_categories", [
        {"id": "своя", "label": "Своя", "kind": "topic", "who": "any",
         "rule": "слово"}])
    свой = ContentIndex(db=db, settings=настройки)
    assert len(свой.categories()) == 1
    db.close()


def test_наборы_доезжают_до_редактора(client):
    ответ = client.get("/api/content/kinds")
    assert ответ.status_code == 200
    наборы = ответ.json().get("category_presets")
    assert наборы and {"id", "title", "description", "count"} <= set(наборы[0])


# --------------------------------------------------------------------------
# Темы без правил
# --------------------------------------------------------------------------

from asrhub import topics as темы_модуль  # noqa: E402

ОПЛАТА = {"оплат": 3, "карт": 2, "спис": 2, "счет": 1, "банк": 1, "перевод": 1}
ДОСТАВКА = {"доставк": 4, "курьер": 3, "адрес": 1, "посылк": 2, "срок": 1, "трек": 1}
ЧАСТОТЫ = {**dict.fromkeys(ОПЛАТА, 6), **dict.fromkeys(ДОСТАВКА, 6)}


def _корпус(сколько: int = 12) -> list[tuple[str, dict[str, int]]]:
    return [(f"j{н}", dict(ОПЛАТА if н % 2 == 0 else ДОСТАВКА))
            for н in range(сколько)]


def test_похожие_записи_собираются_в_свои_кучки():
    найдено = темы_модуль.собрать(_корпус(), ЧАСТОТЫ, 12, минимум=2)
    assert len(найдено) == 2
    основы = [set(т.stems[:3]) for т in найдено]
    assert any("оплат" in о for о in основы)
    assert any("доставк" in о for о in основы)
    # Записи не задвоились и не потерялись.
    все = [ид for т in найдено for ид in т.jobs]
    assert sorted(все) == sorted(ид for ид, _ in _корпус())


def test_частые_слова_не_склеивают_всё_в_одну_кучу():
    """Без TF-IDF все разговоры похожи друг на друга общими словами.

    Слова взяты НЕ из списка «ШУМ» намеренно: тот отсеивается заранее, и
    проверка на нём прошла бы и без взвешивания. Здесь именно обычные слова,
    которые просто есть у всех, — от них спасает только IDF.
    """
    шумные = [(ид, {**основы, "договор": 6, "клиент": 5, "компан": 5})
              for ид, основы in _корпус()]
    # Размер корпуса настоящий, а не двенадцать: IDF о том и говорит, что
    # слово из всех записей архива ничего не различает, а слово из шести
    # процентов — различает. На корпусе в двенадцать записей эта разница
    # вырождается, и проверка мерила бы не то.
    частоты = {**dict.fromkeys(ЧАСТОТЫ, 60),
               "договор": 1000, "клиент": 1000, "компан": 1000}
    найдено = темы_модуль.собрать(шумные, частоты, 1000, минимум=2)
    assert len(найдено) == 2, "частые слова склеили разные темы"


def test_кучка_из_двух_записей_темой_не_считается():
    """Одна запись — это запись, а не тема; список расшифровок не находка."""
    найдено = темы_модуль.собрать(
        [("a", ОПЛАТА), ("b", ДОСТАВКА)], ЧАСТОТЫ, 2, минимум=0)
    assert all(т.size >= 2 for т in найдено)


def test_покрытие_правилами_считается_по_записям():
    найдено = темы_модуль.собрать(_корпус(), ЧАСТОТЫ, 12, минимум=2)
    попадания = {"j0": ["payment"], "j2": ["payment"], "j4": ["payment"],
                 "j6": ["payment"], "j8": ["payment"], "j10": ["payment"]}
    темы_модуль.пометить_покрытие(найдено, попадания)
    про_оплату = [т for т in найдено if "оплат" in т.stems[:3]][0]
    про_доставку = [т for т in найдено if "доставк" in т.stems[:3]][0]
    assert про_оплату.to_dict(12)["uncovered"] is False
    assert про_доставку.to_dict(12)["uncovered"] is True
    assert про_оплату.categories["payment"] == 6


def test_модель_даёт_теме_название():
    найдено = темы_модуль.собрать(_корпус(), ЧАСТОТЫ, 12, минимум=2)
    спрошено: list[str] = []

    def спросить(подсказка: str) -> str:
        спрошено.append(подсказка)
        return "Вопросы по оплате\nещё строка"

    темы_модуль.назвать(найдено, спросить, {"j0": "текст записи"})
    assert all(т.title == "Вопросы по оплате" for т in найдено)
    assert "текст записи" in спрошено[0]


def test_молчание_модели_не_роняет_отчёт():
    """Тема без красивого названия всё равно тема."""
    найдено = темы_модуль.собрать(_корпус(), ЧАСТОТЫ, 12, минимум=2)

    def падает(_: str) -> str:
        raise RuntimeError("модель недоступна")

    темы_модуль.назвать(найдено, падает, {})
    assert all(not т.title for т in найдено)
    # А в ответе вместо названия — ведущие слова, а не пустота.
    assert найдено[0].to_dict(12)["title"]


def test_темы_доезжают_до_api(client):
    db = client.app.state.hub.db
    сейчас = time.time()
    for н in range(12):
        основы = ОПЛАТА if н % 2 == 0 else ДОСТАВКА
        ид = f"t{н}"
        db.execute("INSERT INTO jobs (id, created_at, updated_at, status, text) "
                   "VALUES (?,?,?,'completed',?)", (ид, сейчас - н * 60, сейчас, "текст"))
        db.save_content(ид, {"version": 7},
                        [{"stem": к, "n": з} for к, з in основы.items()])
    ответ = client.get("/api/content/discover?period=week&min_size=2&name=false")
    assert ответ.status_code == 200
    тело = ответ.json()
    assert тело["records"] == 12
    assert len(тело["topics"]) == 2
    assert тело["uncovered"] == 2


def test_короткие_записи_не_идут_в_кластеризацию(tmp_path: Path):
    """На трёх словах похожесть считается, но означает случайность."""
    db = Database(tmp_path / "t.sqlite3")
    сейчас = time.time()
    db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
               "VALUES ('j1',?,?,'completed')", (сейчас, сейчас))
    db.save_content("j1", {"version": 7},
                    [{"stem": "да", "n": 1}, {"stem": "нет", "n": 1}])
    assert db.terms_matrix() == []
    assert len(db.terms_matrix(min_terms=1)) == 1
    db.close()


def test_правила_набора_отдаются_редактору(client):
    """Чтобы человек увидел правила до сохранения, а не после."""
    пустой = client.get("/api/content/kinds").json()
    assert пустой["preset_categories"] == []
    с_набором = client.get("/api/content/kinds?preset=banking").json()
    правила = с_набором["preset_categories"]
    assert правила and {"id", "label", "rule", "kind"} <= set(правила[0])
    assert any(п["id"] == "card_block" for п in правила)


# --------------------------------------------------------------------------
# Контроль качества работы операторов
# --------------------------------------------------------------------------

from asrhub import qa as qa_модуль  # noqa: E402


def _база_с_разбором(tmp_path: Path, баллы: list[float]) -> Database:
    db = Database(tmp_path / "qa.sqlite3")
    сейчас = time.time()
    for н, балл in enumerate(баллы):
        ид = f"q{н}"
        db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
                   "VALUES (?,?,?,'completed')", (ид, сейчас - н * 60, сейчас))
        db.save_content(ид, {"version": 7, "agent_score": балл})
    return db


def test_две_проверки_одной_записи_не_заводятся(tmp_path: Path):
    """Два балла за один разговор — это спор о том, какой настоящий."""
    db = _база_с_разбором(tmp_path, [80.0])
    первый = db.qa_assign("q0", assigned_to="anna", auto_score=80)
    assert первый is not None
    assert db.qa_assign("q0", assigned_to="boris") is None
    db.close()


def test_проверка_несуществующей_записи_не_заводится(tmp_path: Path):
    db = _база_с_разбором(tmp_path, [80.0])
    assert db.qa_assign("нет-такой") is None
    db.close()


def test_согласие_считается_само(tmp_path: Path):
    """Расхождение больше десяти баллов из ста — уже другая оценка."""
    db = _база_с_разбором(tmp_path, [80.0, 80.0])
    близко = db.qa_assign("q0", auto_score=80)
    далеко = db.qa_assign("q1", auto_score=80)
    db.qa_submit(близко, reviewer="anna", score=75)
    db.qa_submit(далеко, reviewer="anna", score=60)
    исходы = {з["job_id"]: з["agree"] for з in db.qa_list()}
    assert исходы["q0"] == 1
    assert исходы["q1"] == 0
    db.close()


def test_закрытую_проверку_второй_раз_не_записать(tmp_path: Path):
    db = _база_с_разбором(tmp_path, [80.0])
    ид = db.qa_assign("q0", auto_score=80)
    assert db.qa_submit(ид, reviewer="anna", score=70) is True
    assert db.qa_submit(ид, reviewer="boris", score=90) is False
    db.close()


def test_сдвиг_проверяющего_считается_отдельно_от_балла(tmp_path: Path):
    """Средний балл говорит о записях, сдвиг — о самом проверяющем.

    Строгая и мягкая проверяющие смотрят записи с одинаковым баллом автомата.
    Средний балл у них разный — но это не про них, а про то, что они увидели;
    про них говорит именно сдвиг.
    """
    db = _база_с_разбором(tmp_path, [80.0, 80.0, 80.0, 80.0])
    # Девяносто не годится: расхождение ровно в десять баллов — это ещё
    # согласие (граница включающая), и проверка мерила бы не сдвиг.
    for н, (кто, балл) in enumerate([("строгая", 60.0), ("строгая", 65.0),
                                     ("мягкая", 93.0), ("мягкая", 95.0)]):
        ид = db.qa_assign(f"q{н}", auto_score=80.0)
        db.qa_submit(ид, reviewer=кто, score=балл)
    сводка = db.qa_stats()
    по_людям = {ч["reviewer"]: ч for ч in сводка["reviewers"]}
    assert по_людям["строгая"]["bias"] < -10
    assert по_людям["мягкая"]["bias"] > 10
    assert сводка["done"] == 4
    assert сводка["agree_share"] == 0.0
    db.close()


def test_выборка_смешанная_а_не_только_слабые(tmp_path: Path):
    """Только слабые — это травля одних и тех же людей.

    Проверка идёт по составу назначенного: в нём должны быть и записи с
    низким баллом, и записи с высоким.
    """
    db = _база_с_разбором(tmp_path, [10.0, 15.0, 20.0, 90.0, 92.0, 95.0, 97.0, 99.0])
    настройки = {"qa_enabled": True, "qa_daily": 4, "qa_worst_share": 0.5,
                 "qa_window_hours": 72.0, "qa_due_hours": 48.0}
    назначено = qa_модуль.набрать(db, настройки)
    assert len(назначено) == 4
    поводы = {з["reason"] for з in назначено}
    assert "слабый балл" in поводы
    assert "случайная выборка" in поводы
    db.close()


def test_суточная_порция_не_набирается_дважды(tmp_path: Path):
    """Пачка в двести проверок не делается никогда."""
    db = _база_с_разбором(tmp_path, [50.0] * 10)
    настройки = {"qa_enabled": True, "qa_daily": 3, "qa_worst_share": 0.5}
    assert len(qa_модуль.набрать(db, настройки)) == 3
    assert qa_модуль.набрать(db, настройки) == []
    db.close()


def test_выключенный_контроль_ничего_не_набирает(tmp_path: Path):
    db = _база_с_разбором(tmp_path, [50.0] * 5)
    assert qa_модуль.набрать(db, {"qa_enabled": False, "qa_daily": 5}) == []
    db.close()


def test_просроченные_считаются_отдельно(tmp_path: Path):
    """Проверка без срока — не проверка, а пожелание."""
    db = _база_с_разбором(tmp_path, [80.0, 80.0])
    сейчас = time.time()
    db.qa_assign("q0", due_at=сейчас - 3600)
    db.qa_assign("q1", due_at=сейчас + 3600)
    просрочено = qa_модуль.просроченные(db, now=сейчас)
    assert [з["job_id"] for з in просрочено] == ["q0"]
    db.close()


def test_проверки_доезжают_до_api(client):
    db = client.app.state.hub.db
    сейчас = time.time()
    db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
               "VALUES ('qa1',?,?,'completed')", (сейчас, сейчас))
    db.save_content("qa1", {"version": 7, "agent_score": 70.0})
    создано = client.post("/api/qa?job_id=qa1&assigned_to=anna")
    assert создано.status_code == 200
    ид = создано.json()["id"]
    # Повторное назначение той же записи отвергается с объяснением.
    assert client.post("/api/qa?job_id=qa1").status_code >= 400
    сдано = client.put(f"/api/qa/{ид}", json={"score": 55, "comment": "мягко"})
    assert сдано.status_code == 200
    список = client.get("/api/qa?status=done").json()
    assert список["items"][0]["score"] == 55.0
    assert список["items"][0]["agree"] == 0
    assert список["stats"]["done"] >= 1


def test_балл_вне_шкалы_отвергается(client):
    db = client.app.state.hub.db
    сейчас = time.time()
    db.execute("INSERT INTO jobs (id, created_at, updated_at, status) "
               "VALUES ('qa2',?,?,'completed')", (сейчас, сейчас))
    db.save_content("qa2", {"version": 7, "agent_score": 70.0})
    ид = client.post("/api/qa?job_id=qa2").json()["id"]
    assert client.put(f"/api/qa/{ид}", json={"score": 140}).status_code >= 400
    assert client.put(f"/api/qa/{ид}", json={"score": "мало"}).status_code >= 400


# --------------------------------------------------------------------------
# Текстовые каналы
# --------------------------------------------------------------------------

from asrhub import textchat  # noqa: E402

ПЕРЕПИСКА = [
    {"speaker": "Клиент", "text": "здравствуйте, когда будет доставка? "
                                  "курьер уже третий день не приезжает"},
    {"speaker": "Оператор", "text": "добрый день! проверю и перезвоню сегодня"},
    {"speaker": "Клиент", "text": "спасибо, буду ждать"},
]


def test_переписка_разбирается_из_строки():
    """Экспорт чата приходит строками «Кто: текст», и это надо принимать."""
    реплики, текст = textchat.разобрать(
        "Клиент: когда доставка\nОператор: завтра\nуточню у курьера")
    assert [р["speaker"] for р in реплики] == ["Клиент", "Оператор"]
    # Строка без подписи — продолжение предыдущей реплики, а не новая.
    assert реплики[1]["text"] == "завтра уточню у курьера"
    assert "Клиент:" in текст


def test_переписка_разбирается_из_списка():
    реплики, _ = textchat.разобрать(ПЕРЕПИСКА)
    assert len(реплики) == 3
    assert реплики[0]["start"] == 0.0 and реплики[1]["start"] == 1.0


def test_пустая_переписка_отвергается():
    with pytest.raises(Exception, match="реплик"):
        textchat.разобрать([{"speaker": "Клиент", "text": "   "}])
    with pytest.raises(Exception, match="текстом или списком"):
        textchat.разобрать(42)


def test_оператор_узнаётся_по_названию_стороны():
    """Иначе скрипт и балл считались бы по клиенту."""
    реплики, _ = textchat.разобрать(ПЕРЕПИСКА)
    assert textchat.оператор_среди(реплики) == "Оператор"
    assert textchat.оператор_среди(реплики, "Клиент") == "Клиент"
    # Подсказка, которой нет среди сторон, игнорируется.
    assert textchat.оператор_среди(реплики, "Вася") == "Оператор"


def test_у_переписки_нет_показателей_в_секундах():
    """Ноль пауз в чате читается как «отвечали мгновенно» — это ложь.

    Между репликами в переписке проходит минута или час, и это не пауза в
    разговоре. Показатель, посчитанный не по чему, хуже отсутствующего.
    """
    реплики, _ = textchat.разобрать(ПЕРЕПИСКА)
    речь = speech.analyze(реплики, 0.0, timed=False)
    assert речь["timed"] is False
    for ключ in ("wpm", "pauses", "long_pauses", "silence_share", "dead_air_s",
                 "interruptions", "overlap_s", "duration_s", "longest_pause_s"):
        assert речь[ключ] is None, ключ
    # А то, что в переписке есть, считается как обычно.
    assert речь["words"] > 0
    assert речь["switches"] == 2


def test_доля_участия_в_переписке_считается_по_словам():
    """По секундам её считать не из чего, по репликам — бессмысленно.

    Данные подобраны так, чтобы доля по словам и доля по репликам расходились
    втрое: оператор написал одно длинное сообщение, клиент — два коротких.
    На похожих числах проверка прошла бы и для неверного способа счёта.
    """
    переписка = [
        {"speaker": "Клиент", "text": "здравствуйте"},
        {"speaker": "Оператор", "text": " ".join(["слово"] * 30)},
        {"speaker": "Клиент", "text": "спасибо"},
    ]
    реплики, _ = textchat.разобрать(переписка)
    речь = speech.analyze(реплики, 0.0, timed=False)
    доли = {с["speaker"]: с["share"] for с in речь["speakers"]}
    # По словам: 30 из 32. По репликам было бы 1 из 3.
    assert доли["Оператор"] == pytest.approx(30 / 32, abs=0.01)
    assert доли["Клиент"] == pytest.approx(2 / 32, abs=0.01)
    assert sum(доли.values()) == pytest.approx(1.0, abs=0.01)


def test_разговор_со_звуком_показателей_не_теряет():
    """Обратная сторона: обычная запись должна считаться как раньше."""
    речь = speech.analyze(РАЗГОВОР, 15.0)
    assert речь["timed"] is True
    assert речь["pauses"] == 2 and речь["wpm"] is not None


def test_переписка_проходит_весь_разбор(client):
    ответ = client.post("/api/jobs/text", json={
        "channel": "chat", "messages": ПЕРЕПИСКА, "external_id": "t-42"})
    assert ответ.status_code == 200
    тело = ответ.json()
    assert тело["messages"] == 3
    assert тело["agent"] == "Оператор"
    разбор = тело["content"]
    сработали = set((разбор.get("categories") or {}).get("matched") or [])
    assert "delivery" in сработали
    assert (разбор.get("speech") or {})["timed"] is False
    # Задание видно как обычное завершённое, но помечено источником.
    задание = client.get(f"/api/jobs/{тело['id']}").json()
    assert задание["status"] == "completed"
    assert задание["source"] == "text"


def test_неизвестный_канал_принимается(client):
    """Список каналов у каждого свой; спорить с интеграцией незачем."""
    ответ = client.post("/api/jobs/text", json={
        "channel": "telegram", "messages": ПЕРЕПИСКА})
    assert ответ.status_code == 200
    assert ответ.json()["known_channel"] is False


def test_переписка_без_реплик_отвергается_с_объяснением(client):
    ответ = client.post("/api/jobs/text", json={"messages": []})
    assert ответ.status_code >= 400
    assert "реплик" in json.dumps(ответ.json(), ensure_ascii=False)


# --------------------------------------------------------------------------
# Подсказки в реальном времени
# --------------------------------------------------------------------------

from asrhub.liveassist import Помощник  # noqa: E402


def test_нарушение_подсказывается_сразу():
    """Стоп-слово оператора — самая срочная подсказка из всех."""
    помощник = Помощник.из_настроек({}, agent="оператор")
    подсказки = помощник.добавить("сами виноваты, не перебивайте",
                                  speaker="оператор", start=0, end=4)
    виды = [п.kind for п in подсказки]
    assert "violation" in виды
    assert подсказки[0].kind == "violation", "нарушение должно быть первым"


def test_возражение_без_отработки_подсказывается():
    помощник = Помощник.из_настроек({}, agent="оператор")
    помощник.добавить("здравствуйте", speaker="оператор", start=0, end=2)
    подсказки = помощник.добавить("это дорого, мне не подходит",
                                  speaker="клиент", start=2, end=6)
    assert any(п.kind == "objection" for п in подсказки)


def test_одна_подсказка_не_повторяется():
    """Повторять «представьтесь» каждые три секунды — способ быть выключенным."""
    помощник = Помощник.из_настроек({}, agent="оператор")
    первый = помощник.добавить("сами виноваты", speaker="оператор", start=0, end=3)
    второй = помощник.добавить("сами виноваты снова", speaker="оператор",
                               start=3, end=6)
    assert any(п.kind == "violation" for п in первый)
    assert not any(п.kind == "violation" for п in второй)


def test_скрипт_напоминает_не_сразу():
    """Пока человек здоровается, напоминать о приветствии бессмысленно."""
    помощник = Помощник.из_настроек({}, agent="оператор")
    рано = помощник.добавить("алло", speaker="оператор", start=0, end=3)
    assert not any(п.kind == "script" for п in рано)
    поздно = помощник.добавить("ну так что", speaker="оператор", start=30, end=40)
    assert any(п.kind == "script" for п in поздно)


def test_пустая_реплика_ничего_не_даёт():
    помощник = Помощник.из_настроек({}, agent="оператор")
    assert помощник.добавить("   ", speaker="оператор") == []
    assert помощник.реплики == []


def _помощник_с_трекером() -> Помощник:
    """Помощник, у которого срабатывают все четыре вида подсказок разом."""
    набор = [*категории_модуль.ГОТОВЫЕ,
             {"id": "vip", "label": "Важный клиент", "kind": "topic",
              "who": "any", "rule": "договор", "notify": True},
             {"id": "urgent", "label": "Срочное", "kind": "topic",
              "who": "any", "rule": "срочно", "notify": True}]
    return Помощник.из_настроек({"content_categories": набор}, agent="оператор")


def test_подсказок_разом_не_больше_трёх():
    """В живом разговоре больше трёх строк не читает никто.

    Реплика подобрана так, чтобы сработало ЧЕТЫРЕ вида сразу: нарушение,
    трекер, возражение и скрипт. На трёх проверка прошла бы и без ограничения.
    """
    помощник = _помощник_с_трекером()
    подсказки = помощник.добавить(
        "сами виноваты, договор нужен срочно",
        speaker="оператор", start=0, end=40)
    assert len(подсказки) == 3


def test_нарушение_важнее_остальных_подсказок():
    """Когда подсказок больше, чем помещается, первым идёт нарушение.

    Порядок здесь не украшение: в отсечку по три попадает то, что наверху, и
    неверный вес означает, что стоп-слово оператора просто не покажут.
    """
    помощник = _помощник_с_трекером()
    подсказки = помощник.добавить(
        "сами виноваты, договор нужен срочно",
        speaker="оператор", start=0, end=40)
    assert подсказки[0].kind == "violation"
    assert "violation" in [п.kind for п in подсказки]


def test_подсказки_идут_в_поток_только_на_закреплённом():
    """Гипотеза переписывается на каждом куске — подсказка по ней мигала бы."""
    from asrhub.streaming import StreamEvent, StreamSession

    сессия = StreamSession.__new__(StreamSession)
    сессия.settings = {"stream_agent": "оператор"}
    сессия.assist = Помощник.из_настроек({}, agent="оператор")

    предварительно = сессия._подсказать(
        [StreamEvent("partial", text="сами виноваты", start=0, end=3)])
    assert [с.type for с in предварительно] == ["partial"]

    закреплено = сессия._подсказать(
        [StreamEvent("final", text="сами виноваты", start=0, end=3)])
    виды = [с.type for с in закреплено]
    assert виды[0] == "final"
    assert "hint" in виды
    подсказка = [с for с in закреплено if с.type == "hint"][0]
    assert подсказка.extra["kind"] == "violation"


def test_без_настройки_подсказок_нет():
    from asrhub.streaming import StreamEvent, StreamSession

    сессия = StreamSession.__new__(StreamSession)
    сессия.settings = {}
    сессия.assist = None
    события = сессия._подсказать([StreamEvent("final", text="сами виноваты")])
    assert [с.type for с in события] == ["final"]


def test_сбой_подсказок_не_роняет_поток():
    """Человек в разговоре: текст важнее подсказок."""
    from asrhub.streaming import StreamEvent, StreamSession

    class Ломается:
        def добавить(self, *args, **kwargs):
            raise RuntimeError("правила испортились")

    сессия = StreamSession.__new__(StreamSession)
    сессия.settings = {}
    сессия.assist = Ломается()
    события = сессия._подсказать([StreamEvent("final", text="текст")])
    assert [с.type for с in события] == ["final"]


def test_трекер_подсказывается_в_разговоре():
    """Категория с пометкой «сообщать» — в живом разговоре нужнее всего.

    Флаг берётся у собранных правил, а не из ответа `apply`: тот его не
    возвращает, и первая версия молча не подсказывала трекеры никогда.
    """
    помощник = _помощник_с_трекером()
    подсказки = помощник.добавить("нужен договор", speaker="клиент",
                                  start=0, end=5)
    трекеры = [п for п in подсказки if п.kind == "tracker"]
    assert трекеры, "трекер не сработал"
    assert трекеры[0].category == "vip"


def test_обычная_категория_трекером_не_становится():
    """Иначе подсказки посыпались бы на каждое слово из набора.

    Набор здесь с пометкой «сообщать» у ОДНОЙ категории, а сработала другая:
    на наборе вовсе без трекеров проверка прошла бы и при снятом отборе.
    """
    помощник = _помощник_с_трекером()
    подсказки = помощник.добавить("вопрос по оплате", speaker="клиент",
                                  start=0, end=5)
    assert not [п for п in подсказки if п.kind == "tracker"]


def test_сессия_без_конструктора_не_падает_на_подсказках():
    """Проверки собирают сессию в обход конструктора — атрибута может не быть.

    Найдено полным прогоном: две прежние проверки потока делают
    `StreamSession.__new__`, чтобы не поднимать движок, и обращение к
    `self.assist` роняло их обе. Необязательная надстройка не должна ломать
    того, что работало до неё.
    """
    from asrhub.streaming import StreamEvent, StreamSession

    сессия = StreamSession.__new__(StreamSession)
    события = сессия._подсказать([StreamEvent("final", text="текст")])
    assert [с.type for с in события] == ["final"]


@нужен_bash
def test_команда_удаления_помещается_на_экран(tmp_path: Path):
    """Чек-лист показывает шесть заметок на пункт — команда должна быть в них.

    Первый же запуск на рабочем сервере обрезал ровно ту строку, ради которой
    всё и писалось: подсказка стояла после трёх строк объяснения, и на экран
    вышло «Лишнее от прежней версии — этих пакетов нет ни в одном списке», а
    команда ушла в журнал. Теперь лекарство идёт первым, а объяснение — только
    когда лекарства нет.
    """
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("onnxruntime>=1.18\n", encoding="utf-8")
    pip = tmp_path / "pip"
    pip.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "check" ]; then\n'
        + "".join(
            f'  echo "пакет{н} 1.0 has requirement transformers<4.58, '
            f'but you have transformers 5.17.0."\n' for н in range(6))
        + '  echo "optimum-onnx 0.1.0 has requirement transformers<4.58, '
          'but you have transformers 5.17.0."\n'
          "  exit 1\n"
          "fi\n"
          'if [ "$1" = "show" ]; then echo "Required-by:"; fi\n'
          "exit 0\n", encoding="utf-8")
    pip.chmod(0o755)

    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'check_dependency_health "{pip}" "{tmp_path}/req"')
    вывод = (итог.stdout + итог.stderr).splitlines()
    заметки = [с for с in вывод if с.strip()]
    # Шесть — предел чек-листа на пункт; всё, что дальше, человек не увидит.
    assert len(заметки) <= 6, "\n".join(заметки)
    команда = [с for с in заметки if "uninstall -y" in с]
    assert команда, "команда удаления не поместилась: " + "\n".join(заметки)
    # И она не последняя из шести: строка «… и ещё N» идёт после неё.
    assert заметки.index(команда[0]) <= 2


@нужен_bash
def test_без_лекарства_объяснение_остаётся(tmp_path: Path):
    """Когда сделать нечего, человеку нужно объяснение, а не пустая жалоба."""
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("transformers>=5\n", encoding="utf-8")
    pip = tmp_path / "pip"
    pip.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "check" ]; then\n'
        '  echo "transformers-extra 1.0 has requirement protobuf~=5.29, '
        'but you have protobuf 7.36.1."\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "show" ]; then echo "Required-by: transformers"; fi\n'
        "exit 0\n", encoding="utf-8")
    pip.chmod(0o755)

    вывод = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'check_dependency_health "{pip}" "{tmp_path}/req"')
    текст = вывод.stdout + вывод.stderr
    assert "Движки требуют несовместимых версий" in текст
    assert "uninstall" not in текст


@нужен_bash
def test_рядом_с_командой_нет_неверного_диагноза(tmp_path: Path):
    """«Движки требуют разных версий» — не про пакет от прежней версии."""
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "a.txt").write_text("onnxruntime>=1.18\n", encoding="utf-8")
    pip = tmp_path / "pip"
    pip.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "check" ]; then\n'
        '  echo "optimum-onnx 0.1.0 has requirement transformers<4.58, '
        'but you have transformers 5.17.0."\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "show" ]; then echo "Required-by:"; fi\n'
        "exit 0\n", encoding="utf-8")
    pip.chmod(0o755)

    итог = _прогнать(
        f'source "{КОРЕНЬ}/scripts/lib/common.sh"; '
        f'check_dependency_health "{pip}" "{tmp_path}/req"')
    текст = итог.stdout + итог.stderr
    assert "uninstall -y optimum-onnx" in текст
    assert "Движки требуют" not in текст
