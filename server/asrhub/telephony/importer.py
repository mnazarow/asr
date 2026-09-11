"""Фоновый импорт звонков: из журнала АТС — в очередь распознавания.

Один поток, один заход раз в `telephony_poll_s`: спросить источник о новых
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
  номером», и распознавать его незачем: `telephony_min_duration_s`
  отсекает такие, а `telephony_skip_unanswered` — неотвеченные.
"""
from __future__ import annotations

import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..logging_setup import get_logger
from .asterisk import ПОЛЯ_AMI as _ПОЛЯ_AMI
from .asterisk import (
    AMIClient,
    AMIError,
    Звонок,
    звонок_из_полей,
    найти_запись,
    направление,
    читать_csv,
)

log = get_logger("telephony")

#: Сколько звонков брать за один заход. Первый заход на живой АТС видит
#: весь журнал; без предела он поставил бы в очередь десятки тысяч заданий
#: разом и занял бы сервер на сутки.
ПОРЦИЯ = 50

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
    """Фоновый перенос звонков с АТС в очередь распознавания."""

    def __init__(self, db: Any, settings: Any, queue: Any):
        self.db = db
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

    # --- настройки --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("telephony_enabled", False))

    @property
    def source(self) -> str:
        return str(self.settings.get("telephony_source") or "cdr_csv")

    def путь(self, ключ: str) -> Path | None:
        значение = str(self.settings.get(ключ) or "").strip()
        return Path(значение) if значение else None

    # --- состояние --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            свод = {
                "enabled": self.enabled,
                "source": self.source,
                "running": bool(self._thread and self._thread.is_alive()),
                "imported": self.imported,
                "skipped": self.skipped,
                "failed": self.failed,
                "last_error": self.last_error,
                "last_run": self.last_run,
                "host": str(self.settings.get("telephony_host") or ""),
                "recordings_dir": str(self.settings.get("telephony_recordings_dir") or ""),
                "cdr_file": str(self.settings.get("telephony_cdr_file") or ""),
            }
        try:
            свод["calls"] = self.db.call_counts()
        except Exception as exc:                             # noqa: BLE001
            log.debug("Счётчики звонков не прочитаны: %s", exc)
            свод["calls"] = {}
        return свод

    # --- поток ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.clear()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="asrhub-telephony",
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
                пауза = max(5.0, _число(self.settings, "telephony_poll_s", 60))
                if итог.get("imported"):
                    # Есть что везти — заходим чаще: живой поток звонков
                    # не должен ждать полного интервала.
                    пауза = 5.0
            except Exception as exc:                         # noqa: BLE001
                with self._lock:
                    self.last_error = str(exc)
                    self.failed += 1
                log.warning("Заход телефонии не удался: %s", exc)
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
        отложенные = [Звонок.из_записи(з)
                      for з in self.db.calls_deferred(limit=max(1, limit // 2))]
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
        return {"seen": len(звонки), "imported": поставлено,
                "skipped": пропущено, "reasons": причины,
                "deferred": len(отложенные)}

    # --- источники --------------------------------------------------------

    def _из_csv(self, limit: int) -> list[Звонок]:
        путь = self.путь("telephony_cdr_file")
        if путь is None:
            raise ConfigError(
                "Не задан журнал звонков (telephony_cdr_file).",
                hint="Обычно это /var/log/asterisk/cdr-csv/Master.csv")
        позиция = int(self.db.get_kv("telephony_cdr_offset", 0) or 0)
        звонки, новая = читать_csv(путь, offset=позиция, limit=limit)
        self.db.set_kv("telephony_cdr_offset", новая)
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
        подпись = (str(self.settings.get("telephony_host") or "127.0.0.1"),
                   int(_число(self.settings, "telephony_port", 5038)),
                   str(self.settings.get("telephony_username") or ""),
                   str(self.settings.get("telephony_secret") or ""))
        if self._ami is not None and self._подпись_ami != подпись:
            log.info("Настройки AMI изменились — соединение открывается заново")
            self._закрыть_ami()
        клиент = self._ami
        if клиент is None:
            клиент = AMIClient(
                str(self.settings.get("telephony_host") or "127.0.0.1"),
                int(self.settings.get("telephony_port") or 5038),
                str(self.settings.get("telephony_username") or ""),
                str(self.settings.get("telephony_secret") or ""))
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
        каталог = self.путь("telephony_recordings_dir")
        if каталог is None or not каталог.is_dir():
            raise ConfigError(
                "Каталог записей не найден (telephony_recordings_dir).",
                hint="Проверьте путь в настройке «Каталог записей» и права "
                     "на чтение у пользователя, от которого работает сервер.")
        известные = self.db.known_call_ids()
        свежее = time.time() - _число(self.settings, "telephony_lookback_days", 7) * 86400
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
            ключ = f"file:{путь.name}"
            if ключ in известные:
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
            звонок = Звонок(uniqueid=ключ,
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
        self.db.save_call(звонок.uniqueid, job_id=None,
                          skipped=f"{self.db.ОТЛОЖЕН}{почему}",
                          owner=self._владелец(звонок), **звонок.поля_записи())
        return почему

    def _взять(self, звонок: Звонок, *, отложенный: bool = False) -> str | None:
        """Ставит задание по звонку; возвращает причину пропуска или None."""
        if not звонок.uniqueid:
            return "без идентификатора"
        if not отложенный and self.db.call_exists(звонок.uniqueid):
            return "уже импортирован"

        минимум = int(_число(self.settings, "telephony_min_duration_s", 0))
        длительность = звонок.billsec or звонок.duration
        if минимум and длительность and длительность < минимум:
            self.db.save_call(звонок.uniqueid, job_id=None, skipped="короткий",
                              owner=self._владелец(звонок), **звонок.поля_записи())
            return "короткий"
        if bool(self.settings.get("telephony_skip_unanswered", True)) and звонок.disposition \
                and not звонок.answered:
            self.db.save_call(звонок.uniqueid, job_id=None, skipped="без ответа",
                              owner=self._владелец(звонок), **звонок.поля_записи())
            return "без ответа"

        выдержка = _число(self.settings, "telephony_settle_s", 30)
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
            каталог = self.путь("telephony_recordings_dir")
            if каталог is None:
                return self._отложить(звонок, "не задан каталог записей")
            путь = найти_запись(звонок, каталог,
                                шаблон=str(self.settings.get("telephony_filename") or ""),
                                окно_дней=int(_число(self.settings, "telephony_lookback_days", 7)))
        if путь is not None and путь.is_file() and выдержка > 0 and not self._дописан(путь):
            # Обещано в справке параметра и в шапке модуля: сервер проверяет,
            # что файл перестал расти. Проверки не было вовсе, и запись,
            # взятая в момент дописывания, распознавалась обрезанной.
            return self._отложить(звонок, "файл ещё растёт")
        if путь is None or not путь.is_file():
            # Файла может не быть законно: запись не велась. Отмечаем, чтобы
            # не искать его на каждом заходе до скончания века.
            self.db.save_call(звонок.uniqueid, job_id=None, skipped="нет записи",
                              owner=self._владелец(звонок), **звонок.поля_записи())
            return "нет записи"

        звонок.direction = направление(
            звонок,
            внутренние_знаков=int(_число(self.settings, "telephony_internal_digits", 5)),
            контексты=dict(self.settings.get("telephony_contexts") or {}))
        звонок.agent = звонок.dst if звонок.direction == "входящий" else звонок.src
        звонок.queue = str(звонок.raw.get("lastdata") or "").split(",")[0] \
            if str(звонок.raw.get("lastapp") or "").lower() == "queue" else ""
        звонок.recording = str(путь)

        владелец = self._владелец(звонок)
        метки = ",".join(м for м in (
            "АТС", звонок.direction or "", f"очередь {звонок.queue}" if звонок.queue else "",
        ) if м)
        try:
            задание = self.queue.submit(
                file_path=путь, filename=путь.name,
                settings=self.settings.merged({}),
                owner=владелец, api_key_name="telephony",
                priority=int(_число(self.settings, "telephony_priority", 40)),
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
        self.db.save_call(звонок.uniqueid, job_id=задание.get("id"), skipped="",
                          owner=владелец, **звонок.поля_записи())
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
        карта = dict(self.settings.get("telephony_owner_map") or {})
        for номер in (звонок.agent, звонок.dst, звонок.src):
            if номер and номер in карта:
                return str(карта[номер])
        return str(self.settings.get("telephony_owner") or "telephony")
