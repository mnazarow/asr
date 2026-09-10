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
    # job_id и computed_at проставляет сама запись в базу; совпадения
    # категорий (`hits`) — строки своей таблицы, база забирает их сама.
    assert set(свод) - {"detail", "hits"} <= колонки, set(свод) - колонки
    assert all({"category", "kind", "count", "first_s"} <= set(с) for с in свод["hits"])
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


# ---------------------------------------------------------------------------
# Связь с остальным сервером
# ---------------------------------------------------------------------------


def test_the_scheduled_digest_talks_about_the_conversations(tmp_path):
    """Ради этих строк сводку и читают, а их в ней не было вовсе."""
    from asrhub.analytics import Analytics
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights
    from asrhub.maintenance import build_digest

    db = _корпус(tmp_path, 60)
    индекс = ContentIndex(db, _Настройки())
    индекс.backfill_once(limit=100)
    настройки = _Настройки(digest_period="month", digest_content=True)

    сводка = build_digest(Analytics(db), настройки, insights=Insights(db, индекс))
    assert "content" in сводка, sorted(сводка)
    свод = сводка["content"]["summary"]
    assert свод["records"] == 60
    assert сводка["content"]["findings"], "выводов в сводке нет"

    # Приёмник входящих сообщений показывает только `text` — значит про
    # разговоры должно быть и там, иначе в чат придёт сводка про сервер.
    текст = сводка["text"]
    assert "О чём говорили" in текст, текст
    assert "Отрицательных разговоров" in текст, текст
    assert any(строка.strip().startswith(("!", "+", "·"))
               for строка in текст.splitlines()), текст

    # Настройка выключает раздел, а не сводку целиком.
    без = build_digest(Analytics(db), _Настройки(digest_content=False),
                       insights=Insights(db, индекс))
    assert "content" not in без and без["text"]
    # И сводка собирается, когда разбора нет вовсе.
    assert "content" not in build_digest(Analytics(db), настройки)


def test_content_metrics_reach_prometheus_and_can_carry_an_alert(tmp_path):
    """Мониторинг видел, что сервер быстр, и не видел, что клиенты недовольны."""
    from asrhub.analytics import Analytics
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights
    from asrhub.monitoring.alerts import default_rules
    from asrhub.monitoring.catalog import METRICS_BY_NAME

    db = _корпус(tmp_path, 40)
    # Записи за последние сутки: метрики содержания считаются за сутки.
    for n in range(40):
        db.execute("UPDATE jobs SET created_at=? WHERE id=?",
                   (time.time() - 600 - n, f"job{n:04d}"))
    индекс = ContentIndex(db, _Настройки())
    индекс.backfill_once(limit=100)

    текст = Analytics(db).prometheus(Insights(db, индекс))
    метрики = {строка.split()[0]: float(строка.split()[1])
               for строка in текст.splitlines()
               if строка.startswith("asrhub_content_")}
    assert метрики.get("asrhub_content_records") == 40, метрики
    assert "asrhub_content_negative_share" in метрики, метрики
    assert метрики.get("asrhub_content_pending") == 0, метрики
    # Категории — по метке на категорию, с видом; доля — от нуля до единицы.
    assert метрики.get('asrhub_content_category_share{category="deadline",kind="topic"}') \
        == round(14 / 40, 4), метрики

    # Каждая выданная метрика описана в каталоге: иначе она не появится ни
    # в справочнике, ни в списке, по которому пишут правила. Метки — не
    # часть имени.
    неописанные = [и for и in метрики if и.split("{")[0] not in METRICS_BY_NAME]
    assert not неописанные, неописанные
    # HELP и TYPE стоят один раз на имя, а не на каждую метку.
    assert текст.count("# HELP asrhub_content_category_share") == 1

    # И на неё можно поставить порог — правила берутся из того же каталога.
    правила = {r.metric for r in default_rules()}
    assert "asrhub_content_negative_share" in правила, sorted(правила)
    assert "asrhub_content_alert_records" in правила

    # Без свода выгрузка метрик не ломается и не пустеет.
    без = Analytics(db).prometheus(None)
    assert "asrhub_jobs_total" in без and "asrhub_content_" not in без


def test_the_job_list_can_be_filtered_by_what_was_said(tmp_path, monkeypatch,
                                                       data_dir):
    """«Отрицательные разговоры про возврат за неделю» — раньше невозможно."""
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        for n, (текст, метка) in enumerate((
            ("Срок сорван, я крайне недоволен, буду жаловаться в суд.", "плохо"),
            ("Спасибо большое, всё отлично, вы очень помогли.", "хорошо"),
            ("Срок сорван, отвратительно, верните деньги.", "плохо"),
        )):
            job_id = db.create_job({"id": f"f{n}", "filename": f"{n}.wav",
                                    "owner": "админ", "media_duration_s": 30.0,
                                    "tags": метка})
            db.update_job(job_id, status="completed", finished_at=1.0, text=текст)
            db.save_segments(job_id, [{"start": 0.0, "end": 20.0, "text": текст,
                                       "speaker": "SPEAKER_00"}])
            app.state.hub.content.analyze_job(job_id)

        ключ = {"X-API-Key": "ah_admin_k"}
        отриц = c.get("/api/jobs?status=completed&content=negative&light=true",
                      headers=ключ).json()
        assert {j["id"] for j in отриц["items"]} == {"f0", "f2"}, отриц["items"]
        assert отриц["total"] == 2

        тревога = c.get("/api/jobs?status=completed&content=alerts&light=true",
                        headers=ключ).json()
        assert {j["id"] for j in тревога["items"]} == {"f0"}, тревога["items"]

        # Отбор складывается с поиском — ровно то, ради чего он и заведён.
        вместе = c.get("/api/jobs?status=completed&content=negative&search=деньги"
                       "&light=true", headers=ключ).json()
        assert {j["id"] for j in вместе["items"]} == {"f2"}, вместе["items"]
        assert вместе["total"] == 1

        assert c.get("/api/jobs?content=чушь", headers=ключ).status_code == 400


def test_the_total_matches_the_list_it_counts(tmp_path, monkeypatch, data_dir):
    """«Показано 30, всего 4000» — листалка вела на пустые страницы."""
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        for n in range(12):
            job_id = db.create_job({"id": f"t{n}", "filename": f"файл{n}.wav",
                                    "owner": "админ", "model": "м1" if n < 4 else "м2",
                                    "media_duration_s": 10.0})
            db.update_job(job_id, status="completed", finished_at=1.0,
                          text="Договор поставки" if n < 5 else "Просто разговор")
        ключ = {"X-API-Key": "ah_admin_k"}
        for запрос in ("search=договор", "model=м1", "search=договор&model=м1",
                       "search=файл3"):
            ответ = c.get(f"/api/jobs?status=completed&{запрос}&limit=500&light=true",
                          headers=ключ).json()
            assert ответ["total"] == len(ответ["items"]), (запрос, ответ["total"],
                                                           len(ответ["items"]))


def test_a_script_marker_that_matches_a_function_word_is_flagged():
    """Примета из служебных слов делает пункт выполненным всегда."""
    from asrhub.content.compliance import ПО_УМОЛЧАНИЮ, check, suspicious

    плохой = [
        {"id": "a", "label": "Представился", "any": ["это"], "where": "start"},
        {"id": "b", "label": "Ответил", "any": ["так", "то"], "where": "any"},
        {"id": "c", "label": "Поздоровался", "any": ["здравствуйте"],
         "where": "start"},
        {"id": "d", "label": "Помощь", "any": ["чем могу помочь"], "where": "start"},
    ]
    слова = {с["word"] for с in suspicious(плохой)}
    assert слова == {"это", "так", "то"}, слова

    # Набор по умолчанию чист — и это проверка на него самого: «передам»
    # стеммится в «перед», и пункт засчитывался на «перед тем как».
    assert suspicious(ПО_УМОЛЧАНИЮ) == [], suspicious(ПО_УМОЛЧАНИЮ)
    итог = check([{"start": 0.0, "end": 5.0,
                   "text": "Перед тем как оформить, я посмотрю документы."}])
    assert not [п for п in итог["items"] if п["passed"]], итог["items"]


def test_words_from_a_comment_did_not_become_stop_words():
    """Пояснение стояло внутри строки, и его слова попали в словарь.

    «Записи», «речь», «глаголы», «шума», «ключевых» переставали быть
    темами разговора — вместе с «#», «—» и словами с запятыми из того же
    пояснения.
    """
    from asrhub.content import keywords
    from asrhub.content.lexicons import СЛУЖЕБНЫЕ, СТОП_СЛОВА

    мусор = sorted(с for с in СТОП_СЛОВА if not с.isalpha())
    assert not мусор, мусор

    из_пояснения = ("записи", "речь", "глаголы", "шума", "ключевых", "списка",
                    "обороты", "разговорные", "частые", "смысла")
    попали = [с for с in из_пояснения if keywords.stem(с) in СТОП_СЛОВА]
    assert not попали, попали

    найдено = {к["word"] for к in keywords.keywords(
        "Ключевые слова записи и смысл разговора. Речь оператора и списки.")}
    assert {"записи", "речь", "смысл"} <= найдено, найдено

    # Вежливость лежит среди разговорных, а не служебных: приметой скрипта
    # «здравствуйте» быть можно, темой разговора — нет.
    assert keywords.stem("здравствуйте") in СТОП_СЛОВА
    assert keywords.stem("здравствуйте") not in СЛУЖЕБНЫЕ


def test_the_script_check_endpoint_runs_a_draft_without_saving_it(
        tmp_path, monkeypatch, data_dir):
    """Понять по списку слов, годится ли примета, нельзя — только на записи."""
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        job_id = db.create_job({"id": "проверка", "filename": "x.wav",
                                "owner": "админ", "media_duration_s": 20.0})
        db.update_job(job_id, status="completed", finished_at=1.0,
                      text="Здравствуйте, это компания Ромашка. Отправлю завтра.")
        db.save_segments(job_id, [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00",
             "text": "Здравствуйте, это компания Ромашка."},
            {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_00",
             "text": "Отправлю завтра."}])
        ключ = {"X-API-Key": "ah_admin_k"}

        черновик = [{"id": "hi", "label": "Поздоровался",
                     "any": ["здравствуйте"], "where": "start"},
                    {"id": "bad", "label": "Плохая", "any": ["это"], "where": "any"}]
        ответ = c.post("/api/content/script/check",
                       json={"job_id": job_id, "script": черновик},
                       headers=ключ)
        assert ответ.status_code == 200, ответ.text
        тело = ответ.json()
        пункты = {п["id"]: п for п in тело["compliance"]["items"]}
        assert пункты["hi"]["passed"] and пункты["hi"]["matched"] == "здравствуйте"
        assert пункты["bad"]["passed"] and пункты["bad"]["matched"] == "это"
        assert {с["word"] for с in тело["suspicious"]} == {"это"}, тело["suspicious"]
        assert тело["default"] is False

        # Черновик в настройки не попал: его ещё правят.
        assert app.state.hub.settings.get("content_script") in (None, [], )

        # Без скрипта проверяется набор по умолчанию.
        свой = c.post("/api/content/script/check", json={"job_id": job_id},
                      headers=ключ).json()
        assert свой["default"] is True and свой["compliance"]["checked"] == 8

        assert c.post("/api/content/script/check", json={"job_id": "нет"},
                      headers=ключ).status_code == 404
        assert c.post("/api/content/script/check",
                      json={"job_id": job_id, "script": "строка"},
                      headers=ключ).status_code == 400


def test_records_without_diarization_are_counted_and_named(tmp_path):
    """Ноль перебиваний на моно-записи — это «нечем считать», а не «не было»."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 30)
    for n in range(0, 30, 2):
        задание = db.get_job(f"job{n:04d}")
        db.save_segments(f"job{n:04d}",
                         [{"start": 0.0, "end": 20.0, "text": задание["text"]}])
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    свод = Insights(db).summary("all")
    assert свод["mono"] == 15, свод["mono"]
    assert свод["mono_share"] == 50.0, свод["mono_share"]

    выводы = [в for в in Insights(db).findings("all") if в.get("metric") == "mono_share"]
    assert выводы, "про неразделённых говорящих раздел молчит"
    assert "не считаются" in выводы[0]["text"], выводы[0]["text"]


# ---------------------------------------------------------------------------
# Стороны разговора — то, что считает каждая система речевой аналитики
# ---------------------------------------------------------------------------


def test_the_sides_of_the_call_are_measured_from_the_operator():
    """Доля речи, монолог против рассказа клиента, пауза перед ответом.

    Числа проверяются по разметке РАЗГОВОР руками: оператор SPEAKER_00
    говорит 4 + 4.3 + 5 + 1.8 = 15.1 с из 15.1 + 4.5 + 4.5 + 2.5 = 26.6 с
    звучащей речи; его самый долгий монолог — одна реплика в пять секунд,
    самый долгий рассказ клиента — 4.5 с. Из четырёх переходов слова к
    оператору один — перебивание (начал на 0.3 с раньше), один — после
    четырёхсекундной паузы (это тишина, не пауза перед ответом), и лишь
    последний — ответ через 0.2 с; он и есть его пауза перед ответом.
    """
    р = _разбор()
    речь = р["speech"]
    стороны = речь["sides"]

    assert стороны["agent"] == "SPEAKER_00" and стороны["customer"] == "SPEAKER_01"
    assert стороны["talk_share"] == pytest.approx(15.1 / 26.6, abs=0.01), стороны
    assert стороны["monologue_s"] == 5.0, стороны
    assert стороны["customer_story_s"] == 4.5, стороны
    # Пауза перед ответом: только ответы, не перебивания и не молчание.
    assert стороны["reply_delay_s"] == pytest.approx(0.2, abs=0.01), стороны
    # А у клиента — три ответа по полсекунды.
    assert стороны["customer_reply_delay_s"] == pytest.approx(0.5, abs=0.01), стороны
    assert стороны["tempo_ratio"] is not None and стороны["tempo_ratio"] > 0

    # Смены говорящего: семь реплик по очереди — шесть смен.
    assert речь["switches"] == 6, речь
    # Наложение: третья реплика началась на 0.3 с раньше конца второй.
    assert речь["overlap_s"] == pytest.approx(0.3, abs=0.01), речь
    # Заметная тишина: одна пауза в четыре секунды.
    assert речь["dead_air_s"] == 4.0, речь


def test_without_an_operator_the_sides_are_unknown_and_not_invented():
    """Не зная, кто оператор, «долю речи оператора» выдумывать нельзя."""
    from asrhub.content import speech

    речь = speech.analyze(РАЗГОВОР, 32.0)
    стороны = speech.sides(речь, None)
    assert стороны["talk_share"] is None and стороны["monologue_s"] is None
    assert стороны["reply_delay_s"] is None and стороны["tempo_ratio"] is None
    # Оператор назван, но его нет среди говорящих — то же самое.
    assert speech.sides(речь, "SPEAKER_09")["talk_share"] is None


def test_a_call_with_both_sharp_and_warm_lines_is_mixed_not_neutral():
    """Средняя по такому разговору — около нуля, и «нейтральная» врала.

    Разговор с четырьмя резкими и четырьмя тёплыми репликами неотличим по
    средней от ровного, хотя слушать нужно именно его. Класс есть у NICE
    ровно по этой причине.
    """
    from asrhub import content

    сегменты = []
    t = 0.0
    for _ in range(4):
        сегменты.append({"start": t, "end": t + 3, "speaker": "SPEAKER_01",
                         "text": "Отвратительное качество, ужасно, я крайне недоволен."})
        t += 3.5
        сегменты.append({"start": t, "end": t + 3, "speaker": "SPEAKER_00",
                         "text": "Спасибо, всё отлично, замечательно, вы очень помогли."})
        t += 3.5
    р = content.analyze(text=" ".join(с["text"] for с in сегменты),
                        segments=сегменты, duration_s=t)
    assert р["sentiment"]["label"] == "смешанная", р["sentiment"]
    assert abs(р["sentiment"]["score"]) < 0.15, р["sentiment"]

    # Одна резкая реплика на фоне ровного разговора — не смешанный.
    ровный = [{"start": i * 4.0, "end": i * 4.0 + 3, "speaker": f"SPEAKER_0{i % 2}",
               "text": "Хорошо, договорились, отправлю сегодня."} for i in range(6)]
    ровный.append({"start": 24.0, "end": 27.0, "speaker": "SPEAKER_01",
                   "text": "Отвратительное качество, ужасно."})
    р = content.analyze(text=" ".join(с["text"] for с in ровный),
                        segments=ровный, duration_s=27.0)
    assert р["sentiment"]["label"] != "смешанная", р["sentiment"]


def test_the_sides_reach_the_database_the_summary_and_the_selections(tmp_path):
    """Показатель, которого нет в своде и отборах, всё равно что не посчитан.

    Именно так и было: пауза перед ответом считалась с первой версии
    разбора и не показывалась нигде.
    """
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 24)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    свод = Insights(db).summary("all")
    for ключ in ("talk_share", "monologue_s", "customer_story_s", "switches",
                 "reply_delay_s", "overlap_s", "dead_air_s", "tempo_ratio",
                 "mixed", "long_monologues"):
        assert ключ in свод, ключ
    # В корпусе оператор здоровается первым и говорит первую реплику:
    # доля его речи — известное число, а не None.
    assert свод["talk_share"] is not None and 0 < свод["talk_share"] < 1, свод
    assert свод["switches"] == 1.0, свод

    for отбор in ("monologue", "customer_story", "mixed", "dead_air", "impatient"):
        ответ = Insights(db).records(отбор, "all")
        assert "items" in ответ, отбор

    # И в списке заданий — те же отборы.
    for отбор in ("monologue", "mixed", "dead_air"):
        assert db.count_jobs(status="completed", content=отбор) >= 0, отбор


def test_findings_speak_about_the_sides_only_when_there_is_something_to_say(tmp_path):
    """Правило про монологи и долю речи — с числами и только по порогу."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 24)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    # В корпусе оператор говорит 8 с из 19 — долю 65 % правило не назовёт.
    тексты = [в["text"] for в in Insights(db).findings("all")]
    assert not any("говорит больше клиента" in т for т in тексты), тексты

    # Перепишем разбор так, будто оператор говорил три четверти времени и
    # в каждой записи был монолог в три минуты, — оба вывода должны
    # появиться и назвать свои числа.
    db.execute("UPDATE content SET talk_share=0.75, monologue_s=180")
    тексты = [в["text"] for в in Insights(db).findings("all")]
    assert any("говорит больше клиента: 75%" in т for т in тексты), тексты
    assert any("монолог оператора" in т and "100.0%" in т for т in тексты), тексты


# ---------------------------------------------------------------------------
# Раздражение, повторное обращение, нецензурная лексика, окна в секундах
# ---------------------------------------------------------------------------


def test_frustration_is_told_apart_from_merely_negative_content():
    """«Доставка задерживается» и «сколько можно!» — обе отрицательные.

    Но слушать нужно второе: клиент не обсуждает плохое, а расстроен. У NICE
    это отдельный признак, и здесь тоже.
    """
    from asrhub import content

    спокойно = [{"start": 0, "end": 3, "speaker": "SPEAKER_00",
                 "text": "Здравствуйте, компания Ромашка."},
                {"start": 3.5, "end": 9, "speaker": "SPEAKER_01",
                 "text": "Доставка задерживается, качество плохое, я недоволен."}]
    р = content.analyze(text=" ".join(с["text"] for с in спокойно),
                        segments=спокойно, duration_s=9)
    assert р["sentiment"]["score"] < 0
    assert р["frustration"]["count"] == 0, р["frustration"]

    резко = [спокойно[0], {"start": 3.5, "end": 9, "speaker": "SPEAKER_01",
                           "text": "Сколько можно! Позовите руководителя, это безобразие."}]
    р = content.analyze(text=" ".join(с["text"] for с in резко),
                        segments=резко, duration_s=9)
    assert р["frustration"]["count"] == 1, р["frustration"]
    слова = р["frustration"]["items"][0]["words"]
    assert "сколько можно" in слова and "позовите руководителя" in слова, слова
    # «Безобразие» вошло в оборот «это безобразие» и второй раз не считается.
    assert "безобразие" not in слова and "это безобразие" in слова, слова


def test_frustration_is_looked_for_in_the_customers_lines_only():
    """«Сколько можно» из уст оператора — другая история."""
    from asrhub import content

    сегменты = [{"start": 0, "end": 3, "speaker": "SPEAKER_00",
                 "text": "Здравствуйте, компания Ромашка. Сколько можно вам объяснять."},
                {"start": 3.5, "end": 6, "speaker": "SPEAKER_01",
                 "text": "Добрый день, спасибо."}]
    р = content.analyze(text=" ".join(с["text"] for с in сегменты),
                        segments=сегменты, duration_s=6)
    assert р["frustration"]["speaker"] == "SPEAKER_01"
    assert р["frustration"]["count"] == 0, р["frustration"]


def test_repeat_contact_is_seen_by_turns_of_phrase_not_single_words():
    """«Снова здравствуйте» — не повторное обращение, «снова не работает» — да."""
    from asrhub import content

    def разбор(текст):
        сег = [{"start": 0, "end": 3, "speaker": "SPEAKER_00", "text": "Здравствуйте."},
               {"start": 3.5, "end": 9, "speaker": "SPEAKER_01", "text": текст}]
        return content.analyze(text="Здравствуйте. " + текст, segments=сег, duration_s=9)

    assert разбор("Снова здравствуйте, опять я.")["repeat_contact"]["count"] == 0
    р = разбор("Я уже звонил вчера, и до сих пор не работает, в третий раз обращаюсь.")
    assert р["repeat_contact"]["count"] == 1, р["repeat_contact"]
    assert set(р["repeat_contact"]["items"][0]["words"]) >= {
        "уже звонил", "до сих пор не", "в третий раз"}, р["repeat_contact"]


def test_profanity_is_off_by_default_and_unknown_is_not_zero():
    """Пока словарь выключен, в записи стоит «не считалось», а не ноль."""
    from asrhub import content

    сег = [{"start": 0, "end": 3, "speaker": "SPEAKER_00",
            "text": "Что за хрень, идиоты, охренели совсем."},
           {"start": 3.5, "end": 6, "speaker": "SPEAKER_01",
            "text": "Срок сорван, из Херсона звоню, фигурку херувима и сучковатую "
                    "доску не привезли, рубля не дали."}]
    текст = " ".join(с["text"] for с in сег)

    р = content.analyze(text=текст, segments=сег, duration_s=6)
    свод, _ = content.features(р)
    assert свод["profanity"] is None and свод["profanity_agent"] is None, свод
    # Выключенный словарь не ищет вовсе: найденное им нигде не оседает.
    assert р["profanity"]["items"] == [] and р["profanity"]["count"] == 0, р["profanity"]

    р = content.analyze(text=текст, segments=сег, duration_s=6, profanity=True)
    свод, _ = content.features(р)
    assert свод["profanity"] == 1 and свод["profanity_agent"] == 1, свод
    # Созвучные слова словарь не трогает: Херсон, рубля, срок — их корень
    # не ловит; херувим и сучковатую корень ловит, и их спасает список
    # исключений.
    assert р["profanity"]["items"][0]["speaker"] == "SPEAKER_00"
    assert {"хрень", "идиоты", "охренели"} == set(р["profanity"]["items"][0]["words"])
    assert len(р["profanity"]["items"]) == 1, р["profanity"]


def test_a_script_item_can_demand_its_marker_within_seconds():
    """«Разговор записывается» обязано прозвучать в первые 30 секунд.

    Пятая часть реплик в часовом разговоре — это двенадцать минут, и
    предупреждение на десятой засчитывалось.
    """
    from asrhub.content import compliance

    сег = [{"start": 0, "end": 3, "speaker": "A", "text": "Алло, слушаю вас."},
           {"start": 3.5, "end": 8, "speaker": "B", "text": "Здравствуйте, по договору."},
           {"start": 40, "end": 45, "speaker": "A", "text": "Разговор записывается."},
           {"start": 46, "end": 50, "speaker": "A", "text": "Всего доброго."}]
    пункт = {"id": "rec", "label": "Предупредил о записи",
             "any": ["разговор записывается"], "where": "start"}

    # По долям реплик начало — первая реплика оператора: не прошло.
    assert not compliance.check(сег, script=[пункт], speaker="A")["items"][0]["passed"]
    # Окно в 45 секунд накрывает реплику на сороковой; окно в 30 — нет.
    assert compliance.check(сег, script=[{**пункт, "within_s": 45}],
                            speaker="A")["items"][0]["passed"]
    assert not compliance.check(сег, script=[{**пункт, "within_s": 30}],
                                speaker="A")["items"][0]["passed"]
    # Окно от конца: прощание в последние пять секунд.
    прощание = {"id": "bye", "label": "Попрощался", "any": ["всего доброго"],
                "where": "end", "within_s": 5}
    assert compliance.check(сег, script=[прощание], speaker="A")["items"][0]["passed"]
    # А в отчёте окно названо, чтобы «где искали» было понятно без кода.
    assert compliance.check(сег, script=[прощание],
                            speaker="A")["items"][0]["within_s"] == 5


def test_the_new_signals_reach_the_summary_the_selections_and_the_digest(tmp_path):
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights
    from asrhub.maintenance import digest_text

    db = _корпус(tmp_path, 24)
    ContentIndex(db, _Настройки()).backfill_once(limit=100)
    свод = Insights(db).summary("all")
    for ключ in ("frustrated", "frustrated_share", "repeat", "repeat_share",
                 "profanity_records", "profanity_agent_records", "profanity_checked"):
        assert ключ in свод, ключ
    # Словарь мата выключен — проверенных записей ноль, и это видно.
    assert свод["profanity_checked"] == 0, свод

    for отбор in ("frustrated", "repeat", "profanity", "profanity_agent"):
        assert "items" in Insights(db).records(отбор, "all"), отбор
        assert db.count_jobs(status="completed", content=отбор) == 0, отбор

    db.execute("UPDATE content SET frustration=2, repeat_contact=1")
    свод = Insights(db).summary("all")
    assert свод["frustrated"] == 24 and свод["repeat_share"] == 100.0, свод
    текст = digest_text({}, {}, {}, содержание={"summary": свод})
    assert "раздражения клиента: 24" in текст, текст
    assert "Повторных обращений по нерешённому вопросу: 24" in текст, текст


# ---------------------------------------------------------------------------
# Здоровье распознавания: подозрительные расшифровки
# ---------------------------------------------------------------------------


def _подозрительные_сегменты():
    return [
        {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00",
         "text": "Здравствуйте, компания Ромашка, меня зовут Анна.",
         "confidence": 0.92, "no_speech_prob": 0.01, "compression_ratio": 1.4,
         "temperature": 0.0},
        {"start": 4.5, "end": 5.0, "speaker": "SPEAKER_01",
         "text": "да да да да да да да да да да да да да да да да да да да да",
         "confidence": 0.41, "no_speech_prob": 0.2, "compression_ratio": 3.1,
         "temperature": 0.4},
        {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01",
         "text": "Субтитры сделал DimaTorzok",
         "confidence": 0.2, "no_speech_prob": 0.9, "compression_ratio": 1.2,
         "temperature": 0.0},
        {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_00",
         "text": "Хорошо, спасибо, всего доброго.",
         "confidence": 0.8, "no_speech_prob": 0.05, "compression_ratio": 1.1,
         "temperature": 0.0},
    ]


def test_hallucinations_are_seen_by_the_signs_whisper_itself_publishes():
    """Средняя уверенность по такому заданию — 0,58, и она ничего не выдала.

    Признаки считаются по сегментам: сжатие выше 2,4 и перебор температур
    (пороги самого Whisper), текст на тишине (no_speech > 0,6 при
    уверенности ниже e^-1), невозможный темп, повторы, известная фраза.
    """
    from asrhub import quality

    оценка = quality.assess(_подозрительные_сегменты(), expected_speakers=2)
    assert оценка["segments"] == 4 and оценка["suspect"] == 2, оценка
    assert оценка["suspect_share"] == 0.5
    причины = оценка["reasons"]
    assert причины["compression"] == 1 and причины["temperature"] == 1
    assert причины["tempo"] == 1 and причины["repeat"] == 1
    assert причины["silence"] == 1 and причины["phrase"] == 1
    # Говорящих двое, ожидалось двое — признака нет.
    assert "speakers" not in оценка["flags"], оценка["flags"]
    # Примеры — с временем и причиной, для карточки.
    фраза = next(п for п in оценка["items"] if п["phrase"])
    assert фраза["start_s"] == 5.0 and фраза["phrase"] == "субтитры сделал"

    # Чистая запись чиста: ни одного признака на обычной речи.
    чистая = [с for с in _подозрительные_сегменты() if с["compression_ratio"] < 2]
    чистая = [с for с in чистая if "субтитры" not in с["text"].lower()]
    assert quality.assess(чистая)["suspect"] == 0


def test_the_expected_number_of_speakers_is_checked_only_when_asked():
    from asrhub import quality

    моно = [{"start": i * 3.0, "end": i * 3.0 + 2.5, "speaker": "SPEAKER_00",
             "text": "Обычная реплика без всяких признаков."} for i in range(8)]
    assert "speakers" not in quality.assess(моно)["flags"]
    assert "speakers" in quality.assess(моно, expected_speakers=2)["flags"]
    # Осколки разметки: когда треть реплик короче секунды.
    осколки = [{"start": i * 1.0, "end": i * 1.0 + 0.4, "speaker": f"SPEAKER_0{i % 3}",
                "text": "да"} for i in range(9)]
    assert "fragments" in quality.assess(осколки)["flags"]


def test_quality_flags_are_stored_with_the_job_and_reach_the_list_and_the_report(tmp_path):
    """Задание получает признаки, отбор «подозрительная расшифровка» их видит."""
    from asrhub import quality
    from asrhub.analytics import Analytics
    from asrhub.content_index import ContentIndex
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for n, сегменты in ((0, _подозрительные_сегменты()),
                        (1, [с for с in _подозрительные_сегменты()
                             if с["compression_ratio"] < 2 and "субтитры" not in с["text"].lower()])):
        job_id = db.create_job({"id": f"q{n}", "filename": f"{n}.wav",
                                "media_duration_s": 12.0, "owner": "anna",
                                "engine": "whisper", "model": "large-v3",
                                "language": "ru", "source": "api"})
        db.update_job(job_id, status="completed", finished_at=сейчас,
                      text=" ".join(с["text"] for с in сегменты),
                      segments_count=len(сегменты))
        db.save_segments(job_id, сегменты)
        db.update_job(job_id, **quality.for_job(quality.assess(сегменты)))

    плохое = db.get_job("q0")
    assert плохое["suspect_segments"] == 2 and плохое["suspect_share"] == 0.5
    assert "phrase" in плохое["quality_flags"], плохое["quality_flags"]
    assert плохое["quality_detail"]["items"][0]["start_s"] == 4.5
    assert db.get_job("q1")["quality_flags"] == []

    assert db.count_jobs(status="completed", content="suspect") == 1
    assert db.count_jobs(status="completed", content="hallucination") == 1
    assert [j["id"] for j in db.list_jobs(status="completed", content="suspect")] == ["q0"]

    отчёт = Analytics(db).suspicious("all")
    assert отчёт["assessed"] == 2 and отчёт["flagged"] == 1
    assert отчёт["suspect_share"] == pytest.approx(100.0 * 2 / 6, abs=0.1), отчёт   # 4 + 2 сегмента
    assert {ф["key"] for ф in отчёт["by_flag"]} >= {"phrase", "repeat", "compression"}
    assert отчёт["by_model"][0]["key"] == "large-v3"
    assert отчёт["worst"][0]["id"] == "q0"

    # Записи, сделанные до появления признаков, доразмечает фоновый разбор.
    db.execute("UPDATE jobs SET quality_flags=NULL, suspect_segments=NULL")
    assert Analytics(db).suspicious("all")["assessed"] == 0
    индекс = ContentIndex(db, _Настройки())
    индекс.backfill_once(limit=10)
    assert db.get_job("q0")["suspect_segments"] == 2
    assert Analytics(db).suspicious("all")["assessed"] == 2

    # И пересчёт одной записи пересчитывает признаки: смена ожидаемого
    # числа говорящих иначе ждала бы фонового разбора, который эту запись
    # уже прошёл.
    db.execute("UPDATE jobs SET quality_flags=NULL, suspect_segments=NULL WHERE id='q0'")
    индекс.recompute(["q0"])
    assert db.get_job("q0")["suspect_segments"] == 2


def test_new_words_of_the_period_are_those_the_previous_period_never_heard(tmp_path):
    """Слово из трёх свежих записей, которого раньше не было, — новое.

    Из одной — ошибка распознавания, а не новость.
    """
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, 40)
    сейчас = time.time()
    # Три свежие записи получают слово «мегаакция», две — «опечатка»: оно
    # проходит порог тем (две записи), но не порог новизны (три).
    for n, слово in ((0, "мегаакция"), (1, "мегаакция"), (2, "мегаакция"),
                     (3, "опечатка"), (4, "опечатка")):
        db.execute("UPDATE jobs SET text = text || ? , created_at=? WHERE id=?",
                   (f" {слово} {слово}", сейчас - n * 60, f"job{n:04d}"))
    # Остальные — в прошлую неделю, чтобы окна «неделя» и «прошлая неделя»
    # были непустыми.
    db.execute("UPDATE jobs SET created_at = ? - 8*86400 - (CAST(substr(id, 4) AS INTEGER) * 60) "
               "WHERE id NOT IN ('job0000','job0001','job0002','job0003','job0004')", (сейчас,))
    ContentIndex(db, _Настройки()).backfill_once(limit=100)

    новые = {т["word"]: т["now"] for т in Insights(db).new_topics("week")}
    assert "мегаакция" in новые and новые["мегаакция"] == 3, новые
    assert "опечатка" not in новые, новые
    # Слова прошлого периода новыми не считаются.
    assert "ромашка" not in новые and "суд" not in новые, новые


# ---------------------------------------------------------------------------
# Персональные данные: удаление по требованию и отметка согласия
# ---------------------------------------------------------------------------


def test_erasure_by_request_finds_by_transcript_and_deletes_only_when_asked(
        data_dir, monkeypatch):
    """Отзыв согласия — и записи уничтожаются в срок до тридцати дней.

    По умолчанию — пробный запуск: показывает, что нашлось, и не удаляет.
    Удаляет только администратор и только с dry_run=false. В журнале
    событий запрос усечён: номер телефона в журнале — ещё одно место,
    откуда его придётся удалять.
    """
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        for n, текст in enumerate(("Мой номер восемь девятьсот 5551234, перезвоните.",
                                   "Обычный разговор без номера.")):
            job_id = db.create_job({"id": f"pd{n}", "filename": f"{n}.wav",
                                    "owner": "пользователь", "media_duration_s": 5.0})
            db.update_job(job_id, status="completed", finished_at=1.0, text=текст)
            db.save_segments(job_id, [{"start": 0, "end": 5, "text": текст}])

        админ = {"X-API-Key": "ah_admin_k"}
        # Обычному ключу нельзя даже смотреть.
        assert c.post("/api/maintenance/erase", json={"query": "5551234"},
                      headers={"X-API-Key": "ah_user_k"}).status_code == 403
        # Короткий запрос отвергается: три символа нашли бы половину архива.
        assert c.post("/api/maintenance/erase", json={"query": "555"},
                      headers=админ).status_code == 400

        пробный = c.post("/api/maintenance/erase", json={"query": "5551234"},
                         headers=админ)
        assert пробный.status_code == 200, пробный.text
        assert пробный.json()["dry_run"] is True
        assert пробный.json()["ids"] == ["pd0"], пробный.json()
        assert db.get_job("pd0") is not None, "пробный запуск удалил запись"

        боевой = c.post("/api/maintenance/erase",
                        json={"query": "5551234", "dry_run": False}, headers=админ)
        assert боевой.status_code == 200 and боевой.json()["deleted"] == 1
        assert db.get_job("pd0") is None and db.get_job("pd1") is not None

        события = [dict(r) for r in db.query(
            "SELECT * FROM events WHERE kind='erase'")]
        assert len(события) == 1, события
        assert "5551234" not in события[0]["message"], события[0]["message"]
        assert "555…" in события[0]["message"], события[0]["message"]


def test_records_without_a_consent_tag_are_listed_when_the_tag_is_set(
        data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        давно = time.time() - 40 * 86400
        for n, метки in enumerate(("согласие", "", "несогласие,vip")):
            job_id = db.create_job({"id": f"cs{n}", "filename": f"{n}.wav",
                                    "owner": "пользователь", "tags": метки,
                                    "media_duration_s": 5.0})
            db.update_job(job_id, status="completed", finished_at=давно, text="текст")
            db.execute("UPDATE jobs SET created_at=? WHERE id=?", (давно, job_id))
        админ = {"X-API-Key": "ah_admin_k"}

        # Метка не задана — проверка выключена и об этом сказано.
        ответ = c.get("/api/maintenance/consent", headers=админ).json()
        assert ответ["enabled"] is False and ответ["count"] == 0

        app.state.hub.settings.set("consent_tag", "согласие")
        ответ = c.get("/api/maintenance/consent", headers=админ).json()
        assert ответ["enabled"] is True and ответ["days"] == 30
        # «Несогласие» — не «согласие»: сравнение по целой метке.
        assert sorted(ответ["ids"]) == ["cs1", "cs2"], ответ
        # Свежие записи (моложе срока) в список не попадают.
        assert c.get("/api/maintenance/consent?days=3650", headers=админ).json()["count"] == 0


def test_the_job_list_accepts_the_quality_selections_too(data_dir, monkeypatch):
    """Ручка проверяла ключ отбора только по перечню содержания.

    Отбор «подозрительная расшифровка» есть и там, где разбор содержания
    выключен, — и ручка отвергала его как неизвестный, хотя база его знала.
    """
    from asrhub import quality
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        сегменты = _подозрительные_сегменты()
        job_id = db.create_job({"id": "qa0", "filename": "0.wav", "owner": "пользователь",
                                "media_duration_s": 12.0})
        db.update_job(job_id, status="completed", finished_at=1.0,
                      text="текст", segments_count=len(сегменты),
                      **quality.for_job(quality.assess(сегменты)))
        заголовки = {"X-API-Key": "ah_user_k"}
        for отбор in ("suspect", "hallucination"):
            ответ = c.get(f"/api/jobs?status=completed&content={отбор}", headers=заголовки)
            assert ответ.status_code == 200, ответ.text
            assert [j["id"] for j in ответ.json()["items"]] == ["qa0"], ответ.json()
        assert c.get("/api/jobs?content=speakers_mismatch",
                     headers=заголовки).json()["items"] == []
        assert c.get("/api/jobs?content=нет-такого", headers=заголовки).status_code == 400


# ---------------------------------------------------------------------------
# Категории обращений: движок правил
# ---------------------------------------------------------------------------


def _текст(*реплики):
    from asrhub.content.rules import Text

    return Text.of([{"text": т} for т in реплики])


def test_rules_understand_and_or_not_near_and_precedence():
    """Операторы и их старшинство — как у Genesys: НЕ, РЯДОМ, И, ИЛИ.

    Проверяется не разбор сам по себе, а то, что правило значит то, что
    прочитает человек: «возврат И НЕ брак» не срабатывает на записи про
    бракованный возврат, а «а ИЛИ б И в» — это «а ИЛИ (б И в)».
    """
    from asrhub.content import rules

    текст = _текст("Хочу оформить возврат, товар с браком, у конкурентов дешевле")

    def значит(правило):
        return rules.evaluate(rules.parse(правило), текст).matched

    assert значит("возврат")
    assert значит("возврат И брак")
    assert not значит("возврат И НЕ брак")
    assert not значит("возврат НЕ брак")            # то же самое без И
    assert значит("доставка ИЛИ возврат И брак")
    assert not значит("доставка ИЛИ возврат И НЕ брак")
    assert значит("(доставка ИЛИ возврат) И НЕ доставка")
    assert not значит("НЕ (возврат ИЛИ доставка)")
    assert значит("конкурент РЯДОМ(2) дешевле")
    # Между «возврат» и «дешевле» ровно пять слов: окно в пять — да, в
    # четыре — нет; без числа — восемь, как у NICE.
    assert значит("возврат РЯДОМ(5) дешевле")
    assert not значит("возврат РЯДОМ(4) дешевле")
    assert значит("возврат РЯДОМ дешевле")
    # Скобки и старшинство — в каноническом виде, который показывает редактор.
    assert rules.describe(rules.parse("а ИЛИ б И НЕ в")) == "а ИЛИ (б И НЕ в)"


def test_rules_match_stems_without_quotes_and_forms_inside_them():
    """Без кавычек — по основам, в кавычках — точно; строчное «и» — слово."""
    from asrhub.content import rules

    текст = _текст("Уточню по оплате: деньги списали, хлеб и соль привезли")
    assert rules.evaluate(rules.parse("уточнить"), текст).matched
    assert rules.evaluate(rules.parse("оплата"), текст).matched
    assert not rules.evaluate(rules.parse('"оплата"'), текст).matched
    assert rules.evaluate(rules.parse('"оплате"'), текст).matched
    assert rules.evaluate(rules.parse("хлеб и соль"), текст).matched
    assert not rules.evaluate(rules.parse("хлеб И соль И масло"), текст).matched
    # Одно слово записи — одно совпадение, сколько бы примет на него ни
    # указывало: «оплата ИЛИ оплатить» сводится к одной основе.
    итог = rules.evaluate(rules.parse("оплата ИЛИ оплатить ИЛИ оплате"), текст)
    assert len(итог.hits) == 1, итог.hits


def test_rules_report_errors_with_a_position_and_respect_the_limits():
    """Ошибка правила — с позицией и по-русски; пределы — как у Genesys."""
    from asrhub.content import rules

    for правило, слово in (("оплата ИЛИ", "оборвано"), ("(оплата", "не закрыта"),
                           ("оплата)", "лишняя"), ("ИЛИ оплата", "без операнда"),
                           ("", "пустое"), ('""', "кавычки"),
                           ("((((а))))", "глубже 3"),
                           (" ИЛИ ".join(f"с{i}" for i in range(21)), "больше 20"),
                           ("а РЯДОМ(0) б", "от 1 до")):
        ошибка = rules.check(правило)
        assert слово in ошибка, (правило, ошибка)
        assert "позиция" in ошибка, ошибка
    with pytest.raises(rules.RuleError) as п:
        rules.parse("оплата ИЛИ ИЛИ платёж")
    assert п.value.position == 11
    assert rules.check("оплата ИЛИ платёж") == ""


def test_the_script_is_a_special_case_of_a_rule():
    """Список примет — это ИЛИ; а пункту доступны и остальные операторы."""
    from asrhub.content import compliance

    сегменты = [{"start": 0.0, "end": 4.0, "speaker": "A",
                 "text": "Алло, это опять вы? Меня зовут Анна"},
                {"start": 4.0, "end": 8.0, "speaker": "B", "text": "Здравствуйте"}]
    скрипт = [{"id": "intro", "label": "Представился",
               "rule": '"меня зовут" НЕ "вы позвонили"', "where": "start"},
              {"id": "bad", "label": "Сломанный", "rule": "меня ИЛИ (", "where": "any"},
              {"id": "old", "label": "По списку", "any": ["опять вы"], "where": "any"}]
    итог = compliance.check(сегменты, script=скрипт)
    пункты = {п["id"]: п for п in итог["items"]}
    assert пункты["intro"]["passed"] and пункты["intro"]["matched"] == "меня зовут"
    assert not пункты["bad"]["passed"] and "оборвано" in пункты["bad"]["error"]
    assert пункты["old"]["passed"] and пункты["old"]["matched"] == "опять вы"
    assert итог["passed"] == 2 and итог["checked"] == 3
    # Проверка примет на частые слова видит операнды правила, а не только список.
    assert {с["word"] for с in compliance.suspicious(
        [{"label": "x", "rule": 'оплата ИЛИ это ИЛИ "то"'}])} == {"это", "то"}


def test_categories_respect_who_said_it_and_where():
    """Фильтр «кто сказал» и окно «где» — то, чем категория отличается
    от поиска по словам."""
    from asrhub.content import categories

    сегменты = [
        {"start": 0.0, "end": 5.0, "speaker": "A", "text": "Здравствуйте, компания Ромашка"},
        {"start": 5.0, "end": 12.0, "speaker": "B",
         "text": "Я уже звонил вчера, оплата не прошла, это безобразие"},
        {"start": 12.0, "end": 20.0, "speaker": "A",
         "text": "Не знаю, проверю оплату и перезвоню"},
        {"start": 20.0, "end": 60.0, "speaker": "B", "text": "Хорошо, жду, до свидания"},
    ]
    набор = [
        {"id": "pay", "label": "Оплата", "rule": "оплата", "who": "any"},
        {"id": "pay_c", "label": "Оплата (клиент)", "rule": "оплата", "who": "customer"},
        {"id": "stop", "label": "Стоп-слова", "rule": '"не знаю"', "who": "agent",
         "kind": "violation"},
        {"id": "stop_c", "label": "Стоп у клиента", "rule": '"не знаю"', "who": "customer"},
        {"id": "bye", "label": "Прощание", "rule": "до свидания", "where": "end",
         "within_s": 30},
        {"id": "bye_early", "label": "Прощание в начале", "rule": "до свидания",
         "where": "start", "within_s": 10},
        # Окно в 25 с от начала захватывает все реплики; без окна «начало»
        # — это пятая часть реплик, то есть одна первая.
        {"id": "bye_wide", "label": "Прощание в первые 25 с", "rule": "до свидания",
         "where": "start", "within_s": 25},
        {"id": "broken", "label": "Сломанная", "rule": "оплата И"},
    ]
    итог = categories.apply(сегменты, набор, agent="A", customer="B", everything=True)
    по = {и["id"]: и for и in итог["items"]}
    assert по["pay"]["count"] == 2 and по["pay"]["first_s"] == 5.0
    assert по["pay_c"]["count"] == 1 and по["pay_c"]["hits"][0]["speaker"] == "B"
    assert по["stop"]["count"] == 1 and по["stop"]["kind"] == "violation"
    assert по["stop_c"]["count"] == 0
    assert по["bye"]["count"] == 1 and по["bye_early"]["count"] == 0
    assert по["bye_wide"]["count"] == 1
    assert по["broken"]["error"] and по["broken"]["count"] == 0
    assert итог["errors"] == 1 and set(итог["matched"]) == {"pay", "pay_c", "stop", "bye",
                                                            "bye_wide"}
    # В базу уходят только сработавшие — по строке на категорию.
    строки = categories.for_db(итог)
    assert {с["category"] for с in строки} == {"pay", "pay_c", "stop", "bye", "bye_wide"}
    assert all({"category", "kind", "count", "first_s"} <= set(с) for с in строки)

    # Без определённых сторон «только оператор» ищет по всей записи и
    # честно об этом говорит.
    без_сторон = categories.apply(сегменты, набор, everything=True)
    по = {и["id"]: и for и in без_сторон["items"]}
    assert по["stop"]["count"] == 1 and по["stop"]["sides"] is False
    assert по["pay"]["sides"] is True


def test_the_ready_made_categories_parse_and_are_used_when_nothing_is_saved():
    """Готовый набор — годный целиком и действует по умолчанию."""
    from asrhub import content
    from asrhub.content import categories

    assert categories.validate(categories.ГОТОВЫЕ) == []
    assert len(categories.ГОТОВЫЕ) == 12
    assert len({к["id"] for к in categories.ГОТОВЫЕ}) == 12
    assert {к["kind"] for к in categories.ГОТОВЫЕ} == {"topic", "objection", "handling"}
    разбор = content.analyze(text="", segments=[
        {"start": 0.0, "end": 4.0, "speaker": "A", "text": "Здравствуйте, компания Ромашка"},
        {"start": 4.0, "end": 9.0, "speaker": "B",
         "text": "Оплата не прошла, а курьер так и не приехал"}])
    assert set(разбор["categories"]["matched"]) == {"payment", "delivery"}
    assert разбор["categories"]["checked"] == 12
    # Пустой список — «не искать», а не «готовый набор»: так просят
    # выключить категории.
    пусто = content.analyze(text="оплата", categories=[])
    assert пусто["categories"]["checked"] == 0

    # Проверка набора при сохранении: ошибка называет категорию и позицию.
    ошибки = categories.validate([{"label": "Оплата", "rule": "оплата ИЛИ ("},
                                  {"label": "", "rule": "х", "who": "кто-то"}])
    assert any("Оплата" in о and "позиция" in о for о in ошибки), ошибки
    assert any("нет названия" in о for о in ошибки), ошибки
    assert any("who" in о for о in ошибки), ошибки


def test_category_hits_reach_the_database_the_counts_and_the_job_list(tmp_path):
    """Совпадения лежат своей таблицей: по ней считается счёт, динамика и
    отбор списка заданий; удаление задания забирает их с собой."""
    from asrhub import content
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, записей=30)
    # Каждая третья запись — плохая, про сорванный срок и суд; остальные —
    # про договор. Категория «Сроки» из готового набора должна собрать
    # ровно плохие, «Эскалация» — их же (суд говорит клиент, SPEAKER_01).
    индекс = ContentIndex(db, settings=None)
    while индекс.backfill_once(limit=20):
        pass
    свод = Insights(db, индекс).categories("all")
    по = {к["id"]: к for к in свод["items"]}
    assert по["deadline"]["records"] == 10 and по["deadline"]["share"] == 33.3
    assert по["escalation"]["records"] == 10
    assert по["payment"]["records"] == 0 and по["payment"]["share"] == 0.0
    assert свод["corpus"] == 30
    assert свод["uncategorized"]["records"] == 20
    # Совпадений в таблице — по строке на пару «запись — категория».
    строк = db.query_one("SELECT COUNT(*) AS n FROM content_hits")["n"]
    assert строк == 20, строк
    # «Без категории» — про категории обращений: нарушение оператора в
    # записи не делает её «про что-то».
    db.save_content("job0001", {"version": content.VERSION, "hits": [
        {"category": "stop", "kind": "violation", "count": 1, "first_s": 0.0}]})
    assert Insights(db, индекс).categories("all")["uncategorized"]["records"] == 20

    # Отбор списка заданий по категории — параметром, а не подстановкой.
    список = db.list_jobs(status="completed", content="category:deadline", light=True)
    assert len(список) == 10 and all(int(j["id"][3:]) % 3 == 0 for j in список)
    assert db.count_jobs(status="completed", content="category:deadline") == 10
    assert db.list_jobs(content="category:нет такой") == []

    # Удаление задания уносит и его совпадения.
    db.delete_job("job0000")
    assert db.query_one("SELECT COUNT(*) AS n FROM content_hits WHERE job_id='job0000'")["n"] == 0
    assert db.count_jobs(status="completed", content="category:deadline") == 9


def test_category_dynamics_compare_shares_between_windows(tmp_path):
    """«Растёт» и «угасает» — по доле к прошлому окну, не по числу."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, записей=40)
    сейчас = time.time()
    # Прошлая неделя: записи 20–39 (по часу назад каждая — все в текущей
    # неделе). Сдвигаем половину на восемь дней назад и делаем их «про
    # оплату», чтобы в текущем окне доля оплаты упала, а сроков — выросла.
    for n in range(20, 40):
        db.execute("UPDATE jobs SET created_at=? WHERE id=?",
                   (сейчас - 8 * 86400 - n * 60, f"job{n:04d}"))
        db.update_job(f"job{n:04d}", text="Оплата не прошла, деньги списали дважды.")
        db.save_segments(f"job{n:04d}", [
            {"start": 0.0, "end": 8.0, "speaker": "SPEAKER_00", "text": "Здравствуйте"},
            {"start": 9.0, "end": 20.0, "speaker": "SPEAKER_01",
             "text": "Оплата не прошла, деньги списали дважды."}])
    индекс = ContentIndex(db, settings=None)
    while индекс.backfill_once(limit=20):
        pass
    свод = Insights(db, индекс).categories("week")
    по = {к["id"]: к for к in свод["items"]}
    assert свод["corpus"] == 20 and свод["corpus_previous"] == 20
    assert по["payment"]["records"] == 0 and по["payment"]["previous"] == 20
    assert по["payment"]["share_previous"] == 100.0 and по["payment"]["delta"] == -100.0
    assert по["deadline"]["previous"] == 0 and по["deadline"]["records"] == 7
    assert по["deadline"]["delta"] == 35.0
    assert "payment" in свод["fading"] and "deadline" in свод["rising"]
    # Выводы называют категорию и оба числа.
    выводы = Insights(db, индекс).findings("week")
    тексты = [в["text"] for в in выводы if в.get("dimension") == "category"]
    assert any("Сроки" in т and "0.0% → 35.0%" in т for т in тексты), тексты
    assert any("Оплата" in т and "меньше" in т for т in тексты), тексты


def test_the_categories_endpoints_check_a_draft_and_report_the_period(
        tmp_path, monkeypatch, data_dir):
    """Ручки: счёт за период, проверка черновика на записи, отбор в списке."""
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        job_id = db.create_job({"id": "кат", "filename": "x.wav",
                                "owner": "админ", "media_duration_s": 20.0})
        db.update_job(job_id, status="completed", finished_at=1.0,
                      text="Здравствуйте. Оплата не прошла, это безобразие.")
        db.save_segments(job_id, [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "text": "Здравствуйте."},
            {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01",
             "text": "Оплата не прошла, это безобразие."}])
        ключ = {"X-API-Key": "ah_admin_k"}
        черновик = [{"id": "pay", "label": "Оплата", "rule": "оплата ИЛИ это", "who": "customer"},
                    {"id": "bad", "label": "Сломанная", "rule": "оплата ИЛИ"}]
        ответ = c.post("/api/content/categories/check",
                       json={"job_id": job_id, "categories": черновик}, headers=ключ)
        assert ответ.status_code == 200, ответ.text
        тело = ответ.json()
        по = {и["id"]: и for и in тело["result"]["items"]}
        assert по["pay"]["count"] == 2 and по["pay"]["hits"][0]["speaker"] == "SPEAKER_01"
        assert "оборвано" in по["bad"]["error"]
        assert {с["word"] for с in тело["suspicious"]} == {"это"}
        assert тело["errors"] and "Сломанная" in тело["errors"][0]
        assert тело["agent"] == "SPEAKER_00" and тело["customer"] == "SPEAKER_01"
        # Черновик в настройки не попал.
        assert app.state.hub.settings.get("content_categories") in (None, [])

        # Сломанный набор не сохраняется; годный — сохраняется и действует.
        плохо = c.put("/api/settings", json={"content_categories": черновик}, headers=ключ)
        assert плохо.status_code == 400 and "Сломанная" in плохо.text
        хорошо = c.put("/api/settings", json={"content_categories": черновик[:1]},
                       headers=ключ)
        assert хорошо.status_code == 200, хорошо.text
        перечни = c.get("/api/content/kinds", headers=ключ).json()
        assert перечни["categories_own"] is True
        assert [к["id"] for к in перечни["categories"]] == ["pay"]
        assert len(перечни["default_categories"]) == 12

        # Разбор записи — по сохранённому набору; счёт за период видит его.
        c.post(f"/api/content/jobs/{job_id}/recompute", headers=ключ)
        свод = c.get("/api/content/categories?period=all", headers=ключ).json()
        assert свод["own"] is True
        assert {к["id"]: к["records"] for к in свод["items"]} == {"pay": 1}
        assert свод["uncategorized"]["records"] == 0
        список = c.get("/api/jobs?content=category:pay", headers=ключ).json()
        assert [j["id"] for j in список["items"]] == [job_id]
        assert c.get("/api/jobs?content=category:none", headers=ключ).json()["items"] == []
        assert c.get("/api/jobs?content=nonsense", headers=ключ).status_code == 400
        # Карточка записи несёт категории с примерами.
        карточка = c.get(f"/api/content/jobs/{job_id}", headers=ключ).json()
        assert карточка["analysis"]["categories"]["matched"] == ["pay"]
        # Чужой ключ не видит чужую запись и в проверке.
        assert c.post("/api/content/categories/check", json={"job_id": job_id},
                      headers={"X-API-Key": "ah_user_k"}).status_code in (403, 404)


def test_categories_reach_the_digest_and_the_export(tmp_path):
    """Сводка называет, о чём звонили; выгрузка получает лист «Категории»."""
    import io
    import zipfile

    from asrhub.content_export import to_csv_zip
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights
    from asrhub.maintenance import _digest_content, digest_text

    db = _корпус(tmp_path, записей=30)
    индекс = ContentIndex(db, settings=None)
    while индекс.backfill_once(limit=20):
        pass
    свод = Insights(db, индекс)

    class Настройки:
        @staticmethod
        def get(key, default=None):
            return default

    содержание = _digest_content(свод, Настройки(), "all")
    assert содержание["categories"], содержание
    assert содержание["categories"][0]["label"] in ("Сроки", "Эскалация")
    assert содержание["uncategorized"]["records"] == 20
    текст = digest_text({}, {}, {}, содержание=содержание)
    assert "О чём звонили" in текст and "Сроки 33.3 %" in текст, текст
    assert "Без категории: 20 записей" in текст, текст

    отчёт = свод.report("all")
    assert отчёт["categories"]["items"]
    архив = zipfile.ZipFile(io.BytesIO(to_csv_zip(отчёт, "all")))
    имена = архив.namelist()
    лист = next(и for и in имена if "Категории" in и)
    содержимое = архив.read(лист).decode("utf-8-sig")
    assert "Сроки" in содержимое and "Правило" in содержимое


# ---------------------------------------------------------------------------
# Категории, часть вторая: возражения, драйверы, разрез, трекеры
# ---------------------------------------------------------------------------


def test_an_objection_counts_as_unhandled_only_when_handling_could_be_seen():
    """«Дорого» без ответа из «отработки» в следующих трёх репликах — без
    отработки; без категорий отработки в наборе — не считается вовсе."""
    from asrhub.content import categories

    сегменты = [
        {"start": 0.0, "end": 3.0, "speaker": "A", "text": "Здравствуйте, компания Ромашка"},
        {"start": 3.0, "end": 6.0, "speaker": "B", "text": "Это дорого, я подумаю"},
        {"start": 6.0, "end": 9.0, "speaker": "A", "text": "Понимаю, могу предложить рассрочку"},
        {"start": 9.0, "end": 12.0, "speaker": "B", "text": "Нет, мне не нужно"},
        {"start": 12.0, "end": 15.0, "speaker": "A", "text": "Хорошо, всего доброго"},
        {"start": 15.0, "end": 18.0, "speaker": "B", "text": "До свидания"},
        {"start": 18.0, "end": 21.0, "speaker": "A", "text": "Спасибо"},
        {"start": 21.0, "end": 24.0, "speaker": "A", "text": "Понимаю вас"},
    ]
    набор = [
        {"id": "obj", "label": "Возражение", "kind": "objection", "who": "customer",
         "rule": "дорого ИЛИ подумаю ИЛИ не нужно"},
        {"id": "hand", "label": "Отработка", "kind": "handling", "who": "agent",
         "rule": "понимаю ИЛИ могу предложить"},
    ]
    итог = categories.apply(сегменты, набор, agent="A", customer="B")
    в = итог["objections"]
    # Две реплики с возражениями («дорого, подумаю» — одна), первая отработана
    # следующей же репликой, вторая — нет: «понимаю вас» на четвёртой после
    # неё уже не считается.
    assert в["count"] == 2 and в["unhandled"] == 1, в
    assert в["items"][0]["handled"] is True and в["items"][0]["matched"] == "дорого, подумаю"
    assert в["items"][1]["handled"] is False and в["items"][1]["start_s"] == 9.0
    # Категория отработки сработала сама по себе — это отдельная категория.
    assert "hand" in итог["matched"]

    # Без отработки в наборе неотработанность неизвестна, а не «все без ответа».
    без = categories.apply(сегменты, набор[:1], agent="A", customer="B")["objections"]
    assert без["count"] == 2 and без["unhandled"] is None
    assert all(в["handled"] is None for в in без["items"])

    # В свод записи уходит и число, и NULL вместо нуля, когда считать нечем.
    from asrhub import content

    свод, _ = content.features(content.analyze(text="", segments=сегменты, categories=набор))
    assert свод["objections"] == 2 and свод["objections_unhandled"] == 1
    свод, _ = content.features(content.analyze(text="", segments=сегменты,
                                               categories=набор[:1]))
    assert свод["objections"] == 2 and свод["objections_unhandled"] is None


def test_drivers_of_negativity_are_lift_over_the_base_rate(tmp_path):
    """Подъём — частота среди отрицательных к частоте вообще, на достаточном
    числе записей; категория благополучных разговоров уходит в «держит наверху»."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights

    db = _корпус(tmp_path, записей=30)
    настройки = _Настройки(content_categories=[
        {"id": "deadline", "label": "Сроки", "rule": "срок ИЛИ сорван"},
        {"id": "contract", "label": "Договор", "rule": "договор"},
        {"id": "rare", "label": "Редкая", "rule": "суд"},
    ])
    # «Суд» есть в каждой плохой записи, но подрежем: у редкой категории
    # записей меньше порога — она не должна попасть в драйверы.
    индекс = ContentIndex(db, настройки)
    while индекс.backfill_once(limit=20):
        pass
    db.execute("DELETE FROM content_hits WHERE category='rare' AND job_id > 'job0015'")
    свод = Insights(db, индекс).drivers("all")
    по = {д["id"]: д for д in свод["items"]}
    # 10 плохих записей из 30 — все отрицательные; «Сроки» только в них:
    # доля вообще 1/3, среди отрицательных 1 → подъём 3.
    assert свод["scored"] == 30 and свод["negative"] == 10
    assert по["deadline"]["lift"] == 3.0 and по["deadline"]["negative_share"] == 100.0
    assert по["contract"]["lift"] == 0.0 and по["contract"]["records"] == 20
    assert "rare" not in по, по
    assert [д["id"] for д in свод["down"]] == ["deadline"]
    assert [д["id"] for д in свод["up"]] == ["contract"]
    # Вывод называет категорию и оба числа.
    выводы = Insights(db, индекс).findings("all")
    строка = next(в for в in выводы if в.get("metric") == "lift")
    assert "Сроки" in строка["text"] and "в 3.0 раза" in строка["text"], строка


def test_the_category_breakdown_carries_labels_kinds_and_overlaps(tmp_path):
    """Разрез по категориям: подписи из набора, вид, и запись входит во все
    свои категории, поэтому суммы по разрезу больше числа записей."""
    from asrhub.content_index import ContentIndex
    from asrhub.insights import РАЗРЕЗЫ, Insights

    assert "category" in РАЗРЕЗЫ
    db = _корпус(tmp_path, записей=30)
    настройки = _Настройки(content_categories=[
        {"id": "deadline", "label": "Сроки", "rule": "срок ИЛИ сорван"},
        {"id": "court", "label": "Суд", "rule": "суд", "kind": "violation"},
        {"id": "contract", "label": "Договор", "rule": "договор"},
    ])
    индекс = ContentIndex(db, настройки)
    while индекс.backfill_once(limit=20):
        pass
    разрез = Insights(db, индекс).breakdown("category", "all")
    по = {г["key"]: г for г in разрез["items"]}
    assert по["deadline"]["label"] == "Сроки" and по["deadline"]["records"] == 10
    assert по["court"]["kind"] == "violation" and по["court"]["records"] == 10
    assert по["contract"]["records"] == 20
    assert sum(г["records"] for г in разрез["items"]) == 40 > 30
    assert по["deadline"]["negative_share"] == 100.0 and по["contract"]["negative_share"] == 0.0
    # Разрез участвует в выводах о группах против среднего.
    выводы = Insights(db, индекс).findings("all")
    assert any(в.get("dimension") == "category" and "категории «Сроки»" in в["text"]
               for в in выводы), [в["text"] for в in выводы]


def test_a_tracker_writes_an_event_and_calls_out_only_on_fresh_records(tmp_path):
    """Категория с флагом «сообщать»: событие в журнале и POST наружу сразу
    после распознавания; при пересчёте архива — тишина."""
    import http.server
    import json
    import threading

    from asrhub.content_index import ContentIndex
    from asrhub.db import Database
    from asrhub.insights import Insights

    принято: list[dict] = []
    готово = threading.Event()

    class Приёмник(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                  # noqa: N802
            тело = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            принято.append(json.loads(тело))
            self.send_response(200)
            self.end_headers()
            готово.set()

        def log_message(self, *args):
            pass

    сервер = http.server.HTTPServer(("127.0.0.1", 0), Приёмник)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    адрес = f"http://127.0.0.1:{сервер.server_port}/hook"

    db = Database(tmp_path / "asrhub.db")
    job_id = db.create_job({"id": "т1", "filename": "звонок.wav", "owner": "анна",
                            "media_duration_s": 20.0})
    db.update_job(job_id, status="completed", finished_at=1.0,
                  text="Здравствуйте. Позовите руководителя, я буду жаловаться в суд.")
    сегменты = [{"start": 0.0, "end": 3.0, "speaker": "A", "text": "Здравствуйте."},
                {"start": 3.0, "end": 9.0, "speaker": "B",
                 "text": "Позовите руководителя, я буду жаловаться в суд."}]
    db.save_segments(job_id, сегменты)
    набор = [{"id": "escalation", "label": "Эскалация", "rule": "руководитель ИЛИ суд",
              "who": "customer", "notify": True},
             {"id": "hello", "label": "Приветствие", "rule": "здравствуйте", "notify": False}]
    try:
        # Внутренний адрес закрыт той же проверкой, что у обратного вызова:
        # без разрешения — событие есть, вызова нет.
        индекс = ContentIndex(db, _Настройки(content_categories=набор, tracker_url=адрес))
        индекс.on_job_completed(job_id, db.get_job(job_id), сегменты)
        время_ожидания = готово.wait(1.0)
        assert not время_ожидания and not принято, принято
        события = [с for с in db.get_events(job_id) if с["kind"] == "tracker"]
        assert len(события) == 1 and "Эскалация" in события[0]["message"], события
        assert события[0]["data"]["category"] == "escalation"
        assert события[0]["data"]["count"] == 2 and события[0]["data"]["hits"]

        # С разрешением на внутреннюю сеть вызов уходит — с записью и примерами.
        индекс = ContentIndex(db, _Настройки(content_categories=набор, tracker_url=адрес,
                                             webhook_allow_internal=True))
        индекс.on_job_completed(job_id, db.get_job(job_id), сегменты)
        assert готово.wait(5.0), "вызов наружу не пришёл"
        assert принято[0]["event"] == "tracker" and принято[0]["category"] == "escalation"
        assert принято[0]["job_id"] == job_id and принято[0]["filename"] == "звонок.wav"
        assert принято[0]["hits"][0]["text"].startswith("Позовите")

        # Пересчёт — не срабатывание: событий больше не становится.
        принято.clear()
        готово.clear()
        индекс.analyze_job(job_id)
        индекс.recompute([job_id])
        assert not готово.wait(0.5) and not принято
        assert len([с for с in db.get_events(job_id) if с["kind"] == "tracker"]) == 2

        # Свод видит срабатывания по журналу — и подпись, и число.
        трекеры = Insights(db, индекс).categories("all")["trackers"]
        assert трекеры == [{"category": "escalation", "label": "Эскалация",
                            "hits": 2, "records": 1}], трекеры
    finally:
        сервер.shutdown()


def test_part_two_reaches_the_endpoints_the_digest_and_the_export(
        tmp_path, monkeypatch, data_dir):
    """Ручка драйверов, отбор «возражение без отработки», строки сводки и
    листы выгрузки."""
    import io
    import zipfile

    from asrhub import content
    from asrhub.content_export import to_csv_zip
    from asrhub.content_index import ContentIndex
    from asrhub.insights import Insights
    from asrhub.maintenance import _digest_content, digest_text
    from fastapi.testclient import TestClient

    app = _клиент_с_ключами(data_dir, monkeypatch)
    with TestClient(app) as c:
        db = app.state.hub.db
        ключ = {"X-API-Key": "ah_admin_k"}
        job_id = db.create_job({"id": "воз", "filename": "x.wav", "owner": "админ",
                                "media_duration_s": 20.0})
        db.update_job(job_id, status="completed", finished_at=1.0,
                      text="Здравствуйте. Это дорого. Всего доброго.")
        db.save_segments(job_id, [
            {"start": 0.0, "end": 3.0, "speaker": "A", "text": "Здравствуйте."},
            {"start": 3.0, "end": 6.0, "speaker": "B", "text": "Это дорого."},
            {"start": 6.0, "end": 9.0, "speaker": "A", "text": "Всего доброго."}])
        c.post(f"/api/content/jobs/{job_id}/recompute", headers=ключ)
        # Готовый набор содержит пару «возражение — отработка»: «дорого» без
        # ответа — возражение без отработки.
        список = c.get("/api/jobs?content=objection_unhandled", headers=ключ).json()
        assert [j["id"] for j in список["items"]] == [job_id]
        assert c.get("/api/jobs?content=objection", headers=ключ).json()["items"]
        ответ = c.get("/api/content/drivers?period=all", headers=ключ)
        assert ответ.status_code == 200 and "note" in ответ.json()
        записи = c.get("/api/content/records?kind=objections&period=all", headers=ключ).json()
        assert [з["job_id"] for з in записи["items"]] == [job_id]
        карточка = c.get(f"/api/content/jobs/{job_id}", headers=ключ).json()
        assert карточка["analysis"]["categories"]["objections"]["unhandled"] == 1

    db = _корпус(tmp_path, записей=30)
    индекс = ContentIndex(db, _Настройки())
    while индекс.backfill_once(limit=20):
        pass
    for n in range(3):
        db.add_event(f"job{n:04d}", "tracker", "Сработал трекер «Эскалация»",
                     {"category": "escalation", "label": "Эскалация"})
    db.save_content("job0001", {"version": content.VERSION, "objections": 4,
                                "objections_unhandled": 3})
    свод = Insights(db, индекс)
    содержание = _digest_content(свод, _Настройки(), "all")
    текст = digest_text({}, {}, {}, содержание=содержание)
    assert "Возражений без отработки: 3 из 4 (75 %)" in текст, текст
    assert "Трекеры: Эскалация — 3 в 3 записях" in текст, текст
    отчёт = свод.report("all")
    assert отчёт["drivers"]["down"] and отчёт["breakdowns"]["category"]["items"]
    архив = zipfile.ZipFile(io.BytesIO(to_csv_zip(отчёт, "all")))
    имена = архив.namelist()
    assert any("Драйверы" in и for и in имена) and any("По категориям" in и for и in имена)
    assert "Подъём" in архив.read(next(и for и in имена if "Драйверы" in и)).decode("utf-8-sig")
