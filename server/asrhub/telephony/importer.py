"""Фоновый импорт звонков: из журналов АТС — в очередь распознавания.

Поток на станцию, заход раз в её `poll_s`: спросить источник о новых
звонках, найти файл записи, поставить задание, запомнить звонок. Всё
остальное — уже обычная жизнь задания: очередь, распознавание, разбор
содержания, смысловой слой.

Три вещи, ради которых этот модуль вообще существует отдельно от клиента
АТС:

* **Не завести одно задание дважды.** Идентификатор звонка уникален, и
  таблица `calls` держит его первичным ключом: повторный заход по тому же
  журналу ничего не добавит, даже если позиция чтения потерялась.
* **Не ставить задание раньше времени.** MixMonitor закрывает файл после
  разговора, и запись, взятая через секунду после события `Cdr`, бывает
  обрезанной. Поэтому свежие звонки выдерживаются `telephony_settle_s`
  секунд, а файл проверяется на то, что он перестал расти.
* **Не тащить всё подряд.** Звонок в шесть секунд — это «ошиблись
  номером», и распознавать его незачем: `min_duration_s` отсекает такие,
  а `skip_unanswered` — неотвеченные.

Станций может быть сколько угодно, и у каждой своё всё: источник, учётные
данные, каталог записей, длины внутренних номеров, правила контекстов,
владелец, приоритет. `Импортёр` обслуживает ОДНУ станцию и ничего не знает
про остальные; `Телефония` держит их набор и приводит его в соответствие с
настройкой — заводит потоки для новых станций, гасит для убранных и
перезапускает для изменившихся.
"""
from __future__ import annotations

import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

from ..errors import ASRHubError, ConfigError
from ..logging_setup import get_logger
from .asterisk import ПОЛЯ_AMI as _ПОЛЯ_AMI
from .asterisk import (
    AMIClient,
    AMIError,
    Звонок,
    звонок_из_полей,
    найти_запись,
    направление,
    проверить,
    разобрать_cdr_строку,
    читать_csv,
)
from .stations import Станция
from .stations import список as станции_из_настроек

log = get_logger("telephony")

#: Сколько звонков брать за один заход. Первый заход на живой АТС видит
#: весь журнал; без предела он поставил бы в очередь десятки тысяч заданий
#: разом и занял бы сервер на сутки.
ПОРЦИЯ = 50

#: Сколько держать звонок в очереди отложенных. Запись, не появившаяся за
#: сутки, не появится уже никогда: либо её не делали, либо она легла не туда.
#: Держать такие вечно — значит однажды забить очередь ими целиком и
#: остановить импорт по станции, не сказав об этом ни слова.
СРОК_ОТЛОЖЕННЫХ = 24 * 3600

#: Пауза после сбоя источника: АТС перезапускают, сеть моргает.
ПАУЗА_СБОЯ = 60.0

#: Сколько ждать между двумя замерами размера файла записи.
ПАУЗА_ЗАМЕРА = 0.4


def _длительность_файла(путь: Path) -> int:
    """Секунды звука в файле — из заголовка, без чтения самого звука.

    Нужна источнику «каталог записей»: у такого звонка длительности нет
    ниоткуда, а ноль выключал бы разом и порог «не брать короче», и
    выдержку — обе проверки написаны как «если длительность известна».

    WAV читается заголовком, остальное — через ffprobe, если он есть.
    Не прочиталось — ноль, и это честнее выдуманного числа.
    """
    if путь.suffix.lower() == ".wav":
        try:
            with wave.open(str(путь), "rb") as файл:
                частота = файл.getframerate() or 1
                return int(файл.getnframes() / частота)
        except (OSError, wave.Error) as exc:
            log.debug("Длительность %s не прочиталась: %s", путь, exc)
            return 0
    try:
        вывод = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(путь)],
            capture_output=True, text=True, timeout=10, check=False)
        return int(float(вывод.stdout.strip() or 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _число(настройки: Any, ключ: str, по_умолчанию: float) -> float:
    """Значение настройки с оглядкой на то, что ноль — это значение.

    `float(настройки.get(ключ) or 30)` выглядит безобидно и врёт ровно на
    нуле: в Python ноль ложен, и «выдержка ноль секунд» превращалась в
    тридцать. Каталог объявляет минимум 0 у половины этих параметров и
    объясняет, что он означает, — значит, ноль надо уметь принимать.
    """
    значение = настройки.get(ключ)
    if значение is None or значение == "":
        return float(по_умолчанию)
    try:
        return float(значение)
    except (TypeError, ValueError):
        return float(по_умолчанию)


class Импортёр:
    """Фоновый перенос звонков с ОДНОЙ станции в очередь распознавания."""

    def __init__(self, db: Any, станция: Станция, queue: Any, settings: Any = None):
        self.db = db
        self.станция = станция
        self.settings = settings
        self.queue = queue
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: Замок на один заход: фоновый поток и кнопка «Забрать сейчас» не
        #: должны читать журнал с одной позиции одновременно.
        self._заход = threading.Lock()
        self.last_error: str | None = None
        self.last_run: float | None = None
        self.imported = 0
        self.skipped = 0
        self.failed = 0
        self._ami: AMIClient | None = None
        #: Чем было открыто соединение: адрес, порт, учётная запись, пароль.
        self._подпись_ami: tuple[str, int, str, str] | None = None
        self._ami_события: list[dict[str, str]] = []
        #: Свод последнего захода — для раздела «АТС».
        self.last_scan: dict[str, Any] = {}
        #: Позиция чтения журнала — СВОЯ у каждой станции. Общий ключ
        #: означал бы, что вторая станция продолжает читать с того места,
        #: до которого дочитала первая: половина архива не приехала бы
        #: никогда, и понять почему было бы нельзя.
        self._ключ_позиции = f"telephony_cdr_offset:{станция.id}"

    # --- настройки --------------------------------------------------------

    @property
    def id(self) -> str:                                     # noqa: A003
        return self.станция.id

    @property
    def enabled(self) -> bool:
        return bool(self.станция.enabled)

    @property
    def source(self) -> str:
        return self.станция.source

    def путь(self, поле: str) -> Path | None:
        """Путь станции по имени поля. Поля называются без приставки."""
        имя = поле.replace("telephony_", "")
        return self.станция.путь(имя)

    # --- состояние --------------------------------------------------------

    def status(self, *, for_admin: bool = False,
               owner: Any = None) -> dict[str, Any]:
        with self._lock:
            свод = {
                **self.станция.to_dict(for_admin=for_admin),
                "running": bool(self._thread and self._thread.is_alive()),
                "imported": self.imported,
                "skipped": self.skipped,
                "failed": self.failed,
                "last_run": self.last_run,
                "last_scan": dict(self.last_scan),
            }
            if for_admin:
                свод["last_error"] = self.last_error
        try:
            свод["calls"] = self.db.call_counts(owner=owner, station=self.id)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Счётчики звонков не прочитаны: %s", exc)
            свод["calls"] = {}
        return свод

    def проверить_связь(self) -> dict[str, Any]:
        """Достучаться до источника этой станции и сказать, что именно не так.

        «Не работает» — не диагноз. Проверка отвечает по-разному на «порт
        закрыт», «пароль не тот», «файла нет» и «файл есть, но в нём ноль
        строк»: каждое из этих состояний чинится по-своему.
        """
        начало = time.perf_counter()
        станция = self.станция
        если_мс = lambda: round((time.perf_counter() - начало) * 1000, 1)  # noqa: E731
        if станция.source == "ami":
            итог = проверить(станция.host or "127.0.0.1", станция.port or 5038,
                             станция.username, станция.secret)
            итог.update({"source": "ami", "station": станция.id})
            return итог
        if станция.source == "folder":
            каталог = станция.путь("recordings_dir")
            if каталог is None or not каталог.is_dir():
                raise ConfigError(
                    f"«{станция.name}»: каталог записей не найден.",
                    hint="Проверьте путь и права на чтение у пользователя, "
                         "от которого работает сервер.")
            файлов = sum(1 for п in каталог.rglob("*")
                         if п.suffix.lower() in (".wav", ".mp3", ".ogg"))
            return {"ok": True, "source": "folder", "station": станция.id,
                    "dir": str(каталог), "files": файлов, "ms": если_мс()}
        журнал = станция.путь("cdr_file")
        if журнал is None or not журнал.is_file():
            raise ConfigError(
                f"«{станция.name}»: журнал звонков не найден.",
                hint="Обычно это /var/log/asterisk/cdr-csv/Master.csv; "
                     "нужен доступ на чтение.")
        размер = журнал.stat().st_size
        # Читаем хвост, а не начало: журнал бывает в гигабайт, а вопрос
        # проверки — «разбирается ли то, что станция пишет сейчас».
        with журнал.open("rb") as файл:
            файл.seek(max(0, размер - 65536))
            строки = [с for с in файл.read().decode("utf-8", "replace").splitlines()
                      if с.strip()]
        образцы = [z for z in (разобрать_cdr_строку(с) for с in строки[-5:]) if z]
        return {
            "ok": True, "source": "cdr_csv", "station": станция.id,
            "file": str(журнал), "bytes": размер,
            "offset": int(self.db.get_kv(self._ключ_позиции, 0) or 0),
            "lines": len(строки),
            "sample": [{"uniqueid": z.uniqueid, "src": z.src, "dst": z.dst,
                        "disposition": z.disposition, "billsec": z.billsec,
                        "direction": направление(
                            z, внутренние_знаков=станция.internal_digits,
                            контексты=станция.contexts),
                        "started_at": z.started_at} for z in образцы],
            "ms": если_мс(),
        }

    # --- поток ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.clear()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name=f"asrhub-pbx-{self.станция.id}"[:15],
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        поток = self._thread
        if поток and поток.is_alive():
            поток.join(timeout=timeout)
            if поток.is_alive():
                log.warning("Поток телефонии ещё занят; завершится сам.")
                return
        self._thread = None
        self._закрыть_ami()

    def _loop(self) -> None:
        пауза = 2.0
        while not self._stop.wait(timeout=пауза):
            if not self.enabled:
                пауза = 30.0
                continue
            try:
                итог = self.scan()
                пауза = max(5.0, float(self.станция.poll_s or 60))
                if итог.get("imported"):
                    # Есть что везти — заходим чаще: живой поток звонков
                    # не должен ждать полного интервала.
                    пауза = 5.0
            except Exception as exc:                         # noqa: BLE001
                with self._lock:
                    self.last_error = str(exc)
                    self.failed += 1
                log.warning("Заход «%s» не удался: %s", self.станция.name, exc)
                пауза = ПАУЗА_СБОЯ

    # --- один заход -------------------------------------------------------

    def scan(self, *, limit: int = ПОРЦИЯ) -> dict[str, Any]:
        """Один заход: взять новые звонки и поставить задания.

        Заходы не идут вдвоём: кнопка «Забрать сейчас» и фоновый поток
        иначе прочитали бы журнал с одной и той же позиции и разобрали бы
        одни и те же звонки дважды. Дублей заданий это не даёт — от них
        держит первичный ключ таблицы, — но заход впустую и путаница в
        счётчиках получаются на ровном месте.
        """
        with self._заход:
            return self._scan(limit=limit)

    def _scan(self, *, limit: int = ПОРЦИЯ) -> dict[str, Any]:
        # Сначала отложенные: звонки, которые в прошлый заход ещё писались
        # или не влезли в очередь. Из журнала они больше не придут — позиция
        # чтения сдвинута, событие AMI разовое, — поэтому очередь отложенных
        # разбирается раньше новых и своей долей порции.
        # Только свои и только те, у которых ещё есть шанс: запись, не
        # появившаяся за срок хранения отложенных, не появится уже никогда,
        # а очередь она занимает вечно — после одного сбоя записи импорт по
        # станции вставал целиком.
        порог = time.time() - СРОК_ОТЛОЖЕННЫХ
        отложенные = [Звонок.из_записи(з) for з in self.db.calls_deferred(
            limit=max(1, limit // 2), station=self.id, older_than=порог)]
        источник = self.source
        if источник != "ami" and self._ami is not None:
            # Источник переключили — соединение больше не нужно. Без этой
            # строки сокет и сессия на станции держались до перезапуска.
            self._закрыть_ami()
        if источник == "ami":
            новые = self._из_ami(limit)
        elif источник == "folder":
            новые = self._из_папки(limit)
        else:
            новые = self._из_csv(limit)
        звонки = [*отложенные, *новые]
        было_отложено = {з.uniqueid for з in отложенные}

        поставлено, пропущено = 0, 0
        причины: dict[str, int] = {}
        for звонок in звонки:
            # Один плохой звонок не должен уносить всю порцию. Позиция
            # чтения журнала уже сдвинута, и необработанный остаток не
            # придёт больше никогда: исключение здесь означало бы тихую
            # потерю сорока девяти разговоров из-за одного.
            try:
                почему = self._взять(
                    звонок, отложенный=звонок.uniqueid in было_отложено)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Звонок %s не разобран: %s", звонок.uniqueid, exc)
                with self._lock:
                    self.failed += 1
                    self.last_error = f"звонок {звонок.uniqueid}: {exc}"
                почему = "сбой разбора"
            if почему is None:
                поставлено += 1
            else:
                пропущено += 1
                причины[почему] = причины.get(почему, 0) + 1
        with self._lock:
            self.imported += поставлено
            self.skipped += пропущено
            self.last_run = time.time()
            if поставлено or пропущено:
                self.last_error = None
        if поставлено:
            log.info("Телефония: поставлено заданий %d, пропущено %d %s",
                     поставлено, пропущено, причины or "")
        свод = {"station": self.станция.id, "name": self.станция.name,
                "seen": len(звонки), "imported": поставлено,
                "skipped": пропущено, "reasons": причины,
                "deferred": len(отложенные), "at": time.time()}
        with self._lock:
            self.last_scan = свод
        return свод

    # --- источники --------------------------------------------------------

    def _из_csv(self, limit: int) -> list[Звонок]:
        путь = self.станция.путь("cdr_file")
        if путь is None:
            raise ConfigError(
                f"«{self.станция.name}»: не задан журнал звонков.",
                hint="Обычно это /var/log/asterisk/cdr-csv/Master.csv")
        позиция = int(self.db.get_kv(self._ключ_позиции, 0) or 0)
        звонки, новая = читать_csv(путь, offset=позиция, limit=limit)
        self.db.set_kv(self._ключ_позиции, новая)
        return звонки

    def _из_ami(self, limit: int) -> list[Звонок]:
        """События `Cdr` из интерфейса управления.

        Соединение держится между заходами: вход на каждый заход — это
        строка в журнале безопасности АТС раз в минуту и повод для
        подозрений у того, кто этот журнал читает.
        """
        # Подпись соединения: адрес, порт, учётная запись и пароль. Раньше
        # соединение просто держалось, и смена любого из них не доходила
        # до станции до перезапуска сервера: правка пароля в настройках
        # ничего не меняла, а переключение источника на журнал CDR
        # оставляло сокет и сессию на станции висеть навсегда — а сессий у
        # AMI ограниченное число.
        подпись = (self.станция.host or "127.0.0.1", int(self.станция.port or 5038),
                   self.станция.username, self.станция.secret)
        if self._ami is not None and self._подпись_ami != подпись:
            log.info("Настройки AMI изменились — соединение открывается заново")
            self._закрыть_ami()
        клиент = self._ami
        if клиент is None:
            клиент = AMIClient(*подпись)
            клиент.connect()
            self._ami = клиент
            self._подпись_ami = подпись
        звонки: list[Звонок] = []
        крайний = time.time() + 5.0
        while len(звонки) < limit and time.time() < крайний:
            try:
                событие = клиент.пакет(timeout=1.0)
            except AMIError:
                # Соединение не просто забывается, а закрывается: брошенный
                # сокет держит и дескриптор здесь, и сессию на станции —
                # а сессий у AMI ограниченное число.
                self._закрыть_ami()
                raise
            if not событие:
                break
            if str(событие.get("Event") or "").lower() != "cdr":
                continue
            поля = {имя: событие.get(ключ, "") for ключ, имя in _ПОЛЯ_AMI.items()}
            звонок = звонок_из_полей(поля)
            if звонок.uniqueid:
                звонки.append(звонок)
        return звонки

    def _закрыть_ami(self) -> None:
        """Закрывает соединение со станцией и забывает его подпись."""
        клиент, self._ami = self._ami, None
        self._подпись_ami = None
        if клиент is not None:
            клиент.close()

    def _из_папки(self, limit: int) -> list[Звонок]:
        """Когда до АТС не дотянуться: звонок — это файл записи.

        Полей у такого звонка ровно столько, сколько удалось прочитать в
        имени файла, и раздел это показывает как есть. Идентификатором
        служит имя файла: оно уникально в пределах папки, а большего для
        защиты от повторного импорта не нужно.
        """
        каталог = self.станция.путь("recordings_dir")
        if каталог is None or not каталог.is_dir():
            raise ConfigError(
                f"«{self.станция.name}»: каталог записей не найден.",
                hint="Проверьте путь и права на чтение у пользователя, "
                     "от которого работает сервер.")
        известные = self.db.known_call_ids(station=self.id)
        свежее = time.time() - float(self.станция.lookback_days or 7) * 86400
        звонки: list[Звонок] = []
        for путь in sorted(каталог.rglob("*")):
            if len(звонки) >= limit:
                break
            try:
                if not путь.is_file() or путь.suffix.lower() not in (".wav", ".mp3", ".ogg"):
                    continue
                изменён = путь.stat().st_mtime
            except OSError:
                continue
            if изменён < свежее:
                continue
            # Ключ — по пути от каталога, а не по имени: Asterisk
            # раскладывает записи по годам и месяцам, и «out-101-…wav» из
            # марта и из апреля — это два разных разговора с одним именем.
            # По имени второй молча терялся как «уже импортирован».
            try:
                относительный = путь.relative_to(каталог).as_posix()
            except ValueError:
                относительный = путь.name
            свой = f"file:{относительный}"
            if (f"{self.id}:{свой}" if self.id else свой) in известные:
                continue
            # Длительность из имени файла не узнать, и поставить ноль
            # значило бы выключить и порог длительности, и выдержку разом:
            # обе проверки написаны как «если длительность известна». Берём
            # настоящую — из самого файла; не прочиталась, значит ноль, и
            # это честно.
            длительность = _длительность_файла(путь)
            # Время последней записи в файл — это КОНЕЦ разговора, а не его
            # начало: запись дописывается по ходу. Ставить mtime началом
            # значило бы отодвинуть конец разговора в будущее на всю его
            # длину — и выдержка откладывала бы каждый файл до тех пор,
            # пока это будущее не наступит.
            звонок = Звонок(uniqueid=свой,
                            started_at=изменён - длительность,
                            duration=длительность,
                            disposition="ANSWERED", answered=True)
            звонок.billsec = звонок.duration
            звонок.recording = str(путь)
            # Номера из имени файла: «...-79161234567-101-...» встречается
            # в шаблонах чаще всего.
            номера = [ч for ч in путь.stem.replace("_", "-").split("-") if ч.isdigit()]
            if len(номера) >= 2:
                звонок.src, звонок.dst = номера[0], номера[1]
            звонки.append(звонок)
        return звонки

    # --- один звонок ------------------------------------------------------

    def _отложить(self, звонок: Звонок, почему: str) -> str:
        """Кладёт звонок в таблицу с пометкой «взять позже».

        Из журнала он больше не придёт: позиция чтения сдвигается сразу
        после чтения строки, а событие AMI вообще разовое. Раньше такой
        звонок просто терялся — и при выдержке в тридцать секунд с опросом
        раз в минуту под это попадала половина потока: за час импортёр
        ставил 289 заданий и терял 311.
        """
        # Владелец нужен и пропущенным: без него карточка «Пропущено» у
        # ключа подразделения вечный ноль, а причины пусты — то есть
        # единственное, что объясняет расхождение архива со станцией, не
        # видно тому, кто с архивом и работает.
        self.db.save_call(self._ключ(звонок), job_id=None,
                          skipped=f"{self.db.ОТЛОЖЕН}{почему}",
                          owner=self._владелец(звонок), station=self.id, **звонок.поля_записи())
        return почему

    def _ключ(self, звонок: Звонок) -> str:
        """Ключ звонка в архиве: станция плюс её идентификатор звонка.

        У Asterisk `uniqueid` уникален только внутри одной АТС — это
        «эпоха.счётчик», где счётчик локален станции и сбрасывается при её
        перезапуске. Две станции в одну секунду дают одинаковый
        идентификатор, и второй звонок молча выбрасывался как «уже
        импортирован». Настоящий идентификатор при этом никуда не девается:
        он едет в колонке `pbx_uid`.
        """
        if not звонок.uniqueid:
            return ""
        return f"{self.id}:{звонок.uniqueid}" if self.id else звонок.uniqueid

    def _взять(self, звонок: Звонок, *, отложенный: bool = False) -> str | None:
        """Ставит задание по звонку; возвращает причину пропуска или None."""
        if not звонок.uniqueid:
            return "без идентификатора"
        ключ = self._ключ(звонок)
        if not отложенный and self.db.call_exists(ключ):
            return "уже импортирован"

        минимум = int(self.станция.min_duration_s or 0)
        длительность = звонок.billsec or звонок.duration
        if минимум and длительность and длительность < минимум:
            self.db.save_call(ключ, job_id=None, skipped="короткий",
                              owner=self._владелец(звонок), station=self.id, **звонок.поля_записи())
            return "короткий"
        if bool(self.станция.skip_unanswered) and звонок.disposition \
                and not звонок.answered:
            self.db.save_call(ключ, job_id=None, skipped="без ответа",
                              owner=self._владелец(звонок), station=self.id, **звонок.поля_записи())
            return "без ответа"

        выдержка = float(self.станция.settle_s or 0)
        # Конец разговора — по полной длительности, а не по разговорной:
        # `billsec` не считает гудки, и разговор «минута дозвона, десять
        # секунд разговора» выглядел законченным пятьдесят секунд назад,
        # хотя MixMonitor закрыл файл только что.
        полная = звонок.duration or звонок.billsec
        if звонок.started_at and полная:
            конец = звонок.started_at + полная
            if time.time() - конец < выдержка:
                return self._отложить(звонок, "ещё пишется")

        путь = Path(звонок.recording) if звонок.recording else None
        if путь is None:
            каталог = self.станция.путь("recordings_dir")
            if каталог is None:
                return self._отложить(звонок, "не задан каталог записей")
            путь = найти_запись(звонок, каталог, шаблон=self.станция.filename,
                                окно_дней=int(self.станция.lookback_days or 7))
        if путь is not None and путь.is_file() and выдержка > 0 and not self._дописан(путь):
            # Обещано в справке параметра и в шапке модуля: сервер проверяет,
            # что файл перестал расти. Проверки не было вовсе, и запись,
            # взятая в момент дописывания, распознавалась обрезанной.
            return self._отложить(звонок, "файл ещё растёт")
        if путь is None or not путь.is_file():
            # Файла может не быть законно: запись не велась. Отмечаем, чтобы
            # не искать его на каждом заходе до скончания века.
            self.db.save_call(ключ, job_id=None, skipped="нет записи",
                              owner=self._владелец(звонок), station=self.id, **звонок.поля_записи())
            return "нет записи"

        звонок.direction = направление(
            звонок, внутренние_знаков=self.станция.internal_digits,
            контексты=self.станция.contexts)
        звонок.agent = звонок.dst if звонок.direction == "входящий" else звонок.src
        звонок.queue = str(звонок.raw.get("lastdata") or "").split(",")[0] \
            if str(звонок.raw.get("lastapp") or "").lower() == "queue" else ""
        звонок.recording = str(путь)

        владелец = self._владелец(звонок)
        метки = ",".join(м for м in (
            "АТС", self.станция.name, звонок.direction or "",
            f"очередь {звонок.queue}" if звонок.queue else "",
            self.станция.tags,
        ) if м)
        try:
            задание = self.queue.submit(
                file_path=путь, filename=путь.name,
                settings=(self.settings.merged({}) if self.settings is not None else {}),
                owner=владелец, api_key_name="telephony",
                priority=int(self.станция.priority or 40),
                source="asterisk", tags=метки)
        except Exception as exc:                             # noqa: BLE001
            with self._lock:
                self.failed += 1
                self.last_error = str(exc)
            log.warning("Звонок %s не поставлен в очередь: %s", звонок.uniqueid, exc)
            # Очередь отказала (полон диск, предел глубины) — это состояние
            # временное. Потерять из-за него разговор нельзя: вернёмся к
            # нему следующим заходом.
            return self._отложить(звонок, "очередь отказала")
        self.db.save_call(ключ, job_id=задание.get("id"), skipped="",
                          owner=владелец, station=self.id, **звонок.поля_записи())
        return None

    def _дописан(self, путь: Path) -> bool:
        """Перестал ли файл расти — короткая проверка в два замера.

        MixMonitor закрывает запись уже после того, как станция отчиталась
        о звонке, а иногда ещё и перекодирует её. Выдержка по времени
        отвечает на вопрос «наверное, пора», а этот замер — на вопрос
        «точно ли». Полсекунды на звонок, и только для тех, что дошли
        досюда.
        """
        try:
            было = путь.stat().st_size
        except OSError:
            return False
        time.sleep(ПАУЗА_ЗАМЕРА)
        try:
            стало = путь.stat().st_size
        except OSError:
            return False
        return стало == было and стало > 0

    def _владелец(self, звонок: Звонок) -> str:
        """Кому принадлежит запись: по внутреннему номеру или общий.

        Разрез по владельцу — это то, что отделяет отдел от отдела в
        отчётах. Сопоставление «номер → владелец» задаётся настройкой; чего
        в нём нет, достаётся общему владельцу.
        """
        карта = self.станция.owner_map
        for номер in (звонок.agent, звонок.dst, звонок.src):
            if номер and номер in карта:
                return str(карта[номер])
        return self.станция.owner or "telephony"


# ---------------------------------------------------------------------------
# Набор станций
# ---------------------------------------------------------------------------

class Телефония:
    """Все станции сразу: заводит потоки, гасит лишние, сводит состояние.

    Настройку правят на ходу — добавили филиал, отключили архив, сменили
    пароль. Каждый заход сверяется со списком станций и приводит набор
    потоков в соответствие: у новой станции поток появляется, у убранной
    исчезает, у изменившейся — перезапускается с новыми полями.

    Сверка идёт по идентификатору, а не по порядку в списке: переставить
    станции местами в настройке не должно ничего значить.
    """

    def __init__(self, db: Any, settings: Any, queue: Any):
        self.db = db
        self.settings = settings
        self.queue = queue
        self._lock = threading.Lock()
        self._станции: dict[str, Импортёр] = {}
        self._работает = False

    # --- набор ------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Включён ли забор записей вообще."""
        return bool(self.settings.get("telephony_enabled", False))

    def станции(self) -> list[Станция]:
        return станции_из_настроек(self.settings)

    def _свести(self) -> None:
        """Приводит набор потоков к тому, что написано в настройке."""
        # Настройки читаются ПОД замком, а не до него. Иначе два запроса,
        # пришедшие одновременно, читали разные версии списка, и тот, что
        # прочитал старую, воскрешал под замком станцию, которую сосед
        # только что убрал: удалённая АТС продолжала ходить и заводить
        # задания, и само это не проходило — `_свести` зовут только ручки.
        with self._lock:
            нужные = {с.id: с for с in self.станции()}
            for ид in list(self._станции):
                if ид not in нужные:
                    log.info("Станция «%s» убрана из настроек — останавливаем",
                             self._станции[ид].станция.name)
                    self._станции.pop(ид).stop(timeout=2.0)
            for ид, станция in нужные.items():
                живая = self._станции.get(ид)
                if живая is None:
                    self._станции[ид] = Импортёр(self.db, станция, self.queue,
                                                 self.settings)
                    if self._работает and self.enabled and станция.enabled:
                        self._станции[ид].start()
                    continue
                if живая.станция != станция:
                    # Поля изменились. Перезапуск честнее донастройки: у
                    # станции может смениться источник, и держать открытым
                    # соединение с прежним — значит не сказать об этом ни
                    # себе, ни человеку.
                    log.info("Настройки станции «%s» изменились — перезапуск",
                             станция.name)
                    живая.stop(timeout=2.0)
                    новая = Импортёр(self.db, станция, self.queue, self.settings)
                    # Счётчики переносим: они про архив, а не про поток.
                    новая.imported, новая.skipped = живая.imported, живая.skipped
                    новая.last_run, новая.last_scan = живая.last_run, живая.last_scan
                    self._станции[ид] = новая
                    живая = новая
                if self._работает and self.enabled and станция.enabled:
                    живая.start()
                elif not станция.enabled or not self.enabled:
                    живая.stop(timeout=2.0)

    def импортёр(self, station_id: str) -> Импортёр | None:
        self._свести()
        with self._lock:
            return self._станции.get(str(station_id or ""))

    # --- жизнь ------------------------------------------------------------

    def start(self) -> None:
        self._работает = True
        self._свести()
        живых = sum(1 for и in self._станции.values() if и.enabled)
        if живых:
            log.info("Телефония: станций в работе %d из %d", живых, len(self._станции))

    def stop(self, timeout: float = 5.0) -> None:
        self._работает = False
        with self._lock:
            станции = list(self._станции.values())
        доля = max(0.5, timeout / max(1, len(станции)))
        for импортёр in станции:
            импортёр.stop(timeout=доля)

    # --- наружу -----------------------------------------------------------

    def status(self, *, for_admin: bool = False, owner: Any = None) -> dict[str, Any]:
        """Состояние всех станций и общий свод по ним."""
        self._свести()
        with self._lock:
            импортёры = list(self._станции.values())
        станции = [и.status(for_admin=for_admin, owner=owner) for и in импортёры]
        итого = {"total": 0, "queued": 0, "skipped": 0, "deferred": 0,
                 "inbound": 0, "outbound": 0, "talk_s": 0}
        for с in станции:
            for ключ in итого:
                итого[ключ] += int(с.get("calls", {}).get(ключ) or 0)
        return {
            "enabled": self.enabled,
            "stations": станции,
            "running": sum(1 for с in станции if с.get("running")),
            "configured": len(станции),
            "calls": итого,
            # Звонки станций, которых больше нет в настройке, из архива не
            # исчезают: сводка по всему архиву отвечает на вопрос «сколько
            # у нас вообще разговоров», а не «сколько прислали живые».
            "archive": self.db.call_counts(owner=owner),
        }

    def scan(self, *, station_id: str = "", limit: int = ПОРЦИЯ) -> dict[str, Any]:
        """Заход по требованию: по одной станции или по всем сразу."""
        self._свести()
        with self._lock:
            импортёры = list(self._станции.values())
        if station_id:
            импортёры = [и for и in импортёры if и.id == station_id]
            if not импортёры:
                raise ConfigError(f"Станция «{station_id}» не настроена.",
                                  hint="Список: GET /api/telephony/stations.")
        итоги, сбои = [], []
        for импортёр in импортёры:
            if not импортёр.enabled and not station_id:
                continue
            try:
                итоги.append(импортёр.scan(limit=limit))
            except ASRHubError as exc:
                сбои.append({"station": импортёр.id, "name": импортёр.станция.name,
                             "error": exc.message, "hint": exc.hint})
            except Exception as exc:                         # noqa: BLE001
                сбои.append({"station": импортёр.id, "name": импортёр.станция.name,
                             "error": str(exc), "hint": ""})
        свод = {"stations": итоги, "errors": сбои}
        for ключ in ("seen", "imported", "skipped", "deferred"):
            свод[ключ] = sum(int(и.get(ключ) or 0) for и in итоги)
        причины: dict[str, int] = {}
        for и in итоги:
            for почему, сколько in (и.get("reasons") or {}).items():
                причины[почему] = причины.get(почему, 0) + int(сколько)
        свод["reasons"] = причины
        return свод

    def проверить(self, station_id: str) -> dict[str, Any]:
        """Проверка связи с одной станцией."""
        импортёр = self.импортёр(station_id)
        if импортёр is None:
            raise ConfigError(f"Станция «{station_id}» не настроена.",
                              hint="Список: GET /api/telephony/stations.")
        return импортёр.проверить_связь()

    def проверить_набросок(self, данные: dict[str, Any]) -> dict[str, Any]:
        """Проверка связи со станцией, которой ещё нет в настройках.

        Это и есть «проверить при добавлении»: человек заполнил форму и
        хочет знать, доедет ли сервер до станции, ДО того как сохранит
        настройки. Заводить ради этого станцию в настройках и потом убирать
        — способ оставить мусор при первом же закрытии вкладки.
        """
        from .stations import _станция_из  # noqa: PLC0415

        набросок = _станция_из(dict(данные or {}), 0)
        набросок.id = набросок.id or "проба"
        return Импортёр(self.db, набросок, self.queue, self.settings).проверить_связь()


__all__ = ["ПОРЦИЯ", "Импортёр", "Телефония"]
