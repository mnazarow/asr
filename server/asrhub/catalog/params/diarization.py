"""Параметры группы «Разделение по говорящим»."""
from __future__ import annotations

from ._base import Ex, P, _p

# ===========================================================================
# ГРУППА: Разделение по говорящим
# ===========================================================================

_p(P(
    key="alignment_backend",
    label="Принудительное выравнивание",
    group="postprocess",
    type="enum",
    default="none",
    options=[
        {"value": "none", "label": "Не выравнивать (таймкоды модели)"},
        {"value": "mfa", "label": "Montreal Forced Aligner (точнее всего)"},
        {"value": "whisperx", "label": "WhisperX (wav2vec2, легче в установке)"},
    ],
    description=(
        "Уточнение границ слов по звуку. Таймкоды, которые выдаёт распознаватель, — "
        "это оценка декодера: он сообщает, на каком кадре, по его мнению, было слово. "
        "Выравнивание решает другую задачу: текст уже известен, и нужно найти, где "
        "именно в сигнале звучит каждое слово."
    ),
    recommendation=(
        "Включайте, когда границы важны: субтитры, дубляж, разбор спорных записей, "
        "нарезка аудио по словам. Если расшифровку читают люди, выигрыш не окупает "
        "ни времени, ни установки. MFA точнее и не требует видеокарты, но ставится "
        "через conda и занимает 2–3 ГБ; WhisperX легче поставить, работает на GPU, "
        "качество границ чуть ниже. При любой неудаче выравнивания задание доводится "
        "до конца на модельных таймкодах."
    ),
    examples=[
        Ex("Расшифровка для чтения", "none", "Границы слов не нужны"),
        Ex("Субтитры к видео", "mfa", "Точные границы заметны при наложении"),
        Ex("Дубляж и озвучание", "mfa", "Липсинк требует пословной точности"),
        Ex("Уже есть GPU и whisperx", "whisperx", "Быстрее и без conda"),
    ],
    impact={"quality": "up", "speed": "down", "memory": "up"},
    advanced=True,
))

_p(P(
    key="alignment_dictionary",
    label="Словарь произношения",
    group="postprocess",
    type="str",
    default="russian_mfa",
    description=(
        "Произносительный словарь MFA: он сопоставляет написание слова его звучанию. "
        "Для русского — russian_mfa. Задаётся только в настройках сервера: значение "
        "уходит аргументом в mfa, и от задания оно не принимается."
    ),
    recommendation=(
        "Словарь должен соответствовать языку записи, иначе выравнивание либо "
        "провалится, либо разложит слова наугад. Загружается один раз: "
        "mfa model download dictionary russian_mfa."
    ),
    examples=[
        Ex("Русский", "russian_mfa", ""),
        Ex("Английский", "english_us_mfa", ""),
        Ex("Свой словарь", "/srv/mfa/наш-словарь.dict",
           "Термины и имена, которых нет в стандартном"),
    ],
    requires={"alignment_backend": "mfa"},
    impact={"quality": "up", "speed": "neutral", "memory": "neutral"},
    advanced=True,
    server_only=True,
))

_p(P(
    key="alignment_acoustic_model",
    label="Акустическая модель выравнивания",
    group="postprocess",
    type="str",
    default="russian_mfa",
    description=("Акустическая модель MFA, по которой ищутся границы звуков. "
                 "Задаётся только в настройках сервера: значение уходит аргументом "
                 "в mfa, и от задания оно не принимается."),
    recommendation=(
        "Берите пару «словарь и модель» одного языка. Загружается один раз: "
        "mfa model download acoustic russian_mfa."
    ),
    examples=[Ex("Русский", "russian_mfa", ""), Ex("Английский", "english_mfa", "")],
    requires={"alignment_backend": "mfa"},
    impact={"quality": "up", "speed": "neutral", "memory": "neutral"},
    advanced=True,
    server_only=True,
))

_p(P(
    key="alignment_retry_beam",
    label="Ширина луча при повторе",
    group="postprocess",
    type="int",
    default=80,
    minimum=10,
    maximum=400,
    step=10,
    description=(
        "Насколько широко MFA ищет соответствие, если с первого прохода выровнять "
        "не удалось. Больше значение — больше шансов на трудной записи и больше времени."
    ),
    recommendation=(
        "80 хватает почти всегда. Поднимайте до 200, если в журнале часто "
        "встречается «beam too narrow»: это записи с шумом, эхом или сильным акцентом."
    ),
    examples=[
        Ex("Обычные записи", 80, ""),
        Ex("Шумные записи", 200, "Медленнее, но выравнивание проходит"),
    ],
    requires={"alignment_backend": "mfa"},
    impact={"quality": "up", "speed": "down", "memory": "neutral"},
    advanced=True,
))

_p(P(
    key="alignment_jobs",
    label="Потоков выравнивания",
    group="postprocess",
    type="int",
    default=4,
    minimum=1,
    maximum=64,
    description="Сколько процессов MFA запускает параллельно.",
    recommendation=(
        "По числу физических ядер, но не больше: MFA считает на процессоре, и "
        "перебор потоков только добавляет накладных расходов. На сервере, где "
        "одновременно идёт распознавание, оставьте половину ядер."
    ),
    examples=[Ex("Восьмиядерный сервер", 4, ""), Ex("Выделенная машина", 8, "")],
    requires={"alignment_backend": "mfa"},
    impact={"quality": "neutral", "speed": "up", "memory": "up"},
    advanced=True,
))

_p(P(
    key="alignment_timeout_s",
    label="Тайм-аут выравнивания",
    group="postprocess",
    type="int",
    default=900,
    minimum=30,
    maximum=7200,
    step=30,
    unit="с",
    description="Сколько ждать MFA, прежде чем считать выравнивание неудавшимся.",
    recommendation=(
        "Выравнивание идёт примерно за десятую часть длительности записи на "
        "четырёх ядрах. Для часовых записей 900 секунд — с запасом. По истечении "
        "тайм-аута задание не падает: остаются модельные таймкоды."
    ),
    examples=[Ex("Записи до часа", 900, ""), Ex("Многочасовые записи", 3600, "")],
    requires={"alignment_backend": "mfa"},
    impact={"quality": "neutral", "speed": "neutral", "memory": "neutral"},
    advanced=True,
))

_p(P(
    key="alignment_keep_text",
    label="Сохранять исходный текст",
    group="postprocess",
    type="bool",
    default=True,
    description=(
        "Брать из выравнивателя только границы, а слова оставлять из расшифровки. "
        "Выравниватель работает с очищенным текстом в нижнем регистре и без знаков "
        "препинания, поэтому его вывод нельзя просто подставить вместо расшифровки."
    ),
    recommendation=(
        "Оставьте включённым. Выключение имеет смысл только при отладке — чтобы "
        "увидеть, что именно выравниватель услышал."
    ),
    examples=[
        Ex("Обычная работа", True, "Пунктуация и заглавные сохраняются"),
        Ex("Отладка выравнивания", False, "Видно, как слова разобрал выравниватель"),
    ],
    requires={"alignment_backend": ["mfa", "whisperx"]},
    impact={"quality": "up", "speed": "neutral", "memory": "neutral"},
    advanced=True,
))

_p(P(
    key="alignment_max_gap_s",
    label="Склейка после выравнивания",
    group="postprocess",
    type="float",
    default=0.0,
    minimum=0.0,
    maximum=5.0,
    step=0.1,
    unit="с",
    description=(
        "Склеивать соседние сегменты одного говорящего, если пауза между ними "
        "короче указанной. Ноль отключает склейку."
    ),
    recommendation=(
        "После выравнивания границы становятся точными, и запись нередко распадается "
        "на множество коротких кусков — по фразе на вдох. 0,5 секунды возвращает "
        "текст к читаемому виду. Для субтитров склейку лучше не включать: там "
        "дробление как раз полезно."
    ),
    examples=[
        Ex("Субтитры", 0.0, "Дробные сегменты удобны для показа"),
        Ex("Протокол для чтения", 0.5, "Фразы не рассыпаются построчно"),
    ],
    requires={"alignment_backend": ["mfa", "whisperx"]},
    impact={"quality": "neutral", "speed": "neutral", "memory": "neutral"},
    advanced=True,
))

_p(P(
    key="align_model",
    label="Модель выравнивания WhisperX",
    group="postprocess",
    type="str",
    default="",
    description=(
        "Модель wav2vec2 с Hugging Face, которой WhisperX выравнивает слова по звуку. "
        "Пусто — по языку записи: для русского jonatasgrosman/wav2vec2-large-xlsr-53-russian, "
        "для других — умолчание WhisperX. Встроенное выравнивание (alignment_backend) "
        "этим ключом не управляется. Задаётся только в настройках сервера: имя уходит "
        "прямо в загрузчик модели, и от задания оно не принимается."
    ),
    recommendation="Оставьте пустым; меняйте, если для языка записи есть модель точнее.",
    examples=[Ex("Русский", "jonatasgrosman/wav2vec2-large-xlsr-53-russian", "")],
    engines=["whisperx"],
    advanced=True,
    impact={"quality": "neutral", "speed": "neutral", "memory": "neutral"},
    server_only=True,
))

_p(P(
    key="diarization_enabled",
    label="Разделять по говорящим",
    group="diarization",
    type="bool",
    default=False,
    description=(
        "Определяет, кто и когда говорит, и размечает реплики метками «Говорящий 1», "
        "«Говорящий 2». Выполняется отдельной моделью поверх результата распознавания "
        "либо встроенными средствами модели (MOSS Transcribe Diarize)."
    ),
    recommendation=(
        "Включайте для совещаний, интервью, подкастов и записей колл-центра. "
        "Диаризация добавляет к времени обработки от 20 до 60 %. Если запись стерео "
        "и собеседники разведены по каналам, вместо диаризации используйте разделение "
        "каналов — это точнее и почти бесплатно."
    ),
    examples=[
        Ex("Совещание на 5 человек", True, ""),
        Ex("Стереозапись звонка", False, "Лучше audio_channels=split"),
        Ex("Диктовка одного человека", False, ""),
    ],
    impact={"quality": "up", "speed": "down", "memory": "up"},
    see_also=["audio_channels", "diarization_backend"],
))

_p(P(
    key="diarization_backend",
    label="Метод диаризации",
    group="diarization",
    type="enum",
    default="auto",
    description=(
        "pyannote — эталон качества, любое число говорящих, требует токена Hugging Face "
        "и принятия лицензии. sortformer — потоковая диаризация NVIDIA с задержкой "
        "от 0.32 секунды, но не более четырёх говорящих и ориентация на английский. "
        "builtin — диаризация внутри самой модели (MOSS), без отдельного этапа. "
        "channels — разделение по аудиоканалам: каждый канал распознаётся отдельно и "
        "становится своим говорящим (то же, что «Сведение каналов» = split), безошибочно "
        "для стереозаписей. На одноканальной записи говорящие расставляются по паузам "
        "с предупреждением."
    ),
    recommendation=(
        "auto берёт говорящих от самой модели, если она их расставляет, иначе pyannote, "
        "а без него — sortformer. Для записей с числом говорящих больше четырёх "
        "используйте только pyannote. В потоковом распознавании диаризации нет: "
        "она работает на файлах."
    ),
    examples=[
        Ex("Совещание, запись файлом", "pyannote", "Лучшее качество"),
        Ex("Модель MOSS", "builtin", "Диаризация уже внутри, отдельный этап не нужен"),
        Ex("Английская речь, до четырёх говорящих", "sortformer", "Работает на NeMo, без pyannote"),
        Ex("Стереозапись звонка", "channels", "Стопроцентная атрибуция"),
    ],
    options=[{"value": "auto", "label": "Автоматически"},
             {"value": "pyannote", "label": "pyannote (эталон)"},
             {"value": "sortformer", "label": "NVIDIA Sortformer (потоковая)"},
             {"value": "builtin", "label": "Встроенная в модель"},
             {"value": "channels", "label": "По аудиоканалам"}],
    requires={"diarization_enabled": True},
    impact={"quality": "up", "speed": "down", "memory": "up"},
))

_p(P(
    key="diarization_num_speakers",
    label="Точное число говорящих",
    group="diarization",
    type="int",
    default=0,
    minimum=0,
    maximum=32,
    step=1,
    description=(
        "Если число участников известно заранее, укажите его — алгоритм не будет "
        "угадывать и не разобьёт одного человека на двоих. Ноль — определять автоматически."
    ),
    recommendation=(
        "Указывайте всегда, когда знаете. Это самая действенная настройка диаризации: "
        "ошибка в определении числа говорящих — основной источник неверной атрибуции реплик."
    ),
    examples=[
        Ex("Разговор двоих", 2, "Телефонный звонок, интервью"),
        Ex("Совещание, состав неизвестен", 0, "Плюс задать границы диапазона"),
        Ex("Планёрка из шести человек", 6, ""),
    ],
    requires={"diarization_enabled": True},
    impact={"quality": "up", "speed": "neutral", "memory": "neutral"},
))

_p(P(
    key="diarization_min_speakers",
    label="Минимум говорящих",
    group="diarization",
    type="int",
    default=1,
    minimum=1,
    maximum=32,
    step=1,
    description="Нижняя граница числа участников при автоопределении.",
    recommendation="Задавайте вместе с максимумом, чтобы сузить пространство поиска.",
    examples=[Ex("Совещание", 2, ""), Ex("Возможен монолог", 1, "")],
    requires={"diarization_enabled": True},
    advanced=True,
    impact={"quality": "up", "speed": "neutral", "memory": "neutral"},
))

_p(P(
    key="diarization_max_speakers",
    label="Максимум говорящих",
    group="diarization",
    type="int",
    default=8,
    minimum=1,
    maximum=32,
    step=1,
    description="Верхняя граница числа участников при автоопределении.",
    recommendation=(
        "Ставьте реалистичную оценку. Завышенный предел — типичная причина того, "
        "что один человек разбивается на нескольких «говорящих» из-за смены интонации "
        "или положения относительно микрофона."
    ),
    examples=[Ex("Планёрка", 8, ""), Ex("Интервью", 3, "Ведущий, гость и запас")],
    requires={"diarization_enabled": True},
    advanced=True,
    impact={"quality": "up", "speed": "neutral", "memory": "neutral"},
))

_p(P(
    key="diarization_assign_words",
    label="Привязывать говорящего к каждому слову",
    group="diarization",
    type="bool",
    default=True,
    description=(
        "Определять говорящего не только для реплики целиком, но и для каждого слова — и "
        "резать реплику там, где говорящий сменился: перебивание, короткий ответ без "
        "паузы. Пробег короче двух слов и 0,6 секунды считается шумом на стыке реплик и "
        "приклеивается к соседу. Выключено — у реплики один говорящий, тот, кто занял "
        "большую её часть, а у слов говорящего нет."
    ),
    recommendation=(
        "Включено. Требует пословных таймкодов; у реплик без них ничего не меняется. "
        "Выключайте, если реплики нужны ровно такими, как их нарезал движок."
    ),
    examples=[Ex("Диалог с перебиваниями", True, ""), Ex("Поочерёдные монологи", False, "")],
    requires={"diarization_enabled": True},
    advanced=True,
    impact={"quality": "up", "speed": "down", "memory": "neutral"},
))

_p(P(
    key="diarization_model",
    label="Модель разделения по говорящим",
    group="diarization",
    type="str",
    default="",
    description=(
        "Модель с Hugging Face для разделения по голосам. Пусто — умолчание механизма: "
        "pyannote/speaker-diarization-community-1 для pyannote и "
        "nvidia/diar_streaming_sortformer_4spk-v2 для Sortformer. Сервер читал этот "
        "ключ и раньше, но задать его было негде: config.yaml отбрасывал его как "
        "неизвестный.\n\n"
        "Задаётся только в настройках сервера. Имя уходит прямо в "
        "from_pretrained вместе с токеном Hugging Face сервера, а контрольные точки "
        "pyannote и NeMo — это pickle: обычный ключ, назвавший чужой репозиторий, "
        "заставлял сервер скачать и развернуть его. Параметр задания с этим именем "
        "не действует."
    ),
    recommendation=(
        "Оставьте пустым. Другая модель нужна, когда есть своя дообученная версия или "
        "старая pyannote/speaker-diarization-3.1 работает на ваших записях лучше."
    ),
    examples=[Ex("Умолчание", "", ""),
              Ex("Прежняя pyannote", "pyannote/speaker-diarization-3.1", "")],
    requires={"diarization_enabled": True},
    advanced=True,
    impact={"quality": "neutral", "speed": "neutral", "memory": "neutral"},
    server_only=True,
))

_p(P(
    key="diarization_pause_s",
    label="Пауза смены говорящего (запасной способ)",
    group="diarization",
    type="float",
    default=1.2,
    minimum=0.3,
    maximum=10.0,
    step=0.1,
    unit="с",
    description=(
        "Запасное разделение — по паузам, когда ни pyannote, ни Sortformer недоступны: "
        "пауза не короче этой считается сменой говорящего. Грубая оценка, а не "
        "разделение по голосам, — и результат задания об этом предупреждает."
    ),
    recommendation="1,2 с для живого диалога; 2–3 с для интервью с длинными ответами.",
    examples=[Ex("Диалог", 1.2, ""), Ex("Интервью", 2.5, "")],
    requires={"diarization_enabled": True},
    advanced=True,
    impact={"quality": "neutral", "speed": "neutral", "memory": "neutral"},
))

_p(P(
    key="speaker_names",
    label="Имена говорящих",
    group="diarization",
    type="text",
    default="",
    description=(
        "Список имён через запятую, подставляемых вместо меток «Говорящий 1», "
        "«Говорящий 2» в порядке первого появления в записи. При разделении "
        "стереозаписи по каналам имена идут по номеру канала: первое — первому "
        "каналу, второе — второму, кто бы ни заговорил первым."
    ),
    recommendation=(
        "Заполняйте после первого прогона, когда видно, кто под каким номером. "
        "Порядок определяется моментом первой реплики, а не громкостью или длительностью. "
        "Для звонков, разведённых по каналам, порядок задаёт станция: обычно в первом "
        "канале звонящий, во втором — принявший."
    ),
    examples=[
        Ex("Интервью", "Ведущий, Гость", ""),
        Ex("Совещание", "Иванов, Петрова, Сидорчук", ""),
        Ex("Колл-центр", "Оператор, Клиент", ""),
    ],
    requires={"diarization_enabled": True},
    impact={"quality": "neutral", "speed": "neutral", "memory": "neutral"},
))
