"""Заход 54: веб-интерфейс — находки ревизии 3.1.20 (15-web1, 16-web2).

Каждая проверка воспроизводит то, что человек видел на экране или что
уходило на сервер: какая пара «модель + движок» уходила с заданием, сколько
запросов давал поток событий, что оставалось в «Копировать» после
«Остановить», какое значение ползунка оказывалось на сервере. Проверяется
поведение — запросы, состояние страницы, содержимое таблиц, — а не текст
исходника.

Стендов два: без входа, с наполненной базой и записями в разных форматах;
и со входом — администратор, пользователь, ключи «только чтение» и с
квотами. Нет браузера или Playwright — проверки пропускаются.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from test_review_46_web import _без_исключений, _вкладка, _Окружение, _открыть, _поднять
from test_review_46_web import _проверить_среду as _среда46
from test_web_ui import _браузер, _наполнить

ПАРОЛЬ = "Проверка-54-пароль"
КЛЮЧ_АДМИНА = "ah_" + "a" * 30
КЛЮЧ_ЧТЕНИЯ = "ah_" + "r" * 30
КЛЮЧ_ПОЛЬЗОВАТЕЛЯ = "ah_" + "u" * 30


def _записи(состояние) -> None:
    """Записи в форматах, которые браузер играет и не играет; длинная без файла."""
    db = состояние.db
    каталог = Path(состояние.settings.paths.uploads)
    каталог.mkdir(parents=True, exist_ok=True)
    сейчас = time.time()
    реплики = [{"start": i * 12.0, "end": i * 12.0 + 10.0, "speaker": f"SPEAKER_{i % 2:02d}",
                "text": f"обычная реплика номер {i} про погоду" if i != 50
                else "а вот здесь прозвучал бронепоезд один раз"} for i in range(70)]
    текст = " ".join(р["text"] for р in реплики)
    db.create_job({"id": "long01", "filename": "длинный.wav", "owner": "борис",
                   "engine": "demo", "model": "demo-simulator", "language": "ru",
                   "source": "web", "media_duration_s": 900.0})
    db.update_job("long01", status="completed", text=текст, finished_at=сейчас,
                  words_count=len(текст.split()), segments_count=len(реплики), rtf=0.1,
                  processing_time_s=5.0, avg_confidence=0.9)
    db.save_segments("long01", реплики)
    for имя, кодек in (("pcm.wav", ["-c:a", "pcm_s16le"]),
                       ("gsm49.wav", ["-c:a", "libgsm_ms"]),
                       ("sound.ac3", ["-ar", "48000", "-c:a", "ac3"])):
        путь = каталог / f"up-{имя}"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=3", "-ar", "8000", "-ac", "1",
                        *кодек, str(путь)], check=True)
        ид = "fmt-" + имя.split(".")[0]
        db.create_job({"id": ид, "filename": имя, "owner": "борис", "engine": "demo",
                       "model": "demo-simulator", "language": "ru", "source": "web",
                       "media_duration_s": 3.0, "file_path": str(путь)})
        db.update_job(ид, status="completed", text="раз два три", finished_at=сейчас,
                      words_count=3, segments_count=1, rtf=0.1, processing_time_s=1.0,
                      avg_confidence=0.9)
        db.save_segments(ид, [{"start": 0.0, "end": 3.0, "text": "раз два три",
                               "speaker": "SPEAKER_00"}])
    for n, (направление, номер) in enumerate((("входящий", "111"), ("исходящий", "333"))):
        db.save_call(f"call54-{n}", owner="борис", src=f"+7900{номер}0000", dst="100",
                     direction=направление, answered=True, duration=60, billsec=50,
                     started_at=сейчас - 600 - n, station="ats1")


def _проверить_среду() -> None:
    _среда46()
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode:
        pytest.skip("нужен ffmpeg для записей в разных форматах")


@pytest.fixture(scope="module")
def стенд(tmp_path_factory):
    """Сервер без входа: записи, звонки, файл конфигурации; очередь без воркеров."""
    _проверить_среду()
    каталог = tmp_path_factory.mktemp("ui54") / "data"
    каталог.mkdir(parents=True)
    конфигурация = каталог / "config.yaml"
    конфигурация.write_text("queue:\n  max_concurrent_jobs: 1\n", encoding="utf-8")
    with _Окружение(ASRHUB_DATA_DIR=str(каталог), ASRHUB_AUTH_ENABLED="false",
                    ASRHUB_MODEL="demo-simulator", ASRHUB_ENGINE="demo",
                    ASRHUB_CONFIG=str(конфигурация)):
        from asrhub.api.app import create_app

        приложение = create_app(start_queue=False)
        _наполнить(приложение)
        состояние = приложение.state.hub
        _записи(состояние)
        адрес, служба, поток = _поднять(приложение)
        yield {"адрес": адрес, "состояние": состояние, "конфигурация": конфигурация}
        служба.should_exit = True
        поток.join(timeout=10)


@pytest.fixture(scope="module")
def стенд_со_входом(tmp_path_factory):
    """Сервер со входом: администратор, пользователь, ключи с разными ролями."""
    _проверить_среду()
    каталог = tmp_path_factory.mktemp("ui54auth") / "data"
    каталог.mkdir(parents=True)
    конфигурация = каталог / "config.yaml"
    конфигурация.write_text("monitoring:\n  monitoring_public: false\n", encoding="utf-8")
    with _Окружение(ASRHUB_DATA_DIR=str(каталог), ASRHUB_AUTH_ENABLED="true",
                    ASRHUB_MODEL="demo-simulator", ASRHUB_ENGINE="demo",
                    ASRHUB_CONFIG=str(конфигурация)):
        from asrhub.api.app import create_app

        приложение = create_app(start_queue=False)
        состояние = приложение.state.hub
        учётки = состояние.accounts
        админ = учётки.by_username("admin")
        учётки.update(админ.id, must_change_password=False)
        учётки.set_password(админ.id, ПАРОЛЬ)
        учётки.create("сотрудник", "Сотрудник-1-пароль", role="user",
                      must_change_password=False)
        ключи = состояние.settings.api_keys
        ключи[КЛЮЧ_АДМИНА] = {"name": "ключ-админа", "role": "admin", "enabled": True}
        ключи[КЛЮЧ_ЧТЕНИЯ] = {"name": "ревизор", "role": "readonly", "enabled": True}
        ключи[КЛЮЧ_ПОЛЬЗОВАТЕЛЯ] = {"name": "интеграция", "role": "user", "enabled": True,
                                    "group": "продажи", "quota_jobs_per_day": 40}
        _записи(состояние)
        адрес, служба, поток = _поднять(приложение)
        yield {"адрес": адрес, "состояние": состояние, "конфигурация": конфигурация}
        служба.should_exit = True
        поток.join(timeout=10)


@pytest.fixture(scope="module")
def браузер():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        # Подставной микрофон — для диктовки: настоящего в окружении нет.
        б = playwright.chromium.launch(executable_path=_браузер(), args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        try:
            yield б
        finally:
            б.close()


def _войти(вкладка, логин: str, пароль: str) -> None:
    ответ = вкладка.request.post(f"{вкладка.адрес}/api/auth/login",
                                 data={"username": логин, "password": пароль})
    assert ответ.ok, ответ.text()


def _перехват(вкладка, образец: str, ответ: dict | None = None, *, метод: str = "",
              статус: int = 200):
    """Запросы по образцу — записываются и получают готовый ответ, мимо сервера."""
    пойманные: list[dict] = []

    def обработать(маршрут, запрос):
        if метод and запрос.method != метод:
            маршрут.continue_()
            return
        пойманные.append({"url": запрос.url, "method": запрос.method,
                          "body": запрос.post_data or ""})
        маршрут.fulfill(status=статус, content_type="application/json",
                        body=json.dumps(ответ if ответ is not None else {}))

    вкладка.route(образец, обработать)
    return пойманные


def _поле_формы(тело: str, имя: str) -> str:
    """Значение поля многочастной формы из тела запроса."""
    метка = f'name="{имя}"'
    начало = тело.index(метка) + len(метка)
    значение = тело[начало:].split("\r\n\r\n", 1)[1]
    return значение.split("\r\n--", 1)[0]


# ---------------------------------------------------------------------------
# Транскрибация: пресет, модель, приоритет, загрузка
# ---------------------------------------------------------------------------


def _отправить(вкладка, файлов: int = 1) -> list[dict]:
    """Поставить в очередь подставные файлы; отдаёт перехваченные запросы."""
    пойманные = _перехват(вкладка, "**/api/jobs", {"id": "j1"}, метод="POST")
    _перехват(вкладка, "**/api/jobs/batch", {"created": файлов, "errors": []}, метод="POST")
    вкладка.evaluate("(n) => { const s = window.__asrhub.state;"
                     " s.files = Array.from({length: n}, (_, i) => new File(['x'], `f${i}.wav`));"
                     " document.querySelector('#btn-submit').disabled = false;"
                     " document.querySelector('#btn-submit').click(); }", файлов)
    вкладка.wait_for_timeout(800)
    return пойманные


def test_модель_после_пресета_уходит_со_своим_движком(стенд, браузер):
    """Пресет закреплял движок, выбор модели его не трогал: demo + gigaam."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    вкладка.select_option("#preset-select", "diagnostics")
    вкладка.wait_for_timeout(500)
    вкладка.select_option("#slot-model select", "gigaam-v3-rnnt")
    вкладка.wait_for_timeout(300)
    пойманные = _отправить(вкладка)
    assert len(пойманные) == 1, пойманные
    настройки = json.loads(_поле_формы(пойманные[0]["body"], "settings"))
    assert настройки["model"] == "gigaam-v3-rnnt"
    assert настройки["engine"] == "gigaam", настройки
    _без_исключений(вкладка)


def test_пресеты_не_накапливаются_и_снимаются(стенд, браузер):
    """«Совещание», потом «Субтитры» — уходили диаризация и выжимка «Совещания»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    вкладка.select_option("#preset-select", "meeting")
    вкладка.wait_for_timeout(500)
    вкладка.select_option("#preset-select", "subtitles")
    вкладка.wait_for_timeout(500)
    пойманные = _отправить(вкладка)
    настройки = json.loads(_поле_формы(пойманные[0]["body"], "settings"))
    for ключ in ("diarization_enabled", "diarization_backend", "summary_enabled",
                 "remove_filler_words"):
        assert ключ not in настройки, (ключ, настройки)
    assert "subtitle_max_lines" in настройки
    # «— выбрать пресет —» снимает пресет целиком.
    вкладка.select_option("#preset-select", "")
    вкладка.wait_for_timeout(500)
    вкладка.unroute("**/api/jobs")
    вкладка.unroute("**/api/jobs/batch")
    пойманные = _отправить(вкладка)
    настройки = json.loads(_поле_формы(пойманные[0]["body"], "settings"))
    assert настройки == {}, настройки
    assert "настройки сервера" in вкладка.inner_text("#job-overrides")
    _без_исключений(вкладка)


def test_приоритет_ноль_и_приоритет_после_пресета(стенд, браузер):
    """`|| 50` делал из нуля пятьдесят; пресет перерисовывал поле обратно в 50."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    вкладка.fill("#job-priority", "0")
    вкладка.dispatch_event("#job-priority", "change")
    пойманные = _отправить(вкладка)
    assert _поле_формы(пойманные[0]["body"], "priority") == "0"
    вкладка.fill("#job-priority", "90")
    вкладка.dispatch_event("#job-priority", "change")
    вкладка.select_option("#preset-select", "subtitles")
    вкладка.wait_for_timeout(500)
    assert вкладка.input_value("#job-priority") == "90"
    _без_исключений(вкладка)


#: Перетаскивание папки: настоящий DataTransfer с папкой из страницы не
#: собрать, поэтому вход — подставной, с тем же устройством, что у браузера:
#: `items[].webkitGetAsEntry()`, `createReader().readEntries()`, `file()`.
ПАПКА = """
  () => {
    const файл = (имя) => ({ isDirectory: false, isFile: true, name: имя,
      file(готово) { готово(new File(['x'], имя, { type: '' })); } });
    const папка = (имя, внутри) => ({ isDirectory: true, isFile: false, name: имя,
      createReader() { let отдано = false;
        return { readEntries(готово) { const было = отдано; отдано = true;
          готово(было ? [] : внутри); } }; } });
    const корень = папка('записи', [файл('a.wav'), файл('опись.txt'),
      папка('март', [файл('b.mp3'), файл('c.ogg')])]);
    const входы = [корень, файл('t1.wav')];
    const перенос = { items: входы.map((в) => ({ kind: 'file', webkitGetAsEntry: () => в })),
                      files: [new File([''], 'записи'), new File(['x'], 't1.wav')] };
    const событие = new Event('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(событие, 'dataTransfer', { value: перенос });
    window.__asrhub.state.files = [];
    document.querySelector('#dropzone').dispatchEvent(событие);
  }
"""


def test_перетащенная_папка_разворачивается_в_записи(стенд, браузер):
    """Папка попадала в список «файлом», и отправка падала «Сервер недоступен»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    вкладка.evaluate(ПАПКА)
    вкладка.wait_for_timeout(500)
    имена = вкладка.evaluate("() => window.__asrhub.state.files.map((f) => f.name)")
    assert sorted(имена) == ["a.wav", "b.mp3", "c.ogg", "t1.wav"], имена
    assert "Пропущено" in вкладка.inner_text("#toasts")
    _без_исключений(вкладка)


def test_пределы_загрузки_проверяются_до_отправки(стенд, браузер):
    """Отказ за размер приходил после того, как гигабайты ушли по сети."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    пойманные = _перехват(вкладка, "**/api/jobs", {"id": "j1"}, метод="POST")
    вкладка.evaluate("() => { const s = window.__asrhub.state; s.settings.max_upload_mb = 1;"
                     " s.files = [new File([new Uint8Array(2 * 1048576)], 'большой.wav')];"
                     " document.querySelector('#btn-submit').disabled = false;"
                     " document.querySelector('#btn-submit').click(); }")
    вкладка.wait_for_timeout(600)
    assert not пойманные, пойманные
    assert "большой.wav" in вкладка.inner_text("#rejected-box")
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Ползунки и частота запросов
# ---------------------------------------------------------------------------


def _протянуть(вкладка, селектор: str, значения: list) -> None:
    """Протянуть ползунок: события input на каждом шаге и одно change в конце."""
    вкладка.evaluate("([с, з]) => { const п = document.querySelector(с);"
                     " з.forEach((v) => { п.value = v; п.dispatchEvent(new Event('input', {bubbles: true})); });"
                     " п.dispatchEvent(new Event('change', {bubbles: true})); }", [селектор, значения])


def test_ползунок_очереди_сохраняет_одно_конечное_значение(стенд, браузер):
    """Одно движение мышью — девятнадцать PUT и перестройки пула на каждом тике."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    пойманные = _перехват(вкладка, "**/api/settings?persist=true",
                          {"applied": {"max_concurrent_jobs": 4}, "persisted": True}, метод="PUT")
    _протянуть(вкладка, "#slot-workers input[type=range]", [2, 3, 5, 7, 9, 12, 8, 6, 4])
    вкладка.wait_for_timeout(800)
    assert len(пойманные) == 1, [п["body"] for п in пойманные]
    assert json.loads(пойманные[0]["body"]) == {"max_concurrent_jobs": 4}
    # Пока тянут, число рядом идёт за ползунком.
    _протянуть(вкладка, "#slot-workers input[type=range]", [3])
    assert вкладка.input_value("#slot-workers input[type=number]") == "3"
    _без_исключений(вкладка)


def test_ползунок_резервных_копий_сохраняет_то_что_на_экране(стенд, браузер):
    """Сохранялось промежуточное: ползунок 60, на сервере 53."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "backup", 2000)
    пойманные = _перехват(вкладка, "**/api/settings?persist=true",
                          {"applied": {"backup_keep_days": 60}, "persisted": True}, метод="PUT")
    _протянуть(вкладка, ".param[data-key='backup_keep_days'] input[type=range]",
               list(range(10, 61, 2)))
    вкладка.wait_for_timeout(800)
    assert len(пойманные) == 1, len(пойманные)
    assert json.loads(пойманные[0]["body"]) == {"backup_keep_days": 60}


def test_поток_событий_склеивается_и_не_упирается_в_предел(стенд, браузер):
    """40 событий давали 192 запроса за 6 с и 40 плашек «Задание готово»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe", 2000)
    было = len(вкладка.запросы)
    вкладка.evaluate("() => { for (let i = 0; i < 40; i += 1) window.__asrhub.util.handleEvent("
                     "{ type: i % 10 === 9 ? 'job.failed' : 'job.completed', id: `x${i}`, rtf: 0.1,"
                     "  error: { message: 'сбой' } }); }")
    вкладка.wait_for_timeout(5000)
    запросы = [з for з in вкладка.запросы[было:] if "/api/" in з]
    assert len(запросы) <= 8, запросы
    assert not any("/api/monitoring/alerts" in з for з in запросы), запросы
    плашки = вкладка.locator("#toasts .toast").count()
    assert плашки <= 3, вкладка.inner_text("#toasts")
    текст = вкладка.inner_text("#toasts")
    assert "Готово заданий: 36" in текст and "Завершились ошибкой: 4" in текст, текст
    _без_исключений(вкладка)


def test_отказ_429_останавливает_фоновый_опрос(стенд, браузер):
    """Опрос глотал 429 и стучал дальше; строка состояния молчала."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue", 1500)
    отказ = {"code": "rate_limited", "message": "Превышен лимит 120 запросов в минуту.",
             "details": {"limit": 120, "retry_after_s": 40}}
    пойманные = _перехват(вкладка, "**/api/queue", отказ, статус=429)
    вкладка.wait_for_timeout(4500)                       # один такт опроса
    assert len(пойманные) == 1, пойманные
    вкладка.wait_for_timeout(9000)                       # ещё два такта — тишина
    assert len(пойманные) == 1, пойманные
    assert "реже" in вкладка.inner_text("#conn-status")
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Очередь
# ---------------------------------------------------------------------------


def test_плитки_очереди_обновляются_с_опросом(стенд, браузер):
    """Таблица пустела, а плитки час показывали прежнее «В очереди»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    db = стенд["состояние"].db
    плитка = "() => document.querySelector('#q-tiles .kpi .kpi-value').innerText"
    было = int(вкладка.evaluate(плитка))
    for n in range(7):
        db.create_job({"id": f"q54-{n}", "filename": f"q{n}.wav", "owner": "a",
                       "engine": "demo", "model": "demo-simulator", "language": "ru",
                       "source": "web", "media_duration_s": 60.0})
    try:
        вкладка.wait_for_timeout(9000)
        assert int(вкладка.evaluate(плитка)) == было + 7
    finally:
        for n in range(7):
            db.execute("DELETE FROM jobs WHERE id=?", (f"q54-{n}",))
    _без_исключений(вкладка)


def test_очередь_без_сервера_пишет_в_таблицу_а_не_плашками(стенд, браузер):
    """Плашка «Сервер недоступен» на каждый такт опроса — около пяти за 17 с."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    вкладка.route("**/api/jobs?*", lambda маршрут: маршрут.abort())
    вкладка.evaluate("() => window.__asrhub.util.handleEvent({ type: 'job.queued' })")
    вкладка.wait_for_timeout(13000)
    assert "Сервер недоступен" not in вкладка.inner_text("#toasts")
    assert "не обновился" in вкладка.inner_text("#queue-table")


def test_таблица_очереди_говорит_сколько_не_показано(стенд, браузер):
    """Молча обрезалась на 120 строках, хотя сервер отдавал total."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    вкладка.select_option("#q-filter", "completed")
    вкладка.evaluate("() => { window.__asrhub.RENDERERS.queue.предел = 10;"
                     " window.__asrhub.RENDERERS.queue.loadTable(); }")
    вкладка.wait_for_timeout(1200)
    текст = вкладка.inner_text("#queue-table")
    assert "Показаны первые 10 из" in текст, текст[-300:]
    вкладка.click("#q-more")
    вкладка.wait_for_timeout(1200)
    assert вкладка.locator("#queue-table tbody tr").count() > 10
    _без_исключений(вкладка)


@pytest.mark.parametrize("кто", ["user", "readonly"])
def test_кнопки_администратора_не_видны_прочим_ролям(стенд_со_входом, браузер, кто):
    """«Приостановить» и настройки очереди рисовались всем и отвечали 403."""
    ключ = КЛЮЧ_ПОЛЬЗОВАТЕЛЯ if кто == "user" else КЛЮЧ_ЧТЕНИЯ
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=ключ)
    _открыть(вкладка, "queue", 2000)
    for сел in ("#q-pause", "#q-retry", "#q-clear", "#slot-workers"):
        assert not вкладка.is_visible(сел), сел
    assert "администратор" in вкладка.inner_text("#content")
    _открыть(вкладка, "transcribe", 1500)
    assert вкладка.is_visible("#dropzone") == (кто == "user")
    assert вкладка.is_visible("#btn-submit") == (кто == "user")
    _без_исключений(вкладка)


def test_администратор_видит_управление_очередью(стенд_со_входом, браузер):
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=КЛЮЧ_АДМИНА)
    _открыть(вкладка, "queue", 2000)
    for сел in ("#q-pause", "#q-retry", "#q-clear", "#slot-workers"):
        assert вкладка.is_visible(сел), сел
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Диктовка — подставной сокет: библиотеки WebSocket для uvicorn в окружении нет
# ---------------------------------------------------------------------------

#: Сокет, который ведёт себя как /api/stream: открывается, шлёт черновик,
#: на «finish» досылает итог через `window.__итогЧерез` мс; или рвётся
#: кодом 1006 через `window.__обрывЧерез` мс после открытия.
ПОДСТАВНОЙ_СОКЕТ = """
  (() => {
    window.__журнал = [];
    class Сокет {
      constructor(url) {
        this.url = url; this.readyState = 0;
        setTimeout(() => {
          this.readyState = 1; this.onopen && this.onopen({});
          const шли = (м) => this.onmessage && this.onmessage({ data: JSON.stringify(м) });
          шли({ type: 'ready', mode: 'window', window_s: 3 });
          шли({ type: 'final', text: 'первая фраза закреплена.' });
          шли({ type: 'partial', text: 'хвост ещё не закреплён' });
          if (window.__обрывЧерез) setTimeout(() => {
            this.readyState = 3; this.onclose && this.onclose({ code: 1006, reason: '' });
          }, window.__обрывЧерез);
        }, 150);
      }
      send(данные) {
        if (typeof данные !== 'string' || !данные.includes('finish')) return;
        window.__журнал.push('finish');
        setTimeout(() => {
          if (this.readyState !== 1) { window.__журнал.push('итог опоздал: сокет закрыт'); return; }
          const шли = (м) => this.onmessage && this.onmessage({ data: JSON.stringify(м) });
          шли({ type: 'final', text: 'хвост закреплён через шесть секунд.' });
          шли({ type: 'done' });
        }, window.__итогЧерез || 6000);
      }
      close() {
        if (this.readyState === 3) return;
        window.__журнал.push('close'); this.readyState = 3;
        this.onclose && this.onclose({ code: 1000, reason: '' });
      }
    }
    Сокет.OPEN = 1; Сокет.CONNECTING = 0; Сокет.CLOSED = 3;
    window.__подменитьСокет = () => { window.WebSocket = Сокет; };
  })();
"""


def _вкладка_диктовки(браузер, адрес):
    контекст = браузер.new_context(viewport={"width": 1600, "height": 1000},
                                   permissions=["microphone"])
    контекст.add_init_script(ПОДСТАВНОЙ_СОКЕТ)
    вкладка = контекст.new_page()
    вкладка.ошибки = []
    вкладка.запросы = []
    вкладка.on("pageerror", lambda e: вкладка.ошибки.append(f"исключение: {e}"))
    вкладка.адрес = адрес
    _открыть(вкладка, "dictation", 1000)
    вкладка.evaluate("() => window.__подменитьСокет()")
    return вкладка


def test_итог_диктовки_после_остановки_не_теряется(стенд, браузер):
    """Сокет закрывался через 4 с, а сервер ждёт модель до 120 с: хвост терялся."""
    вкладка = _вкладка_диктовки(браузер, стенд["адрес"])
    вкладка.click("#dict-toggle")
    вкладка.wait_for_timeout(1200)
    assert "идёт запись" in вкладка.inner_text("#dict-status")
    вкладка.click("#dict-toggle")                         # «Остановить»
    вкладка.wait_for_timeout(500)
    assert "итога" in вкладка.inner_text("#dict-status")
    вкладка.wait_for_timeout(7000)
    журнал = вкладка.evaluate("() => window.__журнал")
    assert "итог опоздал: сокет закрыт" not in журнал, журнал
    итог = вкладка.evaluate("() => window.__asrhub.RENDERERS.dictation.finalText()")
    assert "хвост закреплён через шесть секунд" in итог, итог
    # Итог пришёл — сокет закрыт им, а не часами.
    assert журнал[-1] == "close", журнал
    assert "готово" in вкладка.inner_text("#dict-status")
    _без_исключений(вкладка)


def test_обрыв_диктовки_говорит_об_этом(стенд, браузер):
    """После обрыва — сброшенная кнопка и зелёное «идёт запись»."""
    вкладка = _вкладка_диктовки(браузер, стенд["адрес"])
    вкладка.evaluate("() => { window.__обрывЧерез = 1500; }")
    вкладка.click("#dict-toggle")
    вкладка.wait_for_timeout(3000)
    assert вкладка.inner_text("#dict-toggle").strip() == "Начать диктовку"
    статус = вкладка.inner_text("#dict-status")
    assert "прервано" in статус, статус
    assert "идёт запись" not in статус
    assert "Связь с сервером прервалась" in вкладка.inner_text("#toasts")
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Карточка записи: проигрыватель
# ---------------------------------------------------------------------------


def _карточка(вкладка, ид: str, **параметры) -> None:
    вкладка.evaluate("([и, п]) => window.__asrhub.openJob(и, п)", [ид, параметры])
    вкладка.wait_for_selector("#job-tabs", timeout=10000)


@pytest.mark.parametrize("ид", ["fmt-gsm49", "fmt-sound"])
def test_неиграющий_формат_называется_и_не_качается_дважды(стенд, браузер, ид):
    """wav49 и AC-3: мёртвая «▶», пустая подпись и файл, скачанный дважды."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    ответы: list[tuple[int, str]] = []
    вкладка.on("response", lambda r: ответы.append((r.status, r.request.headers.get("range", "")))
               if "/audio" in r.url else None)
    _карточка(вкладка, ид)
    вкладка.wait_for_timeout(3500)
    играет = вкладка.evaluate("() => { const а = document.querySelector('#job-player audio');"
                              " return !а.error && а.readyState >= 1; }")
    if играет:
        pytest.skip("этот Chromium формат проигрывает — проверять нечего")
    подпись = вкладка.inner_text("#job-player .note")
    assert "не проигрывает этот формат" in подпись, подпись
    полных = [о for о in ответы if о[0] == 200]
    assert len(полных) <= 1, ответы
    _без_исключений(вкладка)


def test_pcm_wav_играется(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    _карточка(вкладка, "fmt-pcm")
    вкладка.wait_for_timeout(2500)
    assert not вкладка.is_disabled("#job-player .play")
    assert вкладка.inner_text("#job-player .note") == ""


def test_пробел_в_карточке_ведёт_звук_а_не_закрывает(стенд, браузер):
    """Первый фокус стоял на «✕», и пробел закрывал карточку."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    _карточка(вкладка, "fmt-pcm")
    вкладка.wait_for_timeout(1500)
    вкладка.keyboard.press(" ")
    вкладка.wait_for_timeout(800)
    assert вкладка.locator(".modal-backdrop").count() == 1
    assert вкладка.evaluate("() => !document.querySelector('#job-player audio').paused")
    вкладка.keyboard.press(" ")
    вкладка.wait_for_timeout(300)
    assert вкладка.evaluate("() => document.querySelector('#job-player audio').paused")
    # Цифра под открытой карточкой раздел не меняет и карточку не закрывает.
    вкладка.keyboard.press("3")
    вкладка.wait_for_timeout(300)
    assert вкладка.locator(".modal-backdrop").count() == 1
    _без_исключений(вкладка)


def test_находка_без_звука_подводит_к_реплике(стенд, браузер):
    """Секунда находки бралась только по готовности звука: без записи — с начала."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    _карточка(вкладка, "long01", play=True, at=600)
    вкладка.wait_for_timeout(1500)
    итог = вкладка.evaluate("""() => {
      const р = [...document.querySelectorAll('.segment')].find((с) => с.innerText.includes('бронепоезд'));
      const к = р.getBoundingClientRect(), т = document.querySelector('.modal-body').getBoundingClientRect();
      return { active: р.classList.contains('active'), видно: к.top >= т.top && к.bottom <= т.bottom }; }""")
    assert итог == {"active": True, "видно": True}, итог


def test_по_ключу_запись_грузится_по_нажатию(стенд_со_входом, браузер):
    """Вошедшему по ключу карточка тянула всю запись при открытии."""
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=КЛЮЧ_АДМИНА)
    _открыть(вкладка, "results")
    было = len(вкладка.запросы)
    _карточка(вкладка, "fmt-pcm")
    вкладка.wait_for_timeout(1500)
    assert not any("/audio" in з for з in вкладка.запросы[было:])
    assert not вкладка.is_disabled("#job-player .play")
    вкладка.click("#job-player .play")
    вкладка.wait_for_timeout(1500)
    assert any("/audio" in з for з in вкладка.запросы[было:])
    assert вкладка.evaluate("() => !document.querySelector('#job-player audio').paused")
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Сессия, хранилище, поля, сохранённые между перерисовками
# ---------------------------------------------------------------------------


def test_после_конца_сессии_лента_не_переподключается(стенд_со_входом, браузер):
    """Отозванный ключ — и билет на ленту просился снова и снова, отказами 401."""
    ключ = "ah_" + "z" * 30
    ключи = стенд_со_входом["состояние"].settings.api_keys
    ключи[ключ] = {"name": "временный", "role": "admin", "enabled": True}
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=ключ)
    try:
        _открыть(вкладка, "queue", 2000)
        del ключи[ключ]
        вкладка.click("#btn-refresh")
        вкладка.wait_for_selector("#login-user", timeout=10000)
        было = len(вкладка.запросы)
        вкладка.wait_for_timeout(9000)
        билеты = [з for з in вкладка.запросы[было:] if "/api/auth/ticket" in з]
        assert not билеты, билеты
    finally:
        ключи.pop(ключ, None)


ХРАНИЛИЩЕ_ЗАПРЕЩЕНО = """
  Object.defineProperty(window, 'localStorage', {
    get() { throw new DOMException("Failed to read the 'localStorage' property", 'SecurityError'); },
  });
"""


def test_запрет_хранилища_не_оставляет_пустую_страницу(стенд, браузер):
    """Исключение в обработчике загрузки — ни формы, ни раздела, «подключение…»."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=ХРАНИЛИЩЕ_ЗАПРЕЩЕНО)
    _открыть(вкладка, "transcribe", 2000)
    assert вкладка.locator("#dropzone").count() == 1
    assert "подключение" not in вкладка.inner_text("#conn-status")
    вкладка.click("#theme-toggle")                     # тема без хранилища — тоже работает
    assert вкладка.evaluate("() => document.body.classList.contains('light')")
    _без_исключений(вкладка)


def test_отбор_по_направлению_виден_после_возврата(стенд, браузер):
    """Журнал отобран по «Входящим», а поле пишет «Все направления»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "telephony")
    вкладка.select_option("#tel-direction", "входящий")
    вкладка.wait_for_timeout(600)
    вкладка.evaluate("() => window.__asrhub.go('results')")
    вкладка.wait_for_timeout(500)
    вкладка.evaluate("() => window.__asrhub.go('telephony')")
    вкладка.wait_for_timeout(1500)
    assert вкладка.input_value("#tel-direction") == "входящий"
    _без_исключений(вкладка)


def test_период_голосовой_аналитики_виден_после_перерисовки(стенд, браузер):
    """В списке «за неделю», а данные — за месяц."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "voice", 2000)
    вкладка.select_option("#vo-period", "month")
    вкладка.wait_for_timeout(800)
    вкладка.evaluate("() => window.__asrhub.renderView()")
    вкладка.wait_for_timeout(1500)
    assert вкладка.input_value("#vo-period") == "month"
    _без_исключений(вкладка)


def test_врезка_станций_в_настройках_видит_станции(стенд, браузер):
    """Читали `stations`, а ответ — `items`: «Ни одной станции не подключено»."""
    состояние = стенд["состояние"]
    прежние = состояние.settings.get("telephony_stations")
    состояние.settings.set("telephony_stations", [{"id": "ats54", "name": "Станция 54",
                                                   "source": "cdr_csv", "enabled": True,
                                                   "cdr_file": "/tmp/нет.csv"}])
    try:
        вкладка = _вкладка(браузер, стенд["адрес"])
        _открыть(вкладка, "settings")
        вкладка.evaluate("() => { window.__asrhub.state.paramGroup = 'telephony';"
                         " window.__asrhub.renderView(); }")
        вкладка.wait_for_selector("#set-stations .card-head", timeout=10000)
        вкладка.wait_for_timeout(800)
        текст = вкладка.inner_text("#set-stations")
        assert "Станция 54" in текст, текст[:300]
        assert "Ни одной станции" not in текст
        вкладка.click("#set-stations [data-station='ats54']")
        вкладка.wait_for_timeout(500)
        assert вкладка.locator(".modal-backdrop").count() == 1, "правка станции не открылась"
        _без_исключений(вкладка)
    finally:
        состояние.settings.set("telephony_stations", прежние or [])


def test_раскрытые_проверки_дашборда_не_схлопываются(стенд, браузер):
    """Опрос раз в 10 с пересобирал карточки — «все проверки» закрывались."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "dashboard", 2500)
    вкладка.wait_for_selector("#db-components details", timeout=15000)
    вкладка.evaluate("() => { document.querySelector('#db-components details').open = true; }")
    вкладка.evaluate("() => window.__asrhub.RENDERERS.dashboard.load(true)")
    вкладка.wait_for_timeout(2500)
    assert вкладка.evaluate("() => document.querySelectorAll('#db-components details[open]').length") == 1
    _без_исключений(вкладка)


def test_ход_установки_модели_переживает_сбой_запроса(стенд, браузер):
    """`catch (err) { return; }` — один сбой, и «Идёт установка» навсегда."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "settings")
    _перехват(вкладка, "**/api/llm/models", {"models": [], "setup": {"running": True, "steps": []},
                                            "service": {}, "hardware": {}})
    опросы: list[int] = []

    def ход(маршрут, запрос):
        опросы.append(1)
        if len(опросы) == 1:
            маршрут.fulfill(status=502, body="Bad Gateway")
        else:
            маршрут.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"running": True, "steps": [], "progress": 0.5}))

    вкладка.route("**/api/llm/setup/status", ход)
    вкладка.evaluate("() => { window.__asrhub.state.paramGroup = 'llm';"
                     " window.__asrhub.renderView(); }")
    вкладка.wait_for_timeout(11000)
    assert len(опросы) >= 2, опросы


#: Задержка ответа проверки CRM — внутри страницы (см. test_web_ui):
#: запрос висит, пока тест жмёт кнопку второй раз.
ЗАДЕРЖКА_CRM = """
  (() => {
    window.__отправок = 0;
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/crm/test')) {
        if (адрес.includes('dry_run=false')) window.__отправок += 1;
        return new Promise((готово) => setTimeout(() => готово(new Response(
          JSON.stringify({ kind: 'bitrix24', url: 'https://crm', body: '{}', sent: true,
                           response: { status: 200 } }),
          { status: 200, headers: { 'Content-Type': 'application/json' } })), 1500));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_отправка_в_crm_не_уходит_дважды(стенд, браузер):
    """Двойной щелчок по «Отправить по-настоящему» — два комментария в чужой карточке."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=ЗАДЕРЖКА_CRM)
    _открыть(вкладка, "settings")
    вкладка.evaluate("() => { window.__asrhub.state.paramGroup = 'crm';"
                     " window.__asrhub.renderView(); }")
    вкладка.wait_for_selector("#crm-send", timeout=10000)
    вкладка.evaluate("() => { const к = document.querySelector('#crm-send'); к.click(); к.click(); }")
    вкладка.wait_for_timeout(2500)
    assert вкладка.evaluate("() => window.__отправок") == 1
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Аналитика записей: черновики, сбросы, чужие правки
# ---------------------------------------------------------------------------


def _вкладка_разбора(вкладка, что: str) -> None:
    вкладка.click(f'#content-tabs button[data-tab="{что}"]')
    вкладка.wait_for_selector("#script-list .script-item, #cat-list .script-item", timeout=15000)
    вкладка.wait_for_timeout(500)


def test_черновик_скрипта_переживает_смену_периода_и_темы(стенд, браузер):
    """Правка пункта пропадала от смены периода, клавиши «t» и соседней вкладки."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    _вкладка_разбора(вкладка, "script")
    вкладка.fill("#script-list .script-item .script-title", "Поздоровался по-новому")
    вкладка.click('#content-period button[data-period="week"]')
    вкладка.wait_for_selector("#script-list .script-item", timeout=15000)
    вкладка.keyboard.press("t")
    вкладка.wait_for_selector("#script-list .script-item", timeout=15000)
    вкладка.wait_for_timeout(500)
    assert вкладка.input_value("#script-list .script-item .script-title") == "Поздоровался по-новому"
    assert вкладка.is_visible("#script-dirty")
    _без_исключений(вкладка)


def test_черновик_категорий_переживает_уход_по_кнопке_записи(стенд, браузер):
    """«Записи» в таблице той же вкладки стирала черновик молча."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    _вкладка_разбора(вкладка, "categories")
    вкладка.fill("#cat-list .script-item .cat-title", "Сроки, правка 54")
    вкладка.click('#cat-table button[data-category]')
    вкладка.wait_for_selector("#results-table", timeout=15000)
    вкладка.evaluate("() => window.__asrhub.go('content')")
    вкладка.wait_for_selector("#content-tabs", timeout=10000)
    _вкладка_разбора(вкладка, "categories")
    assert вкладка.input_value("#cat-list .script-item .cat-title") == "Сроки, правка 54"
    assert not вкладка.is_disabled("#cat-save")
    _без_исключений(вкладка)


def test_сброс_набора_к_готовому_спрашивает_до_записи(стенд, браузер):
    """«Вернуть готовый набор» сохранял сразу; вопрос был уже про пересчёт архива."""
    состояние = стенд["состояние"]
    свой = [{"id": "k1", "label": "Доставка", "rule": "доставка", "kind": "topic"},
            {"id": "k2", "label": "Возврат", "rule": "возврат", "kind": "topic"}]
    состояние.settings.set("content_categories", свой)
    try:
        вкладка = _вкладка(браузер, стенд["адрес"])
        _открыть(вкладка, "content")
        _вкладка_разбора(вкладка, "categories")
        вопросы: list[str] = []
        вкладка.on("dialog", lambda д: (вопросы.append(д.message), д.dismiss()))
        пойманные = _перехват(вкладка, "**/api/settings?persist=true", {}, метод="PUT")
        вкладка.click("#cat-default")
        вкладка.wait_for_timeout(600)
        assert вопросы and "Вернуть готовый набор" in вопросы[0], вопросы
        assert "2 категории" in вопросы[0], вопросы[0]
        assert not пойманные, "набор записан до ответа на вопрос"
        assert состояние.settings.get("content_categories") == свой
    finally:
        состояние.settings.set("content_categories", [])


def test_чужое_сохранение_набора_не_стирается_молча(стенд, браузер):
    """А и Б правят категории; Б сохранил; А сохранил — правки Б пропали молча."""
    состояние = стенд["состояние"]
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    _вкладка_разбора(вкладка, "categories")
    вкладка.fill("#cat-list .script-item .cat-title", "Сроки (правка А)")
    чужой = [{"id": "dostavka", "label": "Доставка (Б)", "rule": "доставка", "kind": "topic"}]
    состояние.settings.set("content_categories", чужой)          # «Б» сохранил своё
    try:
        вопросы: list[str] = []
        вкладка.on("dialog", lambda д: (вопросы.append(д.message), д.dismiss()))
        вкладка.click("#cat-save")
        вкладка.wait_for_timeout(1500)
        assert вопросы and "изменили, пока шла ваша правка" in вопросы[0], вопросы
        assert состояние.settings.get("content_categories") == чужой
        assert not вкладка.is_disabled("#cat-save"), "черновик А должен остаться"
    finally:
        состояние.settings.set("content_categories", [])


# ---------------------------------------------------------------------------
# Настройки: «Сбросить», условия отображения, чужие правки
# ---------------------------------------------------------------------------


def _группа(вкладка, группа: str) -> None:
    вкладка.evaluate("(г) => { window.__asrhub.state.paramGroup = г;"
                     " window.__asrhub.renderView(); }", группа)
    вкладка.wait_for_selector("#params-body .param", timeout=10000)


def test_сбросить_не_включает_вход_и_не_трогает_сервер_до_применения(стенд, браузер):
    """POST /api/settings/reset включал вход без ключей и стирал станции."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "settings")
    _группа(вкладка, "server")
    сброс = _перехват(вкладка, "**/api/settings/reset", {})
    вопросы: list[str] = []
    вкладка.on("dialog", lambda д: (вопросы.append(д.message), д.accept()))
    вкладка.evaluate("() => { window.__asrhub.state.settings.rate_limit_per_minute = 999; }")
    вкладка.click("#p-reset")
    вкладка.wait_for_timeout(500)
    assert not сброс, "звался общий сброс сервера"
    состояние = стенд["состояние"]
    assert состояние.settings.get("auth_enabled") is False
    if вопросы:
        assert "Вход" in вопросы[0] and "не сбрасываются" in вопросы[0], вопросы[0]
    изменено = вкладка.evaluate("() => [...window.__asrhub.state.settingsChanged]")
    assert "auth_enabled" not in изменено, изменено
    _без_исключений(вкладка)


def test_параметр_с_невыполненным_условием_приглушён(стенд, браузер):
    """`requires` не читался: порог VAD выглядел действующим при выключенном VAD."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "settings")
    вкладка.evaluate("() => { const s = window.__asrhub.state; s.showAdvanced = true;"
                     " s.settings.vad_enabled = false; }")
    _группа(вкладка, "vad")
    порог = ".param[data-key='vad_threshold']"
    assert "param-off" in (вкладка.get_attribute(порог, "class") or "")
    assert "детектор речи" in вкладка.inner_text(f"{порог} .param-requires").lower()
    вкладка.click(".param[data-key='vad_enabled'] .switch")
    вкладка.wait_for_timeout(200)
    assert "param-off" not in (вкладка.get_attribute(порог, "class") or "")
    _без_исключений(вкладка)


def test_применить_предупреждает_о_чужой_правке_того_же_параметра(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "settings")
    вкладка.evaluate("() => { window.__asrhub.state.showAdvanced = true; }")
    _группа(вкладка, "queue")
    состояние = стенд["состояние"]
    прежнее = состояние.settings.get("max_queue_size")
    вкладка.fill(".param[data-key='max_queue_size'] input[type=number]", "777")
    вкладка.dispatch_event(".param[data-key='max_queue_size'] input[type=number]", "change")
    состояние.settings.set("max_queue_size", 555)                # правка второго администратора
    try:
        вопросы: list[str] = []
        вкладка.on("dialog", lambda д: (вопросы.append(д.message), д.dismiss()))
        вкладка.click("#p-apply")
        вкладка.wait_for_timeout(1000)
        assert вопросы and "изменили" in вопросы[0], вопросы
        assert состояние.settings.get("max_queue_size") == 555
    finally:
        состояние.settings.set("max_queue_size", прежнее)


# ---------------------------------------------------------------------------
# Распознавание записей: «не менять»
# ---------------------------------------------------------------------------


def test_чем_распознавать_по_умолчанию_ничего_не_меняет(стенд, браузер):
    """Поля показывали умолчания каталога, а уходило «ничего»; пунктуации не было."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "rescan", 2000)
    поля = вкладка.evaluate("""() => [...document.querySelectorAll('#rs-params label')].map((м) => {
      const п = м.querySelector('select, input');
      return [м.querySelector('span').innerText, п.tagName === 'SELECT'
        ? п.selectedOptions[0].textContent : п.value]; })""")
    for подпись, значение in поля:
        assert значение in ("— не менять —", ""), (подпись, значение)
    assert any("унктуац" in подпись for подпись, _ in поля), поля
    # Модель без движка — с движком этой модели; «не менять» снова снимает правку.
    пойманные = _перехват(вкладка, "**/api/jobs/rescan", {"queued": 1, "failed": []})
    вкладка.select_option("#rs-params label:first-child select", label=вкладка.evaluate(
        "() => [...document.querySelector('#rs-params label:first-child select').options]"
        ".find((о) => о.textContent.startsWith('GigaAM v3 RNNT')).textContent"))
    вкладка.click("#rs-table [data-one]")
    вкладка.wait_for_timeout(800)
    тело = json.loads(пойманные[0]["body"])
    assert тело["overrides"] == {"model": "gigaam-v3-rnnt", "engine": "gigaam"}, тело
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Оформление: светлая тема, липкие панели
# ---------------------------------------------------------------------------


def test_цвета_состояний_в_светлой_теме_контрастны(стенд, браузер):
    """В светлой теме оставались зелёный, жёлтый и красный тёмной: 2,2–2,9:1."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    цвета = вкладка.evaluate("""() => { document.body.classList.add('light');
      const с = getComputedStyle(document.body);
      return ['--ok', '--warn', '--err', '--info'].map((и) => с.getPropertyValue(и).trim()); }""")

    def яркость(цвет: str) -> float:
        к = [int(цвет[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        к = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in к]
        return 0.2126 * к[0] + 0.7152 * к[1] + 0.0722 * к[2]

    for цвет in цвета:
        контраст = (1.0 + 0.05) / (яркость(цвет) + 0.05)        # на белом
        assert контраст >= 4.5, (цвет, round(контраст, 2))


def test_липкая_панель_не_уезжает_под_шапку(стенд, браузер):
    """Панель липла на 58 px, а шапка при 1024 px — выше: первая строка под ней."""
    вкладка = _вкладка(браузер, стенд["адрес"], ширина=1024)
    _открыть(вкладка, "rescan", 1500)
    вкладка.evaluate("() => window.scrollTo(0, 600)")
    вкладка.wait_for_timeout(300)
    низ_шапки, верх_панели = вкладка.evaluate("""() => [
      document.querySelector('.topbar').getBoundingClientRect().bottom,
      document.querySelector('#content .settings-toolbar').getBoundingClientRect().top]""")
    assert верх_панели >= низ_шапки - 1, (низ_шапки, верх_панели)


# ---------------------------------------------------------------------------
# Графики
# ---------------------------------------------------------------------------


def _график(вкладка, код: str):
    return вкладка.evaluate("""(код) => { let узел = document.querySelector('#проба-графика');
      if (!узел) { узел = document.createElement('div'); узел.id = 'проба-графика';
        узел.style.width = '640px'; document.querySelector('#content').prepend(узел); }
      return (new Function('узел', код))(узел); }""", код)


def test_длинная_подпись_столбца_укорачивается_а_не_режется(стенд, браузер):
    """«Техническая поддержка корпоративных клиентов» → «орпоративных клиентов»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    левые = _график(вкладка, """Charts.hbars(узел, { labelWidth: 150, items: [
        { label: 'Техническая поддержка корпоративных клиентов', value: 30 },
        { label: 'Техническая поддержка частных клиентов', value: 20 }] });
      return [...узел.querySelectorAll('text[text-anchor=end]')].map((т) =>
        [т.getBBox().x, т.textContent, т.dataset.full || '']);""")
    for x, текст, полный in левые:
        assert x >= -1, (x, текст)
        assert текст.startswith("Техническая") and текст.endswith("…"), текст
        assert полный.startswith("Техническая поддержка")


def test_ось_времени_не_слипается_и_одна_точка_под_подписью(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    наложения = _график(вкладка, """
      const подписи = Array.from({ length: 38 }, (_, i) => `${String(10 + (i >> 3)).padStart(2, '0')}.10 ${String(i % 24).padStart(2, '0')}:00`);
      Charts.line(узел, { labels: подписи, series: [{ name: 'р', values: подписи.map((_, i) => i) }] });
      const рамки = [...узел.querySelectorAll('svg > text')].filter((т) => т.textContent.includes('.10'))
        .map((т) => т.getBBox()).sort((а, б) => а.x - б.x);
      const ширина = узел.querySelector('svg').viewBox.baseVal.width;
      return { наезд: рамки.slice(1).map((р, i) => рамки[i].x + рамки[i].width - р.x).filter((в) => в > 0),
               справа: рамки[рамки.length - 1].x + рамки[рамки.length - 1].width - ширина };""")
    assert not наложения["наезд"], наложения
    assert наложения["справа"] <= 0.5, наложения
    одна = _график(вкладка, """Charts.line(узел, { labels: ['09.10'], series: [{ name: 'р', values: [5] }] });
      const т = узел.querySelector('svg > text[text-anchor=middle]');
      return [Number(узел.querySelector('path[stroke]').getAttribute('d').split(',')[0].slice(1)),
              Number(т.getAttribute('x'))];""")
    assert abs(одна[0] - одна[1]) < 1, одна


def test_график_перерисовывается_по_ширине_окна(стенд, браузер):
    """Ширина зашивалась в viewBox: сузили окно — подписи становились крошечными."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    было = _график(вкладка, """узел.style.width = '100%';
      Charts.line(узел, { labels: ['a', 'b', 'c'], series: [{ name: 'р', values: [1, 2, 3] }] });
      return узел.querySelector('svg').viewBox.baseVal.width;""")
    вкладка.set_viewport_size({"width": 1000, "height": 1000})
    вкладка.wait_for_timeout(800)
    стало = вкладка.evaluate("() => document.querySelector('#проба-графика svg').viewBox.baseVal.width")
    assert стало < было - 100, (было, стало)
    ширина = вкладка.evaluate("() => document.querySelector('#проба-графика').clientWidth")
    assert abs(стало - ширина) < 2, (стало, ширина)


def test_подписи_свода_по_часам_различаются(стенд, браузер):
    """«Свод» за сутки: «09.10, 09.10, 09.10…» — одна дата на всей оси."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    вкладка.click('#content-period button[data-period="day"]')
    вкладка.wait_for_selector("#chart-tone-time svg", timeout=15000)
    вкладка.wait_for_timeout(500)
    подписи = вкладка.eval_on_selector_all("#chart-tone-time svg > text",
                                           "т => т.map((x) => x.textContent)")
    подписи = [п for п in подписи if ":" in п or "." in п]
    assert len(подписи) >= 2 and len(set(подписи)) == len(подписи), подписи


def test_карта_по_часам_подписывает_пояс_сервера(стенд, браузер):
    """Сервер по UTC, браузер в Москве — пик в 06–15 без единого слова."""
    смещение = time.localtime().tm_gmtoff
    пояс = "Asia/Vladivostok" if смещение != 36000 else "Europe/Moscow"
    контекст = браузер.new_context(viewport={"width": 1600, "height": 1000}, timezone_id=пояс)
    вкладка = контекст.new_page()
    вкладка.адрес = стенд["адрес"]
    _открыть(вкладка, "analytics", 3000)
    вкладка.wait_for_selector("#chart-hours svg", timeout=15000)
    assert "по поясу сервера" in вкладка.inner_text("#chart-hours")
    контекст.close()


# ---------------------------------------------------------------------------
# Что сервер умеет, а интерфейс не умел
# ---------------------------------------------------------------------------


def test_ключ_создаётся_с_подразделением_и_квотами(стенд_со_входом, браузер):
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=КЛЮЧ_АДМИНА)
    _открыть(вкладка, "system", 2500)
    assert "40 заданий/сутки" in вкладка.inner_text("#keys-body")
    вкладка.fill("#key-name", "склад-54")
    вкладка.evaluate("() => document.querySelector('#key-create').closest('.card')"
                     ".querySelector('details').open = true")
    вкладка.fill("#key-new-group", "склад")
    вкладка.fill("#key-new-qjobs", "25")
    вкладка.fill("#key-new-qhours", "1.5")
    вкладка.on("dialog", lambda д: д.accept())
    вкладка.click("#key-create")
    вкладка.wait_for_timeout(1500)
    ключи = стенд_со_входом["состояние"].settings.api_keys
    новый = next(с for с in ключи.values() if с.get("name") == "склад-54")
    assert новый["group"] == "склад" and новый["quota_jobs_per_day"] == 25
    assert новый["quota_audio_hours_per_day"] == 1.5
    _без_исключений(вкладка)


def test_роль_и_вход_учётной_записи_правятся_в_строке(стенд_со_входом, браузер):
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=КЛЮЧ_АДМИНА)
    _открыть(вкладка, "system", 2500)
    учётки = стенд_со_входом["состояние"].accounts
    ид = учётки.by_username("сотрудник").id
    вкладка.select_option(f"tr[data-user='{ид}'] select[data-field=role]", "readonly")
    вкладка.wait_for_timeout(800)
    assert учётки.get(ид).role == "readonly"
    вкладка.fill(f"tr[data-user='{ид}'] input[data-field=group]", "продажи")
    вкладка.dispatch_event(f"tr[data-user='{ид}'] input[data-field=group]", "change")
    вкладка.wait_for_timeout(800)
    assert учётки.get(ид).group == "продажи"
    учётки.update(ид, role="user", group="")
    _без_исключений(вкладка)


def test_расход_квоты_виден(стенд_со_входом, браузер):
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=КЛЮЧ_ПОЛЬЗОВАТЕЛЯ)
    _открыть(вкладка, "system", 2500)
    текст = вкладка.inner_text("#usage-body")
    assert "из 40" in текст and "продажи" in текст, текст
    # Пользователю не видны формы администратора.
    assert not вкладка.is_visible("#key-create")
    assert not вкладка.is_visible("#erase-query")


def test_удаление_по_требованию_сначала_ищет_потом_удаляет(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "system", 2500)
    вызовы = _перехват(вкладка, "**/api/maintenance/erase",
                       {"dry_run": True, "matched": 3, "calls": 2, "ids": [], "limit": 500})
    вкладка.fill("#erase-query", "+79001110000")
    вкладка.click("#erase-find")
    вкладка.wait_for_timeout(600)
    assert json.loads(вызовы[0]["body"]) == {"query": "+79001110000", "dry_run": True}
    assert "3" in вкладка.inner_text("#erase-out")
    вкладка.on("dialog", lambda д: д.dismiss())
    вкладка.click("#erase-go")
    вкладка.wait_for_timeout(400)
    assert len(вызовы) == 1, "удаление без подтверждения"
    _без_исключений(вкладка)


def test_агента_можно_включить_из_раздела(стенд, браузер):
    """Сервер говорит «Включите его в разделе «АТС»», а кнопки не было."""
    db = стенд["состояние"].db
    db.agent_save("pbx54", {"name": "станция-54", "station": "ats1", "enabled": False})
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "pbx", 1500)
    вкладка.click('#pbx-tabs button[data-tab="agents"]')
    вкладка.wait_for_selector("[data-enable][data-agent='pbx54']", timeout=10000)
    вкладка.click("[data-enable][data-agent='pbx54']")
    вкладка.wait_for_timeout(1200)
    assert db.agent_get("pbx54")["enabled"]
    _без_исключений(вкладка)


def test_правила_тревог_правятся_и_сохраняются_целиком(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "monitoring", 3000)
    вкладка.click("#mon-rules-box summary")
    вкладка.wait_for_selector("#mon-rules tr[data-rule]", timeout=10000)
    всего = вкладка.locator("#mon-rules tr[data-rule]").count()
    пойманные = _перехват(вкладка, "**/api/monitoring/alerts/rules", {"rules": всего}, метод="PUT")
    вкладка.fill("#mon-rules tr[data-rule='0'] [data-f=threshold]", "12345")
    вкладка.dispatch_event("#mon-rules tr[data-rule='0'] [data-f=threshold]", "change")
    вкладка.click("#mon-rules-save")
    вкладка.wait_for_timeout(600)
    набор = json.loads(пойманные[0]["body"])
    assert len(набор) == всего, "правила каталога потерялись бы"
    assert набор[0]["threshold"] == 12345
    assert "История срабатываний" in вкладка.inner_text("#content")
    assert вкладка.locator("#mon-history").count() == 1
    _без_исключений(вкладка)


def test_приёмник_influxdb_заводится_с_базой_и_заголовком(стенд, браузер):
    """Из интерфейса InfluxDB с токеном не завести: заголовков и базы не было."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "monitoring", 3000)
    пойманные = _перехват(вкладка, "**/api/monitoring/targets",
                          {"target": {"name": "influxdb"}, "persisted": True}, метод="POST")
    вкладка.click("#mon-add-target")
    вкладка.select_option("#tg-kind", "influxdb")
    вкладка.fill("#tg-url", "http://influx:8086")
    вкладка.fill("#tg-database", "звонки")
    вкладка.fill("#tg-headers", "Authorization: Token секрет")
    вкладка.click("#tg-save")
    вкладка.wait_for_timeout(600)
    тело = json.loads(пойманные[0]["body"])
    assert тело["database"] == "звонки"
    assert тело["headers"] == {"Authorization": "Token секрет"}
    _без_исключений(вкладка)


def test_импорт_по_адресу_спрашивает_про_увольнения(стенд, браузер):
    """Выгрузка одного отдела по адресу «увольняла» всех остальных без вопроса."""
    состояние = стенд["состояние"]
    состояние.settings.set("employees_url", "http://портал/сотрудники.xml")
    вкладка = _вкладка(браузер, стенд["адрес"])
    try:
        _открыть(вкладка, "staff", 2000)
        вызовы = _перехват(вкладка, "**/api/employees/import",
                           {"rows": 1, "added": 0, "updated": 1, "deactivated": 2})
        вопросы: list[str] = []
        вкладка.on("dialog", lambda д: (вопросы.append(д.message), д.dismiss()))
        вкладка.click("#st-import")
        вкладка.wait_for_timeout(800)
        assert json.loads(вызовы[0]["body"]).get("dry_run") is True
        assert len(вызовы) == 1, "импорт ушёл без подтверждения"
        assert вопросы and "уволенными: 2" in вопросы[0], вопросы
    finally:
        состояние.settings.set("employees_url", "")


def test_отправку_файлов_можно_отменить(стенд, браузер):
    """Отменить загрузку было нечем, кроме закрытия вкладки."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    висят: list = []
    вкладка.route("**/api/jobs", lambda маршрут: висят.append(маршрут))
    вкладка.evaluate("() => { const s = window.__asrhub.state;"
                     " s.files = [new File(['x'.repeat(1000)], 'долгий.wav')];"
                     " document.querySelector('#btn-submit').disabled = false;"
                     " document.querySelector('#btn-submit').click(); }")
    вкладка.wait_for_timeout(800)
    assert вкладка.inner_text("#btn-submit").startswith("Отправка")
    assert вкладка.inner_text("#btn-clear-files") == "Отменить отправку"
    вкладка.click("#btn-clear-files")
    вкладка.wait_for_timeout(500)
    assert "Отправка отменена" in вкладка.inner_text("#toasts")
    assert вкладка.evaluate("() => window.__asrhub.state.files.length") == 1
    assert not вкладка.evaluate("() => window.__asrhub.state.отправка")
    for маршрут in висят:
        try:
            маршрут.abort()
        except Exception:                                    # noqa: BLE001
            pass
    _без_исключений(вкладка)


def test_опрос_сбора_архива_одной_цепочкой(стенд, браузер):
    """Каждый заход на «Разбор» заводил ещё одну цепочку опроса."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "pbx", 1500)
    _перехват(вкладка, "**/api/telephony/collect", {"runs": [{"station": "ats1", "running": True,
                                                            "passes": 1, "seen": 3}]})
    for _ in range(4):
        вкладка.click('#pbx-tabs button[data-tab="diag"]')
        вкладка.wait_for_timeout(300)
        вкладка.click('#pbx-tabs button[data-tab="overview"]')
        вкладка.wait_for_timeout(300)
    вкладка.click('#pbx-tabs button[data-tab="diag"]')
    вкладка.wait_for_timeout(1000)
    было = len(вкладка.запросы)
    вкладка.wait_for_timeout(6500)
    опросов = sum(1 for з in вкладка.запросы[было:] if "/api/telephony/collect" in з)
    assert опросов <= 3, опросов
    _без_исключений(вкладка)


def test_простаивающее_окно_не_тратит_предел_частоты(стенд, браузер):
    """Окно без дела — около 32 запросов в минуту; три-четыре окна логина — и 429."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue", 2500)
    было = len(вкладка.запросы)
    вкладка.wait_for_timeout(12500)
    на_очереди = [з for з in вкладка.запросы[было:] if "/api/" in з]
    # Прежде: очередь, счётчик тревог и таблица на каждом из трёх тактов.
    assert len(на_очереди) <= 6, на_очереди
    _открыть(вкладка, "analytics", 2500)
    было = len(вкладка.запросы)
    вкладка.wait_for_timeout(16500)
    очередь = [з for з in вкладка.запросы[было:] if з.endswith("/api/queue")]
    assert len(очередь) <= 2, очередь
    _без_исключений(вкладка)


def test_пунктуация_меняется_при_перераспознавании(стенд, браузер):
    """Ключа `punctuation` в каталоге нет, а `punctuation_enabled` сервер отбрасывал."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    db = стенд["состояние"].db
    try:
        ответ = вкладка.request.post(f"{стенд['адрес']}/api/jobs/rescan", data={
            "ids": ["fmt-pcm"], "overrides": {"punctuation_enabled": False}})
        assert ответ.ok, ответ.text()
        assert ответ.json()["queued"] == 1, ответ.json()
        assert db.get_job("fmt-pcm")["params"].get("punctuation_enabled") is False
    finally:
        db.update_job("fmt-pcm", status="completed")
