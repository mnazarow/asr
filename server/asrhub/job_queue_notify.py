"""Уведомления очереди: обратный вызов по готовности задания, его заходы
доставки и досылка после сбоя или перезапуска.

Вынесено из `job_queue.py` примесью к `JobQueue`. Уведомление —
состояние задания (`webhook_status`), а не разовая попытка; паузы между
заходами и сроки — здесь же.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import settings_access as S
from .db_util import now
from .errors import ConfigError
from .job_status import EMPTY_STATUSES
from .logging_setup import get_logger
from .monitoring.collector import RUNTIME
from .outbound import check_outbound_url, открыватель_наружу, узел_на_паузе, узел_ответил

if TYPE_CHECKING:
    from .db import Database

log = get_logger("queue")

#: Паузы между заходами доставки уведомления, секунд. Уведомление — это
#: состояние задания (`webhook_status`), а не разовая попытка: заход —
#: несколько быстрых попыток подряд (на мгновенный сбой сети), а неудачный
#: заход переносится на следующую паузу из этого списка — их досылает
#: служебный цикл, в том числе после перезапуска. Прежде всё укладывалось в
#: пять попыток за пятнадцать секунд, и минутная недоступность приёмника,
#: остановка сервера посреди доставки или отказ instance_lost означали, что
#: результат не придёт никогда: интеграция phone_asr ждёт обратный вызов как
#: единственный канал результата. Последняя пауза — через сутки.
ПАУЗЫ_УВЕДОМЛЕНИЯ = (60.0, 300.0, 900.0, 1800.0, 3600.0, 3 * 3600.0, 6 * 3600.0,
                     12 * 3600.0, 24 * 3600.0)
#: Попыток в одном заходе доставки и паузы между ними.
ПОПЫТОК_ЗА_ЗАХОД = 3
#: На сколько секунд заход доставки «берёт» уведомление себе. Дольше —
#: значит, процесс умер посреди доставки, и уведомление снова ждёт.
СРОК_ЗАХОДА_ДОСТАВКИ_С = 300.0


class УведомленияОчереди:
    """Примесь к `JobQueue`: доставка обратных вызовов."""

    if TYPE_CHECKING:
        db: Database
        settings: Any
        on_event: Callable[[str, dict[str, Any]], None] | None
        _lock: threading.RLock
        _stop: threading.Event
        _owners: dict[str, str]
        _webhooks: Any

        def _прежний_путь(self, job_id: str) -> Path: ...

    # --- уведомления -----------------------------------------------------

    def _уведомление_ждёт(self, job: dict[str, Any], *,
                          кроме_отката: bool = False) -> dict[str, Any]:
        """Поля «уведомление ждёт доставки» — к той же записи, что и исход задания.

        Пишутся вместе с исходом: процесс, упавший сразу после записи «готово»
        или «ошибка», иначе не оставлял следа, что уведомление должно было
        уйти. `кроме_отката` — для исхода, за которым последует возврат
        прежнего результата (повтор готового задания): такой откат — не итог
        для заказчика, и уведомление по нему не уходит, как и прежде.
        """
        if not job.get("webhook_url"):
            return {}
        job_id = str(job.get("id") or "")
        if кроме_отката and job_id and self._прежний_путь(job_id).is_dir():
            return {}
        return {"webhook_status": "pending", "webhook_attempts": 0,
                "webhook_next_at": now()}

    def _send_webhook(self, job_id: str) -> None:
        """Заводит уведомление об исходе задания и сразу пробует его доставить.

        Уведомление — состояние задания: «pending» ждёт доставки, «sending» —
        доставка идёт, «ok:<код>», «failed» и «blocked» — итог. Не дошедшее
        с первого захода досылает служебный цикл (`_дослать_уведомления`) с
        растущей паузой, в том числе после перезапуска сервера.
        """
        job = self.db.get_job(job_id)
        if not job or not job.get("webhook_url"):
            return
        if str(job.get("webhook_status") or "") not in ("pending", "sending"):
            self.db.update_job(job_id, webhook_status="pending", webhook_attempts=0,
                               webhook_next_at=now())
        if self._stop.is_set():
            # После остановки пул заводить нельзя: `stop` уже обнулил ссылку
            # на него, и новый оказался бы непогашенным — его потоки не
            # демоны и держали бы выход из процесса. Уведомление не теряется:
            # оно ждёт в базе и уйдёт после запуска.
            log.info("Уведомление для %s уйдёт после запуска: очередь остановлена",
                     job_id, extra={"job_id": job_id})
            return
        self._webhook_pool().submit(self._доставить, job_id)

    def _дослать_уведомления(self) -> int:
        """Уведомления, которым пора: ждущие и брошенные посреди доставки."""
        if self._stop.is_set():
            return 0
        try:
            номера = self.db.webhook_due(limit=max(1, S.integer(self.settings,
                                                                "webhook_workers", 4)) * 5)
        except Exception as exc:                            # noqa: BLE001
            log.debug("Ждущие уведомления не перечислены: %s", exc)
            return 0
        if not номера:
            return 0
        пул = self._webhook_pool()
        for job_id in номера:
            пул.submit(self._доставить, job_id)
        return len(номера)

    def _доставить(self, job_id: str) -> None:
        """Один заход доставки — если его не взял себе другой поток или сервер.

        Исключения задач пула никто не читал: сбой доставки исчезал без
        следа. Теперь он в журнале, а уведомление остаётся ждать.
        """
        try:
            if not self.db.webhook_claim(job_id, hold_s=СРОК_ЗАХОДА_ДОСТАВКИ_С):
                return
            job = self.db.get_job(job_id)
            if not job or not job.get("webhook_url"):
                return
            self._webhook_worker(job)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Доставка уведомления для %s дала сбой: %s", job_id, exc,
                        extra={"job_id": job_id})

    def _отложить_уведомление(self, job: dict[str, Any], причина: str, *,
                              без_попытки: bool = False, до: float | None = None) -> None:
        """Неудачный заход доставки — на следующую паузу, после последней — «failed»."""
        job_id = str(job["id"])
        if без_попытки:
            self.db.update_job(job_id, webhook_status="pending",
                               webhook_next_at=до if до else now())
            return
        строка = self.db.get_job(job_id) or {}
        попыток = int(строка.get("webhook_attempts") or 0) + 1
        if попыток > len(ПАУЗЫ_УВЕДОМЛЕНИЯ):
            self.db.update_job(job_id, webhook_status="failed", webhook_attempts=попыток,
                               webhook_next_at=None)
            RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})
            log.warning("Уведомление для %s так и не доставлено после %d заходов: %s",
                        job_id, попыток, причина, extra={"job_id": job_id})
            return
        пауза = ПАУЗЫ_УВЕДОМЛЕНИЯ[попыток - 1]
        self.db.update_job(job_id, webhook_status="pending", webhook_attempts=попыток,
                           webhook_next_at=now() + пауза)
        log.info("Уведомление для %s не доставлено (%s) — следующий заход через %.0f с",
                 job_id, причина, пауза, extra={"job_id": job_id})

    def _webhook_pool(self) -> Any:
        """Ограниченный пул для доставки уведомлений.

        Создаётся при первой доставке, чтобы не держать потоки на серверах,
        где уведомления не используются.
        """
        with self._lock:
            if self._webhooks is None:
                from concurrent.futures import ThreadPoolExecutor

                size = max(1, int(self.settings.get("webhook_workers") or 4))
                self._webhooks = ThreadPoolExecutor(
                    max_workers=size, thread_name_prefix="asrhub-webhook")
                log.debug("Пул доставки уведомлений: %d потоков", size)
            return self._webhooks

    def _webhook_worker(self, job: dict[str, Any]) -> None:
        import json as json_mod

        # Задание, принятое по схеме phone_asr, и отвечать должно её телом:
        # приёмник на той стороне разбирает результат по своей схеме и на
        # наши поля не рассчитан. Признак — блок _phone в параметрах задания,
        # который кладёт маршрут /process-call.
        call = (job.get("params") or {}).get("_phone")
        if call:
            payload = json_mod.dumps(self._phone_callback_body(job, call),
                                     ensure_ascii=False).encode("utf-8")
            self._deliver_webhook(job, payload)
            return

        пустой = job["status"] in EMPTY_STATUSES
        body: dict[str, Any] = {
            "id": job["id"], "status": job["status"], "filename": job.get("filename"),
            "model": job.get("model"), "rtf": job.get("rtf"),
            "duration_s": job.get("media_duration_s"),
            "words": job.get("words_count"),
            # У записи без речи и короткой причина — пояснение к итогу, а не
            # ошибка: приёмник, который считает отказы по полю `error`, не
            # должен записывать их в отказы.
            "error": None if пустой else job.get("error_message"),
        }
        if пустой:
            body["reason"] = job.get("error_message")
        # Огибающая в уведомлении — совместимость с приёмниками phone_asr,
        # которые ждут её полем `waveforms`. По умолчанию выключено: тело
        # уведомления задумано коротким, а повторов доставки до пяти.
        if self.settings.get("webhook_waveform") and job.get("waveform"):
            from .pipeline.waveform import to_phone_asr

            body["waveforms"] = to_phone_asr(job["waveform"])
        payload = json_mod.dumps(body, ensure_ascii=False).encode("utf-8")

        self._deliver_webhook(job, payload)

    def _phone_callback_body(self, job: dict[str, Any],
                             call: dict[str, Any]) -> dict[str, Any]:
        """Тело обратного вызова в схеме phone_asr."""
        from .phone_compat import PhoneRequest, callback_body

        request = PhoneRequest(
            call_id=str(call.get("call_id") or ""), files=[],
            base_url="", part=int(call.get("part") or 1),
            total_parts=int(call.get("total_parts") or 1),
            swap_sides=bool(call.get("swap_sides")))
        segments = self.db.get_segments(job["id"])
        body = callback_body(request, job, segments)
        # base_path храним с приёма: имена исходных файлов после обработки
        # уже не восстановить, а приёмник по ним и сопоставляет запись.
        body["base_path"] = str(call.get("base_path") or body["base_path"])
        return body

    def _deliver_webhook(self, job: dict[str, Any], payload: bytes) -> None:
        """Доставка с повторами. Одна на оба вида тела."""
        import hashlib
        import hmac
        import urllib.error
        import urllib.parse
        import urllib.request

        secret = str(self.settings.get("webhook_secret") or "").encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "User-Agent": "ASRHub/3.0",
                   # Сопоставить уведомление и отбросить повтор: номер
                   # доставки один на все попытки одного исхода задания и
                   # новый — у исхода после повтора задания. Раньше приёмнику
                   # оставалось сравнивать тела.
                   # Номер задания бывает не латиницей («phone-звонок_1» у
                   # разговоров phone_asr), а заголовок HTTP — только latin-1:
                   # кодируем как в адресе.
                   "X-ASRHub-Delivery": (f"{urllib.parse.quote(str(job['id']), safe='-_.')}."
                                         f"{int(float(job.get('finished_at') or 0) * 1000)}"),
                   "X-ASRHub-Event": f"job.{job.get('status') or 'finished'}"}
        if secret:
            headers["X-ASRHub-Signature"] = hmac.new(secret, payload, hashlib.sha256).hexdigest()

        # Адрес кодируем перед отправкой: urllib требует уже закодованный
        # путь и бросает «'ascii' codec can't encode characters», не дойдя до
        # сети. Кириллица в адресе обратного вызова — обычное дело (имя
        # проекта в пути), и без этого доставка молча падала пять раз подряд
        # и помечалась «failed», а вызывающая сторона ничего не получала.
        # Преобразование идемпотентно: уже закодованный адрес не меняется.
        from .phone_compat import encode_url

        target = encode_url(str(job["webhook_url"]))
        # Адрес проверяется ещё раз — перед отправкой: имя, которое при приёме
        # указывало наружу, к этому времени могло начать указывать внутрь.
        allow_internal = bool(self.settings.get("webhook_allow_internal", False))
        try:
            check_outbound_url(target, allow_internal)
        except ConfigError as exc:
            log.warning("Уведомление для %s не отправлено: %s", job["id"], exc.message,
                        extra={"job_id": job["id"]})
            self.db.update_job(job["id"], webhook_status="blocked", webhook_next_at=None)
            RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})
            return
        opener = открыватель_наружу(allow_internal)

        узел = urllib.parse.urlsplit(target).netloc.lower()
        пауза_до = узел_на_паузе(узел)
        if пауза_до:
            # Узел не отвечает несколько заходов подряд: уведомления к нему
            # ждут паузы, не занимая пул доставки. Раньше каждое ждало свои
            # попытки по 15 с, и один мёртвый приёмник держал все потоки —
            # уведомления исправным приёмникам стояли за ним.
            self._отложить_уведомление(job, "приёмник на паузе", без_попытки=True,
                                       до=пауза_до)
            return
        attempts = ПОПЫТОК_ЗА_ЗАХОД
        причина = ""
        for attempt in range(attempts):
            if self._stop.is_set():
                log.info("Доставка уведомления для %s отложена до запуска: очередь "
                         "остановлена", job["id"], extra={"job_id": job["id"]})
                self._отложить_уведомление(job, "остановка", без_попытки=True)
                return
            try:
                # Подпись со временем отправки: старая подпись (только тело)
                # годилась навсегда, и перехваченное уведомление можно было
                # прислать приёмнику снова через месяц. Приёмник сверяет
                # HMAC от «время.тело» и отбрасывает старше пяти минут.
                момент = str(int(time.time()))
                headers["X-ASRHub-Timestamp"] = момент
                headers["X-ASRHub-Attempt"] = str(attempt + 1)
                if secret:
                    headers["X-ASRHub-Signature-256"] = "sha256=" + hmac.new(
                        secret, момент.encode() + b"." + payload, hashlib.sha256).hexdigest()
                request = urllib.request.Request(target, data=payload,
                                                 headers=headers, method="POST")
                with opener.open(request, timeout=15) as response:
                    if 200 <= response.status < 300:
                        self.db.update_job(job["id"], webhook_status=f"ok:{response.status}",
                                           webhook_next_at=None)
                        RUNTIME.inc("asrhub_webhooks_total", {"result": "ok"})
                        узел_ответил(узел, True)
                        return
                    причина = f"ответ {response.status}"
            except ConfigError as exc:
                # Перенаправление внутрь сети (или адрес, ставший внутренним
                # к моменту соединения) — повторять незачем.
                log.warning("Уведомление для %s не отправлено: %s", job["id"], exc.message,
                            extra={"job_id": job["id"]})
                self.db.update_job(job["id"], webhook_status="blocked", webhook_next_at=None)
                RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})
                return
            except (urllib.error.URLError, OSError, ValueError) as exc:
                причина = str(exc)
                log.info("Уведомление для %s не доставлено (попытка %d из %d): %s",
                         job["id"], attempt + 1, attempts, exc)
            if attempt < attempts - 1:      # после последней попытки ждать незачем
                # Ждём на событии остановки, а не в sleep: пул доставки
                # гасится без ожидания, и уже запущенная попытка иначе
                # продолжала бы жить своей жизнью — процесс не завершался
                # после «Очередь остановлена». Остановка — причина отложить
                # доставку до запуска, а не повод её дождаться.
                if self._stop.wait(timeout=min(60, 2 ** attempt)):
                    log.info("Доставка уведомления для %s прервана остановкой",
                             job["id"], extra={"job_id": job["id"]})
                    self._отложить_уведомление(job, "остановка", без_попытки=True)
                    return
                if узел_на_паузе(узел):
                    # Пока ждали, соседний поток поставил узел на паузу.
                    break
        узел_ответил(узел, False, порог=self.ОТКАЗОВ_ДО_ПАУЗЫ_УЗЛА)
        self._отложить_уведомление(job, причина or "нет ответа")

    #: Сколько заходов подряд без ответа ставит узел приёмника на паузу.
    ОТКАЗОВ_ДО_ПАУЗЫ_УЗЛА = 3

    def _узел_на_паузе(self, узел: str) -> float:
        """До какого момента узел приёмника на паузе (0 — не на паузе)."""
        return узел_на_паузе(узел)

    def _узел_ответил(self, узел: str, удачно: bool) -> None:
        узел_ответил(узел, удачно, порог=self.ОТКАЗОВ_ДО_ПАУЗЫ_УЗЛА)

    def _owner_of(self, job_id: str) -> str:
        """Владелец задания для адресной рассылки событий.

        Держим небольшой словарь вместо запроса к базе: события прогресса
        идут по нескольку раз в секунду на каждое задание, и поход в базу
        на каждое сделал бы рассылку дороже самой работы.
        """
        with self._lock:
            cached = self._owners.get(job_id)
        if cached is not None:
            return cached
        job = self.db.get_job(job_id)
        owner = str((job or {}).get("owner") or "")
        with self._lock:
            self._owners[job_id] = owner
            # Словарь не должен расти бесконечно на долгоживущем сервере.
            if len(self._owners) > 4096:
                for key in list(self._owners)[:2048]:
                    self._owners.pop(key, None)
        return owner

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        # Событие о задании адресное: имя файла, текст ошибки и сам факт
        # работы — сведения владельца. Подписчику, который не владелец и не
        # администратор, оно не уходит. Поле служебное и до клиента не
        # доезжает — рассылка снимает его перед отправкой.
        if "_owner" not in data and isinstance(data.get("id"), str):
            data = {**data, "_owner": self._owner_of(data["id"])}
        try:
            self.on_event(kind, data)
        except Exception as exc:
            log.debug("Обработчик события «%s» дал сбой: %s", kind, exc)
