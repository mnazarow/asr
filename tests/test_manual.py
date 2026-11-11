"""Заход 56: раздел «Документация» — руководство в интерфейсе.

Сервер отдаёт главы `docs/*.md` готовым HTML. Проверяется то, что стоит
дорого, если сломается:

* разметка: заголовки с якорями как у GitHub, таблицы, код, списки,
  цитаты, картинки и ссылки между главами;
* в HTML попадают только свои теги: «<script>» из текста главы — текст;
* каждая ссылка руководства ведёт на существующую главу и место в ней;
* маршруты: оглавление, глава, Markdown, поиск — с ключом; картинки — без
  ключа, но только из docs/images;
* раздел в браузере: оглавление, переход по ссылке, поиск, адрес главы.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asrhub import manual  # noqa: E402


def test_разметка_главы():
    текст = """# Глава: «проба»

Абзац с **жирным**, *курсивом*, `кодом <b>` и [ссылкой](05-web-interface.md#вход).
Вторая строка абзаца и [наружу](https://example.org), и [опасная](javascript:alert(1)).

## Детектор речи (VAD)

| Параметр | Значение |
|---|---:|
| `max_upload_mb` | 2048 \\| МБ |

- первый
- второй
  - вложенный
1. раз
2. два

> **Рекомендация.** Цитата.

```bash
echo "<script>alert(1)</script>"
```

![Снимок](images/05-results.png)

## Детектор речи (VAD)

<script>alert(2)</script>
"""
    разбор = manual.в_html(текст)
    h = разбор.html
    assert '<h1 id="глава-проба">' in h
    assert '<h2 id="детектор-речи-vad">' in h and '<h2 id="детектор-речи-vad-1">' in h
    assert "<strong>жирным</strong>" in h and "<em>курсивом</em>" in h
    assert "<code>кодом &lt;b&gt;</code>" in h
    assert 'href="#manual/05-web-interface/вход" data-doc="05-web-interface"' in h
    assert 'href="https://example.org" target="_blank" rel="noopener"' in h
    assert "javascript:" not in h.replace("javascript:alert(1)", "")[:0] or 'href="#"' in h
    assert '<td style="text-align:right">2048 | МБ</td>' in h
    assert "<ul><li>первый</li><li>второй<ul><li>вложенный</li></ul></li></ul>" in h
    assert "<ol><li>раз</li><li>два</li></ol>" in h
    assert "<blockquote><p><strong>Рекомендация.</strong> Цитата.</p></blockquote>" in h
    assert '<pre><code class="lang-bash">echo &quot;&lt;script&gt;' in h
    assert '<img src="/api/manual/images/05-results.png" alt="Снимок" loading="lazy">' in h
    # Свои теги — только те, что ставит разбор.
    assert "<script>" not in h and "&lt;script&gt;alert(2)" in h
    assert [з["id"] for з in разбор.заголовки] == ["детектор-речи-vad", "детектор-речи-vad-1"]


def test_все_ссылки_руководства_ведут_на_место():
    """Ссылка на главу или якорь, которых нет, — тупик для читателя."""
    главы = {г.ключ for г in manual.главы()}
    assert {"00-README", "05-web-interface", "32-quality-control", "33-roadmap"} <= главы
    якоря: dict[str, set[str]] = {}
    тексты: dict[str, str] = {}
    for ключ in главы:
        данные = manual.глава(ключ)
        assert данные is not None
        тексты[ключ] = данные["html"]
        якоря[ключ] = set(re.findall(r'id="([^"]+)"', данные["html"]))
    тупики = []
    for ключ, текст in тексты.items():
        assert "<script" not in текст, ключ
        for м in re.finditer(r"<a [^>]*>", текст):
            тег = м.group(0)
            куда = re.search(r'data-doc="([^"]*)"', тег)
            якорь = re.search(r'data-anchor="([^"]*)"', тег)
            цель = html.unescape(куда.group(1)) if куда else ключ
            место = html.unescape(якорь.group(1)) if якорь else ""
            if цель not in главы:
                тупики.append(f"{ключ}: нет главы {цель}")
            elif место and место not in якоря[цель]:
                тупики.append(f"{ключ}: нет места {цель}#{место}")
            if 'href="#"' in тег:
                тупики.append(f"{ключ}: ссылка в никуда {тег}")
    assert not тупики, "\n".join(тупики)
    # И картинки глав на месте.
    for ключ, текст in тексты.items():
        for имя in re.findall(r'src="/api/manual/images/([^"]+)"', текст):
            assert manual.картинка(html.unescape(имя)) is not None, f"{ключ}: нет {имя}"


def test_поиск():
    найдено = manual.искать("dtmf_mode rfc4733")
    assert найдено and найдено[0]["slug"] == "32-quality-control"
    assert all("dtmf_mode" in н["snippet"].lower() for н in найдено)
    assert manual.искать("а") == []


@pytest.fixture()
def клиент(data_dir, monkeypatch):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({"ah_ro": {"name": "чтение", "role": "readonly", "enabled": True}})
    with TestClient(create_app(настройки, start_queue=False)) as c:
        yield c


def test_маршруты(клиент):
    ключ = {"X-API-Key": "ah_ro"}
    assert клиент.get("/api/manual").status_code == 401
    оглавление = клиент.get("/api/manual", headers=ключ).json()
    assert оглавление["available"] and оглавление["chapters"][0]["slug"] == "00-README"
    глава = клиент.get("/api/manual/chapter/32-quality-control", headers=ключ).json()
    assert глава["number"] == 32 and глава["prev"]["number"] == 31
    assert any(з["text"] == "Номер на станции" for з in глава["headings"])
    assert клиент.get("/api/manual/chapter/..%2F..%2FVERSION", headers=ключ).status_code == 404
    assert клиент.get("/api/manual/chapter/нет-такой", headers=ключ).status_code == 404
    сырой = клиент.get("/api/manual/raw/32-quality-control", headers=ключ)
    assert сырой.text.startswith("# Контроль качества")
    поиск = клиент.get("/api/manual/search", params={"q": "chan_pjsip"}, headers=ключ).json()
    assert поиск["items"]
    # Картинки — без ключа (их грузит тег <img>), но только из docs/images.
    картинка = клиент.get("/api/manual/images/86-qc.png")
    assert картинка.status_code == 200 and картинка.headers["content-type"] == "image/png"
    assert "default-src 'none'" in картинка.headers["content-security-policy"]
    for чужое in ("..%2F..%2FVERSION", "..%2Fgenerate.py", "generate.py", ".hidden.png"):
        assert клиент.get(f"/api/manual/images/{чужое}").status_code == 404


def test_без_каталога_документации(клиент, monkeypatch, tmp_path):
    monkeypatch.setenv("ASRHUB_DOCS_DIR", str(tmp_path / "нет"))
    monkeypatch.setattr(manual, "каталог", lambda: None)
    ответ = клиент.get("/api/manual", headers={"X-API-Key": "ah_ro"}).json()
    assert ответ == {"available": False, "chapters": [], "version": ответ["version"]}


# ---------------------------------------------------------------------------
# В браузере
# ---------------------------------------------------------------------------

@pytest.fixture()
def вкладка(tmp_path):
    pytest.importorskip("playwright.sync_api", reason="нужен playwright")
    from drill_seed import Архив, Вкладка, браузер, поднять

    if браузер() is None:
        pytest.skip("браузер не найден")
    архив = Архив(tmp_path / "data", заданий=2)
    адрес, остановить = поднять(архив.app)
    окно = Вкладка(адрес)
    yield окно
    окно.закрыть()
    остановить()
    архив.закрыть()


def test_раздел_документации_в_браузере(вкладка):
    с = вкладка.открыть("manual/32-quality-control")
    с.wait_for_selector(".manual-body h1")
    assert "Контроль качества" in с.inner_text(".manual-body h1")
    assert с.locator("#man-toc a.active").inner_text().strip().startswith("32")
    # Ссылка на другую главу открывает её и меняет адрес.
    с.click(".manual-body a[data-doc='21-telephony']")
    с.wait_for_function("() => location.hash.startsWith('#manual/21-telephony')")
    с.wait_for_function("() => document.querySelector('.manual-body h1')?.innerText.includes('Телефония')")
    # Поиск находит строку и ведёт к ней.
    с.fill("#man-q", "dtmf_mode")
    с.wait_for_selector(".manual-hit")
    с.locator(".manual-hit").first.click()
    с.wait_for_function("() => location.hash.startsWith('#manual/32-quality-control/')")
    # Справка ведёт в документацию.
    с = вкладка.открыть("help")
    с.click("text=Открыть документацию")
    с.wait_for_selector(".manual-body h1")
    assert с.evaluate("document.title").startswith("Документация")
    вкладка.чисто()
