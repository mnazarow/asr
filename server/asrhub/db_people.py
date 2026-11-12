"""Часть базы о людях: справочник сотрудников, агенты на станциях и
обратная запись в CRM.

Вынесено из `db.py` примесью к `Database`.
"""
from __future__ import annotations

import time
from typing import Any

from .db_util import ОсноваБазы, _экранировать_like, new_id, now


class ЛюдиБазы(ОсноваБазы):
    """Примесь к `Database`: сотрудники, агенты, CRM."""

    # --- сотрудники -------------------------------------------------------

    #: Поля карточки сотрудника, которые приходят снаружи. Список закрытый:
    #: он же — белый список колонок для записи, и через него нельзя
    #: дописать в таблицу ничего постороннего.
    ПОЛЯ_СОТРУДНИКА = (
        "external_id", "source", "active", "active_until",
        "last_name", "first_name", "middle_name", "position", "department",
        "phone_work", "phone_ext", "phone_mobile", "email", "suppliers",
        "note", "owner",
    )

    def employee_save(self, данные: dict[str, Any], *,
                      id: str | None = None) -> dict[str, Any]:       # noqa: A002
        """Заводит или обновляет карточку сотрудника; возвращает её целиком.

        Ключ поиска — сначала явный `id`, потом пара «источник и внешний
        ключ». Вторая нужна импорту: справочник отдаёт одних и тех же людей
        каждый раз, и без неё повторный импорт заводил бы отдел заново.
        """
        поля = {к: данные.get(к) for к in self.ПОЛЯ_СОТРУДНИКА if к in данные}
        поля.setdefault("source", "ручной")
        if "active" in поля:
            поля["active"] = 1 if поля["active"] else 0
        существующий = None
        if id:
            существующий = self.employee_get(str(id))
        if существующий is None and поля.get("external_id"):
            найден = self.query_one(
                "SELECT * FROM employees WHERE source=? AND external_id=?",
                (str(поля.get("source") or "ручной"), str(поля["external_id"])))
            существующий = dict(найден) if найден is not None else None
            существующий = dict(существующий) if существующий else None
        мгновение = now()
        if существующий is None:
            новый = new_id("emp")
            колонки = ["id", *поля.keys(), "created_at", "updated_at"]
            значения = [новый, *поля.values(), мгновение, мгновение]
            self.execute(
                f"INSERT INTO employees ({', '.join(колонки)}) "
                f"VALUES ({', '.join('?' * len(колонки))})", значения)
            return self.employee_get(новый) or {}
        if not поля:
            return существующий
        назначения = ", ".join(f"{к}=?" for к in поля)
        self.execute(f"UPDATE employees SET {назначения}, updated_at=? WHERE id=?",
                     [*поля.values(), мгновение, существующий["id"]])
        return self.employee_get(str(существующий["id"])) or {}

    def employee_get(self, id: str) -> dict[str, Any] | None:         # noqa: A002
        row = self.query_one("SELECT * FROM employees WHERE id=?", (str(id),))
        return dict(row) if row else None

    def employee_by_key(self, source: str, external_id: str) -> dict[str, Any] | None:
        """Карточка по паре «источник и внешний ключ» — так её узнаёт импорт."""
        if not external_id:
            return None
        row = self.query_one(
            "SELECT * FROM employees WHERE source=? AND external_id=?",
            (str(source or "ручной"), str(external_id)))
        return dict(row) if row else None

    def employee_by_ext(self, номер: str) -> dict[str, Any] | None:
        """Сотрудник по внутреннему номеру — так звонок узнаёт оператора."""
        номер = str(номер or "").strip()
        if not номер:
            return None
        row = self.query_one(
            "SELECT * FROM employees WHERE phone_ext=? "
            "ORDER BY active DESC, updated_at DESC, id DESC", (номер,))
        return dict(row) if row else None

    #: Внутренние номера, чья главная карточка — в этом отделе (один `?`).
    #:
    #: Номер бывает у двоих: общий номер отдела, номер, переданный новому
    #: человеку при старой карточке уволенного. Отчёты подписывают номер
    #: одним человеком — работающим раньше уволенного, свежей карточкой
    #: раньше старой, при равенстве — последней заведённой
    #: (`employees_by_ext`), — и отдел номера — отдел этого человека.
    #: Отбор по отделу обязан смотреть туда же. Прежнее «есть карточка с
    #: этим номером в отделе» относило номер сразу к двум отделам: таблица
    #: «По отделам» показывала его разговоры у одного, а отбор и список под
    #: числом — у обоих, подписанные чужим отделом. Подзапрос не зависит от
    #: строки звонка: база считает его один раз на запрос.
    ДОБАВОЧНЫЕ_ОТДЕЛА = (
        "SELECT e.phone_ext FROM employees e WHERE e.department = ? "
        "AND COALESCE(e.phone_ext,'') <> '' AND NOT EXISTS ("
        "SELECT 1 FROM employees b WHERE b.phone_ext = e.phone_ext AND b.id <> e.id AND ("
        "COALESCE(b.active,0) > COALESCE(e.active,0) OR (COALESCE(b.active,0) = COALESCE(e.active,0) "
        "AND (COALESCE(b.updated_at,0) > COALESCE(e.updated_at,0) "
        "OR (COALESCE(b.updated_at,0) = COALESCE(e.updated_at,0) AND b.id > e.id)))))")
    #: Разговор отдела: у задания строка звонка с добавочным отдела.
    ОТДЕЛ_ЗАДАНИЯ = ("EXISTS (SELECT 1 FROM calls cl WHERE cl.job_id = j.id "
                     f"AND cl.agent IN ({ДОБАВОЧНЫЕ_ОТДЕЛА}))")
    #: Звонок отдела: оператор звонка — добавочный отдела.
    ОТДЕЛ_ЗВОНКА = f"cl.agent IN ({ДОБАВОЧНЫЕ_ОТДЕЛА})"

    def employees_by_ext(self, номера: Any) -> dict[str, dict[str, Any]]:
        """Сотрудники по внутренним номерам — разом, для таблиц отчётов.

        Отчёт показывает сотни строк, и спрашивать справочник по строке —
        сотни запросов. Здесь один запрос на пятьсот номеров. На номер,
        который делят двое, — тот же выбор, что у `employee_by_ext`:
        работающий раньше уволенного, свежая карточка раньше старой.
        """
        список = sorted({str(н).strip() for н in (номера or []) if str(н or "").strip()})
        итог: dict[str, dict[str, Any]] = {}
        for начало in range(0, len(список), 500):
            кусок = список[начало:начало + 500]
            строки = self.query(
                "SELECT phone_ext, last_name, first_name, middle_name, department, "
                "       position, active FROM employees "
                f"WHERE phone_ext IN ({','.join('?' for _ in кусок)}) "
                # Последней пишется лучшая — она и остаётся в словаре. Порядок
                # полный, до ключа карточки: при равном времени правки выбор
                # не должен зависеть от того, как база отдала строки, — по
                # нему же отбор «отдел» относит номер к отделу
                # (`ДОБАВОЧНЫЕ_ОТДЕЛА`).
                "ORDER BY active ASC, updated_at ASC, id ASC", кусок)
            for r in строки:
                имя = " ".join(str(r[поле] or "").strip() for поле in
                               ("last_name", "first_name", "middle_name")).strip()
                итог[str(r["phone_ext"])] = {
                    "name": " ".join(имя.split()), "department": str(r["department"] or ""),
                    "position": str(r["position"] or ""), "active": bool(r["active"])}
        return итог

    def agents_for_jobs(self, ids: Any) -> dict[str, str]:
        """Добавочный оператора по заданию — из журнала звонков, разом.

        Строка звонка у задания одна (у разговора частями — у каждой части
        своя); если их всё же две, берётся первая с непустым оператором.
        Ищется по указателю `idx_calls_job_agent` без чтения строк звонков.
        """
        список = sorted({str(i) for i in (ids or []) if i})
        итог: dict[str, str] = {}
        for начало in range(0, len(список), 500):
            кусок = список[начало:начало + 500]
            for r in self.query(
                    "SELECT job_id, agent FROM calls "
                    f"WHERE job_id IN ({','.join('?' for _ in кусок)}) "
                    "AND COALESCE(agent,'') <> ''", кусок):
                итог.setdefault(str(r["job_id"]), str(r["agent"]))
        return итог

    def employee_list(self, *, query: str = "", department: str = "",
                      active: bool | None = None, source: str = "",
                      limit: int = 500, offset: int = 0) -> dict[str, Any]:
        условия: list[str] = []
        параметры: list[Any] = []
        if query:
            # «_» и «%» — буквы запроса («ivan_petrov@…»), а не образцы LIKE.
            искомое = f"%{_экранировать_like(query.strip().lower())}%"
            условия.append(
                "(LOWER(last_name) LIKE ? ESCAPE '\\' OR LOWER(first_name) LIKE ? ESCAPE '\\' OR "
                " LOWER(middle_name) LIKE ? ESCAPE '\\' OR LOWER(position) LIKE ? ESCAPE '\\' OR "
                " LOWER(department) LIKE ? ESCAPE '\\' OR LOWER(email) LIKE ? ESCAPE '\\' OR "
                " phone_ext LIKE ? ESCAPE '\\' OR phone_mobile LIKE ? ESCAPE '\\' OR "
                " phone_work LIKE ? ESCAPE '\\')")
            параметры.extend([искомое] * 9)
        if department:
            условия.append("department=?")
            параметры.append(department)
        if source:
            условия.append("source=?")
            параметры.append(source)
        if active is not None:
            условия.append("active=?")
            параметры.append(1 if active else 0)
        где = (" WHERE " + " AND ".join(условия)) if условия else ""
        всего = self.query_one(f"SELECT COUNT(*) AS n FROM employees{где}", параметры)
        rows = self.query(
            f"SELECT * FROM employees{где} ORDER BY active DESC, last_name, first_name, id "
            f"LIMIT ? OFFSET ?", [*параметры, max(1, int(limit)), max(0, int(offset))])
        return {"total": int(всего["n"]) if всего else 0,
                "items": [dict(r) for r in rows]}

    def employee_delete(self, id: str) -> bool:                       # noqa: A002
        return self.execute("DELETE FROM employees WHERE id=?", (str(id),)) > 0

    def employee_departments(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT department, COUNT(*) AS n, "
            "       SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active "
            "FROM employees GROUP BY department ORDER BY n DESC")
        return [dict(r) for r in rows]

    #: Сколько имён в каждом перечне «что изменилось» отдаёт импорт.
    ИМЁН_В_ИТОГЕ_ИМПОРТА = 50

    def employees_import(self, записи: list[dict[str, Any]], *, source: str,
                         deactivate_missing: bool = True,
                         dry_run: bool = False,
                         предел_увольнений: tuple[float, int] | None = None
                         ) -> dict[str, Any]:
        """Разом заводит выгрузку справочника. Возвращает, что изменилось.

        Пропавших из выгрузки не удаляем, а помечаем неработающими: по ним
        есть звонки, и удаление превратило бы половину архива в разговоры
        неизвестно с кем. Это же и честнее: человек уволился, а разговоры
        остались.

        `dry_run` — посчитать то же самое, ничего не записав: так работает
        предпросмотр импорта. Перечни `changes` — кого добавить, у кого
        какие поля поменять, кого отметить уволенным (по полсотни имён).
        """
        было = {str(r["external_id"]): dict(r) for r in self.query(
            "SELECT * FROM employees WHERE source=?", (source,)) if r["external_id"]}
        добавлено = обновлено = без_изменений = сменились = 0
        пришли: set[str] = set()
        предел = self.ИМЁН_В_ИТОГЕ_ИМПОРТА
        перечни: dict[str, list[dict[str, Any]]] = {
            "added": [], "updated": [], "deactivated": [], "replaced": []}
        # Предохранитель (`предел_увольнений` — доля и порог «без вопросов»):
        # оборванная или кривая выгрузка «увольняла» остаток штата молча.
        заблокировано = 0
        if deactivate_missing and предел_увольнений is not None:
            ключи = {str(з.get("external_id") or "").strip() for з in записи} - {""}
            работают = [к for к, п in было.items() if int(п.get("active") or 0)]
            пропадут = sum(1 for к in работают if к not in ключи)
            доля, без_вопросов = предел_увольнений
            if пропадут > max(int(без_вопросов), доля * len(работают)):
                deactivate_missing = False
                заблокировано = пропадут

        def имя_части(карточка: dict[str, Any], поле: str) -> str:
            return str(карточка.get(поле) or "").strip().lower().replace("ё", "е")

        def другой_человек(прежний: dict[str, Any], данные: dict[str, Any]) -> bool:
            """Тот же ключ-номер, но фамилия И имя другие — на месте новый человек.

            Ключ карточки — внутренний номер, а номер уволенного отдают
            тому, кто сел на его место. Импорт находил карточку по номеру и
            переписывал её: прежний человек исчезал из справочника, его
            примечание переезжало к новому, а сверка приписывала новому
            звонки прежнего. Смена одной фамилии (замужество) или
            исправленное имя — тот же человек; обе сразу — другой.
            """
            return all(имя_части(прежний, п) and имя_части(данные, п)
                       and имя_части(прежний, п) != имя_части(данные, п)
                       for п in ("last_name", "first_name"))

        def кто(карточка: dict[str, Any]) -> dict[str, Any]:
            return {"name": " ".join(str(карточка.get(п) or "").strip() for п in
                                     ("last_name", "first_name", "middle_name")).strip(),
                    "phone_ext": str(карточка.get("phone_ext") or ""),
                    "department": str(карточка.get("department") or "")}

        for запись in записи:
            данные = {к: з for к, з in запись.items() if к in self.ПОЛЯ_СОТРУДНИКА}
            данные["source"] = source
            ключ = str(данные.get("external_id") or "").strip()
            if not ключ:
                continue
            пришли.add(ключ)
            прежний = было.get(ключ)
            if прежний is None:
                if not dry_run:
                    self.employee_save(данные)
                добавлено += 1
                if len(перечни["added"]) < предел:
                    перечни["added"].append(кто(данные))
                continue
            if другой_человек(прежний, данные):
                # Прежнюю карточку не трогаем по содержанию: она уходит в
                # неработающие с датой и с ключом, которого импорт больше не
                # встретит, а новый человек получает свою карточку.
                if not dry_run:
                    self.execute(
                        "UPDATE employees SET active=0, external_id=?, active_until=CASE "
                        "WHEN COALESCE(active_until,'')='' THEN ? ELSE active_until END, "
                        "updated_at=? WHERE id=?",
                        (f"{ключ}~{прежний['id']}",
                         time.strftime("%Y-%m-%d"), now(), прежний["id"]))
                    self.employee_save(данные)
                сменились += 1
                добавлено += 1
                if len(перечни["replaced"]) < предел:
                    перечни["replaced"].append({**кто(данные), "previous": кто(прежний)["name"]})
                continue
            различия = {к: з for к, з in данные.items()
                        if str(прежний.get(к) or "") != str(з or "")}
            if not различия:
                без_изменений += 1
                continue
            if not dry_run:
                self.employee_save(данные, id=str(прежний["id"]))
            обновлено += 1
            if len(перечни["updated"]) < предел:
                перечни["updated"].append({**кто(данные), "fields": sorted(различия)})
        уволено = 0
        if deactivate_missing:
            for ключ, прежний in было.items():
                if ключ not in пришли and int(прежний.get("active") or 0):
                    if not dry_run:
                        self.execute("UPDATE employees SET active=0, updated_at=? WHERE id=?",
                                     (now(), прежний["id"]))
                    уволено += 1
                    if len(перечни["deactivated"]) < предел:
                        перечни["deactivated"].append(кто(прежний))
        return {"added": добавлено, "updated": обновлено, "unchanged": без_изменений,
                "deactivated": уволено, "replaced": сменились, "received": len(пришли),
                "deactivation_blocked": заблокировано, "changes": перечни}

    # --- агенты на станциях ------------------------------------------------

    ПОЛЯ_АГЕНТА = ("name", "host", "station", "version", "asterisk", "os",
                   "source", "state", "enabled", "owner")

    def agent_save(self, id: str, данные: dict[str, Any] | None = None,  # noqa: A002
                   *, seen: bool = True) -> dict[str, Any]:
        """Заводит или обновляет агента, приславшего о себе весть."""
        поля = {к: з for к, з in (данные or {}).items() if к in self.ПОЛЯ_АГЕНТА}
        if "enabled" in поля:
            поля["enabled"] = 1 if поля["enabled"] else 0
        мгновение = now()
        прежний = self.agent_get(id)
        if прежний is None:
            колонки = ["id", *поля.keys(), "first_seen", "last_seen"]
            значения = [str(id), *поля.values(), мгновение, мгновение]
            self.execute(
                f"INSERT INTO agents ({', '.join(колонки)}) "
                f"VALUES ({', '.join('?' * len(колонки))})", значения)
            return self.agent_get(id) or {}
        назначения = [f"{к}=?" for к in поля]
        параметры = list(поля.values())
        if seen:
            назначения.append("last_seen=?")
            параметры.append(мгновение)
        if назначения:
            self.execute(f"UPDATE agents SET {', '.join(назначения)} WHERE id=?",
                         [*параметры, str(id)])
        return self.agent_get(id) or {}

    def agent_get(self, id: str) -> dict[str, Any] | None:            # noqa: A002
        row = self.query_one("SELECT * FROM agents WHERE id=?", (str(id),))
        return dict(row) if row else None

    def agent_list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM agents ORDER BY last_seen DESC")]

    def agent_delete(self, id: str) -> bool:                          # noqa: A002
        return self.execute("DELETE FROM agents WHERE id=?", (str(id),)) > 0

    def agent_count(self, id: str, *, calls: int = 0, files: int = 0,  # noqa: A002
                    size: int = 0, errors: int = 0, error: str = "") -> None:
        """Счётчики агента. Пишутся одним запросом: их шлют часто."""
        self.execute(
            "UPDATE agents SET calls_sent=calls_sent+?, files_sent=files_sent+?, "
            "bytes_sent=bytes_sent+?, errors=errors+?, "
            "last_error=CASE WHEN ?<>'' THEN ? ELSE last_error END, last_seen=? "
            "WHERE id=?",
            (int(calls), int(files), int(size), int(errors), error, error, now(), str(id)))

    def agent_command_set(self, id: str, команда: str) -> bool:       # noqa: A002
        return self.execute("UPDATE agents SET command=?, command_at=? WHERE id=?",
                            (команда, now(), str(id))) > 0

    def agent_command_take(self, id: str) -> str:                     # noqa: A002
        """Отдаёт команду агенту и тут же её снимает — чтобы не повторялась.

        Чтение и снятие — одним действием под замком записи. Раньше это
        были два шага, и между ними успевал вклиниться второй заход того же
        агента: агент опрашивает сервер по расписанию, а сеть иногда
        задваивает запрос — обе стороны читали «собрать всё» и обе его
        выполняли. Сбор архива запускался дважды, шёл по журналу с одной
        позиции и грузил станцию вдвое. Проба на восьми одновременных
        обращениях ловила повтор примерно в каждом двадцатом заходе.
        """
        with self.write() as conn:
            строка = conn.execute(
                "SELECT command FROM agents WHERE id=?", (str(id),)).fetchone()
            команда = str((строка["command"] if строка else "") or "")
            if команда:
                conn.execute("UPDATE agents SET command='' WHERE id=?", (str(id),))
        return команда

    # --- обратная запись в CRM ----------------------------------------------

    #: Состояния обратной записи по заданию. Пусто — ещё не решали.
    CRM_ЖДЁТ_МОДЕЛЬ = "ждёт модель"
    CRM_ОТПРАВЛЯЕТСЯ = "отправляется"
    CRM_ОТПРАВЛЕНО = "отправлено"
    CRM_БЕЗ_СДЕЛКИ = "без сделки"

    def crm_claim(self, job_id: str) -> bool:
        """Занимает отправку примечания по заданию; False — уже отправляли.

        Одним UPDATE с условием: два пути, дошедшие до отправки разом
        (разбор модели закончился, пока шла досылка по сроку, или два
        сервера на общей базе), не отправят примечание дважды — дубль в
        чужой ленте заметнее и неприятнее пропуска.
        """
        return self.execute(
            "UPDATE jobs SET crm_status=?, crm_at=? WHERE id=? "
            "AND COALESCE(crm_status,'') IN ('', ?, ?)",
            (self.CRM_ОТПРАВЛЯЕТСЯ, now(), str(job_id),
             self.CRM_ЖДЁТ_МОДЕЛЬ, self.CRM_БЕЗ_СДЕЛКИ)) > 0

    def crm_mark(self, job_id: str, status: str, *, только_если: str | None = None) -> bool:
        """Ставит состояние обратной записи. `updated_at` задания не трогает:
        отметка о CRM — не правка записи, и списки «недавно изменённых» от
        неё не должны перестраиваться."""
        if только_если is None:
            return self.execute("UPDATE jobs SET crm_status=?, crm_at=? WHERE id=?",
                                (str(status)[:300], now(), str(job_id))) > 0
        return self.execute(
            "UPDATE jobs SET crm_status=?, crm_at=? WHERE id=? AND COALESCE(crm_status,'')=?",
            (str(status)[:300], now(), str(job_id), только_если)) > 0

    def crm_waiting(self, limit: int = 50) -> list[dict[str, Any]]:
        """Задания, чьё примечание ждёт смыслового разбора, — со строкой очереди."""
        строки = self.query(
            "SELECT j.id AS id, j.crm_at AS crm_at, q.state AS llm_state "
            "FROM jobs j LEFT JOIN llm_queue q ON q.job_id=j.id "
            "WHERE j.crm_status=? ORDER BY j.crm_at LIMIT ?",
            (self.CRM_ЖДЁТ_МОДЕЛЬ, max(1, int(limit))))
        return [dict(с) for с in строки]
