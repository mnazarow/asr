"""Аналитика записей: разбор русского, словари, признаки, свод.

Разбор проверяется сличением с эталонной реализацией алгоритма Snowball, а
не набором слов, которые я счёл правильными: своё мнение о том, как должно
стеммиться слово, — это не проверка.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Разбор русского
# ---------------------------------------------------------------------------


def _эталон():
    """Эталонная реализация Snowball, если она есть в окружении."""
    try:
        import snowballstemmer
    except ImportError:                                      # pragma: no cover
        return None
    return snowballstemmer.stemmer("russian")


def test_the_stemmer_matches_the_reference_implementation(repo_root: Path):
    """Своё мнение о том, как должно стеммиться слово, — не проверка.

    Алгоритм Snowball опубликован и детерминирован, поэтому единственная
    честная проверка — сличение с его эталонной реализацией на настоящем
    русском тексте. Берём всю документацию проекта: девять тысяч разных слов
    делового и технического русского.
    """
    эталон = _эталон()
    if эталон is None:
        pytest.skip("нужен пакет snowballstemmer для сличения с эталоном")

    from asrhub.content.stemmer import stem

    текст = "".join(p.read_text(encoding="utf-8")
                    for p in sorted((repo_root / "docs").glob("*.md")))
    слова = sorted(set(re.findall(r"[а-яё]{2,}", текст.lower())))
    assert len(слова) > 3000, f"корпус для сличения мал: {len(слова)} слов"

    расхождения = [(w, stem(w), эталон.stemWord(w))
                   for w in слова if stem(w) != эталон.stemWord(w)]
    assert not расхождения, f"{len(расхождения)} расхождений, первые: {расхождения[:5]}"


def test_word_forms_collapse_but_different_words_do_not():
    """Ради чего разбор и заведён: формы одного слова считаются вместе.

    И обратное не менее важно: переусечение склеивает разные слова, а это
    отравляет и ключевые слова, и словарь оценок — основа, под которую
    попадает каждая вторая реплика, делает разрез бессмысленным.
    """
    from asrhub.content.stemmer import stem

    for формы in (
        ["договор", "договора", "договору", "договором", "договорами"],
        ["клиент", "клиента", "клиентам", "клиентами"],
        ["оплата", "оплаты", "оплате", "оплатой"],
        ["договорились", "договорился", "договориться"],   # возвратные
        ["хороший", "хорошая", "хорошие", "хорошим"],
    ):
        основы = {stem(f) for f in формы}
        assert len(основы) == 1, f"{формы} дали основы {sorted(основы)}"

    for первое, второе in (("мост", "мода"), ("хвост", "хватать"),
                           ("касса", "каска"), ("данные", "дали"),
                           ("окно", "окончание")):
        assert stem(первое) != stem(второе), (
            f"«{первое}» и «{второе}» склеились в «{stem(первое)}»")


def test_short_words_and_abbreviations_survive_stemming():
    """Местоимения и аббревиатуры в расшифровке разговора — обычное дело.

    «он» теряло «н» и становилось «о», «ООО» — «оо»: такие основы склеивают
    что попало и лезут в верх любого разреза по частоте.
    """
    from asrhub.content.stemmer import stem

    for слово in ("он", "мы", "вы", "не", "что", "дом", "бы", "же"):
        assert stem(слово) == слово.replace("ё", "е"), f"«{слово}» → «{stem(слово)}»"
    for сокращение in ("ООО", "НДС", "ИП", "АО"):
        assert stem(сокращение) == сокращение.lower(), сокращение
    # Латиница и числа не трогаются вовсе.
    assert stem("IT") == "it"
    assert stem("2026") == "2026"


def test_words_and_sentences_survive_real_transcript_punctuation():
    """Расшифровка приходит со знаками, кавычками и дефисами."""
    from asrhub.content.stemmer import sentences, words

    строка = "Здравствуйте! Это ООО «Ромашка», счёт на 15 000 руб. — из-за задержки."
    список = words(строка)
    assert "из-за" in список, список          # дефис не разрывает слово
    assert "ромашка" in список and "ооо" in список, список
    assert all("«" not in w and "," not in w for w in список), список

    предложения = sentences("Добрый день. Это по договору? Да, конечно!")
    assert len(предложения) == 3, предложения


# ---------------------------------------------------------------------------
# Разбор записи
# ---------------------------------------------------------------------------


РАЗГОВОР = [
    {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00",
     "text": "Здравствуйте, компания Ромашка, меня зовут Анна, чем могу помочь?"},
    {"start": 4.5, "end": 9.0, "speaker": "SPEAKER_01",
     "text": "Добрый день. Срок поставки сорван второй раз, я крайне недоволен."},
    {"start": 8.7, "end": 13.0, "speaker": "SPEAKER_00",
     "text": "Правильно понимаю, что заказ на 250 000 рублей до сих пор не пришёл?"},
    {"start": 13.5, "end": 18.0, "speaker": "SPEAKER_01",
     "text": "Именно так. Ещё раз сорвёте — буду жаловаться в суд."},
    {"start": 22.0, "end": 27.0, "speaker": "SPEAKER_00",
     "text": "Извините, пожалуйста. Отправлю завтра и лично проконтролирую."},
    {"start": 27.5, "end": 30.0, "speaker": "SPEAKER_01",
     "text": "Хорошо, спасибо, договорились."},
    {"start": 30.2, "end": 32.0, "speaker": "SPEAKER_00",
     "text": "До свидания, хорошего дня!"},
]


def _разбор(**kw):
    from asrhub import content

    текст = " ".join(с["text"] for с in РАЗГОВОР)
    return content.analyze(text=текст, segments=РАЗГОВОР, duration_s=32.0, **kw)


def test_analysis_finds_what_a_human_would_find_in_the_call():
    """Проверка не по одному разрезу, а по всему, ради чего разбор и нужен.

    Разговор написан так, что человек назовёт в нём то же самое: клиент
    недоволен, звучала угроза судом, названа сумма, оператор пообещал
    отправить завтра, к концу разговор выправился.
    """
    р = _разбор()

    assert р["sentiment"]["score"] < 0, р["sentiment"]
    # Разворот к лучшему: начали руганью, кончили «спасибо, договорились».
    assert р["sentiment"]["turn"]["shift"] > 0, р["sentiment"]["turn"]

    # Суд — то, ради чего разрез тревожных упоминаний и заведён.
    слова_тревоги = {w for т in р["alerts"]["items"] for w in т["words"]}
    assert "суд" in слова_тревоги, р["alerts"]

    # Сумма и срок: их выписывают из записи руками, и это самая частая
    # причина слушать её второй раз.
    assert 250000.0 in {с["amount"] for с in р["entities"]["money"]}, р["entities"]
    assert "завтра" in р["entities"]["deadlines"], р["entities"]

    обещания = {о["text"] for о in р["commitments"]["items"]}
    assert any("Отправлю завтра" in о for о in обещания), обещания
    assert р["commitments"]["with_deadline"] >= 1, р["commitments"]

    assert р["questions"]["count"] >= 2, р["questions"]

    # Оператор определился сам — по тому, кто заговорил первым.
    assert р["compliance"]["speaker"] == "SPEAKER_00", р["compliance"]
    выполнено = {п["id"] for п in р["compliance"]["items"] if п["passed"]}
    assert {"greeting", "introduce", "company", "farewell"} <= выполнено, выполнено

    # Перебивание: третья реплика начинается раньше конца второй.
    assert р["speech"]["interruptions"] == 1, р["speech"]
    # Пауза: между 18.0 и 22.0 — четыре секунды.
    assert р["speech"]["pauses"] == 1, р["speech"]
    assert р["speech"]["longest_pause_s"] == 4.0, р["speech"]

    assert р["politeness"]["приветствие"] and р["politeness"]["извинение"]


def test_negation_and_intensifiers_change_the_score_the_way_they_should():
    """Иначе словарь оценок считает «не понравилось» похвалой.

    Отрицание — не украшение разбора, а условие его осмысленности: в живой
    речи оно встречается в каждой третьей оценочной фразе.
    """
    from asrhub.content.sentiment import score_text

    assert score_text("Мне понравилось").score > 0
    assert score_text("Мне не понравилось").score < 0
    assert score_text("Всё отлично").score > 0
    assert score_text("Не сказать что отлично").score < 0
    # Усилитель поднимает величину, но не переворачивает знак. Проверка
    # именно на величине, а не на знаке: пока оценка обрезалась по границе,
    # «плохо» и «очень плохо» давали одно и то же число, и усилитель, хоть
    # и считался, не значил ничего.
    обычно = score_text("Плохое обслуживание").score
    сильно = score_text("Очень плохое обслуживание").score
    assert сильно < обычно < 0, (обычно, сильно)

    # И оценка не упирается в границу: пока она обрезалась, список «самых
    # тяжёлых разговоров» становился списком записей с одинаковой оценкой
    # −1.00, то есть выбранных как попало. Проверяем именно то, что тогда
    # ломалось: две разные по силе брани различаются числом и ни одна не
    # достигает −1.
    сильно = score_text("Отвратительно, ужасно, безобразие, кошмар").score
    сильнее = score_text(
        "Крайне отвратительно, совершенно ужасно, полное безобразие").score
    assert -1.0 < сильнее < сильно < -0.5, (сильно, сильнее)


def test_the_lexicon_stays_quiet_where_there_is_nothing_to_evaluate():
    """Словарь обязан молчать там, где оценки нет.

    Каждое ложное срабатывание — это разговор, который в отчёте окажется
    отрицательным без всякой причины, и оператор, которого по такому отчёту
    вызовут объясняться.
    """
    from asrhub.content.sentiment import score_text

    нейтральные = [
        "Заказ номер 4512 отгружен со склада в Подольске третьего июня.",
        "Договор подписан обеими сторонами, счёт выставлен на оплату.",
        "По дороге к вам, буду через сорок минут, ждите у второго подъезда.",
        "Вопрос по срокам поставки я передал в отдел логистики.",
        "Года через два планируем открыть второй склад в этом же районе.",
        "Ради ускорения отправки укажите индекс и номер телефона получателя.",
    ]
    сработало = [(т, round(score_text(т).score, 3), score_text(т).words)
                 for т in нейтральные if abs(score_text(т).score) > 0.15]
    assert not сработало, f"словарь сработал на нейтральном тексте: {сработало}"


def test_frequent_neutral_words_never_collide_with_the_lexicon():
    """Защита от переусечения: частое нейтральное слово не должно оценивать.

    Основа, под которую попадает каждая вторая реплика, отравляет весь
    разрез: «договор» и «дорого» дают разные основы, а вот «дороге» и
    «дорого» — при более грубом усечении одну, и тогда каждая запись с
    описанием маршрута становилась бы жалобой на цену. Эти слова уже
    приходилось убирать из словаря по одному; список — чтобы они не
    вернулись туда следующей правкой.
    """
    from asrhub.content.sentiment import score_text

    безобидные = ["договор", "договора", "договоре", "вопрос", "вопроса",
                  "года", "году", "дороге", "дорогу", "ради", "сроки",
                  "заказ", "заказа", "работа", "работы", "отдел", "склад",
                  "поставка", "номер", "адрес", "клиент", "менеджер"]
    сработали = [(с, score_text(с).words) for с in безобидные
                 if score_text(с).score != 0.0]
    assert not сработали, f"нейтральные слова попали в словарь оценок: {сработали}"


def test_features_split_the_analysis_the_way_the_database_stores_it():
    """Свод, основы и подробности — три разные вещи, и путать их нельзя."""
    from asrhub import content
    from asrhub.db import Database

    разбор = _разбор()
    свод, основы = content.features(разбор)

    колонки = {к.strip() for к in Database.CONTENT_COLUMNS.split(",")}
    # job_id и computed_at проставляет сама запись в базу.
    assert set(свод) - {"detail"} <= колонки, set(свод) - колонки
    assert свод["version"] == content.VERSION
    assert свод["alerts"] == разбор["alerts"]["count"]
    assert свод["commitments_dated"] == разбор["commitments"]["with_deadline"]
    assert свод["agent_speaker"] == "SPEAKER_00"

    # Основы уходят в свой перечень и в подробностях их быть не должно:
    # иначе карточка записи таскала бы служебный словарь.
    assert основы and all({"stem", "word", "n"} <= set(т) for т in основы)
    assert "terms" not in свод["detail"]


def test_the_script_can_be_replaced_and_is_checked_where_it_should_be():
    """Скрипт у каждого свой; проверка «в начале» — это именно начало."""

    свой = [
        {"id": "promo", "label": "Предложил акцию",
         "any": ["акция", "специальное предложение"], "where": "any"},
        {"id": "hello", "label": "Поздоровался", "any": ["здравствуйте"],
         "where": "start"},
    ]
    р = _разбор(script=свой)
    пункты = {п["id"]: п["passed"] for п in р["compliance"]["items"]}
    assert пункты == {"promo": False, "hello": True}, р["compliance"]

    # «До свидания» звучит в конце, поэтому пункт «в начале» его не найдёт.
    только_в_начале = [{"id": "bye", "label": "Попрощался",
                        "any": ["до свидания"], "where": "start"}]
    assert not _разбор(script=только_в_начале)["compliance"]["items"][0]["passed"]


# ---------------------------------------------------------------------------
# Хранение признаков и свод по корпусу
# ---------------------------------------------------------------------------


class _Настройки:
    """Заглушка настроек: разбор берёт из них скрипт и метку оператора."""

    def __init__(self, **значения):
        self.значения = {"content_analysis": True, "content_backfill": True,
                         "content_backfill_batch": 50, "content_script": [],
                         "content_agent_speaker": "", **значения}

    def get(self, ключ, по_умолчанию=None):
        return self.значения.get(ключ, по_умолчанию)


def _корпус(tmp_path, записей: int = 30, шум: int = 0, общих: int = 0):
    """Небольшой архив завершённых заданий с расшифровками.

    `шум` добавляет в каждую запись столько уникальных слов, `общих` —
    столько одинаковых во всех записях. Общие слова встречаются во всех
    записях и потому стоят в самом верху любого разреза тем, вытесняя из
    среза настоящие темы: это и есть условие, при котором ломалось
    сравнение окон.
    """
    from asrhub.db import Database

    хорошая = ("Здравствуйте, компания Ромашка, меня зовут Анна. Договор готов, "
               "отправлю сегодня. Спасибо, всего доброго!")
    плохая = ("Срок поставки сорван, я крайне недоволен, отвратительное "
              "качество. Буду жаловаться в суд.")
    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for n in range(записей):
        текст = плохая if n % 3 == 0 else хорошая
        if шум:
            текст += " " + " ".join(f"словцо{n}штука{k}" for k in range(шум))
        if общих:
            текст += " " + " ".join(f"общеслово{k}" for k in range(общих))
        когда = сейчас - n * 3600
        job_id = db.create_job({
            "id": f"job{n:04d}", "filename": f"{n}.wav", "media_duration_s": 40.0,
            "owner": "anna" if n % 2 else "boris", "engine": "gigaam",
            "model": "v2", "language": "ru", "source": "api",
            # Метка «vip» стоит только у благополучных записей и заметно
            # реже второй: на равномерной раскладке разрез по меткам
            # сходится сам собой и не проверяет ничего.
            "tags": ("продажи,vip" if (n % 3 and n % 2 == 0) else "продажи")})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, job_id))
        db.update_job(job_id, status="completed", text=текст, finished_at=когда)
        db.save_segments(job_id, [
            {"start": 0.0, "end": 8.0, "speaker": "SPEAKER_00",
             "text": текст.split(". ")[0]},
            {"start": 9.0, "end": 20.0, "speaker": "SPEAKER_01",
             "text": ". ".join(текст.split(". ")[1:])},
        ])
    return db


def test_the_archive_is_analysed_in_the_background_and_only_once(tmp_path):
    """Пересчёт накопленного архива: порциями, до конца и без повторов."""
    from asrhub import content
    from asrhub.content_index import ContentIndex

    db = _корпус(tmp_path, 30)
    индекс = ContentIndex(db, _Настройки(content_backfill_batch=7))

    сначала = индекс.status()
    assert сначала["total"] == 30 and сначала["analyzed"] == 0

    порции = []
    while (сделано := индекс.backfill_once()):
        порции.append(сделано)
    assert порции[0] == 7, порции          # порция ровно та, что в настройках
    assert sum(порции) == 30, порции       # и дошли до конца
    assert индекс.backfill_once() == 0     # второго прохода по тем же не будет

    после = индекс.status()
    assert после == {**после, "analyzed": 30, "pending": 0}

    # Смена версии разбора возвращает архив в очередь — ради этого версия
    # и существует: словари меняются, и старые числа сравнивать с новыми
    # нельзя.
    ожидают = db.content_pending(content.VERSION + 1, limit=100)
    assert len(ожидают) == 30, len(ожидают)


def test_deleting_a_job_takes_its_analysis_and_its_terms_with_it(tmp_path):
    """Иначе удалённая запись навсегда остаётся в сводах и в весе тем."""
    from asrhub.content_index import ContentIndex

    db = _корпус(tmp_path, 12)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    _, корпус = db.document_frequency()
    assert корпус == 12
    assert db.get_content("job0000") is not None

    db.delete_job("job0000")
    assert db.get_content("job0000") is None
    _, после = db.document_frequency()
    assert после == 11, после
    assert db.query("SELECT 1 FROM content_terms WHERE job_id='job0000'") == []


def test_the_summary_counts_the_same_thing_no_matter_how_it_is_sliced(tmp_path):
    """Разрез — это та же арифметика с группировкой, и сходиться он обязан.

    Проверка не формальная: свод считается одним запросом к базе, а разрезы
    — другим, с группировкой, и метки вдобавок доскладываются в памяти,
    потому что база не умеет разбирать строку «продажи,vip». Разойтись
    здесь легче лёгкого, а заметить расхождение по глазам — нельзя.
    """
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 24)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    свод_класс = Insights(db)

    свод = свод_класс.summary("all")
    assert свод["records"] == 24
    assert свод["negative"] + свод["positive"] + свод["neutral"] == свод["scored"]

    for разрез in ("owner", "model", "source", "weekday", "hour"):
        группы = свод_класс.breakdown(разрез, "all")["items"]
        assert sum(г["records"] for г in группы) == свод["records"], разрез
        assert sum(г["negative"] for г in группы) == свод["negative"], разрез

    # У меток сумма больше: запись с двумя метками попадает в обе. Это не
    # ошибка, а свойство разреза, и проверяем именно его.
    метки = {г["key"]: г for г in свод_класс.breakdown("tag", "all")["items"]}
    assert метки["продажи"]["records"] == 24
    assert метки["vip"]["records"] == 8
    assert "без метки" not in метки


def test_weighted_averages_survive_splitting_a_tag_combination(tmp_path):
    """Среднее по метке — взвешенное, иначе редкое сочетание весит как частое."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 24)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    свод_класс = Insights(db)

    общий = свод_класс.summary("all")
    метки = {г["key"]: г for г in свод_класс.breakdown("tag", "all")["items"]}
    # «продажи» стоит у всех записей, значит её среднее обязано в точности
    # совпасть с общим. Сочетаний два — «продажи» и «продажи,vip», — они
    # разного размера и разной тональности, поэтому простое среднее от их
    # средних даёт другое число, и проверка это увидит.
    assert метки["vip"]["sentiment"] > метки["продажи"]["sentiment"], метки
    assert abs(метки["продажи"]["sentiment"] - общий["sentiment"]) < 0.0005, (
        метки["продажи"]["sentiment"], общий["sentiment"])


def test_tf_idf_demotes_words_that_are_in_every_record(tmp_path):
    """Ради чего корпусная частота и заведена.

    «Договор» есть в каждой второй записи и темой быть не может; без
    знаменателя он занимал бы верх ключевых слов в каждой из них.
    """
    from asrhub import content
    from asrhub.content_index import ContentIndex

    db = _корпус(tmp_path, 24)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    частоты, размер = db.document_frequency()
    assert размер == 24 and частоты

    текст = db.get_job("job0001")["text"]
    без = [к["word"] for к in content.analyze(text=текст)["keywords"]]
    с_корпусом = [к["word"] for к in content.analyze(
        text=текст, document_frequency=частоты, corpus_size=размер)["keywords"]]
    частые = {о for о, df in частоты.items() if df >= размер * 0.6}
    assert частые, частоты
    # Частое слово корпуса опускается в списке, а не исчезает: оно может
    # быть важным, просто перестаёт быть новостью.
    for основа in частые:
        слово = next((к for к in без if content.stem(к) == основа), None)
        if слово is None or слово not in с_корпусом:
            continue
        assert с_корпусом.index(слово) >= без.index(слово), (слово, без, с_корпусом)


def test_topics_are_labelled_with_a_readable_word_not_a_stem(tmp_path):
    """Иначе раздел показывает «оперативн» и выглядит как опечатка."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 18)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    темы = Insights(db).topics("all")
    assert темы["corpus"] == 18
    assert темы["items"], темы
    for тема in темы["items"]:
        assert тема["word"] and тема["word"] != "", тема
        assert 0 < тема["share"] <= 100, тема
        # Доля — от разобранных записей окна, и больше единицы быть не может.
        assert тема["records"] <= темы["corpus"], тема


def test_weak_and_thin_correlations_are_not_shown(tmp_path):
    """Показанный коэффициент читается как факт, поэтому слабые не показываем."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import МИН_ПАР, МИН_СВЯЗЬ, Insights

    db = _корпус(tmp_path, 20)               # записей меньше порога в 50
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    связи = Insights(db).correlations("all")
    assert связи["items"] == [], связи
    assert связи["sampled"] == 20

    db2 = _корпус(tmp_path / "второй", 120)
    ContentIndex(db2, _Настройки()).backfill_once(limit=200)
    связи2 = Insights(db2).correlations("all")
    for с in связи2["items"]:
        assert abs(с["r"]) >= МИН_СВЯЗЬ and с["n"] >= МИН_ПАР, с


def test_findings_name_the_numbers_they_are_made_of(tmp_path):
    """Вывод без чисел проверить нельзя, а непроверяемым тут не место."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 60)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    выводы = Insights(db).findings("all")
    assert выводы, "на треть отрицательных записей вывода не нашлось"
    for в in выводы:
        assert в["severity"] in ("warning", "good", "info"), в
        assert в["text"] and any(c.isdigit() for c in в["text"]), в
    # Ровно то, ради чего разрез и открывают: треть разговоров отрицательные.
    assert any("отрицательн" in в["text"] for в in выводы), выводы
    # И порядок: то, что требует внимания, — сверху.
    уровни = [в["severity"] for в in выводы]
    assert уровни == sorted(уровни, key=lambda у: {"warning": 0, "good": 1}.get(у, 2))


def test_an_empty_archive_gives_an_empty_report_and_not_an_error(tmp_path):
    """Первый запуск — тоже случай: раздел открывают до первой записи."""
    from asrhub.db import Database
    from asrhub.insights import Insights

    свод_класс = Insights(Database(tmp_path / "пусто.db"))
    свод = свод_класс.summary("week")
    assert свод["records"] == 0 and свод["sentiment"] is None
    assert свод_класс.findings("week") == []
    assert свод_класс.topics("week")["items"] == []
    assert свод_класс.correlations("week")["items"] == []
    assert свод_класс.breakdown("owner", "week")["items"] == []
    assert свод_класс.records("negative", "week")["items"] == []
    отчёт = свод_класс.report("week")
    assert отчёт["summary"]["records"] == 0


# ---------------------------------------------------------------------------
# Программный интерфейс раздела
# ---------------------------------------------------------------------------


def _завершённое_задание(client, sample_wav: Path) -> str:
    """Прогоняет запись через сервер и возвращает номер готового задания."""
    with sample_wav.open("rb") as поток:
        ответ = client.post("/api/jobs",
                            files={"file": ("проба.wav", поток, "audio/wav")})
    assert ответ.status_code == 200, ответ.text
    job_id = ответ.json()["id"]
    for _ in range(200):
        задание = client.get(f"/api/jobs/{job_id}").json()
        if задание["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert задание["status"] == "completed", задание
    return job_id


def test_the_analysis_is_computed_when_the_job_finishes(client, sample_wav: Path):
    """Признаки нужны вместе с результатом, а не после фонового прохода."""
    job_id = _завершённое_задание(client, sample_wav)

    разбор = client.get(f"/api/content/jobs/{job_id}")
    assert разбор.status_code == 200, разбор.text
    тело = разбор.json()
    assert тело["job_id"] == job_id
    assert set(тело["analysis"]) >= {"sentiment", "speech", "entities",
                                     "keywords", "compliance"}
    # Посчитан именно при завершении, а не сейчас по запросу карточки.
    assert тело["computed_at"] and тело["computed_at"] > 0

    состояние = client.get("/api/content/status").json()
    assert состояние["analyzed"] >= 1, состояние


def test_every_section_of_the_report_answers_and_rejects_nonsense(client):
    """Раздел открывают по частям, и каждая часть обязана отвечать сама."""
    разделы = ["/api/content/status", "/api/content/summary?period=week",
               "/api/content/timeline?period=week", "/api/content/topics",
               "/api/content/correlations?period=week",
               "/api/content/findings?period=week",
               "/api/content/records?kind=negative&period=week",
               "/api/content/kinds", "/api/content?period=week"]
    for путь in разделы:
        ответ = client.get(путь)
        assert ответ.status_code == 200, (путь, ответ.text)
        assert isinstance(ответ.json(), dict), путь

    for разрез in ("owner", "speaker", "tag", "weekday", "hour", "model"):
        ответ = client.get(f"/api/content/breakdown/{разрез}?period=week")
        assert ответ.status_code == 200, (разрез, ответ.text)
        assert ответ.json()["dimension"] == разрез

    # Неизвестные имена приходят из строки запроса и подставляются в SQL
    # через перечень отборов; проверяем, что перечень их не пропускает.
    assert client.get("/api/content/breakdown/чушь").status_code == 400
    assert client.get("/api/content/records?kind=чушь").status_code == 400
    assert client.get("/api/content/jobs/нет-такого").status_code == 404
    assert client.get("/api/content/summary?period=вечность").status_code == 422


def test_the_report_can_be_exported_as_a_workbook_and_as_csv(client, sample_wav: Path):
    """Отчёт, который нельзя отдать руководителю, — это отчёт наполовину."""
    _завершённое_задание(client, sample_wav)

    книга = client.get("/api/content/export?period=month&fmt=xlsx")
    assert книга.status_code in (200, 503), книга.text
    if книга.status_code == 200:
        assert книга.content[:2] == b"PK", "это не книга Excel"
        assert книга.headers["Cache-Control"] == "no-store"
        import io

        from openpyxl import load_workbook
        листы = load_workbook(io.BytesIO(книга.content)).sheetnames
        assert "Свод" in листы, листы

    архив = client.get("/api/content/export?period=month&fmt=csv")
    assert архив.status_code == 200, архив.text
    import io
    import zipfile

    имена = zipfile.ZipFile(io.BytesIO(архив.content)).namelist()
    assert "Свод.csv" in имена and "period.txt" in имена, имена


def test_recomputing_the_whole_archive_needs_an_administrator(client, sample_wav: Path):
    """Пересчёт занимает служебный поток на часы и меняет чужие числа тоже."""
    job_id = _завершённое_задание(client, sample_wav)

    одна = client.post(f"/api/content/jobs/{job_id}/recompute")
    assert одна.status_code == 200 and одна.json()["recomputed"] is True

    весь = client.post("/api/content/recompute", json={})
    assert весь.status_code == 200, весь.text
    assert весь.json()["queued"] >= 1, весь.json()
    # После сброса версии запись снова ждёт разбора.
    assert client.get("/api/content/status").json()["pending"] >= 1


def test_no_script_marker_matches_an_everyday_word():
    """Примета, совпадающая с частым словом, делает пункт выполненным всегда.

    На этом уже попался пункт «Представился»: среди примет стояло голое
    «это», и «алло, это опять вы?» засчитывалось как представление
    оператора — разрез соблюдения скрипта показывал единицу у всех.
    """
    from asrhub.content.compliance import ПО_УМОЛЧАНИЮ, check

    обиходные = [
        "Алло, это опять вы?",
        "Да, то есть по срокам всё понятно.",
        "Ну это, короче, там сроки сдвинулись.",
        "Нет, это не то, что я просил.",
    ]
    for фраза in обиходные:
        итог = check([{"start": 0.0, "end": 3.0, "text": фраза}])
        сработали = [п["label"] for п in итог["items"] if п["passed"]]
        assert not сработали, (фраза, сработали)

    # И обратное: настоящее представление по-прежнему находится.
    настоящее = check([{"start": 0.0, "end": 4.0,
                        "text": "Здравствуйте, меня зовут Анна, компания Ромашка."}])
    выполнено = {п["id"] for п in настоящее["items"] if п["passed"]}
    assert {"greeting", "introduce", "company"} <= выполнено, выполнено

    # Пункты набора по умолчанию описаны полностью — иначе проверка молча
    # пропускает пункт, а отчёт показывает его невыполненным всегда.
    for пункт in ПО_УМОЛЧАНИЮ:
        assert пункт["id"] and пункт["label"] and пункт["any"], пункт
        assert пункт["where"] in ("start", "end", "any"), пункт


# ---------------------------------------------------------------------------
# Что нашла ревизия
# ---------------------------------------------------------------------------


def test_filler_words_are_matched_by_form_not_by_stem():
    """По основам «слушаю» и «скажите» становились словами-паразитами.

    Паразит — неизменяемая частица, а её основа общая с обычным словом:
    «так» ловило «такая», «просто» — «простой», «слушай» — «слушаю». На
    вежливой реплике оператора доля паразитов выходила 0.24 при пороге
    вывода 0.04 — хуже всех выглядел тот, кто чаще говорит «скажите,
    пожалуйста», то есть делает ровно то, чего требует скрипт.
    """
    from asrhub.content import speech

    вежливо = ("Здравствуйте, я вас слушаю. Скажите, пожалуйста, номер заказа. "
               "Я вас понимаю, сейчас посмотрим. Такая ситуация решается быстро.")
    чисто = speech.analyze(
        [{"start": 0.0, "end": 15.0, "speaker": "A", "text": вежливо}], 15.0)
    assert чисто["filler_rate"] == 0.0, чисто["filler_rate"]
    assert чисто["fillers"] == 0

    мусор = "Ну вот, короче, это как бы типа, в общем, я э-э не знаю."
    грязно = speech.analyze(
        [{"start": 0.0, "end": 10.0, "speaker": "A", "text": мусор}], 10.0)
    assert грязно["fillers"] >= 6, грязно
    assert грязно["filler_rate"] > 0.3, грязно["filler_rate"]


def test_negation_does_not_reach_across_a_sentence_boundary():
    """«Никаких претензий. Спасибо, помогли!» — это благодарность.

    Окно отрицания смотрит на три слова назад и точки не видело, а оценка
    записи считается по склеенному тексту всего разговора: отрицание из
    реплики клиента переворачивало похвалу в реплике оператора.
    """
    from asrhub import content
    from asrhub.content.sentiment import score_text

    оценка = score_text("Никаких претензий. Спасибо, помогли!")
    assert оценка.score > 0.3, (оценка.score, оценка.words)
    assert оценка.label == "положительная"

    сегменты = [
        {"start": 0.0, "end": 3.0, "speaker": "S1", "text": "Нет, я не про это."},
        {"start": 3.0, "end": 8.0, "speaker": "S0",
         "text": "Хорошо, помогу вам быстро."},
    ]
    р = content.analyze(text=" ".join(с["text"] for с in сегменты),
                        segments=сегменты, duration_s=8.0)
    assert р["sentiment"]["score"] > 0.2, р["sentiment"]

    # И обратное: отрицание внутри одного предложения по-прежнему работает.
    assert score_text("Мне не понравилось").score < 0


def test_statements_that_start_with_a_question_word_are_not_questions():
    """Соотносительные обороты начинаются теми же словами, что и вопросы."""
    from asrhub.content.analyze import _вопросы

    утверждения = [
        "Как я уже сказал, счёт мы выставили вчера",
        "Что касается доставки, она будет в среду",
        "Сколько нужно, столько и сделаем",
        "Как договорились, отправим завтра",
    ]
    сегменты = [{"start": float(i), "end": float(i + 1), "text": т}
                for i, т in enumerate(утверждения)]
    assert _вопросы(сегменты) == [], _вопросы(сегменты)

    вопросы = ["Когда вы отправите документы", "Почему счёт не пришёл?",
               "Какой у вас номер договора", "Сколько это будет стоить"]
    сегменты = [{"start": float(i), "end": float(i + 1), "text": т}
                for i, т in enumerate(вопросы)]
    assert len(_вопросы(сегменты)) == 4, _вопросы(сегменты)


def test_a_common_turn_of_speech_is_not_an_alarming_mention():
    """«Судя по всему» стеммится в «суд» и поднимало запись в «послушать срочно»."""
    from asrhub.content.analyze import _тревожные

    обычное = _тревожные([{"start": 0.0, "end": 5.0,
                           "text": "Судя по всему, вам подойдёт второй тариф. "
                                   "Давайте обсудим детали."}])
    assert обычное == [], обычное

    настоящее = _тревожные([{"start": 0.0, "end": 5.0,
                             "text": "Я подам на вас в суд и напишу жалобу."}])
    assert настоящее and {"суд", "жалобу"} <= set(настоящее[0]["words"]), настоящее


def test_a_decimal_fraction_is_not_a_date_and_a_courier_is_not_a_deadline():
    """Иначе каждому обещанию приписан срок, и открытых обещаний не бывает."""
    from asrhub.content import entities

    дробь = entities.extract("Вышлю счёт на 1.5 миллиона, скидка 2.5 процента.")
    assert дробь["dates"] == [], дробь["dates"]

    настоящие = entities.extract("Отгрузка 01.05, договор от 1.5.2026, встреча 5 июня.")
    assert {"01.05", "1.5.2026", "5 июня"} <= set(настоящие["dates"]), настоящие["dates"]

    через = entities.extract("Документы передам через курьера, свяжемся через "
                             "нашего менеджера.")
    assert через["deadlines"] == [], через["deadlines"]

    срок = entities.extract("Перезвоню через 15 минут, решим в течение недели.")
    assert len(срок["deadlines"]) == 2, срок["deadlines"]


def test_a_promise_without_a_named_deadline_is_visible(tmp_path):
    """Разрез «обещания без срока» — один из тех, ради которых раздел открывают."""
    from asrhub import content

    сегменты = [
        {"start": 0.0, "end": 4.0, "speaker": "A",
         "text": "Я вам вышлю счёт на 1.5 миллиона рублей."},
        {"start": 4.0, "end": 8.0, "speaker": "A",
         "text": "Документы передам через курьера."},
        {"start": 8.0, "end": 12.0, "speaker": "A",
         "text": "Перезвоню завтра и всё уточню."},
    ]
    р = content.analyze(text=" ".join(с["text"] for с in сегменты),
                        segments=сегменты, duration_s=12.0)
    assert р["commitments"]["count"] == 3, р["commitments"]
    assert р["commitments"]["with_deadline"] == 1, [
        (о["text"], о["deadline"]) for о in р["commitments"]["items"]]


def test_an_unseen_stem_does_not_outweigh_everything_in_the_corpus():
    """Отсечка `min_df` заводилась против опечаток, а поднимала их наверх."""
    from asrhub.content import keywords

    # Оба слова встречаются в записи по одному разу, значит их вес
    # различает только редкость по корпусу. «Редкое» в словаре есть с
    # частотой 2, «абракадабрность» в словарь не попала: без потолка она
    # получала IDF больше, чем у любой основы, которая в словаре есть.
    частоты = {"договор": 800, "поставк": 40, "редк": 2}
    веса = {к["stem"]: к["weight"] for к in keywords.keywords(
        "Договор подписан, поставка идёт. Это редкое слово и абракадабрность.",
        document_frequency=частоты, corpus_size=1000, min_df=2)}
    assert {"редк", "абракадабрн"} <= set(веса), веса
    assert веса["абракадабрн"] <= веса["редк"], веса
    assert веса["абракадабрн"] > веса["договор"], веса


def test_an_empty_denominator_is_unknown_and_not_zero(tmp_path):
    """Иначе неизмеренный прошлый период объявляется нулём и «ростом»."""
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 24)
    # Разбор со сбоем: строка есть, версия есть, оценки нет — так пишет
    # backfill_once, когда разбор записи не удался.
    for n in range(24):
        db.save_content(f"job{n:04d}", {"version": 1, "detail": {"error": "проба"}}, [])

    свод = Insights(db).summary("all")
    assert свод["records"] == 24 and свод["scored"] == 0
    assert свод["negative_share"] is None, свод["negative_share"]
    # И вывод о росте не появляется там, где мерить было нечего.
    assert not [в for в in Insights(db).findings("all")
                if "выросла" in в["text"]], Insights(db).findings("all")


def test_findings_do_not_fire_on_a_handful_of_records(tmp_path):
    """Семь тревог на одной записи — это встреча нового сервера с человеком."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import МИН_КОРПУСА, Insights

    db = _корпус(tmp_path, 3)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    assert Insights(db).findings("all") == []

    db2 = _корпус(tmp_path / "больше", МИН_КОРПУСА + 20)
    ContentIndex(db2, _Настройки()).backfill_once(limit=100)
    assert Insights(db2).findings("all")


def test_a_finding_never_contradicts_its_own_number(tmp_path):
    """«Каждый четвёртый разговор отрицательный: 100.0%» проверить нельзя."""
    from asrhub.insights import _каждое, _каждый

    assert _каждый(100.0) == "Почти каждый"
    assert _каждый(50.0) == "Каждый второй"
    assert _каждый(33.0) == "Каждый третий"
    assert _каждый(25.0) == "Каждый четвёртый"
    assert "второе" in _каждое(0.5)
    assert "двадцать пятое" in _каждое(0.04)


def test_an_ordered_breakdown_covers_the_whole_window(tmp_path):
    """Разрез «по дням за год» отдавал полсотни самых оживлённых дней.

    База отдаёт группы по убыванию числа записей, а разрез потом сортирует
    по дате: получался непрерывный отрезок в три месяца вместо года, и при
    этом «скрыто: 0».
    """
    import time as _time

    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 240)
    # Раскидываем по ста двадцати суткам подряд, но неравномерно: в одни
    # сутки одна запись, в другие три. На равномерной раскладке отбор «сто
    # самых оживлённых» ничем не отличался бы от «первых ста».
    сейчас = _time.time()
    сутки, n = 0, 0
    while n < 240:
        сколько = 1 if сутки % 3 else 3
        for _ in range(min(сколько, 240 - n)):
            db.execute("UPDATE jobs SET created_at=? WHERE id=?",
                       (сейчас - сутки * 86400, f"job{n:04d}"))
            n += 1
        сутки += 1
    ContentIndex(db, _Настройки()).backfill_once(limit=400)

    целиком = Insights(db).breakdown("date", "year", limit=500)
    assert len(целиком["items"]) == сутки, (len(целиком["items"]), сутки)
    assert целиком["hidden"] == 0, целиком["hidden"]
    assert sum(г["records"] for г in целиком["items"]) == n

    # Со срезом отдаются первые по дате, а не самые оживлённые, и число
    # скрытых считается после среза, а не до.
    урезанный = Insights(db).breakdown("date", "year", limit=60)
    assert len(урезанный["items"]) == 60
    assert урезанный["hidden"] == сутки - 60, урезанный["hidden"]
    ожидались = [г["key"] for г in целиком["items"][:60]]
    assert [г["key"] for г in урезанный["items"]] == ожидались


def test_the_topic_trend_does_not_invent_new_topics(tmp_path):
    """Тема, не попавшая в срез прошлого окна, объявлялась новой."""
    import time as _time

    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    # Шум делает словарь корпуса больше любого среза: именно на этом и
    # ломалось сравнение окон — тема, не попавшая в верхушку прошлого
    # окна, считалась встретившейся там ноль раз.
    db = _корпус(tmp_path, 60, шум=10, общих=250)
    сейчас = _time.time()
    # Половину записей — в прошлую неделю, половину — в текущую.
    for n in range(60):
        сдвиг = 3 * 86400 if n % 2 else 10 * 86400
        db.execute("UPDATE jobs SET created_at=? WHERE id=?",
                   (сейчас - сдвиг, f"job{n:04d}"))
    ContentIndex(db, _Настройки()).backfill_once(limit=200)

    тренд = Insights(db).topic_trend("week", limit=5)
    assert тренд, "тренд пуст — сравнивать окна не с чем"
    # Записи в обоих окнах одинаковые, значит и доли одинаковые: выдуманных
    # скачков быть не должно.
    крупные = [т for т in тренд if abs(т["delta"]) > 20]
    assert not крупные, крупные
    # И «раньше» не должно быть нулём ни у одной темы: записи в обоих
    # окнах одинаковые, значит каждая тема была и раньше.
    assert all(т["before"] > 0 for т in тренд), тренд


def test_a_term_can_be_looked_up_in_a_window_regardless_of_its_rank(tmp_path):
    """Механика, на которой ломалось сравнение окон.

    Прошлое окно спрашивалось своей верхушкой, и тема, не попавшая в срез,
    считалась встретившейся там ноль раз — то есть новой. На корпусе, где
    основ тысячи, в срез не попадает почти ничто, кроме самого частого, и
    список «стало звучать чаще» состоял из тем, которые не менялись.
    """
    from asrhub.content_index import ContentIndex

    db = _корпус(tmp_path, 30, общих=200)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    верхушка = db.top_terms(limit=20)
    assert len(верхушка) == 20
    в_верхушке = {т["stem"] for т in верхушка}

    # Берём основу, которая в корпусе есть, но в срез не попала.
    всё = db.top_terms(limit=100000, min_records=1)
    заслонённые = [т for т in всё if т["stem"] not in в_верхушке and т["records"] > 1]
    assert заслонённые, "не удалось получить основу за пределами среза"
    основа = заслонённые[0]["stem"]

    точечно = db.top_terms(stems=[основа], min_records=0)
    assert len(точечно) == 1 and точечно[0]["stem"] == основа, точечно
    assert точечно[0]["records"] == заслонённые[0]["records"], (точечно, заслонённые[0])

    # И пустой перечень — это «ничего не спрашивали», а не «спросить всё».
    assert db.top_terms(stems=[]) == []


def test_the_timeline_never_runs_past_the_end_of_its_window(tmp_path):
    """Двести корзин по урезанному шагу давали полуторачасовой хвост в будущем."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 10)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    лента = Insights(db).timeline("hour", buckets=200)
    точки = лента["buckets"]
    assert точки, лента
    assert точки[-1]["ts"] <= лента["to"] + лента["step_s"], (
        точки[-1]["ts"], лента["to"], лента["step_s"])
    ширина = лента["to"] - лента["from"]
    assert len(точки) * лента["step_s"] <= ширина + лента["step_s"], (
        len(точки), лента["step_s"], ширина)


def test_coverage_counts_the_same_jobs_on_both_sides(tmp_path):
    """Повтор задания делал «разобрано всё» из наполовину разобранного архива."""
    from asrhub import content
    from asrhub.content_index import ContentIndex

    db = _корпус(tmp_path, 20)
    индекс = ContentIndex(db, _Настройки())
    индекс.backfill_once(limit=10)
    было = индекс.status()
    assert было["total"] == 20 and было["analyzed"] == 10 and было["pending"] == 10

    # Половину заданий возвращаем в очередь — как это делает повтор: статус
    # меняется, текст и разбор остаются.
    for n in range(10):
        db.update_job(f"job{n:04d}", status="queued")
    стало = индекс.status()
    assert стало["analyzed"] <= стало["total"], стало
    assert стало["pending"] == стало["total"] - стало["analyzed"], стало
    # Разобранными считаются только те, что и попали в «всего».
    ожидают = db.content_pending(content.VERSION, limit=100)
    assert стало["pending"] == len(ожидают), (стало, len(ожидают))


def _клиент_с_ключами(data_dir, monkeypatch):
    """Сервер с тремя ключами разных прав — для проверок разграничения."""
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_admin_k": {"name": "админ", "role": "admin", "enabled": True},
        "ah_user_k": {"name": "пользователь", "role": "user", "enabled": True},
        "ah_ro_k": {"name": "чтение", "role": "readonly", "enabled": True},
    })
    return create_app(настройки, start_queue=False)


def _засеять(app, владелец: str = "пользователь", сколько: int = 2,
             версия: int = 999) -> None:
    """Готовые записи с разбором прямо в базе приложения."""
    db = app.state.hub.db
    for n in range(сколько):
        job_id = db.create_job({"id": f"seed{n}", "filename": f"{n}.wav",
                                "owner": владелец, "media_duration_s": 10.0})
        db.update_job(job_id, status="completed", finished_at=1.0,
                      text="Здравствуйте, всё отлично, спасибо большое.")
        db.save_content(job_id, {"version": версия, "sentiment": 0.5},
                        [{"stem": "проб", "word": "проба", "n": 1}])


def test_only_an_administrator_may_reset_the_whole_archive(data_dir, monkeypatch):
    """Список из одного несуществующего задания открывал пересчёт всем.

    `if задания:` было истинно, отбор оставлял пустой список, а
    `recompute([])` уходил в ветку «весь архив»: `require_admin` строкой
    ниже не выполнялся никогда, и любой ключ одним запросом сбрасывал
    разбор всех записей сервера, включая чужие.
    """
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        _засеять(app)
        было = [dict(r) for r in app.state.hub.db.query(
            "SELECT job_id, version FROM content")]
        assert все_версии(было) == {999}, было

        ответ = c.post("/api/content/recompute", json={"job_ids": ["нет-такого"]},
                       headers={"X-API-Key": "ah_user_k"})
        assert ответ.status_code == 200, ответ.text
        assert ответ.json() == {"recomputed": 0, "queued": 0}, ответ.json()
        стало = [dict(r) for r in app.state.hub.db.query(
            "SELECT job_id, version FROM content")]
        assert все_версии(стало) == {999}, стало

        # Пустой список — тоже не «пересчитать всё».
        c.post("/api/content/recompute", json={"job_ids": []},
               headers={"X-API-Key": "ah_user_k"})
        assert все_версии([dict(r) for r in app.state.hub.db.query(
            "SELECT job_id, version FROM content")]) == {999}

        # Без списка — только администратору.
        assert c.post("/api/content/recompute", json={},
                      headers={"X-API-Key": "ah_user_k"}).status_code == 403
        админ = c.post("/api/content/recompute", json={},
                       headers={"X-API-Key": "ah_admin_k"})
        assert админ.status_code == 200 and админ.json()["queued"] >= 1


def все_версии(строки) -> set:
    return {int(с["version"]) for с in строки}


def test_a_read_only_key_does_not_write_analysis(data_dir, monkeypatch):
    """Ключ «только чтение» перезаписывал разбор, и делал это обычным GET."""
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        # Версия ноль — значит разбор устарел и карточка посчитает его заново.
        _засеять(app, владелец="чтение", версия=0)
        ключ = {"X-API-Key": "ah_ro_k"}

        assert c.post("/api/content/jobs/seed0/recompute",
                      headers=ключ).status_code == 403
        assert c.post("/api/content/recompute", json={"job_ids": ["seed0"]},
                      headers=ключ).status_code == 403

        # GET карточки считает разбор, но в базу его не кладёт.
        ответ = c.get("/api/content/jobs/seed0", headers=ключ)
        assert ответ.status_code == 200, ответ.text
        assert ответ.json()["analysis"].get("sentiment"), ответ.json()
        assert app.state.hub.db.get_content("seed0")["version"] == 0, "разбор записан"


def test_turning_the_analysis_off_actually_turns_it_off(data_dir, monkeypatch):
    """Настройка «не разбирать» ничего не выключала: карточка писала мимо неё."""
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        job_id = db.create_job({"id": "off1", "filename": "x.wav",
                                "owner": "админ", "media_duration_s": 10.0})
        db.update_job(job_id, status="completed", finished_at=1.0,
                      text="Здравствуйте, всё отлично, спасибо.")
        app.state.hub.settings.set("content_analysis", False)

        ключ = {"X-API-Key": "ah_admin_k"}
        assert c.get("/api/content/status", headers=ключ).json()["enabled"] is False
        ответ = c.get(f"/api/content/jobs/{job_id}", headers=ключ)
        assert ответ.status_code == 200 and ответ.json()["analysis"], ответ.text
        assert db.get_content(job_id) is None, "разбор попал в базу при выключенной настройке"
        assert db.query("SELECT 1 FROM content_terms") == []
