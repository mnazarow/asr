"""Часть базы о телефонии: звонки с АТС, события очередей, переводов и мостов,
отрезки участников и разрезы раздела «АТС».

Вынесено из `db.py` (3.1.39) примесью к `Database`: файл базы был самым
большим в сервере, а телефония — самая большая и самая обособленная его
область. Методы и постоянные — те же, `Database` наследует их отсюда;
соединения, запись и общий отбор по владельцу остаются в `db.py`.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from .db_util import _экранировать_like, now
from .errors import StorageError


class ТелефонияБазы:
    """Примесь к `Database`: всё о звонках станций."""

    if TYPE_CHECKING:
        # Основа — у `Database` (db.py); здесь только объявления для проверки типов.
        def write(self) -> AbstractContextManager[sqlite3.Connection]: ...
        def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]: ...
        def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None: ...
        def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...
        def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int: ...

        @staticmethod
        def _owner_clause(owner: str | list[str] | None,
                          alias: str = "j") -> tuple[str, list[Any]]: ...

    # --- звонки с АТС ----------------------------------------------------
    #
    # Разговор с телефонной станции — это задание распознавания плюс
    # десяток полей, которых у обычного задания нет: кто кому звонил, через
    # какую очередь, ответили ли. Держать их в `params` задания было
    # заманчиво, но по JSON внутри колонки не построишь ни отбора «входящие
    # за вторник», ни разреза по очереди — а ради них всё и затевалось.

    #: Поля звонка, которые пишутся в таблицу. Перечень явный: словарь
    #: приходит из разбора журнала АТС, и лишний ключ в нём (у разных сборок
    #: Asterisk свой набор) не должен ломать вставку.
    ПОЛЯ_ЗВОНКА = (
        "src", "dst", "clid", "channel", "dstchannel", "context",
        "disposition", "direction", "queue", "agent", "duration", "billsec",
        "answered", "started_at", "recording", "userfield", "accountcode",
        "station", "pbx_uid", "linkedid", "sequence",
    )

    def save_call(self, uniqueid: str, *, job_id: str | None = None,
                  owner: str | None = None, skipped: str = "",
                  **поля: Any) -> None:
        """Запоминает звонок. Повторный вызов обновляет запись, не задваивая.

        `INSERT OR REPLACE` здесь неуместен: он стирает поля, которых нет в
        текущем вызове, и звонок, сохранённый импортёром со всеми полями, а
        потом дописанный одной пометкой, терял бы номера. Поэтому вставка с
        `ON CONFLICT DO UPDATE` и явным списком того, что обновляется.

        Пустое — не то же самое, что «не передали». `job_id` и `owner`
        пустыми не затираются: пометка «нет записи», поставленная вторым
        вызовом, иначе отвязывала бы звонок от распознавания и от отдела, а
        разрез по владельцу — единственное, что отделяет чужие разговоры от
        своих в отчётах.
        """
        if not uniqueid:
            raise StorageError("Звонок без идентификатора не сохраняется.")
        отложен = str(skipped or "").startswith(self.ОТЛОЖЕН)
        данные: dict[str, Any] = {"uniqueid": str(uniqueid),
                                  "job_id": job_id or None,
                                  "owner": str(owner) if owner else None,
                                  "skipped": str(skipped or ""),
                                  "imported_at": now(),
                                  "deferred_at": now() if отложен else None}
        for имя in self.ПОЛЯ_ЗВОНКА:
            if имя not in поля:
                continue
            значение = поля[имя]
            if имя == "answered":
                значение = 1 if значение else 0
            elif имя in ("duration", "billsec", "sequence"):
                значение = int(значение or 0)
            elif имя == "started_at":
                значение = float(значение or 0.0)
            else:
                значение = str(значение or "")
            данные[имя] = значение
        колонки = list(данные)
        места = ",".join("?" for _ in колонки)
        УДЕРЖИВАТЬ = ("job_id", "owner")
        обновить = ",".join(
            f"{к}=excluded.{к}" for к in колонки
            if к != "uniqueid" and к not in УДЕРЖИВАТЬ and к != "deferred_at")
        обновить += "".join(f",{к}=COALESCE(excluded.{к}, calls.{к})"
                            for к in УДЕРЖИВАТЬ)
        # Время ПЕРВОГО откладывания держится, пока звонок остаётся
        # отложенным: иначе каждое повторное откладывание продлевало ему
        # жизнь, и безнадёжный звонок не протухал никогда. У строк прежних
        # версий этого времени нет — для них отсчёт идёт от прошлой записи.
        обновить += (
            ",deferred_at=CASE WHEN excluded.deferred_at IS NULL THEN NULL "
            f"WHEN calls.skipped LIKE '{self.ОТЛОЖЕН}%' "
            "THEN COALESCE(calls.deferred_at, calls.imported_at, excluded.deferred_at) "
            "ELSE excluded.deferred_at END")
        self.execute(
            f"INSERT INTO calls ({','.join(колонки)}) VALUES ({места}) "
            f"ON CONFLICT(uniqueid) DO UPDATE SET {обновить}",
            [данные[к] for к in колонки])

    #: Приставка пометки «взять позже». Отличает звонок, который ещё
    #: нельзя распознать, от звонка, который распознавать не надо.
    ОТЛОЖЕН = "отложен: "

    def calls_deferred(self, limit: int = 50, *, station: str = "",
                       older_than: float | None = None) -> list[dict[str, Any]]:
        """Звонки, отложенные до следующего захода, — старые первыми.

        Из журнала они больше не придут: позиция чтения сдвинута сразу
        после чтения, а событие AMI вообще разовое. Без этой очереди
        звонок, у которого запись ещё дописывалась, терялся навсегда — а
        при выдержке в тридцать секунд и опросе раз в минуту под это
        попадала половина потока.

        `station` обязателен, когда станций несколько: без него импортёр
        головного офиса забирал отложенные звонки филиала, обрабатывал их
        своими правилами и переписывал им владельца и станцию — то есть
        разговоры филиала уезжали в чужой отчёт и в чужую очередь, а запись
        искалась в чужом каталоге и не находилась никогда.

        `older_than` отсекает безнадёжных: звонок, чья запись не появится
        уже никогда, иначе навечно занимает место в очереди отложенных, и
        импорт по станции встаёт целиком после одного сбоя записи.

        Срок считается от ПЕРВОГО откладывания (`deferred_at`), а не от
        времени разговора. По времени разговора любой архивный звонок
        выпадал из очереди в тот же миг, как туда попадал: сбор архива
        десятидневной давности, одна переполненная очередь — и разговор
        навсегда оставался с пометкой «отложен» и без задания. И не от
        последней записи строки (`imported_at`): её обновляет каждое
        повторное откладывание, и срок не наступал никогда.
        """
        условия = ["skipped LIKE ? ESCAPE '\\'"]
        args: list[Any] = [f"{_экранировать_like(self.ОТЛОЖЕН)}%"]
        if station:
            условия.append("COALESCE(station,'')=?")
            args.append(str(station))
        if older_than is not None:
            бессрочные = ",".join("?" for _ in self.ОТЛОЖЕН_БЕССРОЧНО)
            условия.append("(COALESCE(deferred_at, imported_at, started_at, 0) >= ? "
                           f"OR skipped IN ({бессрочные}))")
            args.append(float(older_than))
            args.extend(f"{self.ОТЛОЖЕН}{п}" for п in self.ОТЛОЖЕН_БЕССРОЧНО)
        args.append(max(1, int(limit)))
        rows = self.query(
            f"SELECT * FROM calls WHERE {' AND '.join(условия)} "
            "ORDER BY COALESCE(imported_at, started_at, 0) LIMIT ?", args)
        звонки = []
        for r in rows:
            звонок = dict(r)
            звонок["answered"] = bool(звонок.get("answered"))
            звонки.append(звонок)
        return звонки

    #: Приставка окончательного пропуска для отложенного звонка, чья запись
    #: так и не появилась за срок хранения отложенных.
    НЕ_ДОЖДАЛИСЬ = "не дождались записи: "

    #: Причины откладывания, у которых нет срока. Звонок, отложенный из-за
    #: полной очереди, — не безнадёжный: запись у него на месте, ждёт он
    #: места в очереди. Раньше через сутки он списывался «не дождались
    #: записи: очередь отказала», и первое подключение к большому архиву
    #: теряло всё, что очередь не успела разобрать за день.
    ОТЛОЖЕН_БЕССРОЧНО = ("очередь отказала",)

    def calls_expire_deferred(self, *, station: str = "",
                              older_than: float) -> int:
        """Отложенные дольше срока — в окончательный пропуск с причиной.

        Раньше такой звонок просто переставал выбираться, но оставался
        «отложенным» навсегда: в счётчике отложенных, без ответа на вопрос,
        чем кончилось. Теперь пометка честная — «не дождались записи: ещё
        пишется» (или какая была причина откладывания), и она видна в
        разрезе пропусков.
        """
        бессрочные = ",".join("?" for _ in self.ОТЛОЖЕН_БЕССРОЧНО)
        условия = ["skipped LIKE ? ESCAPE '\\'",
                   "COALESCE(deferred_at, imported_at, started_at, 0) < ?",
                   f"skipped NOT IN ({бессрочные})"]
        args: list[Any] = [f"{_экранировать_like(self.ОТЛОЖЕН)}%", float(older_than),
                           *(f"{self.ОТЛОЖЕН}{п}" for п in self.ОТЛОЖЕН_БЕССРОЧНО)]
        if station:
            условия.append("COALESCE(station,'')=?")
            args.append(str(station))
        return self.execute(
            f"UPDATE calls SET skipped = ? || substr(skipped, ?), deferred_at = NULL "
            f"WHERE {' AND '.join(условия)}",
            [self.НЕ_ДОЖДАЛИСЬ, len(self.ОТЛОЖЕН) + 1, *args])

    def call_owner(self, uniqueid: str) -> str | None:
        """Кому принадлежит строка архива. None — строки нет вовсе.

        Нужно ровно одному вызывающему: маршрут постановки задания собирает
        ключ архива из полей формы, а `save_call` обновляет строку с таким
        ключом, не спрашивая, чья она. Без этой проверки чужой звонок менял
        владельца и пропадал из отчётов того, кому принадлежал.
        """
        if not uniqueid:
            return None
        строка = self.query_one("SELECT owner FROM calls WHERE uniqueid=?",
                                (str(uniqueid),))
        return None if строка is None else (строка["owner"] or "")

    def call_exists(self, uniqueid: str) -> bool:
        """Был ли звонок уже разобран — главный предохранитель импортёра."""
        if not uniqueid:
            return False
        return self.query_one("SELECT 1 FROM calls WHERE uniqueid=?",
                              (str(uniqueid),)) is not None

    def call_get(self, uniqueid: str) -> dict[str, Any] | None:
        """Строка архива по ключу звонка; None — такого звонка нет.

        Импортёру мало «есть ли»: у Asterisk на один звонок бывает несколько
        строк CDR, и пришедшая позже может оказаться той самой, ради которой
        звонок и записывали (см. `telephony.asterisk.нога_лучше`).
        """
        if not uniqueid:
            return None
        строка = self.query_one("SELECT * FROM calls WHERE uniqueid=?", (str(uniqueid),))
        if строка is None:
            return None
        звонок = dict(строка)
        звонок["answered"] = bool(звонок.get("answered"))
        return звонок

    def call_has_recording(self, recording: str, *, station: str = "",
                           кроме: str = "") -> bool:
        """Отдан ли файл записи звонку станции, кроме названного.

        Подбор записи по номерам и по маске — догадка, и один файл доставался
        двум звонкам: второй уезжал в распознавание с чужим разговором.
        Отложенные не в счёт: файл им ещё не отдан окончательно.
        """
        if not recording:
            return False
        return self.query_one(
            "SELECT 1 FROM calls WHERE recording=? AND COALESCE(station,'')=? "
            "AND uniqueid<>? AND COALESCE(skipped,'') NOT LIKE ? ESCAPE '\\' LIMIT 1",
            (str(recording), str(station or ""), str(кроме or ""),
             f"{_экранировать_like(self.ОТЛОЖЕН)}%")) is not None

    #: Сколько известных звонков держать в памяти при обходе каталога.
    ПРЕДЕЛ_ИЗВЕСТНЫХ_ЗВОНКОВ = 100000

    def known_call_ids(self, limit: int | None = None, *,
                       station: str = "") -> set[str]:
        """Множество известных идентификаторов — для обхода папки записей.

        Спрашивать базу по файлу на каждом заходе — это тысячи запросов на
        каталог в десять тысяч записей. Одно множество дешевле и по времени,
        и по блокировкам. Множество ограничено свежими звонками; если в нём
        ровно `limit` — оно неполное, и вызывающий доспрашивает промахи
        через `call_exists` (см. `telephony.importer`).
        """
        где = " WHERE station=?" if station else ""
        args: list[Any] = [station] if station else []
        rows = self.query(
            f"SELECT uniqueid FROM calls{где} ORDER BY imported_at DESC LIMIT ?",
            (*args, max(1, int(limit or self.ПРЕДЕЛ_ИЗВЕСТНЫХ_ЗВОНКОВ))))
        return {str(r["uniqueid"]) for r in rows}

    # --- события очередей (queue_log) -----------------------------------

    def queue_events_add(self, station: str, события: list[dict[str, Any]]) -> int:
        """Сохраняет события очередей станции; повтор строки не задваивает."""
        if not события:
            return 0
        return self.executemany(
            "INSERT OR IGNORE INTO queue_events (station, ts, callid, queue, agent, event, "
            "wait_s, ring_s, talk_s) VALUES (?,?,?,?,?,?,?,?,?)",
            [(station, float(с["ts"]), str(с.get("callid") or ""), str(с.get("queue") or ""),
              str(с.get("agent") or ""), str(с["event"]), с.get("wait_s"), с.get("ring_s"),
              с.get("talk_s")) for с in события])

    def queue_events(self, *, station: str = "", since: float = 0.0,
                     until: float = 0.0, limit: int = 200000) -> list[dict[str, Any]]:
        условия: list[str] = ["ts>=?"]
        аргументы: list[Any] = [float(since or 0)]
        if until:
            условия.append("ts<?")
            аргументы.append(float(until))
        if station:
            условия.append("station=?")
            аргументы.append(station)
        return [dict(с) for с in self.query(
            f"SELECT * FROM queue_events WHERE {' AND '.join(условия)} ORDER BY ts LIMIT ?",
            [*аргументы, int(limit)])]

    def call_counts(self, *, owner: str | list[str] | None = None,
                    station: str = "") -> dict[str, Any]:
        """Сводка по звонкам: всего, поставлено, пропущено и почему."""
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, station]
        условие = " AND ".join(части)
        где = f" WHERE {условие}" if условие else ""
        строка = self.query_one(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END) AS queued,"
            "       SUM(CASE WHEN COALESCE(skipped,'')<>'' "
            "                AND skipped NOT LIKE 'отложен: %' THEN 1 ELSE 0 END) AS skipped,"
            "       SUM(CASE WHEN skipped LIKE 'отложен: %' THEN 1 ELSE 0 END) AS deferred,"
            "       SUM(CASE WHEN direction='входящий' THEN 1 ELSE 0 END) AS inbound,"
            "       SUM(CASE WHEN direction='исходящий' THEN 1 ELSE 0 END) AS outbound,"
            "       SUM(COALESCE(billsec,0)) AS talk_s,"
            "       MAX(started_at) AS last_call,"
            "       MAX(imported_at) AS last_import "
            f"FROM calls c{где}", args)
        свод = {к: (строка[к] if строка and строка[к] is not None else 0)
                for к in ("total", "queued", "skipped", "deferred",
                          "inbound", "outbound", "talk_s", "last_call",
                          "last_import")}
        причины = self.query(
            "SELECT skipped AS reason, COUNT(*) AS n FROM calls c "
            "WHERE COALESCE(skipped,'')<>'' AND skipped NOT LIKE 'отложен: %'"
            f"{(' AND ' + условие) if условие else ''} "
            "GROUP BY skipped ORDER BY n DESC LIMIT 20", args)
        свод["reasons"] = {str(r["reason"]): int(r["n"]) for r in причины}
        return свод

    #: Основа «решения с первого раза»: отвеченный входящий, у которого
    #: известен номер звонящего. `{c}` — псевдоним таблицы звонков.
    ОСНОВА_ПОВТОРОВ = ("{c}.direction='входящий' AND COALESCE({c}.answered,0)=1"
                       " AND COALESCE({c}.src,'')<>''")

    #: Повторное обращение: тот же номер позвонил входящим ещё раз в пределах
    #: окна после этого звонка. `{c}` — псевдоним звонка, `{окно}` — место
    #: длины окна в секундах. Выражение одно на свод (`call_kpi`) и на список
    #: раскрытия (`drill`): посчитай они «повторный» по-разному, показатель
    #: раскрывался бы не в те звонки, из которых сложился.
    ПОВТОРНЫЙ_ЗВОНОК = ("EXISTS ("
                        "SELECT 1 FROM calls p"
                        " WHERE p.src = {c}.src AND p.direction='входящий'"
                        " AND p.started_at > {c}.started_at"
                        " AND p.started_at <= {c}.started_at + {окно}"
                        " AND p.uniqueid <> {c}.uniqueid)")

    def call_kpi(self, *, owner: str | list[str] | None = None,
                 station: str = "", queue: str = "",
                 since: float | None = 0.0, until: float | None = 0.0,
                 service_level_s: float = 20.0,
                 repeat_window_days: float = 7.0) -> dict[str, Any]:
        """Показатели контакт-центра по журналу звонков.

        Всё считается по CDR, который и так лежит в базе: время разговора,
        признак ответа, очередь и номер звонящего. Отдельной интеграции ради
        этих чисел не нужно, а спрашивают их первыми — это тот самый список,
        по которому заказчик сравнивает системы между собой.

        Что именно считается и чего в этих числах НЕТ:

        * `aht_s` — среднее время РАЗГОВОРА по отвеченным. Настоящее «среднее
          время обработки» включает ещё удержание и постобработку; ни того ни
          другого в CDR нет, и называть эту величину полным AHT было бы
          подлогом. Здесь она честно подписана как разговор.
        * `abandoned_share` — доля входящих, на которые не ответили. Считается
          от всех входящих, а не от попавших в очередь: клиенту всё равно,
          дошёл ли его вызов до очереди.
        * `service_level` — доля отвеченных входящих, взятых быстрее порога.
          Ожидание считается как разница между общей длительностью вызова и
          временем разговора: столько вызов звонил, пока его не сняли.
        * `fcr` — доля обращений, после которых тот же номер не позвонил
          снова в течение окна. Это ОЦЕНКА решения с первого раза, а не оно
          само: повторный звонок бывает и по другому поводу. Зато считается
          без единого опроса и по всему архиву сразу.

        Звонки без номера звонящего (`src` пуст) в расчёт FCR не идут: у них
        нет ключа, по которому узнают повторное обращение, и молча считать их
        решёнными значит завышать показатель.

        Повторный звонок ищется по всему архиву, а не внутри периода и не
        внутри станции: клиент, позвонивший первого числа и перезвонивший
        третьего, решённым не был — независимо от того, что отчёт запрошен по
        одному дню и что перезвонил он в филиал.
        """
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, station]
        if queue:
            части.append("c.queue=?")
            args = [*args, queue]
        if since:
            части.append("c.started_at>=?")
            args = [*args, float(since)]
        if until:
            части.append("c.started_at<?")
            args = [*args, float(until)]
        условие = " AND ".join(части)
        где = f" WHERE {условие}" if условие else ""
        порог = max(0.0, float(service_level_s))

        строка = self.query_one(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN c.direction='входящий' THEN 1 ELSE 0 END) AS inbound,"
            "       SUM(CASE WHEN c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "                THEN 1 ELSE 0 END) AS inbound_answered,"
            "       SUM(CASE WHEN COALESCE(c.answered,0)=1 THEN 1 ELSE 0 END) AS answered,"
            "       SUM(CASE WHEN COALESCE(c.answered,0)=1"
            "                THEN COALESCE(c.billsec,0) ELSE 0 END) AS talk_s,"
            "       SUM(CASE WHEN c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "                THEN MAX(0, COALESCE(c.duration,0) - COALESCE(c.billsec,0))"
            "                ELSE 0 END) AS wait_s,"
            "       SUM(CASE WHEN c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "                AND MAX(0, COALESCE(c.duration,0) - COALESCE(c.billsec,0)) <= ?"
            "                THEN 1 ELSE 0 END) AS in_time "
            f"FROM calls c{где}", [порог, *args])
        свод = {к: float(строка[к] or 0) if строка else 0.0
                for к in ("total", "inbound", "inbound_answered", "answered",
                          "talk_s", "wait_s", "in_time")}

        окно = max(0.0, float(repeat_window_days)) * 86400.0
        повтор = self.query_one(
            "SELECT COUNT(*) AS base,"
            f"       SUM(CASE WHEN {self.ПОВТОРНЫЙ_ЗВОНОК.format(c='c', окно='?')}"
            "           THEN 1 ELSE 0 END) AS repeated "
            f"FROM calls c{где}{' AND' if где else ' WHERE'} "
            f"  {self.ОСНОВА_ПОВТОРОВ.format(c='c')}", [окно, *args]) if окно > 0 else None

        основа = float((повтор["base"] if повтор else 0) or 0)
        повторных = float((повтор["repeated"] if повтор else 0) or 0)
        return {
            "total": int(свод["total"]),
            "inbound": int(свод["inbound"]),
            "answered": int(свод["answered"]),
            "inbound_answered": int(свод["inbound_answered"]),
            "abandoned": int(свод["inbound"] - свод["inbound_answered"]),
            "abandoned_share": (round((свод["inbound"] - свод["inbound_answered"])
                                      / свод["inbound"], 4)
                                if свод["inbound"] else None),
            # Среднее время разговора — по отвеченным: делить на все вызовы
            # значит смешивать разговор с гудками.
            "aht_s": (round(свод["talk_s"] / свод["answered"], 1)
                      if свод["answered"] else None),
            "wait_avg_s": (round(свод["wait_s"] / свод["inbound_answered"], 1)
                           if свод["inbound_answered"] else None),
            "service_level": (round(свод["in_time"] / свод["inbound_answered"], 4)
                              if свод["inbound_answered"] else None),
            "service_level_s": порог,
            # Числитель уровня обслуживания и число повторных обращений —
            # сами счётчики, а не только доли: долю интерфейс раскрывает в
            # список звонков, и сверять список приходится с числом.
            "in_time": int(свод["in_time"]),
            "fcr": (round(1.0 - повторных / основа, 4) if основа else None),
            "fcr_base": int(основа),
            "repeated": int(повторных),
            "repeat_window_days": round(float(repeat_window_days), 2),
            # Окно в секундах без округления: по нему список повторных
            # обращений отбирается тем же условием, что и счёт.
            "repeat_window_s": окно,
        }

    def list_calls(self, *, owner: str | list[str] | None = None,
                   direction: str = "", queue: str = "", agent: str = "",
                   station: str = "", skipped: str = "",
                   since: float | None = None, until: float | None = None,
                   only_queued: bool = False, search: str = "",
                   limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Журнал звонков с отбором — то, что показывает раздел «Телефония»."""
        where: list[str] = []
        args: list[Any] = []
        условие, свои = self._owner_clause(owner, "c")
        if условие:
            where.append(условие)
            args += свои
        if direction:
            where.append("c.direction=?")
            args.append(direction)
        if queue:
            where.append("c.queue=?")
            args.append(queue)
        if agent:
            where.append("c.agent=?")
            args.append(agent)
        if station:
            where.append("c.station=?")
            args.append(station)
        if skipped == "yes":
            where.append("COALESCE(c.skipped,'')<>'' AND c.skipped NOT LIKE 'отложен: %'")
        elif skipped == "deferred":
            where.append("c.skipped LIKE 'отложен: %'")
        elif skipped == "no":
            where.append("COALESCE(c.skipped,'')=''")
        if since is not None:
            where.append("c.started_at>=?")
            args.append(float(since))
        if until is not None:
            where.append("c.started_at<?")
            args.append(float(until))
        if only_queued:
            where.append("c.job_id IS NOT NULL")
        if search:
            образец = f"%{_экранировать_like(search)}%"
            # И по ключу архива, и по идентификатору станции: в журнале АТС
            # человек видит второй, а в наших ответах — первый, и искать он
            # будет тот, что у него перед глазами.
            where.append("(c.src LIKE ? ESCAPE '\\' OR c.dst LIKE ? ESCAPE '\\' "
                         "OR c.clid LIKE ? ESCAPE '\\' OR c.uniqueid LIKE ? ESCAPE '\\' "
                         "OR c.pbx_uid LIKE ? ESCAPE '\\')")
            args += [образец] * 5
        где = f" WHERE {' AND '.join(where)}" if where else ""
        всего = self.query_one(f"SELECT COUNT(*) AS n FROM calls c{где}", args)
        предел = max(1, min(int(limit), 500))
        rows = self.query(
            # Начало расшифровки — строкой в двести знаков, как в списке
            # заданий: полный текст каждой записи страницы ради превью —
            # мегабайты на каждое открытие журнала звонков.
            "SELECT c.*, j.status AS job_status, substr(j.text, 1, 200) AS job_text,"
            "       j.media_duration_s AS job_duration "
            f"FROM calls c LEFT JOIN jobs j ON j.id=c.job_id{где} "
            "ORDER BY c.started_at DESC, c.imported_at DESC LIMIT ? OFFSET ?",
            [*args, предел, max(0, int(offset))])
        звонки = []
        for r in rows:
            звонок = dict(r)
            звонок["answered"] = bool(звонок.get("answered"))
            текст = звонок.pop("job_text", None) or ""
            звонок["preview"] = текст[:200]
            звонки.append(звонок)
        return {"total": int(всего["n"]) if всего else 0, "calls": звонки,
                "limit": предел, "offset": max(0, int(offset))}

    def call_for_job(self, job_id: str) -> dict[str, Any] | None:
        """Карточка задания спрашивает: а это вообще звонок и чей?"""
        строка = self.query_one("SELECT * FROM calls WHERE job_id=?", (str(job_id),))
        if строка is None:
            return None
        звонок = dict(строка)
        звонок["answered"] = bool(звонок.get("answered"))
        return звонок

    def save_agent_segments(self, job_id: str, участники: list[dict[str, Any]], *,
                            записанный: str = "", ног: int = 0, переводов: int = 0,
                            мостов: int = 0) -> None:
        """Отрезки записи по участникам (заменяют прежние); пусто — убрать.

        `ног`, `переводов` и `мостов` — с чем считалось: поздняя нога CDR,
        событие перевода или моста, пришедшие после подсчёта, возвращают
        запись в `jobs_to_split`.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM agent_segments WHERE job_id=?", (str(job_id),))
            conn.executemany(
                "INSERT INTO agent_segments (job_id, agent, talk_s, words, replicas, "
                "sentiment, share, recorded, legs, transfers, bridges, computed_at) "
                "SELECT ?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM jobs WHERE id=?)",
                [(str(job_id), str(у["agent"]), float(у.get("talk_s") or 0),
                  int(у.get("words") or 0), int(у.get("replicas") or 0),
                  (у.get("sentiment") or {}).get("score"), у.get("share"),
                  1 if str(у["agent"]) == записанный else 0, int(ног), int(переводов),
                  int(мостов), now(), str(job_id))
                 for у in участники])

    def jobs_to_split(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Записанные ноги разговоров с переводом, которым нужны отрезки.

        Нет отрезков вовсе — или ног с разговором у этого `linkedid` стало
        больше или меньше, чем было при подсчёте (поздняя нога перевода
        приходит отдельной строкой CDR, иногда — следующим заходом агента),
        или пришли события перевода из AMI, которых при подсчёте не было.
        """
        строки = self.query(
            "SELECT * FROM ("
            "  SELECT c.job_id AS job_id, c.started_at AS started_at, "
            "    (SELECT COUNT(*) FROM calls l WHERE l.station=c.station "
            "       AND l.linkedid=c.linkedid AND COALESCE(l.billsec,0) > 0) AS legs, "
            "    (SELECT COUNT(DISTINCT NULLIF(l.agent,'')) FROM calls l "
            "       WHERE l.station=c.station AND l.linkedid=c.linkedid "
            "       AND COALESCE(l.billsec,0) > 0) AS agents, "
            "    (SELECT COUNT(*) FROM call_transfers t WHERE t.station=c.station "
            "       AND t.linkedid=c.linkedid) AS transfers, "
            "    (SELECT COUNT(*) FROM bridge_events b WHERE b.station=c.station "
            "       AND b.linkedid=c.linkedid) AS bridges, "
            "    (SELECT MAX(s.legs) FROM agent_segments s WHERE s.job_id=c.job_id) AS done, "
            "    (SELECT MAX(s.transfers) FROM agent_segments s "
            "       WHERE s.job_id=c.job_id) AS done_transfers, "
            "    (SELECT MAX(s.bridges) FROM agent_segments s "
            "       WHERE s.job_id=c.job_id) AS done_bridges "
            "  FROM calls c JOIN jobs j ON j.id = c.job_id "
            "  WHERE c.job_id IS NOT NULL AND COALESCE(c.linkedid,'') <> '' "
            "    AND j.status='completed') "
            "WHERE agents >= 2 AND (COALESCE(done, -1) <> legs "
            "                       OR COALESCE(done_transfers, 0) <> transfers "
            "                       OR COALESCE(done_bridges, 0) <> bridges) "
            "ORDER BY started_at DESC LIMIT ?", (max(1, int(limit)),))
        return [dict(с) for с in строки]

    def agent_segments_by_agent(self, where: str, args: list[Any],
                                негатив: float = -0.15) -> list[dict[str, Any]]:
        """Участие в разговорах с переводом по добавочным — для разрезов."""
        return [dict(с) for с in self.query(
            "SELECT s.agent AS agent, COUNT(*) AS records, SUM(s.talk_s) AS talk_s, "
            "       SUM(s.words) AS words, "
            "       SUM(CASE WHEN s.sentiment IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
            "       SUM(CASE WHEN s.sentiment < ? THEN 1 ELSE 0 END) AS negative, "
            "       SUM(CASE WHEN s.recorded = 0 THEN 1 ELSE 0 END) AS received "
            f"FROM agent_segments s JOIN jobs j ON j.id = s.job_id "
            f"WHERE s.agent <> '' AND ({where}) GROUP BY s.agent", [негатив, *args])]

    def call_transfers_add(self, station: str, переводы: list[dict[str, Any]]) -> int:
        """События перевода станции (`telephony.transfers`); повтор не задваивает."""
        if not переводы:
            return 0
        return self.executemany(
            "INSERT OR IGNORE INTO call_transfers (station, linkedid, ts, kind, from_agent, "
            "to_agent) VALUES (?,?,?,?,?,?)",
            [(str(station), str(п["linkedid"]), float(п["ts"]), str(п["kind"]),
              str(п.get("from_agent") or ""), str(п.get("to_agent") or "")) for п in переводы])

    def call_transfers(self, звонок: dict[str, Any]) -> list[dict[str, Any]]:
        """События перевода разговора: та же станция и тот же linkedid, по времени."""
        связь = str(звонок.get("linkedid") or "")
        if not связь:
            return []
        return [dict(с) for с in self.query(
            "SELECT ts, kind, from_agent, to_agent FROM call_transfers "
            "WHERE station=? AND linkedid=? ORDER BY ts LIMIT 50",
            (str(звонок.get("station") or ""), связь))]

    def bridge_events_add(self, station: str, события: list[dict[str, Any]]) -> int:
        """Вход в мост и выход из него (`telephony.transfers`); повтор не задваивает."""
        if not события:
            return 0
        return self.executemany(
            "INSERT OR IGNORE INTO bridge_events (station, linkedid, ts, kind, uniqueid, "
            "bridge, agent) VALUES (?,?,?,?,?,?,?)",
            [(str(station), str(с["linkedid"]), float(с["ts"]), str(с["kind"]),
              str(с["uniqueid"]), str(с["bridge"]), str(с.get("agent") or ""))
             for с in события])

    def bridge_events(self, звонок: dict[str, Any]) -> list[dict[str, Any]]:
        """События мостов разговора: та же станция и тот же linkedid, по времени."""
        связь = str(звонок.get("linkedid") or "")
        if not связь:
            return []
        return [dict(с) for с in self.query(
            "SELECT ts, kind, uniqueid, bridge, agent FROM bridge_events "
            "WHERE station=? AND linkedid=? ORDER BY ts, kind LIMIT 500",
            (str(звонок.get("station") or ""), связь))]

    def bridge_events_prune(self, *, older_than: float) -> int:
        """Мосты разговоров без перевода — не нужны: их событий на звонок по
        четыре-восемь, а нужны они только там, где участников больше одного.
        Разговор старше `older_than`, у которого добавочных с разговором
        меньше двух, теряет свои события мостов."""
        return self.execute(
            "DELETE FROM bridge_events WHERE ts < ? AND NOT EXISTS ("
            "  SELECT 1 FROM calls l WHERE l.station = bridge_events.station "
            "    AND l.linkedid = bridge_events.linkedid AND COALESCE(l.billsec,0) > 0 "
            "  GROUP BY l.station, l.linkedid "
            "  HAVING COUNT(DISTINCT NULLIF(l.agent,'')) >= 2)", (float(older_than),))

    def call_legs(self, звонок: dict[str, Any], *, предел: int = 20) -> list[dict[str, Any]]:
        """Другие ноги того же разговора: та же станция и тот же linkedid.

        Перевод с оператора на оператора, очередь, второй участник — у
        Asterisk 12+ это отдельные строки CDR со своими uniqueid и общим
        linkedid. Порядок — по sequence, затем по началу.
        """
        связь = str(звонок.get("linkedid") or "")
        if not связь:
            return []
        строки = self.query(
            "SELECT uniqueid, pbx_uid, job_id, src, dst, agent, queue, direction, "
            "disposition, billsec, duration, started_at, sequence, skipped, owner FROM calls "
            "WHERE station=? AND linkedid=? AND uniqueid<>? "
            "ORDER BY sequence, started_at LIMIT ?",
            (str(звонок.get("station") or ""), связь, str(звонок.get("uniqueid") or ""),
             max(1, int(предел))))
        return [dict(с) for с in строки]

    def conversations(self, *, owner: str | list[str] | None = None, station: str = "",
                      since: float | None = None, until: float | None = None,
                      limit: int = 500, only_transferred: bool = False) -> dict[str, Any]:
        """Разговоры как единица: ноги одного linkedid — одной строкой.

        Asterisk 12+ пишет перевод, очередь и второго оператора отдельными
        строками CDR, и отчёты считали ногу за звонок: один разговор с двумя
        переводами — три звонка, три оператора по полному времени. Здесь
        ноги сводятся по (станция, linkedid; без него — сам звонок): кто
        участвовал по порядку, сколько было переводов, когда разговор начался
        и кончился и сколько в нём говорили.
        """
        части, args = self._условия_звонков(owner, station, since, until)
        где = f" WHERE {' AND '.join(части)}" if части else ""
        ключ = "COALESCE(NULLIF(c.linkedid,''), COALESCE(NULLIF(c.pbx_uid,''), c.uniqueid))"
        строки = self.query(
            f"SELECT c.station AS station, {ключ} AS conv, COUNT(*) AS legs, "
            "MIN(c.started_at) AS started_at, "
            "MAX(c.started_at + COALESCE(c.duration, c.billsec, 0)) AS ended_at, "
            "SUM(COALESCE(c.billsec,0)) AS talk_s, "
            "MAX(CASE WHEN c.answered THEN 1 ELSE 0 END) AS answered, "
            "GROUP_CONCAT(CASE WHEN COALESCE(c.agent,'')<>'' THEN c.agent END, ',') AS agents, "
            "MAX(c.direction) AS direction, MAX(c.src) AS src, "
            "MAX(c.job_id) AS job_id, MAX(c.queue) AS queue "
            # Ноги — по порядку разговора (sequence, начало): так склеиваются
            # участники в GROUP_CONCAT и на старом SQLite без ORDER BY в нём.
            f"FROM (SELECT * FROM calls c{где} ORDER BY c.sequence, c.started_at) c "
            f"GROUP BY c.station, {ключ} "
            + ("HAVING COUNT(DISTINCT NULLIF(c.agent,'')) > 1 " if only_transferred else "")
            + "ORDER BY started_at DESC LIMIT ?", [*args, max(1, int(limit))])
        разговоры = []
        for с in строки:
            по_порядку: list[str] = []
            for кто in str(с["agents"] or "").split(","):
                if кто and (not по_порядку or по_порядку[-1] != кто):
                    по_порядку.append(кто)
            разговоры.append({
                "station": с["station"], "conversation": с["conv"], "legs": int(с["legs"] or 0),
                "started_at": с["started_at"], "ended_at": с["ended_at"],
                "talk_s": int(с["talk_s"] or 0), "answered": bool(с["answered"]),
                "agents": по_порядку, "transfers": max(0, len(по_порядку) - 1),
                "direction": с["direction"] or "", "src": с["src"] or "",
                "job_id": с["job_id"], "queue": с["queue"] or ""})
        свод = self.query_one(
            f"SELECT COUNT(*) AS calls, COUNT(DISTINCT c.station || '|' || {ключ}) AS convs "
            f"FROM calls c{где}", args)
        всего = int(свод["convs"] or 0) if свод else 0
        с_переводом = self.query_one(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM calls c"
            f"{где} GROUP BY c.station, {ключ} "
            "HAVING COUNT(DISTINCT NULLIF(c.agent,'')) > 1)", args)
        return {"items": разговоры,
                "calls": int(свод["calls"] or 0) if свод else 0,
                "conversations": всего,
                "transferred": int(с_переводом["n"] or 0) if с_переводом else 0}

    def call_dimensions(self, *, owner: str | list[str] | None = None,
                        station: str = "") -> dict[str, list[str]]:
        """Очереди, операторы и станции, которые встречались, — для отбора."""
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, station]
        условие = " AND ".join(части)
        где = f" WHERE {условие}" if условие else ""
        def значения(поле: str) -> list[str]:
            rows = self.query(
                f"SELECT {поле} AS v, COUNT(*) AS n FROM calls c{где} "
                f"{'AND' if где else 'WHERE'} COALESCE({поле},'')<>'' "
                f"GROUP BY {поле} ORDER BY n DESC LIMIT 200", args)
            return [str(r["v"]) for r in rows]
        return {"queues": значения("c.queue"), "agents": значения("c.agent"),
                "directions": значения("c.direction"),
                "stations": значения("c.station")}

    # --- разрезы для раздела «АТС» ----------------------------------------

    def _условия_звонков(self, owner: Any, station: str, since: float | None,
                         until: float | None) -> tuple[list[str], list[Any]]:
        """Общее «где» для всех сводок раздела: владелец, станция, период.

        Одно место вместо пяти: разрез по владельцу — самая частая забытая
        строчка в этом проекте, и повторять её в каждом запросе значит рано
        или поздно один раз не повторить.
        """
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, str(station)]
        if since is not None:
            части.append("c.started_at>=?")
            args = [*args, float(since)]
        if until is not None:
            части.append("c.started_at<?")
            args = [*args, float(until)]
        return части, args

    def call_horizon(self, now: float | None = None) -> float:
        """Верхняя граница окна сводов по звонкам: «сейчас», но не раньше
        самого позднего звонка архива.

        Своды раздела «АТС» сверху не ограничены: звонок, чьё время начала
        ушло вперёд (часы станции спешат, пояс не задан), в них входит. Список
        раскрытия режется по обеим границам — и граница «сейчас» выбросила бы
        из него ровно эти звонки, то есть число разошлось бы со списком на
        той самой станции, где и так что-то не в порядке. Поэтому верх окна
        поднимается до последнего звонка: всё, что свод посчитал, в список
        попадёт, а звонки, начавшиеся позже ответа, — уже нет.

        Самый поздний звонок берётся по всему архиву, без разреза по
        владельцу: это граница, а не данные, и чужого она не выдаёт, зато
        читается одним шагом по указателю `idx_calls_started`.
        """
        сейчас = float(now if now is not None else time.time())
        строка = self.query_one("SELECT MAX(started_at) AS t FROM calls")
        последний = float(строка["t"] or 0.0) if строка else 0.0
        # Верхняя граница не входит в окно, отсюда секунда запаса.
        return max(сейчас, последний + 1.0)

    #: Что считаем в каждой корзине времени. Держим одним списком: колонки
    #: нужны и линии нагрузки, и сводке за период, и расходиться они не должны.
    СЧЁТ_ЗВОНКОВ = (
        "COUNT(*) AS total",
        "SUM(CASE WHEN c.direction='входящий' THEN 1 ELSE 0 END) AS inbound",
        "SUM(CASE WHEN c.direction='исходящий' THEN 1 ELSE 0 END) AS outbound",
        "SUM(CASE WHEN c.direction='внутренний' THEN 1 ELSE 0 END) AS internal",
        "SUM(CASE WHEN c.answered=1 THEN 1 ELSE 0 END) AS answered",
        "SUM(CASE WHEN c.job_id IS NOT NULL THEN 1 ELSE 0 END) AS queued",
        "SUM(CASE WHEN COALESCE(c.skipped,'')<>'' "
        "         AND c.skipped NOT LIKE 'отложен: %' THEN 1 ELSE 0 END) AS skipped",
        "SUM(COALESCE(c.billsec,0)) AS talk_s",
        "SUM(CASE WHEN c.answered=1 THEN "
        "    MAX(COALESCE(c.duration,0) - COALESCE(c.billsec,0), 0) ELSE 0 END) AS wait_s",
    )

    ШАГ_КОРЗИНЫ = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}

    #: Эпоха началась в четверг: неделя, отсчитанная от неё делением, шла с
    #: четверга по среду, и «неделя с Thu 10.09» собирала четверг,
    #: воскресенье и понедельник. Первый понедельник эпохи — 5 января 1970.
    СДВИГ_НЕДЕЛИ = 4 * 86400

    def _корзина_звонка(self, шаг: int, сдвиг: int) -> tuple[str, str]:
        """SQL начала корзины (секунды эпохи) и её порядкового номера.

        Час и сутки — арифметикой по местному сдвигу. Неделя — так же, но
        от понедельника. Месяц — календарный: «30 суток» уводили корзины
        от первых чисел на день за месяц, и к декабрю «месяц» начинался
        25 ноября. Календарный месяц считает сам SQLite: `localtime` →
        начало месяца → `utc`; номер корзины — год·12 + месяц, чтобы
        соседние месяцы шли подряд, как и прочие корзины.
        """
        t = "c.started_at"
        if шаг == self.ШАГ_КОРЗИНЫ["month"]:
            начало = (f"CAST(strftime('%s', {t}, 'unixepoch', 'localtime', "
                      "'start of month', 'utc') AS INTEGER)")
            номер = (f"(CAST(strftime('%Y', {t}, 'unixepoch', 'localtime') AS INTEGER) * 12"
                     f" + CAST(strftime('%m', {t}, 'unixepoch', 'localtime') AS INTEGER))")
            return начало, номер
        неделя = self.СДВИГ_НЕДЕЛИ if шаг == self.ШАГ_КОРЗИНЫ["week"] else 0
        номер = f"CAST(({t} + {сдвиг} - {неделя}) / {шаг} AS INTEGER)"
        return f"{номер} * {шаг} + {неделя} - {сдвиг}", номер

    def _конец_корзины_звонка(self, шаг: int, сдвиг: int) -> str:
        """SQL конца корзины (секунды эпохи): первая секунда следующей.

        Нужен раскрытию точки линии в список звонков: «с такого-то по
        такое-то» обязано быть ровно той корзиной, в которую звонок попал
        при счёте. Час, сутки и неделя — начало плюс шаг; месяц — начало
        следующего календарного месяца, а не «плюс тридцать суток».
        """
        if шаг == self.ШАГ_КОРЗИНЫ["month"]:
            return ("CAST(strftime('%s', c.started_at, 'unixepoch', 'localtime', "
                    "'start of month', '+1 month', 'utc') AS INTEGER)")
        начало, _ = self._корзина_звонка(шаг, сдвиг)
        return f"{начало} + {шаг}"

    @staticmethod
    def _сдвиг_времени() -> int:
        """Смещение местного времени от UTC, секунды.

        Нужно там, где сутки режутся арифметикой, а не `strftime`: вопрос
        «сколько звонков было в четверг» задаёт человек, живущий по своим
        часам, и сутки у него начинаются в полночь, а не в 03:00.

        Берётся на текущий момент, а не на каждую строку: внутри одного
        отчёта сдвиг обязан быть один, иначе переход на зимнее время
        разрезал бы одни сутки на двое.
        """
        return -int(time.timezone if not time.daylight or not time.localtime().tm_isdst
                    else time.altzone)

    def call_timeline(self, *, owner: str | list[str] | None = None,
                      station: str = "", since: float | None = None,
                      until: float | None = None, bucket: str = "day",
                      limit: int = 2000) -> list[dict[str, Any]]:
        """Звонки по корзинам времени — линия нагрузки в разделе «АТС».

        Корзина считается на стороне SQLite из `started_at`, а не в Python:
        за год звонков набегает миллион, и тащить их в память ради
        группировки по часам — это секунды ожидания при каждом открытии
        раздела.
        """
        шаг = self.ШАГ_КОРЗИНЫ.get(str(bucket), 86400)
        части, args = self._условия_звонков(owner, station, since, until)
        части.append("c.started_at>0")
        # Суточные и более крупные корзины режутся по МЕСТНОМУ времени, а не
        # по UTC: сервер живёт в UTC, и без поправки сутки начинались в 03:00,
        # ночные звонки попадали во вчера, а соседняя тепловая карта (она
        # считается через `localtime`) противоречила этой же линии в одном
        # и том же ответе.
        сдвиг = self._сдвиг_времени() if шаг >= 86400 else 0
        начало, _ = self._корзина_звонка(шаг, сдвиг)
        конец = self._конец_корзины_звонка(шаг, сдвиг)
        rows = self.query(
            # Конец у всех звонков корзины один; MAX — чтобы он был
            # выражением группы, а не «какой-нибудь строки».
            f"SELECT {начало} AS bucket, MAX({конец}) AS bucket_end, "
            + ", ".join(self.СЧЁТ_ЗВОНКОВ)
            + f" FROM calls c WHERE {' AND '.join(части)} "
            f"GROUP BY bucket ORDER BY bucket LIMIT {int(limit)}", args)
        return [{"t": float(r["bucket"]),
                 # Точные границы корзины: звонок в ней, если t <= начало < until.
                 "until": float(r["bucket_end"]),
                 **{к: int(r[к] or 0) for к in
                    ("total", "inbound", "outbound", "internal", "answered",
                     "queued", "skipped", "talk_s", "wait_s")}}
                for r in rows]

    def call_timeline_by_station(self, *, owner: str | list[str] | None = None,
                                 since: float | None = None,
                                 until: float | None = None, bucket: str = "day",
                                 points: int = 24) -> dict[str, list[int]]:
        """Короткая лента звонков по каждой станции — линия на её карточке.

        Возвращает ровно `points` последних корзин у всех станций, включая
        пустые: линия с пропущенными сутками врёт о нагрузке сильнее, чем
        линия с нулём, потому что читается как «в этот день было столько
        же».
        """
        шаг = self.ШАГ_КОРЗИНЫ.get(str(bucket), 86400)
        части, args = self._условия_звонков(owner, "", since, until)
        части.append("c.started_at>0")
        сдвиг = self._сдвиг_времени() if шаг >= 86400 else 0
        _, номер = self._корзина_звонка(шаг, сдвиг)
        rows = self.query(
            f"SELECT COALESCE(c.station,'') AS station, "
            f"       {номер} AS bucket, "
            "       COUNT(*) AS n "
            f"FROM calls c WHERE {' AND '.join(части)} "
            "GROUP BY station, bucket", args)
        if not rows:
            return {}
        последняя = max(int(r["bucket"]) for r in rows)
        первая = последняя - max(1, int(points)) + 1
        ленты: dict[str, list[int]] = {}
        for r in rows:
            корзина = int(r["bucket"])
            if корзина < первая:
                continue
            лента = ленты.setdefault(str(r["station"] or ""), [0] * (последняя - первая + 1))
            лента[корзина - первая] = int(r["n"] or 0)
        return ленты

    def call_by_station(self, *, owner: str | list[str] | None = None,
                        since: float | None = None,
                        until: float | None = None) -> list[dict[str, Any]]:
        """Те же счётчики, но в разрезе станций — для сравнения АТС между собой."""
        части, args = self._условия_звонков(owner, "", since, until)
        где = f" WHERE {' AND '.join(части)}" if части else ""
        rows = self.query(
            "SELECT COALESCE(c.station,'') AS station, "
            + ", ".join(self.СЧЁТ_ЗВОНКОВ)
            + f" FROM calls c{где} GROUP BY c.station ORDER BY total DESC", args)
        return [{"station": str(r["station"] or ""),
                 **{к: int(r[к] or 0) for к in
                    ("total", "inbound", "outbound", "internal", "answered",
                     "queued", "skipped", "talk_s", "wait_s")}}
                for r in rows]

    def call_tops(self, field: str, *, owner: str | list[str] | None = None,
                  station: str = "", since: float | None = None,
                  until: float | None = None, limit: int = 15) -> list[dict[str, Any]]:
        """Топ по очереди, оператору, направлению, причине пропуска или номеру.

        Поле не подставляется в запрос как есть: имена колонок в SQL нельзя
        передать параметром, а значит любая опечатка снаружи стала бы
        внедрением. Разрешён только известный список.
        """
        колонки = {"queue": "c.queue", "agent": "c.agent",
                   "direction": "c.direction", "skipped": "c.skipped",
                   "src": "c.src", "dst": "c.dst", "context": "c.context",
                   "disposition": "c.disposition", "station": "c.station"}
        колонка = колонки.get(str(field))
        if колонка is None:
            raise ValueError(f"Неизвестный разрез звонков: {field}")
        части, args = self._условия_звонков(owner, station, since, until)
        части.append(f"COALESCE({колонка},'')<>''")
        if field == "skipped":
            # Отложенные — не причина пропуска, а «ещё подождём»: смешивать
            # их с «нет записи» значит каждый раз объяснять человеку, почему
            # в причинах лидирует строка, которая сама рассосётся.
            части.append(f"{колонка} NOT LIKE 'отложен: %'")
        rows = self.query(
            f"SELECT {колонка} AS value, COUNT(*) AS n, "
            "       SUM(COALESCE(c.billsec,0)) AS talk_s, "
            # Среднее — только по отвеченным: недозвон длится ноль секунд,
            # и в знаменателе он занижает «средний разговор» ровно на свою
            # долю. Соседняя гистограмма длительностей считает так же.
            "       AVG(CASE WHEN c.answered=1 THEN COALESCE(c.billsec,0) END) AS avg_s, "
            "       SUM(CASE WHEN c.answered=1 THEN 1 ELSE 0 END) AS answered "
            f"FROM calls c WHERE {' AND '.join(части)} "
            f"GROUP BY {колонка} ORDER BY n DESC LIMIT {int(limit)}", args)
        return [{"value": str(r["value"]), "count": int(r["n"] or 0),
                 "talk_s": int(r["talk_s"] or 0),
                 "avg_s": round(float(r["avg_s"] or 0), 1),
                 "answered": int(r["answered"] or 0)} for r in rows]

    def call_load_heatmap(self, *, owner: str | list[str] | None = None,
                          station: str = "", since: float | None = None,
                          until: float | None = None) -> dict[str, Any]:
        """День недели × час: когда звонят. Основание для графика смен.

        Время местное — то же, в котором человек смотрит на расписание.
        SQLite переводит эпоху в местное время сам (`localtime`), и делать
        это в Python значило бы тащить сюда все звонки поимённо.
        """
        части, args = self._условия_звонков(owner, station, since, until)
        части.append("c.started_at>0")
        rows = self.query(
            "SELECT CAST(strftime('%w', c.started_at, 'unixepoch', 'localtime') AS INTEGER) AS dow,"
            "       CAST(strftime('%H', c.started_at, 'unixepoch', 'localtime') AS INTEGER) AS hour,"
            "       COUNT(*) AS n, SUM(COALESCE(c.billsec,0)) AS talk_s "
            f"FROM calls c WHERE {' AND '.join(части)} "
            "GROUP BY dow, hour", args)
        # В SQLite неделя начинается с воскресенья; у нас — с понедельника.
        сетка = [[0] * 24 for _ in range(7)]
        разговор = [[0] * 24 for _ in range(7)]
        for r in rows:
            день = (int(r["dow"] or 0) + 6) % 7
            час = max(0, min(23, int(r["hour"] or 0)))
            сетка[день][час] = int(r["n"] or 0)
            разговор[день][час] = int(r["talk_s"] or 0)
        return {"days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
                "hours": list(range(24)), "calls": сетка, "talk_s": разговор}

    #: Границы корзин длительности, секунды. До полминуты — «не разговор»,
    #: дальше шаг растёт: разницу между 20 и 25 минутами никто не смотрит.
    КОРЗИНЫ_ДЛИТЕЛЬНОСТИ = (30, 60, 120, 300, 600, 1200, 1800)

    def call_duration_histogram(self, *, owner: str | list[str] | None = None,
                                station: str = "", since: float | None = None,
                                until: float | None = None) -> list[dict[str, Any]]:
        """Сколько звонков какой длины. Среднее без этого обманывает.

        Средняя длительность в три минуты бывает и у равномерных разговоров,
        и у сотни тридцатисекундных плюс десятка получасовых — а это разные
        организации с разными проблемами.
        """
        части, args = self._условия_звонков(owner, station, since, until)
        части.append("c.answered=1")
        границы = self.КОРЗИНЫ_ДЛИТЕЛЬНОСТИ
        случаи = " ".join(
            f"WHEN COALESCE(c.billsec,0) < {г} THEN {номер}"
            for номер, г in enumerate(границы))
        rows = self.query(
            f"SELECT CASE {случаи} ELSE {len(границы)} END AS корзина,"
            "        COUNT(*) AS n, SUM(COALESCE(c.billsec,0)) AS talk_s "
            f"FROM calls c WHERE {' AND '.join(части)} "
            "GROUP BY корзина ORDER BY корзина", args)
        имена = []
        предыдущая = 0
        for г in границы:
            имена.append(f"{предыдущая // 60 if предыдущая >= 60 else предыдущая}"
                         f"{'м' if предыдущая >= 60 else 'с'}–"
                         f"{г // 60 if г >= 60 else г}{'м' if г >= 60 else 'с'}")
            предыдущая = г
        имена.append(f"дольше {границы[-1] // 60}м")
        по_корзинам = {int(r["корзина"]): (int(r["n"] or 0), int(r["talk_s"] or 0))
                       for r in rows}
        # Границы корзины в секундах: нижняя входит, верхняя нет; у первой
        # нижней нет, у последней — верхней. По ним столбец раскрывается в
        # список звонков тем же условием, которым посчитан.
        края = (None, *границы, None)
        return [{"label": имя,
                 "from": края[н], "to": края[н + 1],
                 "count": по_корзинам.get(н, (0, 0))[0],
                 "talk_s": по_корзинам.get(н, (0, 0))[1]}
                for н, имя in enumerate(имена)]

    def forget_calls(self, *, before: float, file_keys_before: float | None = None) -> int:
        """Убирает старые строки журнала звонков — те, чьих заданий уже нет.

        Сама строка занимает сотни байт, но за год их набегает миллион, и
        обход папки записей начинает читать это множество целиком. Срок
        хранения тот же, что у заданий.

        Звонок с живым заданием не трогается. Срок звонка считается от
        импорта, а срок задания — от окончания, и задание, простоявшее в
        очереди, теряло свой звонок раньше себя: разрезы по оператору,
        очереди и станции давали «—», а импорт из каталога записей больше не
        узнавал файл и заводил его заново.

        `file_keys_before` — свой, более ранний срок для строк, заведённых
        обходом каталога (ключ `file:…`): только по ним импорт узнаёт уже
        распознанный файл, и строка, забытая раньше, чем файл выходит из
        глубины просмотра станции, означает повторное распознавание записи.
        """
        граница_файлов = float(before) if file_keys_before is None \
            else min(float(before), float(file_keys_before))
        # События очередей и переводов — тем же сроком: в них номера звонков.
        self.execute("DELETE FROM queue_events WHERE ts < ?", (float(before),))
        self.execute("DELETE FROM call_transfers WHERE ts < ?", (float(before),))
        self.execute("DELETE FROM bridge_events WHERE ts < ?", (float(before),))
        return self.execute(
            "DELETE FROM calls WHERE imported_at < ? "
            "  AND (job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs)) "
            "  AND (instr(uniqueid, 'file:') = 0 OR imported_at < ?)",
            (float(before), граница_файлов))
