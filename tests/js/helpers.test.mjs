// Чистые помощники веб-интерфейса — без браузера: `node --test tests/js/*.test.mjs`.
//
// app.js — один файл без модулей, поэтому функции берутся из его текста по
// имени и исполняются в отдельном контексте vm. Проверяются те, на которых
// держится безопасность разметки и разбор чисел.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const текст = readFileSync(new URL("../../server/asrhub/web/app.js", import.meta.url), "utf8");

function функция(имя) {
  const начало = текст.indexOf(`function ${имя}(`);
  assert.ok(начало >= 0, `нет функции ${имя}`);
  const конец = текст.indexOf("\n}\n", начало);
  return текст.slice(начало, конец + 2);
}

function класс(имя) {
  const начало = текст.indexOf(`class ${имя} {`);
  assert.ok(начало >= 0, `нет класса ${имя}`);
  return текст.slice(начало, текст.indexOf("\n}\n", начало) + 2);
}

const контекст = vm.createContext({});
vm.runInContext([класс("Разметка"),
  ...["esc", "jsArg", "act", "числоЯчейки", "raw", "вставкаРазметки", "html"].map(функция),
  "globalThis.html = html; globalThis.raw = raw; globalThis.Разметка = Разметка;"].join("\n"),
контекст);

test("esc закрывает разметку и апостроф", () => {
  assert.equal(контекст.esc(`<a href="x">'&`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;");
  assert.equal(контекст.esc(null), "");
});

test("act кладёт аргументы JSON в атрибут, а не кодом", () => {
  const атрибуты = контекст.act("openJob", "a');alert(1);//", 5);
  assert.match(атрибуты, /^ data-act="openJob" data-args="/);
  assert.ok(!атрибуты.includes("'"), атрибуты);
  const json = атрибуты.split('data-args="')[1].slice(0, -1)
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  assert.deepEqual(JSON.parse(json), ["a');alert(1);//", 5]);
});

test("числоЯчейки читает русскую запись чисел", () => {
  assert.equal(контекст.числоЯчейки("1 234,5"), 1234.5);
  assert.equal(контекст.числоЯчейки("12 %"), 12);
  assert.equal(контекст.числоЯчейки("−3"), -3);
  assert.equal(контекст.числоЯчейки("—"), null);
});

test("html`…` экранирует подстановки, а готовую разметку берёт как есть", () => {
  const { html, raw } = контекст;
  const имя = `<img src=x onerror="alert(1)">'`;
  assert.equal(String(html`<b title="${имя}">${имя}</b>`),
    "<b title=\"&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;\">"
    + "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;</b>");
  // Вложенный шаблон и raw() — без второго экранирования.
  assert.equal(String(html`<p>${html`<i>${"a&b"}</i>`}${raw("<hr>")}</p>`),
    "<p><i>a&amp;b</i><hr></p>");
  // Массивы склеиваются; null, undefined и false — пусто; 0 — печатается.
  assert.equal(String(html`${["<", html`<br>`]}|${null}|${undefined}|${false}|${0}`),
    "&lt;<br>||||0");
  // В обычный шаблон и в innerHTML — строкой.
  assert.equal(`${html`<u>${"x"}</u>`}`, "<u>x</u>");
  const готово = html`<s></s>`;
  assert.equal(raw(готово), готово, "raw() не оборачивает готовую разметку второй раз");
  assert.ok(готово instanceof контекст.Разметка);
});
