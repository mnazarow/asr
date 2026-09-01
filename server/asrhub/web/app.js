/* ASR Hub — веб-интерфейс. Без сборки и без внешних зависимостей. */
(function () {
'use strict';

// ==========================================================================
// Состояние и утилиты
// ==========================================================================

const state = {
  view: 'transcribe',
  catalog: null,
  params: null,
  presets: [],
  engines: [],
  models: [],
  settings: {},
  queue: null,
  system: null,
  analytics: null,
  period: 'week',
  jobSettings: {},
  selectedJob: null,
  files: [],
  paramGroup: 'model',
  showAdvanced: false,
  paramSearch: '',
  compare: [],
  ws: null,
  wsRetry: 0,
  timer: null,
};

const API = {
  async call(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    const key = localStorage.getItem('asrhub_key');
    if (key) opts.headers['X-API-Key'] = key;
    if (opts.json !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      throw { code: 'network', message: 'Сервер недоступен',
              hint: 'Проверьте, что служба asrhub запущена и доступна по сети.' };
    }
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!response.ok) {
      const detail = (data && data.detail) || data || {};
      throw {
        code: detail.code || `http_${response.status}`,
        message: detail.message || `Ошибка ${response.status}`,
        hint: detail.hint || '',
        status: response.status,
      };
    }
    return data;
  },
  get(path) { return this.call(path); },
  post(path, body) { return this.call(path, { method: 'POST', json: body === undefined ? {} : body }); },
  put(path, body) { return this.call(path, { method: 'PUT', json: body }); },
  del(path) { return this.call(path, { method: 'DELETE' }); },
};

function h(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}
function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function qs(sel, root) { return (root || document).querySelector(sel); }
function qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

function toast(message, kind, hint) {
  const box = qs('#toasts');
  const node = h(`<div class="toast ${kind || ''}">
    <div class="t-title">${esc(message)}</div>
    ${hint ? `<div class="t-hint">${esc(hint)}</div>` : ''}
  </div>`);
  box.appendChild(node);
  setTimeout(() => { node.style.opacity = '0'; setTimeout(() => node.remove(), 250); },
    kind === 'err' ? 9000 : 4200);
}
function fail(err) {
  console.error(err);
  toast(err.message || 'Ошибка', 'err', err.hint);
}

function fmtDur(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  seconds = Math.max(0, Number(seconds));
  const h1 = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h1) return `${h1}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit' });
}
function fmtAgo(ts) {
  if (!ts) return '—';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return 'только что';
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return `${Math.floor(diff / 86400)} дн назад`;
}
function fmtBytes(bytes) {
  if (!bytes) return '—';
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let value = bytes, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}
function num(value, digits) { return window.Charts.fmtNum(value, digits); }
function pct(value, digits) {
  if (value === null || value === undefined) return '—';
  return (value * 100).toFixed(digits === undefined ? 1 : digits) + ' %';
}

const STATUS_LABELS = {
  queued: 'в очереди', running: 'обработка', completed: 'готово',
  failed: 'ошибка', cancelled: 'отменено', paused: 'пауза', retry: 'повтор',
};
const STATUS_CLASS = {
  completed: 'ok', failed: 'err', cancelled: '', running: 'accent',
  queued: '', retry: 'warn', paused: 'warn',
};
const QUALITY_LABELS = {
  excellent: 'отличное', good: 'хорошее', fair: 'среднее',
  poor: 'слабое', none: 'нет русского',
};
const QUALITY_CLASS = {
  excellent: 'ok', good: 'accent', fair: 'warn', poor: 'err', none: '',
};

// ==========================================================================
// Загрузка данных
// ==========================================================================

async function bootstrap() {
  try {
    const [catalog, settings] = await Promise.all([
      API.get('/api/catalog'),
      API.get('/api/settings').catch(() => ({ values: {} })),
    ]);
    state.catalog = catalog;
    state.models = catalog.models;
    state.params = catalog.params;
    state.presets = catalog.presets;
    state.settings = settings.values || {};
    state.jobSettings = Object.assign({}, state.settings);
    qs('#badge-models').textContent = catalog.models.length;
    qs('#badge-params').textContent = catalog.params.length;
    await Promise.all([refreshEngines(), refreshQueue()]);
    connectWs();
    render();
    state.timer = setInterval(tick, 4000);
  } catch (err) {
    if (err.status === 401) { promptKey(); return; }
    fail(err);
    qs('#content').innerHTML = `<div class="card"><div class="empty">
      <b>Не удалось связаться с сервером</b><div class="small" style="margin-top:8px">
      ${esc(err.message)}<br>${esc(err.hint || '')}</div></div></div>`;
  }
}

function promptKey() {
  const content = qs('#content');
  content.innerHTML = '';
  const card = h(`<div class="card" style="max-width:520px;margin:60px auto">
    <div class="card-head"><h2>Требуется ключ доступа</h2></div>
    <p class="dim small">Сервер запущен с включённой аутентификацией. Ключ, созданный
    при первом запуске, находится в файле <span class="mono">api-key.txt</span>
    в каталоге данных сервера.</p>
    <div class="stack" style="gap:10px">
      <input type="password" id="key-input" placeholder="ah_…" autocomplete="off">
      <button class="primary" id="key-save">Сохранить и войти</button>
    </div></div>`);
  content.appendChild(card);
  qs('#key-save').onclick = () => {
    localStorage.setItem('asrhub_key', qs('#key-input').value.trim());
    location.reload();
  };
  qs('#key-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') qs('#key-save').click();
  });
}

async function refreshEngines() {
  try { state.engines = (await API.get('/api/engines')).items; } catch (e) { /* не критично */ }
}
async function refreshQueue() {
  try {
    state.queue = await API.get('/api/queue');
    const depth = state.queue.queue_depth || 0;
    qs('#badge-queue').textContent = depth;
    qs('#chip-queue').textContent = `очередь: ${depth}`;
    const busy = (state.queue.workers || []).filter((w) => w.busy).length;
    qs('#chip-workers').textContent = `воркеры: ${busy}/${state.queue.worker_count || 0}`;
  } catch (e) { /* не критично */ }
}

function tick() {
  refreshQueue().then(() => {
    if (state.view === 'queue' || state.view === 'transcribe') renderView(true);
  });
}

// ==========================================================================
// WebSocket
// ==========================================================================

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const key = localStorage.getItem('asrhub_key');
  const url = `${proto}://${location.host}/ws${key ? `?api_key=${encodeURIComponent(key)}` : ''}`;
  try { state.ws = new WebSocket(url); } catch (e) { return; }

  state.ws.onopen = () => {
    state.wsRetry = 0;
    qs('#conn-status').innerHTML = '<span class="status-dot ok"></span>подключено';
  };
  state.ws.onclose = () => {
    qs('#conn-status').innerHTML = '<span class="status-dot err"></span>нет связи';
    state.wsRetry++;
    setTimeout(connectWs, Math.min(30000, 1500 * state.wsRetry));
  };
  state.ws.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (e) { return; }
    handleEvent(message);
  };
}

function handleEvent(message) {
  switch (message.type) {
    case 'job.completed':
      toast(`Задание готово (RTF ${num(message.rtf, 3)})`, 'ok');
      refreshQueue(); if (state.view !== 'settings') renderView(true);
      break;
    case 'job.failed':
      toast('Задание завершилось ошибкой', 'err',
            (message.error && message.error.message) || '');
      refreshQueue(); renderView(true);
      break;
    case 'job.retry':
      toast(`Повтор ${message.attempt} через ${message.delay_s} с`, 'warn', message.error);
      break;
    case 'job.progress':
      updateProgress(message);
      break;
    case 'job.queued':
      refreshQueue();
      break;
    case 'queue.paused': toast('Очередь приостановлена', 'warn'); refreshQueue(); break;
    case 'queue.resumed': toast('Очередь возобновлена', 'ok'); refreshQueue(); break;
  }
}

function updateProgress(message) {
  qsa(`[data-progress="${message.id}"]`).forEach((node) => {
    const bar = qs('span', node);
    if (bar) bar.style.width = `${(message.progress * 100).toFixed(1)}%`;
  });
  qsa(`[data-stage="${message.id}"]`).forEach((node) => {
    node.textContent = `${message.stage} · ${(message.progress * 100).toFixed(0)} %`;
  });
}

// ==========================================================================
// Навигация
// ==========================================================================

const VIEWS = {
  transcribe: { title: 'Транскрибация', subtitle: 'Загрузка файлов и распознавание речи' },
  queue:      { title: 'Очередь', subtitle: 'Управление заданиями, приоритетами и воркерами' },
  results:    { title: 'Результаты', subtitle: 'Выполненные задания и выгрузка' },
  analytics:  { title: 'Аналитика', subtitle: 'Показатели производительности и качества' },
  models:     { title: 'Модели', subtitle: 'Каталог моделей, лицензии, требования, загрузка весов' },
  compare:    { title: 'Сравнение моделей', subtitle: 'Качество, скорость и лицензии рядом' },
  settings:   { title: 'Настройки', subtitle: 'Все параметры с описаниями, рекомендациями и примерами' },
  system:     { title: 'Сервер', subtitle: 'Оборудование, движки, хранилище, ключи доступа' },
  logs:       { title: 'Журнал', subtitle: 'События сервера и заданий' },
  help:       { title: 'Справка', subtitle: 'Как пользоваться, программный интерфейс, устранение неполадок' },
};

function go(view) {
  state.view = view;
  qsa('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  const meta = VIEWS[view] || { title: view, subtitle: '' };
  qs('#view-title').textContent = meta.title;
  qs('#view-subtitle').textContent = meta.subtitle;
  location.hash = view;
  renderView();
}

function render() {
  const hash = location.hash.replace('#', '');
  go(VIEWS[hash] ? hash : 'transcribe');
}

function renderView(soft) {
  const content = qs('#content');
  const renderer = RENDERERS[state.view];
  if (!renderer) { content.innerHTML = ''; return; }
  if (soft && renderer.soft) { renderer.soft(); return; }
  content.innerHTML = '';
  renderer.render(content);
}

window.addEventListener('hashchange', render);

document.addEventListener('DOMContentLoaded', () => {
  qsa('.nav-item').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  qs('#btn-refresh').addEventListener('click', () => {
    refreshQueue().then(() => renderView());
    toast('Обновлено');
  });
  if (localStorage.getItem('asrhub_theme') === 'light') document.body.classList.add('light');
  qs('#theme-toggle').addEventListener('click', () => {
    document.body.classList.toggle('light');
    localStorage.setItem('asrhub_theme',
      document.body.classList.contains('light') ? 'light' : 'dark');
    renderView();
  });
  installHotkeys();
  bootstrap();
});

// --------------------------------------------------------------------------
// Горячие клавиши
// --------------------------------------------------------------------------

const HOTKEY_VIEWS = ['transcribe', 'queue', 'results', 'analytics', 'models',
                      'compare', 'settings', 'system', 'logs', 'help'];

const HOTKEY_HELP = [
  ['1 … 0', 'переход к разделу по номеру'],
  ['/', 'поиск в текущем разделе'],
  ['u', 'выбрать файлы для загрузки'],
  ['r', 'обновить данные раздела'],
  ['t', 'переключить светлую и тёмную тему'],
  ['Esc', 'закрыть карточку или диалог'],
  ['?', 'этот список'],
];

// Поля ввода имеют приоритет: пока курсор в них, буквенные сокращения молчат,
// иначе набрать «текст» в поиске было бы невозможно.
function inEditable(target) {
  if (!target) return false;
  const tag = (target.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
}

function closeTopModal() {
  const modals = qsa('.modal-backdrop');
  if (!modals.length) return false;
  modals[modals.length - 1].remove();
  return true;
}

function focusSearch() {
  const field = qs('#content input[type="search"]')
    || qsa('#content input[type="text"]').find((i) => /поиск|найти/i.test(i.placeholder || ''))
    || qs('#content input[type="text"]');
  if (field) { field.focus(); field.select(); return true; }
  return false;
}

function showHotkeys() {
  const rows = HOTKEY_HELP
    .map(([key, what]) => `<tr><td><kbd>${esc(key)}</kbd></td><td>${esc(what)}</td></tr>`)
    .join('');
  const backdrop = h(`<div class="modal-backdrop"><div class="modal" style="max-width:520px">
    <div class="modal-head"><b>Горячие клавиши</b><span class="spacer"></span>
      <button class="ghost icon" id="hk-close">✕</button></div>
    <div class="modal-body"><table class="table"><tbody>${rows}</tbody></table>
      <p class="hint" style="margin-top:12px">Буквенные сокращения не срабатывают,
        пока курсор находится в поле ввода.</p></div>
  </div></div>`);
  document.body.appendChild(backdrop);
  qs('#hk-close', backdrop).onclick = () => backdrop.remove();
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
}

function installHotkeys() {
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (e.key === 'Escape') {
      if (closeTopModal()) e.preventDefault();
      else if (inEditable(e.target)) e.target.blur();
      return;
    }
    if (inEditable(e.target)) return;

    if (e.key >= '0' && e.key <= '9') {
      const index = e.key === '0' ? 9 : Number(e.key) - 1;
      const view = HOTKEY_VIEWS[index];
      if (view) { e.preventDefault(); go(view); }
      return;
    }

    switch (e.key) {
      case '/':
        if (focusSearch()) e.preventDefault();
        break;
      case '?':
        e.preventDefault(); showHotkeys();
        break;
      // Раскладка может быть русской — обрабатываем обе буквы на клавише.
      case 'u': case 'U': case 'г': case 'Г': {
        const input = qs('#file-input');
        if (state.view !== 'transcribe') { go('transcribe'); setTimeout(() => qs('#file-input') && qs('#file-input').click(), 60); }
        else if (input) input.click();
        e.preventDefault();
        break;
      }
      case 'r': case 'R': case 'к': case 'К':
        e.preventDefault();
        refreshQueue().then(() => renderView());
        toast('Обновлено');
        break;
      case 't': case 'T': case 'е': case 'Е':
        e.preventDefault();
        qs('#theme-toggle').click();
        break;
      default:
        break;
    }
  });
}

const RENDERERS = {};
window.__asrhub = { state, API, RENDERERS, go, toast, renderView, showHotkeys };

// ==========================================================================
// Общие компоненты
// ==========================================================================

function kpi(label, value, sub, trend) {
  return `<div class="kpi">
    <div class="kpi-label">${esc(label)}</div>
    <div class="kpi-value">${value}</div>
    ${sub ? `<div class="kpi-sub">${sub}</div>` : ''}
    ${trend ? `<div class="kpi-trend ${trend.dir}">${esc(trend.text)}</div>` : ''}
  </div>`;
}

function card(title, hint, body, actions) {
  return `<section class="card">
    <div class="card-head"><h3>${esc(title)}</h3>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}
      <span class="spacer"></span>${actions || ''}</div>
    ${body}</section>`;
}

function statusChip(status) {
  return `<span class="chip ${STATUS_CLASS[status] || ''}">${STATUS_LABELS[status] || status}</span>`;
}

function modelById(id) { return state.models.find((m) => m.id === id); }
function paramByKey(key) { return (state.params || []).find((p) => p.key === key); }

/** Рисует поле ввода для одного параметра. */
function paramControl(spec, value, onChange, compact) {
  const id = `p_${spec.key}`;
  let control;

  if (spec.type === 'bool') {
    control = h(`<label class="switch"><input type="checkbox" id="${id}"
      ${value ? 'checked' : ''}><span class="track"></span></label>`);
    qs('input', control).addEventListener('change', (e) => onChange(e.target.checked));
  } else if (spec.type === 'enum') {
    const options = (spec.options && spec.options.length)
      ? spec.options
      : dynamicOptions(spec.key);
    control = h(`<select id="${id}">${options.map((o) =>
      `<option value="${esc(o.value)}" ${String(o.value) === String(value) ? 'selected' : ''}>${
        esc(o.label)}</option>`).join('')}</select>`);
    control.addEventListener('change', (e) => {
      const raw = e.target.value;
      const opt = options.find((o) => String(o.value) === raw);
      onChange(opt && typeof opt.value === 'number' ? Number(raw) : raw);
    });
  } else if (spec.type === 'multi') {
    control = h(`<div class="stack" style="gap:5px"></div>`);
    (spec.options || []).forEach((opt) => {
      const checked = Array.isArray(value) && value.includes(opt.value);
      const row = h(`<label class="row" style="gap:7px;font-size:12.5px;cursor:pointer">
        <input type="checkbox" ${checked ? 'checked' : ''} value="${esc(opt.value)}"
          style="width:auto">${esc(opt.label)}</label>`);
      qs('input', row).addEventListener('change', () => {
        const picked = qsa('input:checked', control).map((i) => i.value);
        onChange(picked);
      });
      control.appendChild(row);
    });
  } else if (spec.type === 'int' || spec.type === 'float') {
    const step = spec.step || (spec.type === 'int' ? 1 : 0.1);
    control = h(`<div class="row" style="gap:8px">
      <input type="range" min="${spec.minimum ?? 0}" max="${spec.maximum ?? 100}"
        step="${step}" value="${value ?? spec.default}" style="flex:1">
      <input type="number" min="${spec.minimum ?? ''}" max="${spec.maximum ?? ''}"
        step="${step}" value="${value ?? spec.default}"
        style="width:88px;text-align:right" class="mono">
      ${spec.unit ? `<span class="faint small">${esc(spec.unit)}</span>` : ''}
    </div>`);
    const [range, number] = qsa('input', control);
    const push = (raw) => {
      let v = spec.type === 'int' ? parseInt(raw, 10) : parseFloat(raw);
      if (Number.isNaN(v)) return;
      if (spec.minimum !== null && spec.minimum !== undefined) v = Math.max(spec.minimum, v);
      if (spec.maximum !== null && spec.maximum !== undefined) v = Math.min(spec.maximum, v);
      range.value = v; number.value = v;
      onChange(v);
    };
    range.addEventListener('input', (e) => push(e.target.value));
    number.addEventListener('change', (e) => push(e.target.value));
  } else if (spec.type === 'text') {
    control = h(`<textarea id="${id}" rows="${compact ? 2 : 3}"
      placeholder="${esc(spec.examples && spec.examples[0] ? String(spec.examples[0].value) : '')}"
      >${esc(value || '')}</textarea>`);
    control.addEventListener('change', (e) => onChange(e.target.value));
  } else if (spec.type === 'json') {
    control = h(`<textarea id="${id}" rows="3" class="mono">${
      esc(JSON.stringify(value ?? spec.default, null, 1))}</textarea>`);
    control.addEventListener('change', (e) => {
      try { onChange(JSON.parse(e.target.value)); e.target.style.borderColor = ''; }
      catch (err) {
        e.target.style.borderColor = 'var(--err)';
        toast('Некорректный JSON', 'err', String(err.message));
      }
    });
  } else {
    control = h(`<input type="text" id="${id}" value="${esc(value ?? '')}">`);
    control.addEventListener('change', (e) => onChange(e.target.value));
  }
  return control;
}

/** Значения для перечислений, зависящих от каталога. */
function dynamicOptions(key) {
  if (key === 'model') {
    return state.models.map((m) => ({
      value: m.id,
      label: `${m.name} — ${QUALITY_LABELS[m.ru_quality]} · ${m.license}`,
    }));
  }
  if (key === 'engine') {
    return [{ value: 'auto', label: 'Автоматически (по модели)' }].concat(
      state.engines.map((e) => ({
        value: e.id, label: `${e.name}${e.available ? '' : ' — не установлен'}` })));
  }
  if (key === 'model_fallback') {
    return [{ value: '', label: 'Не использовать' }].concat(
      state.models.map((m) => ({ value: m.id, label: m.name })));
  }
  return [];
}

/** Полная карточка параметра: описание, рекомендация, примеры, поле ввода. */
function paramCard(spec, value, onChange) {
  const impacts = Object.entries(spec.impact || {})
    .filter(([, v]) => v !== 'neutral')
    .map(([k, v]) => {
      const names = { quality: 'качество', speed: 'скорость', memory: 'память' };
      const arrow = v === 'up' ? '↑' : '↓';
      const cls = (k === 'quality' && v === 'up') || (k === 'speed' && v === 'up')
        ? 'ok' : (k === 'memory' && v === 'up' ? 'warn' : '');
      return `<span class="chip ${cls}">${names[k]} ${arrow}</span>`;
    }).join('');

  const node = h(`<div class="param" data-key="${esc(spec.key)}">
    <div>
      <div class="param-head">
        <span class="param-label">${esc(spec.label)}</span>
        <span class="param-key">${esc(spec.key)}</span>
        ${spec.advanced ? '<span class="chip">для опытных</span>' : ''}
        ${spec.experimental ? '<span class="chip warn">экспериментальный</span>' : ''}
        ${(spec.engines || []).length
          ? `<span class="chip">только: ${esc(spec.engines.join(', '))}</span>` : ''}
      </div>
      <div class="param-desc">${esc(spec.description)}</div>
      ${spec.recommendation
        ? `<div class="param-rec"><b>Рекомендация.</b> ${esc(spec.recommendation)}</div>` : ''}
      ${(spec.examples || []).length ? `<details class="help"><summary>Примеры настройки
        (${spec.examples.length})</summary><div class="param-examples"></div></details>` : ''}
      ${spec.see_also && spec.see_also.length
        ? `<div class="param-meta" style="margin-top:6px">См. также: ${
            spec.see_also.map((k) => `<span class="mono">${esc(k)}</span>`).join(', ')}</div>` : ''}
    </div>
    <div class="param-control">
      <div class="control-slot"></div>
      <div class="param-impact">${impacts}</div>
      <div class="param-meta">
        ${spec.minimum !== null && spec.minimum !== undefined ? `мин ${spec.minimum}` : ''}
        ${spec.maximum !== null && spec.maximum !== undefined ? ` · макс ${spec.maximum}` : ''}
        ${spec.unit ? ` · ${esc(spec.unit)}` : ''}
        · по умолчанию <span class="mono">${esc(JSON.stringify(spec.default))}</span>
      </div>
    </div>
  </div>`);

  qs('.control-slot', node).appendChild(paramControl(spec, value, (v) => {
    onChange(v);
    const meta = qs('.param-meta', node);
    if (meta && JSON.stringify(v) !== JSON.stringify(spec.default)) {
      node.style.background = 'var(--accent-soft)';
      setTimeout(() => { node.style.background = ''; }, 500);
    }
  }));

  const exBox = qs('.param-examples', node);
  if (exBox) {
    (spec.examples || []).forEach((ex) => {
      const row = h(`<div class="param-example" title="Применить это значение">
        <span class="val">${esc(JSON.stringify(ex.value))}</span>
        <span><b>${esc(ex.title)}</b>${ex.comment ? ` — <span class="dim">${
          esc(ex.comment)}</span>` : ''}</span></div>`);
      row.addEventListener('click', () => {
        onChange(ex.value);
        renderView();
        toast(`Применено: ${ex.title}`, 'ok');
      });
      exBox.appendChild(row);
    });
  }
  return node;
}

// ==========================================================================
// Вид: Транскрибация
// ==========================================================================

RENDERERS.transcribe = {
  render(root) {
    const s = state.jobSettings;
    const model = modelById(s.model) || state.models[0];

    root.innerHTML = `
      <div class="split">
        <div>
          <section class="card">
            <div class="card-head"><h3>Файлы</h3>
              <span class="hint">аудио и видео: wav, mp3, m4a, flac, ogg, opus, mp4, mkv, mov…</span>
            </div>
            <div class="dropzone" id="dropzone">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <path d="M7 10l5-5 5 5M12 5v13"/></svg>
              <div><b>Перетащите файлы сюда</b> или нажмите для выбора</div>
              <div class="small faint" style="margin-top:6px">
                Можно выбрать сразу несколько — они пойдут одной группой</div>
              <input type="file" id="file-input" multiple accept="audio/*,video/*" class="hidden">
            </div>
            <div id="file-list" style="margin-top:12px"></div>
            <div class="row" style="margin-top:12px">
              <button class="primary" id="btn-submit" disabled>
                Поставить в очередь</button>
              <button class="ghost" id="btn-clear-files">Очистить</button>
              <span class="spacer"></span>
              <label class="small dim">Приоритет</label>
              <input type="number" id="job-priority" value="${esc(s.priority ?? 50)}"
                min="0" max="100" style="width:74px" class="mono">
            </div>
          </section>

          <section class="card">
            <div class="card-head"><h3>Активные задания</h3>
              <span class="spacer"></span>
              <button class="ghost sm" onclick="__asrhub.go('queue')">Вся очередь →</button>
            </div>
            <div id="active-jobs"></div>
          </section>

          <section class="card">
            <div class="card-head"><h3>Последние результаты</h3>
              <span class="spacer"></span>
              <button class="ghost sm" onclick="__asrhub.go('results')">Все результаты →</button>
            </div>
            <div id="recent-jobs"></div>
          </section>
        </div>

        <div>
          <section class="card">
            <div class="card-head"><h3>Быстрая настройка</h3></div>
            <div class="stack" style="gap:12px">
              <div>
                <label>Готовый набор настроек</label>
                <select id="preset-select" style="margin-top:4px">
                  <option value="">— выбрать пресет —</option>
                  ${state.presets.map((p) =>
                    `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}
                </select>
                <div class="small faint" id="preset-desc" style="margin-top:6px"></div>
              </div>
              <div>
                <label>Модель</label>
                <div id="slot-model" style="margin-top:4px"></div>
                <div class="small faint" id="model-hint" style="margin-top:6px"></div>
              </div>
              <div>
                <label>Язык</label>
                <div id="slot-language" style="margin-top:4px"></div>
              </div>
              <div class="row">
                <label style="flex:1">Разделять по говорящим</label>
                <div id="slot-diar"></div>
              </div>
              <div class="row">
                <label style="flex:1">Детектор речи (VAD)</label>
                <div id="slot-vad"></div>
              </div>
              <div>
                <label>Форматы результата</label>
                <div id="slot-formats" style="margin-top:4px"></div>
              </div>
              <div>
                <label>Подсказка модели (имена, термины)</label>
                <div id="slot-prompt" style="margin-top:4px"></div>
              </div>
              <button class="ghost" onclick="__asrhub.go('settings')">
                Все настройки (${(state.params || []).length}) →</button>
            </div>
          </section>

          <section class="card" id="model-card"></section>
        </div>
      </div>`;

    this.wireFiles();
    this.wireQuick();
    this.renderModelCard();
    this.soft();
  },

  wireFiles() {
    const zone = qs('#dropzone');
    const input = qs('#file-input');
    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('over'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault(); zone.classList.remove('over');
      addFiles(Array.from(e.dataTransfer.files));
    });
    input.addEventListener('change', (e) => addFiles(Array.from(e.target.files)));
    qs('#btn-clear-files').onclick = () => { state.files = []; renderFileList(); };
    qs('#btn-submit').onclick = submitFiles;
    renderFileList();
  },

  wireQuick() {
    const s = state.jobSettings;
    const bind = (key, slot) => {
      const spec = paramByKey(key);
      if (!spec) return;
      const host = qs(slot);
      if (!host) return;
      host.innerHTML = '';
      host.appendChild(paramControl(spec, s[key], (v) => {
        s[key] = v;
        if (key === 'model') { RENDERERS.transcribe.renderModelCard(); updateModelHint(); }
      }, true));
    };
    bind('model', '#slot-model');
    bind('language', '#slot-language');
    bind('diarization_enabled', '#slot-diar');
    bind('vad_enabled', '#slot-vad');
    bind('output_formats', '#slot-formats');
    bind('initial_prompt', '#slot-prompt');
    updateModelHint();

    const select = qs('#preset-select');
    select.addEventListener('change', () => {
      const preset = state.presets.find((p) => p.id === select.value);
      if (!preset) { qs('#preset-desc').textContent = ''; return; }
      Object.assign(state.jobSettings, preset.values);
      qs('#preset-desc').innerHTML =
        `${esc(preset.description)}<br><b>Сценарий:</b> ${esc(preset.scenario)}` +
        `<br><b>Железо:</b> ${esc(preset.hardware_hint)}` +
        (preset.expected ? `<br><b>Ожидаемо:</b> ${esc(preset.expected)}` : '');
      renderView();
      toast(`Применён пресет «${preset.name}»`, 'ok');
    });
  },

  renderModelCard() {
    const host = qs('#model-card');
    if (!host) return;
    const model = modelById(state.jobSettings.model);
    if (!model) { host.innerHTML = ''; return; }
    const wer = (model.benchmarks || []).filter((b) => b.language === 'ru' && b.metric === 'WER');
    host.innerHTML = `
      <div class="card-head"><h3>${esc(model.name)}</h3>
        <span class="spacer"></span>
        <span class="chip badge-license">${esc(model.license)}</span></div>
      <div class="stack" style="gap:8px">
        <div class="row wrap" style="gap:6px">
          <span class="chip ${QUALITY_CLASS[model.ru_quality]}">русский: ${
            QUALITY_LABELS[model.ru_quality]}</span>
          ${model.streaming ? '<span class="chip info">потоковый</span>' : ''}
          ${model.punctuation ? '<span class="chip ok">пунктуация</span>' : ''}
          ${model.diarization ? '<span class="chip info">диаризация</span>' : ''}
          ${model.translation ? '<span class="chip">перевод</span>' : ''}
          ${model.gated ? '<span class="chip warn">нужен токен HF</span>' : ''}
        </div>
        <div class="small dim">${esc((model.strengths || []).join('. '))}</div>
        ${model.weaknesses && model.weaknesses.length ? `<div class="small"
          style="color:var(--warn)">Ограничения: ${esc(model.weaknesses.join('. '))}</div>` : ''}
        <table style="margin-top:4px">
          <tr><td class="dim">Параметров</td><td class="num">${
            model.params_m ? num(model.params_m) + ' млн' : '—'}</td></tr>
          <tr><td class="dim">Размер на диске</td><td class="num">${
            model.disk_mb ? model.disk_mb + ' МБ' : '—'}</td></tr>
          <tr><td class="dim">Видеопамять</td><td class="num">${
            model.vram_gb ? model.vram_gb + ' ГБ' : '—'}</td></tr>
          <tr><td class="dim">Макс. фрагмент</td><td class="num">${
            model.max_audio_s ? fmtDur(model.max_audio_s) : 'не ограничен'}</td></tr>
          ${model.rtfx ? `<tr><td class="dim">RTFx</td><td class="num">${
            num(model.rtfx)}</td></tr>` : ''}
        </table>
        ${wer.length ? `<div style="margin-top:6px"><div class="small faint"
          style="margin-bottom:4px">WER на русских наборах</div>
          <div id="model-wer"></div></div>` : ''}
        <div class="small faint">Источник: ${esc(model.source)}</div>
      </div>`;
    if (wer.length) {
      window.Charts.hbars(qs('#model-wer'), {
        items: wer.slice(0, 6).map((b) => ({
          label: b.dataset.length > 22 ? b.dataset.slice(0, 21) + '…' : b.dataset,
          value: b.value, display: b.value.toFixed(1) + ' %', note: b.source })),
        labelWidth: 140, rowHeight: 22, unit: ' %',
      });
    }
  },

  soft() {
    const active = qs('#active-jobs');
    if (!active || !state.queue) return;
    const items = (state.queue.items || []).slice(0, 6);
    active.innerHTML = items.length ? items.map((job) => `
      <div class="file-item" style="align-items:flex-start">
        <div style="flex:1;min-width:0">
          <div class="row"><span class="truncate"><b>${esc(job.filename)}</b></span>
            <span class="spacer"></span>${statusChip(job.status)}</div>
          <div class="small faint" data-stage="${esc(job.id)}">${
            esc(job.stage || '—')} · ${((job.progress || 0) * 100).toFixed(0)} %</div>
          <div class="progress" data-progress="${esc(job.id)}" style="margin-top:5px">
            <span style="width:${((job.progress || 0) * 100).toFixed(1)}%"></span></div>
        </div>
      </div>`).join('') : '<div class="empty small">Активных заданий нет</div>';

    API.get('/api/jobs?status=completed,failed&limit=6').then((data) => {
      const host = qs('#recent-jobs');
      if (!host) return;
      host.innerHTML = data.items.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Файл</th><th>Модель</th><th class="num">Длит.</th>
        <th class="num">RTF</th><th>Статус</th><th></th></tr></thead><tbody>
        ${data.items.map((job) => `<tr>
          <td class="truncate" style="max-width:210px">${esc(job.filename)}</td>
          <td class="small dim">${esc(job.model || '')}</td>
          <td class="num">${fmtDur(job.media_duration_s)}</td>
          <td class="num">${job.rtf ? num(job.rtf, 3) : '—'}</td>
          <td>${statusChip(job.status)}</td>
          <td><button class="ghost sm" onclick="__asrhub.openJob('${esc(job.id)}')">
            Открыть</button></td></tr>`).join('')}
        </tbody></table></div>` : '<div class="empty small">Пока нет завершённых заданий</div>';
    }).catch(() => {});
  },
};

function updateModelHint() {
  const host = qs('#model-hint');
  if (!host) return;
  const model = modelById(state.jobSettings.model);
  if (!model) { host.textContent = ''; return; }
  const engine = state.engines.find((e) => e.id === model.engine);
  if (engine && !engine.available) {
    host.innerHTML = `<span style="color:var(--warn)">Движок «${esc(engine.name)}» не установлен.
      ${esc(engine.reason || '')}</span>`;
  } else {
    host.innerHTML = `<span class="dim">Движок: ${esc(model.engine)} · языки: ${
      esc(model.languages.slice(0, 6).join(', '))}</span>`;
  }
}

function addFiles(files) {
  files.forEach((f) => state.files.push(f));
  renderFileList();
}

function renderFileList() {
  const host = qs('#file-list');
  if (!host) return;
  host.innerHTML = state.files.map((f, i) => `
    <div class="file-item">
      <span class="truncate" style="flex:1">${esc(f.name)}</span>
      <span class="faint small nowrap">${fmtBytes(f.size)}</span>
      <button class="ghost sm" onclick="__asrhub.removeFile(${i})">✕</button>
    </div>`).join('');
  const btn = qs('#btn-submit');
  if (btn) {
    btn.disabled = state.files.length === 0;
    btn.textContent = state.files.length > 1
      ? `Поставить в очередь (${state.files.length})` : 'Поставить в очередь';
  }
}

async function submitFiles() {
  if (!state.files.length) return;
  const btn = qs('#btn-submit');
  btn.disabled = true;
  btn.textContent = 'Отправка…';
  const priority = parseInt(qs('#job-priority').value, 10) || 50;
  const settings = JSON.stringify(state.jobSettings);
  try {
    if (state.files.length === 1) {
      const form = new FormData();
      form.append('file', state.files[0]);
      form.append('settings', settings);
      form.append('priority', String(priority));
      await API.call('/api/jobs', { method: 'POST', body: form });
      toast('Задание поставлено в очередь', 'ok');
    } else {
      const form = new FormData();
      state.files.forEach((f) => form.append('files', f));
      form.append('settings', settings);
      form.append('priority', String(priority));
      const result = await API.call('/api/jobs/batch', { method: 'POST', body: form });
      toast(`Поставлено заданий: ${result.created}`, 'ok',
        result.errors.length ? `С ошибками: ${result.errors.length}` : '');
    }
    state.files = [];
    renderFileList();
    await refreshQueue();
    renderView(true);
  } catch (err) {
    fail(err);
  } finally {
    btn.disabled = state.files.length === 0;
    btn.textContent = 'Поставить в очередь';
  }
}

window.__asrhub.removeFile = (index) => { state.files.splice(index, 1); renderFileList(); };

// ==========================================================================
// Вид: Очередь
// ==========================================================================

RENDERERS.queue = {
  render(root) {
    const q = state.queue || { counts: {}, workers: [], items: [] };
    root.innerHTML = `
      <div class="grid cols-6" style="margin-bottom:16px">
        ${kpi('В очереди', q.queue_depth ?? 0, 'ожидают запуска')}
        ${kpi('Выполняется', q.active ?? 0, `воркеров: ${q.worker_count ?? 0}`)}
        ${kpi('Готово', (q.counts && q.counts.completed) ?? 0, 'за всё время')}
        ${kpi('Ошибок', (q.counts && q.counts.failed) ?? 0, 'требуют внимания')}
        ${kpi('Аудио в ожидании', fmtDur(q.pending_audio_s), 'суммарная длительность')}
        ${kpi('Оценка времени', fmtDur(q.eta_s), 'до опустошения очереди')}
      </div>

      <section class="card">
        <div class="card-head"><h3>Управление очередью</h3>
          <span class="hint">политика: ${esc(q.policy || '')}</span>
          <span class="spacer"></span>
          <button id="q-pause" class="${q.paused ? 'primary' : ''}">
            ${q.paused ? '▶ Возобновить' : '⏸ Приостановить'}</button>
          <button id="q-retry">Повторить неудавшиеся</button>
          <button id="q-clear" class="danger">Отменить ожидающие</button>
        </div>
        <div class="row wrap" style="gap:16px">
          <div style="min-width:250px">
            <label class="small">Политика планирования</label>
            <div id="slot-policy" style="margin-top:4px"></div>
          </div>
          <div style="min-width:230px">
            <label class="small">Одновременных заданий</label>
            <div id="slot-workers" style="margin-top:4px"></div>
          </div>
          <div style="min-width:230px">
            <label class="small">На одну модель</label>
            <div id="slot-permodel" style="margin-top:4px"></div>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head"><h3>Воркеры</h3>
          <span class="hint">каждый обрабатывает одно задание</span></div>
        <div class="grid cols-4" id="workers"></div>
      </section>

      <section class="card">
        <div class="card-head"><h3>Задания</h3>
          <span class="spacer"></span>
          <select id="q-filter" style="width:190px">
            <option value="active">Активные</option>
            <option value="">Все</option>
            <option value="queued">В очереди</option>
            <option value="running">Выполняются</option>
            <option value="retry">Ожидают повтора</option>
            <option value="failed">С ошибкой</option>
            <option value="completed">Завершённые</option>
          </select>
          <input type="search" id="q-search" placeholder="поиск по имени файла"
            style="width:220px">
        </div>
        <div class="table-wrap" id="queue-table"></div>
      </section>`;

    const bindSetting = (key, slot, after) => {
      const spec = paramByKey(key);
      const host = qs(slot);
      if (!spec || !host) return;
      host.appendChild(paramControl(spec, state.settings[key], async (v) => {
        try {
          await API.put('/api/settings', { [key]: v });
          state.settings[key] = v;
          toast('Настройка применена', 'ok');
          if (after) after(v);
          await refreshQueue();
          renderView(true);
        } catch (err) { fail(err); }
      }, true));
    };
    bindSetting('scheduling_policy', '#slot-policy');
    bindSetting('max_concurrent_jobs', '#slot-workers');
    bindSetting('max_concurrent_per_model', '#slot-permodel');

    qs('#q-pause').onclick = async () => {
      try {
        state.queue = await API.post(q.paused ? '/api/queue/resume' : '/api/queue/pause');
        renderView();
      } catch (err) { fail(err); }
    };
    qs('#q-retry').onclick = async () => {
      try {
        const r = await API.post('/api/queue/retry-failed');
        toast(`Возвращено в очередь: ${r.requeued}`, 'ok');
        await refreshQueue(); renderView();
      } catch (err) { fail(err); }
    };
    qs('#q-clear').onclick = async () => {
      if (!confirm('Отменить все ожидающие задания?')) return;
      try {
        const r = await API.post('/api/queue/clear');
        toast(`Отменено: ${r.cancelled}`, 'warn');
        await refreshQueue(); renderView();
      } catch (err) { fail(err); }
    };
    qs('#q-filter').onchange = () => this.loadTable();
    let searchTimer;
    qs('#q-search').oninput = () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => this.loadTable(), 300);
    };

    this.soft();
    this.loadTable();
  },

  soft() {
    const host = qs('#workers');
    if (!host || !state.queue) return;
    const workers = state.queue.workers || [];
    host.innerHTML = workers.length ? workers.map((w) => `
      <div class="worker-card ${w.busy ? 'busy' : ''}">
        <div class="row"><b>Воркер ${w.index + 1}</b><span class="spacer"></span>
          <span class="chip ${w.busy ? 'accent' : ''}">${w.busy ? 'занят' : 'свободен'}</span></div>
        ${w.busy ? `
          <div class="small dim truncate" style="margin-top:6px">${esc(w.model || '')}</div>
          <div class="small faint">${esc(w.stage || '')} · ${
            ((w.progress || 0) * 100).toFixed(0)} % · ${fmtDur(w.elapsed_s)}</div>
          <div class="progress" style="margin-top:6px">
            <span style="width:${((w.progress || 0) * 100).toFixed(1)}%"></span></div>`
          : '<div class="small faint" style="margin-top:6px">ожидает задание</div>'}
      </div>`).join('') : '<div class="empty small">Воркеры не запущены</div>';

    const chip = qs('#q-pause');
    if (chip && state.queue) {
      chip.textContent = state.queue.paused ? '▶ Возобновить' : '⏸ Приостановить';
      chip.classList.toggle('primary', !!state.queue.paused);
    }
    this.loadTable();
  },

  async loadTable() {
    const host = qs('#queue-table');
    if (!host) return;
    const filter = qs('#q-filter') ? qs('#q-filter').value : 'active';
    const search = qs('#q-search') ? qs('#q-search').value.trim() : '';
    try {
      const params = new URLSearchParams({ limit: '120' });
      if (filter) params.set('status', filter);
      if (search) params.set('search', search);
      const data = await API.get(`/api/jobs?${params}`);
      host.innerHTML = data.items.length ? `<table>
        <thead><tr>
          <th style="width:26px"></th><th>Файл</th><th>Модель</th>
          <th class="num">Приор.</th><th class="num">Длит.</th>
          <th>Состояние</th><th style="width:150px">Прогресс</th>
          <th class="num">RTF</th><th>Создано</th><th style="width:210px">Действия</th>
        </tr></thead><tbody>
        ${data.items.map((job) => this.row(job)).join('')}
      </tbody></table>` : '<div class="empty">Нет заданий по выбранному фильтру</div>';
    } catch (err) { fail(err); }
  },

  row(job) {
    const active = ['queued', 'running', 'retry', 'paused'].includes(job.status);
    return `<tr class="queue-row ${job.status}">
      <td>${job.status === 'running' ? '▶' : job.status === 'failed' ? '✕'
            : job.status === 'completed' ? '✓' : '·'}</td>
      <td><div class="truncate" style="max-width:230px" title="${esc(job.filename)}">
        ${esc(job.filename)}</div>
        <div class="small faint mono">${esc(job.id.slice(0, 16))}</div></td>
      <td class="small dim">${esc(job.model || '—')}</td>
      <td class="num">${job.priority}</td>
      <td class="num">${fmtDur(job.media_duration_s)}</td>
      <td>${statusChip(job.status)}${job.retries
        ? ` <span class="chip warn">повтор ${job.retries}</span>` : ''}</td>
      <td>
        <div class="progress" data-progress="${esc(job.id)}">
          <span style="width:${((job.progress || 0) * 100).toFixed(1)}%"></span></div>
        <div class="small faint" data-stage="${esc(job.id)}">${esc(job.stage || '')}</div>
      </td>
      <td class="num">${job.rtf ? num(job.rtf, 3) : '—'}</td>
      <td class="small faint nowrap">${fmtAgo(job.created_at)}</td>
      <td><div class="row" style="gap:3px">
        ${active ? `<button class="ghost sm" title="Поднять наверх"
            onclick="__asrhub.jobAction('${job.id}','top')">▲</button>
          <button class="ghost sm" title="Опустить"
            onclick="__asrhub.jobAction('${job.id}','bottom')">▼</button>` : ''}
        ${job.status === 'queued' ? `<button class="ghost sm" title="Приостановить"
            onclick="__asrhub.jobAction('${job.id}','pause')">⏸</button>` : ''}
        ${job.status === 'paused' ? `<button class="ghost sm" title="Возобновить"
            onclick="__asrhub.jobAction('${job.id}','resume')">▶</button>` : ''}
        ${active ? `<button class="ghost sm danger" title="Отменить"
            onclick="__asrhub.jobAction('${job.id}','cancel')">✕</button>` : ''}
        ${job.status === 'failed' ? `<button class="ghost sm" title="Повторить"
            onclick="__asrhub.jobAction('${job.id}','retry')">↻</button>` : ''}
        <button class="ghost sm" onclick="__asrhub.openJob('${job.id}')">Открыть</button>
      </div></td>
    </tr>`;
  },
};

window.__asrhub.jobAction = async (id, action) => {
  try {
    if (action === 'cancel' && !confirm('Отменить задание?')) return;
    await API.post(`/api/jobs/${id}/${action}`);
    toast('Выполнено', 'ok');
    await refreshQueue();
    renderView(true);
  } catch (err) { fail(err); }
};

// ==========================================================================
// Вид: Результаты
// ==========================================================================

RENDERERS.results = {
  render(root) {
    root.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Завершённые задания</h3>
          <span class="spacer"></span>
          <input type="search" id="r-search" placeholder="поиск по файлу или тексту"
            style="width:260px">
          <select id="r-order" style="width:200px">
            <option value="created_at DESC">Сначала новые</option>
            <option value="created_at ASC">Сначала старые</option>
            <option value="media_duration_s DESC">Самые длинные</option>
            <option value="rtf DESC">Самые медленные</option>
            <option value="rtf ASC">Самые быстрые</option>
          </select>
        </div>
        <div class="table-wrap" id="results-table"></div>
      </section>`;
    let timer;
    qs('#r-search').oninput = () => { clearTimeout(timer); timer = setTimeout(() => this.load(), 300); };
    qs('#r-order').onchange = () => this.load();
    this.load();
  },

  async load() {
    const host = qs('#results-table');
    const search = qs('#r-search').value.trim();
    const order = qs('#r-order').value;
    try {
      const params = new URLSearchParams({ status: 'completed', limit: '150', order });
      if (search) params.set('search', search);
      const data = await API.get(`/api/jobs?${params}`);
      host.innerHTML = data.items.length ? `<table>
        <thead><tr><th>Файл</th><th>Модель</th><th class="num">Длит.</th>
          <th class="num">Слов</th><th class="num">Сегм.</th><th class="num">RTF</th>
          <th class="num">Уверенность</th><th>Говорящие</th><th>Готово</th>
          <th style="width:230px">Выгрузка</th></tr></thead><tbody>
        ${data.items.map((job) => `<tr>
          <td><div class="truncate" style="max-width:240px">${esc(job.filename)}</div>
            <div class="small faint truncate" style="max-width:240px">${
              esc((job.text || '').slice(0, 70))}</div></td>
          <td class="small dim">${esc(job.model || '')}</td>
          <td class="num">${fmtDur(job.media_duration_s)}</td>
          <td class="num">${num(job.words_count)}</td>
          <td class="num">${num(job.segments_count)}</td>
          <td class="num">${job.rtf ? num(job.rtf, 3) : '—'}</td>
          <td class="num">${job.avg_confidence ? pct(job.avg_confidence, 0) : '—'}</td>
          <td class="num">${job.speakers_count || '—'}</td>
          <td class="small faint nowrap">${fmtAgo(job.finished_at)}</td>
          <td><div class="row" style="gap:3px">
            ${['txt', 'srt', 'json', 'docx'].map((f) =>
              `<a class="btn sm" href="/api/jobs/${job.id}/download?fmt=${f}${
                localStorage.getItem('asrhub_key')
                  ? '&api_key=' + encodeURIComponent(localStorage.getItem('asrhub_key')) : ''
              }" download>${f}</a>`).join('')}
            <button class="ghost sm" onclick="__asrhub.openJob('${job.id}')">Открыть</button>
          </div></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">Завершённых заданий пока нет</div>';
    } catch (err) { fail(err); }
  },
};

// ==========================================================================
// Карточка задания
// ==========================================================================

window.__asrhub.openJob = async (id) => {
  try {
    const job = await API.get(`/api/jobs/${id}?with_segments=true`);
    showJobModal(job);
  } catch (err) { fail(err); }
};

function showJobModal(job) {
  const segments = job.segments || [];
  const params = job.params || {};
  const changed = Object.entries(params).filter(([k, v]) =>
    !k.startsWith('_') && paramByKey(k) &&
    JSON.stringify(v) !== JSON.stringify(paramByKey(k).default));

  const backdrop = h(`<div class="modal-backdrop"><div class="modal">
    <div class="modal-head">
      <b>${esc(job.filename)}</b>
      ${statusChip(job.status)}
      <span class="spacer"></span>
      <button class="ghost icon" id="modal-close">✕</button>
    </div>
    <div class="modal-body">
      <div class="grid cols-4" style="margin-bottom:14px">
        ${kpi('Длительность', fmtDur(job.media_duration_s))}
        ${kpi('RTF', job.rtf ? num(job.rtf, 3) : '—',
              job.processing_time_s ? `обработка ${fmtDur(job.processing_time_s)}` : '')}
        ${kpi('Слов', num(job.words_count), `сегментов: ${job.segments_count || 0}`)}
        ${kpi('Уверенность', job.avg_confidence ? pct(job.avg_confidence, 1) : '—',
              job.wer !== null && job.wer !== undefined ? `WER ${pct(job.wer, 2)}` : '')}
      </div>

      ${job.error_message ? `<div class="card tight" style="border-color:var(--err)">
        <b style="color:var(--err)">${esc(job.error_code || 'ошибка')}</b>
        <div style="margin-top:4px">${esc(job.error_message)}</div>
        ${job.error_hint ? `<div class="small dim" style="margin-top:6px;white-space:pre-wrap">${
          esc(job.error_hint)}</div>` : ''}</div>` : ''}

      <div class="tabs" id="job-tabs">
        <button class="active" data-tab="text">Текст</button>
        <button data-tab="segments">Сегменты (${segments.length})</button>
        <button data-tab="params">Параметры (${changed.length} изменено)</button>
        <button data-tab="events">События</button>
      </div>
      <div id="job-tab-body"></div>
    </div>
    <div class="modal-foot">
      <span class="small faint mono">${esc(job.id)}</span>
      <span class="spacer"></span>
      ${job.status === 'completed' ? ['txt', 'srt', 'vtt', 'json', 'csv', 'docx'].map((f) =>
        `<a class="btn sm" href="/api/jobs/${job.id}/download?fmt=${f}${
          localStorage.getItem('asrhub_key')
            ? '&api_key=' + encodeURIComponent(localStorage.getItem('asrhub_key')) : ''
        }" download>${f}</a>`).join('') : ''}
      ${job.status === 'failed'
        ? `<button class="primary" onclick="__asrhub.jobAction('${job.id}','retry')">
             Повторить</button>` : ''}
    </div></div></div>`);

  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  qs('#modal-close', backdrop).onclick = close;
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });

  const body = qs('#job-tab-body', backdrop);
  const tabs = {
    text: () => `<div class="transcript" style="white-space:pre-wrap;line-height:1.7">${
      esc(job.text || '—')}</div>`,
    segments: () => segments.length ? `<div class="transcript">${segments.map((s) => `
      <div class="segment">
        <div class="ts">${fmtDur(s.start)}<br><span style="opacity:.6">${
          fmtDur(s.end)}</span></div>
        <div class="${(s.confidence !== null && s.confidence < 0.7) ? 'conf-low' : ''}">
          ${s.speaker ? `<div class="speaker">${esc(s.speaker)}</div>` : ''}
          ${esc(s.text)}
          ${s.confidence !== null && s.confidence !== undefined
            ? `<span class="chip ${s.confidence < 0.7 ? 'warn' : ''}"
                 style="margin-left:8px">${pct(s.confidence, 0)}</span>` : ''}
        </div></div>`).join('')}</div>` : '<div class="empty">Сегменты недоступны</div>',
    params: () => `<div class="table-wrap"><table>
      <thead><tr><th>Параметр</th><th>Значение</th><th>По умолчанию</th></tr></thead><tbody>
      ${changed.map(([k, v]) => {
        const spec = paramByKey(k);
        return `<tr><td>${esc(spec.label)}<div class="small faint mono">${esc(k)}</div></td>
          <td class="mono" style="color:var(--accent)">${esc(JSON.stringify(v))}</td>
          <td class="mono faint">${esc(JSON.stringify(spec.default))}</td></tr>`;
      }).join('')}</tbody></table></div>
      ${changed.length === 0 ? '<div class="empty small">Использованы значения по умолчанию</div>' : ''}`,
    events: () => `<div class="table-wrap"><table>
      <thead><tr><th>Время</th><th>Событие</th><th>Сообщение</th></tr></thead><tbody>
      ${(job.events || []).map((e) => `<tr>
        <td class="small faint nowrap">${fmtTime(e.ts)}</td>
        <td><span class="chip">${esc(e.kind)}</span></td>
        <td class="small">${esc(e.message || '')}</td></tr>`).join('')}
      </tbody></table></div>`,
  };
  const show = (name) => {
    body.innerHTML = tabs[name]();
    qsa('#job-tabs button', backdrop).forEach((b) =>
      b.classList.toggle('active', b.dataset.tab === name));
  };
  qsa('#job-tabs button', backdrop).forEach((b) =>
    b.addEventListener('click', () => show(b.dataset.tab)));
  show('text');
}

// ==========================================================================
// Вид: Аналитика
// ==========================================================================

const PERIOD_LABELS = { hour: 'час', day: 'сутки', week: 'неделя',
                        month: 'месяц', quarter: 'квартал', year: 'год', all: 'всё время' };

RENDERERS.analytics = {
  async render(root) {
    root.innerHTML = `
      <div class="settings-toolbar">
        <span class="small dim">Период:</span>
        <div class="group-nav" id="period-nav">
          ${Object.entries(PERIOD_LABELS).map(([k, v]) =>
            `<button data-period="${k}" class="${state.period === k ? 'active' : ''}">${v}</button>`
          ).join('')}
        </div>
        <span class="spacer"></span>
        <a class="btn sm" href="/api/metrics" target="_blank">Метрики Prometheus</a>
      </div>
      <div id="analytics-body"><div class="empty">Загрузка аналитики…</div></div>`;

    qsa('#period-nav button').forEach((b) => b.addEventListener('click', () => {
      state.period = b.dataset.period;
      renderView();
    }));
    this.load();
  },

  async load() {
    const host = qs('#analytics-body');
    try {
      const data = await API.get(`/api/analytics?period=${state.period}`);
      state.analytics = data;
      this.draw(host, data);
    } catch (err) {
      fail(err);
      host.innerHTML = `<div class="empty">Не удалось загрузить аналитику: ${esc(err.message)}</div>`;
    }
  },

  draw(host, data) {
    const o = data.overview;
    const perf = o.performance;
    const quality = o.quality;

    host.innerHTML = `
      <div class="grid cols-6" style="margin-bottom:16px">
        ${kpi('Заданий', num(o.jobs.total),
              `готово ${o.jobs.completed} · ошибок ${o.jobs.failed}`)}
        ${kpi('Успешность', o.jobs.success_rate !== null ? pct(o.jobs.success_rate, 1) : '—',
              o.jobs.cached ? `из кеша: ${o.jobs.cached}` : 'доля завершённых')}
        ${kpi('Аудио', `${num(o.volume.audio_hours)} ч`,
              `${num(o.volume.words)} слов`)}
        ${kpi('Средний RTF', num(perf.rtf.avg, 3),
              `p95: ${num(perf.rtf.p95, 3)}`)}
        ${kpi('Ускорение', perf.speedup ? `×${num(perf.speedup, 1)}` : '—',
              'аудио / машинное время')}
        ${kpi('Уверенность', quality.confidence.count ? pct(quality.confidence.avg, 1) : '—',
              `низких: ${quality.low_confidence_jobs}`)}
      </div>

      <div class="grid cols-2">
        ${card('Поток заданий', 'завершённые и ошибки по времени',
               '<div id="chart-flow"></div>')}
        ${card('Коэффициент реального времени', 'меньше — быстрее',
               '<div id="chart-rtf"></div>')}
      </div>

      <div class="grid cols-3">
        ${card('Статусы заданий', '', '<div id="chart-status"></div>')}
        ${card('Время по этапам', 'куда уходит машинное время',
               '<div id="chart-stages"></div>')}
        ${card('Распределение уверенности', 'доля сегментов по интервалам',
               '<div id="chart-conf"></div>')}
      </div>

      <div class="grid cols-2">
        ${card('Нагрузка на сервер', 'процессор, память, видеокарта',
               '<div id="chart-system"></div>')}
        ${card('Глубина очереди', 'сколько заданий ждало обработки',
               '<div id="chart-queue"></div>')}
      </div>

      <div class="grid cols-2">
        ${card('Длительность записей', 'сколько файлов какой длины',
               '<div id="chart-dur"></div>')}
        ${card('Профиль нагрузки по часам', 'когда сервер загружен',
               '<div id="chart-hours"></div>')}
      </div>

      ${card('Сравнение по моделям', 'фактические показатели за период, а не заявленные',
             '<div class="table-wrap full" id="table-models"></div>')}

      <div class="grid cols-2">
        ${card('Ошибки', 'по коду, с подсказками по устранению',
               '<div id="errors-body"></div>')}
        ${card('Эффективность', 'ресурсы на час аудио',
               '<div id="efficiency-body"></div>')}
      </div>

      <div class="grid cols-3">
        ${card('По языкам', '', '<div id="chart-lang"></div>')}
        ${card('По пользователям', '', '<div id="chart-owner"></div>')}
        ${card('Самые медленные задания', 'кандидаты на оптимизацию',
               '<div class="table-wrap" id="table-slow"></div>')}
      </div>`;

    const ts = data.timeseries;
    const labels = (ts.labels || []).map((t) => {
      const d = new Date(t * 1000);
      return ts.bucket_seconds < 7200
        ? d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
        : d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
    });

    Charts.line(qs('#chart-flow'), {
      labels, height: 210, area: true,
      series: [
        { name: 'Готово', values: ts.completed },
        { name: 'Ошибки', values: ts.failed },
      ],
    });

    Charts.line(qs('#chart-rtf'), {
      labels, height: 210, unit: '',
      series: [
        { name: 'RTF', values: ts.rtf },
        { name: 'Ожидание в очереди, с', values: ts.queue_time },
      ],
    });

    const statusColors = Charts.status();
    Charts.donut(qs('#chart-status'), {
      size: 168, centerLabel: 'заданий',
      parts: [
        { label: 'Готово', value: o.jobs.completed, color: statusColors.ok },
        { label: 'Ошибка', value: o.jobs.failed, color: statusColors.err },
        { label: 'В работе', value: o.jobs.in_progress, color: statusColors.info },
        { label: 'Отменено', value: o.jobs.cancelled, color: statusColors.idle },
      ],
    });

    Charts.stacked(qs('#chart-stages'), {
      unit: ' с',
      parts: o.stages.labels.map((label, i) => ({ label, value: o.stages.seconds[i] })),
    });

    const buckets = quality.confidence_distribution || [];
    Charts.bars(qs('#chart-conf'), {
      height: 190,
      labels: buckets.map((b) => `${(b.from * 100).toFixed(0)}–${(b.to * 100).toFixed(0)}%`),
      values: buckets.map((b) => b.count),
      colors: buckets.map((b) => b.from < 0.7 ? statusColors.warn : Charts.palette()[0]),
    });

    const sys = ts.system || {};
    const sysLabels = (sys.ts || []).map((t) =>
      new Date(t * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }));
    Charts.line(qs('#chart-system'), {
      labels: sysLabels, height: 200, unit: '',
      series: [
        { name: 'Процессор, %', values: sys.cpu || [] },
        { name: 'Видеокарта, %', values: sys.gpu || [] },
        { name: 'Память, ГБ', values: (sys.ram_used_mb || []).map((v) =>
            v === null || v === undefined ? null : +(v / 1024).toFixed(2)) },
      ],
    });
    Charts.line(qs('#chart-queue'), {
      labels: sysLabels, height: 200, area: true,
      series: [
        { name: 'В очереди', values: sys.queue_depth || [] },
        { name: 'Выполняется', values: sys.active_jobs || [] },
      ],
    });

    Charts.bars(qs('#chart-dur'), {
      height: 200, labels: data.durations.labels, values: data.durations.counts,
    });
    Charts.heat(qs('#chart-hours'), {
      values: data.profile.hours, labels: data.profile.hour_labels, cell: 24,
    });

    qs('#table-models').innerHTML = data.models.length ? `<table>
      <thead><tr><th>Модель</th><th class="num">Заданий</th><th class="num">Успешно</th>
        <th class="num">RTF ср.</th><th class="num">RTF p90</th><th class="num">Ускорение</th>
        <th class="num">Уверенность</th><th class="num">Аудио, ч</th>
        <th class="num">WER (каталог)</th><th>Лицензия</th></tr></thead><tbody>
      ${data.models.map((m) => `<tr>
        <td><b>${esc(m.name)}</b><div class="small faint mono">${esc(m.model)}</div></td>
        <td class="num">${m.jobs}</td>
        <td class="num">${m.success_rate !== null ? pct(m.success_rate, 0) : '—'}</td>
        <td class="num">${m.rtf_avg !== null ? num(m.rtf_avg, 3) : '—'}</td>
        <td class="num">${m.rtf_p90 !== null ? num(m.rtf_p90, 3) : '—'}</td>
        <td class="num">${m.speedup ? '×' + num(m.speedup, 1) : '—'}</td>
        <td class="num">${m.confidence_avg !== null ? pct(m.confidence_avg, 0) : '—'}</td>
        <td class="num">${num(m.audio_hours, 2)}</td>
        <td class="num">${m.catalog_ru_wer !== null && m.catalog_ru_wer !== undefined
          ? m.catalog_ru_wer.toFixed(1) + ' %' : '—'}</td>
        <td class="small"><span class="chip badge-license">${esc(m.license || '')}</span></td>
      </tr>`).join('')}</tbody></table>` : '<div class="empty">Нет данных за период</div>';

    const errors = data.errors;
    qs('#errors-body').innerHTML = errors.by_code.length ? `
      <div class="row" style="margin-bottom:10px">
        <span class="chip err">неудач: ${errors.total_failed}</span>
        <span class="chip">доля: ${pct(errors.failure_rate, 2)}</span></div>
      ${errors.by_code.map((e) => `<div class="card tight" style="margin-bottom:8px">
        <div class="row"><b class="mono">${esc(e.code)}</b>
          <span class="spacer"></span><span class="chip err">${e.count}</span></div>
        <div class="small dim" style="margin-top:4px">${esc(e.message)}</div>
        ${e.hint ? `<div class="small faint" style="margin-top:6px;white-space:pre-wrap">${
          esc(e.hint)}</div>` : ''}
        <div class="small faint" style="margin-top:6px">Модели: ${
          Object.entries(e.models).map(([m, c]) => `${esc(m)} (${c})`).join(', ')}</div>
      </div>`).join('')}` : '<div class="empty small">Ошибок за период не было</div>';

    const eff = data.efficiency;
    qs('#efficiency-body').innerHTML = `<table>
      <tr><td class="dim">Обработано аудио</td><td class="num">${num(eff.audio_hours, 2)} ч</td></tr>
      <tr><td class="dim">Машинное время</td><td class="num">${num(eff.compute_hours, 2)} ч</td></tr>
      <tr><td class="dim">Машинных часов на час аудио</td><td class="num">${
        eff.compute_per_audio_hour !== null ? num(eff.compute_per_audio_hour, 3) : '—'}</td></tr>
      <tr><td class="dim">Доля времени на загрузку моделей</td><td class="num">${
        eff.model_load_share !== null ? pct(eff.model_load_share, 1) : '—'}</td></tr>
      <tr><td class="dim">Попаданий в кеш</td><td class="num">${eff.cache_hits} (${
        pct(eff.cache_hit_rate, 1)})</td></tr>
      <tr><td class="dim">Сэкономлено кешем</td><td class="num">${
        num(eff.saved_compute_hours, 3)} ч</td></tr></table>
      <div class="small faint" style="margin-top:10px">
        Доля времени на загрузку моделей выше 15 % означает, что кеш моделей слишком мал
        либо задания слишком часто чередуют разные модели.</div>`;

    Charts.hbars(qs('#chart-lang'), {
      items: data.languages.slice(0, 8).map((l) => ({
        label: l.key, value: l.jobs, note: `аудио: ${num(l.audio_hours, 2)} ч` })),
      labelWidth: 90, rowHeight: 24,
    });
    Charts.hbars(qs('#chart-owner'), {
      items: data.owners.slice(0, 8).map((o2) => ({
        label: o2.key.length > 14 ? o2.key.slice(0, 13) + '…' : o2.key,
        value: o2.jobs, note: `аудио: ${num(o2.audio_hours, 2)} ч` })),
      labelWidth: 110, rowHeight: 24,
    });

    qs('#table-slow').innerHTML = data.slowest.length ? `<table>
      <thead><tr><th>Файл</th><th class="num">RTF</th><th class="num">Длит.</th></tr></thead>
      <tbody>${data.slowest.slice(0, 10).map((j) => `<tr>
        <td class="truncate" style="max-width:150px">${esc(j.filename)}</td>
        <td class="num">${num(j.rtf, 3)}</td>
        <td class="num">${fmtDur(j.duration_s)}</td></tr>`).join('')}
      </tbody></table>` : '<div class="empty small">Нет данных</div>';
  },
};

// ==========================================================================
// Вид: Модели
// ==========================================================================

RENDERERS.models = {
  render(root) {
    const summary = state.catalog.summary;
    root.innerHTML = `
      <div class="grid cols-4" style="margin-bottom:16px">
        ${kpi('Моделей в каталоге', summary.total, `семейств: ${
          Object.keys(summary.families).length}`)}
        ${kpi('Поддерживают русский', summary.russian, 'из общего числа')}
        ${kpi('Потоковых', summary.streaming, 'для реального времени')}
        ${kpi('С диаризацией', summary.diarization, 'разделяют говорящих')}
      </div>

      <div class="settings-toolbar">
        <input type="search" id="m-search" placeholder="поиск по названию, семейству, тегу"
          style="width:280px">
        <select id="m-family" style="width:170px">
          <option value="">Все семейства</option>
          ${Object.keys(summary.families).sort().map((f) =>
            `<option value="${esc(f)}">${esc(f)} (${summary.families[f]})</option>`).join('')}
        </select>
        <select id="m-quality" style="width:190px">
          <option value="">Любое качество на русском</option>
          <option value="excellent">Отличное</option>
          <option value="good">Хорошее</option>
          <option value="fair">Среднее</option>
        </select>
        <label class="row" style="gap:6px;cursor:pointer">
          <input type="checkbox" id="m-streaming" style="width:auto">
          <span class="small">только потоковые</span></label>
        <label class="row" style="gap:6px;cursor:pointer">
          <input type="checkbox" id="m-installed" style="width:auto">
          <span class="small">движок установлен</span></label>
        <span class="spacer"></span>
        <span class="small faint" id="m-count"></span>
      </div>

      <div id="models-list"></div>

      ${card('Исключённые модели', 'почему их нет в каталоге',
        `<div class="table-wrap"><table>
          <thead><tr><th>Модель</th><th>Лицензия</th><th>Причина</th></tr></thead><tbody>
          ${state.catalog.excluded.map((e) => `<tr>
            <td><b>${esc(e.name)}</b></td>
            <td><span class="chip warn badge-license">${esc(e.license)}</span></td>
            <td class="small dim">${esc(e.reason)}</td></tr>`).join('')}
        </tbody></table></div>`)}`;

    ['#m-search', '#m-family', '#m-quality', '#m-streaming', '#m-installed'].forEach((sel) => {
      const node = qs(sel);
      node.addEventListener(node.type === 'checkbox' ? 'change' : 'input', () => this.list());
    });
    this.list();
  },

  list() {
    const search = qs('#m-search').value.trim().toLowerCase();
    const family = qs('#m-family').value;
    const quality = qs('#m-quality').value;
    const streaming = qs('#m-streaming').checked;
    const installed = qs('#m-installed').checked;
    const availableEngines = new Set(state.engines.filter((e) => e.available).map((e) => e.id));

    let items = state.models.filter((m) => {
      if (family && m.family !== family) return false;
      if (quality && m.ru_quality !== quality) return false;
      if (streaming && !m.streaming) return false;
      if (installed && !availableEngines.has(m.engine)) return false;
      if (search) {
        const blob = `${m.id} ${m.name} ${m.family} ${m.tags.join(' ')} ${m.source}`.toLowerCase();
        if (!blob.includes(search)) return false;
      }
      return true;
    });

    qs('#m-count').textContent = `показано ${items.length} из ${state.models.length}`;
    const host = qs('#models-list');
    if (!items.length) { host.innerHTML = '<div class="card"><div class="empty">Ничего не найдено</div></div>'; return; }

    const families = {};
    items.forEach((m) => { (families[m.family] = families[m.family] || []).push(m); });

    host.innerHTML = Object.entries(families).map(([fam, models]) => `
      <section class="card">
        <div class="card-head"><h3>${esc(fam)}</h3>
          <span class="hint">${models.length} модел${models.length === 1 ? 'ь' : 'ей'}</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Модель</th><th>Русский</th><th class="num">Парам.</th>
            <th class="num">Диск</th><th class="num">VRAM</th><th class="num">WER ru</th>
            <th>Возможности</th><th>Лицензия</th><th style="width:200px"></th></tr></thead>
          <tbody>${models.map((m) => `<tr>
            <td><b>${esc(m.name)}</b>
              <div class="small faint mono">${esc(m.id)}</div></td>
            <td><span class="chip ${QUALITY_CLASS[m.ru_quality]}">${
              QUALITY_LABELS[m.ru_quality]}</span></td>
            <td class="num">${m.params_m ? num(m.params_m) : '—'}</td>
            <td class="num">${m.disk_mb ? m.disk_mb + ' МБ' : '—'}</td>
            <td class="num">${m.vram_gb ? m.vram_gb + ' ГБ' : '—'}</td>
            <td class="num">${(() => {
              const w = (m.benchmarks || []).filter((b) => b.language === 'ru' && b.metric === 'WER');
              return w.length ? Math.min(...w.map((b) => b.value)).toFixed(1) + ' %' : '—';
            })()}</td>
            <td><div class="row wrap" style="gap:3px">
              ${m.streaming ? '<span class="chip info">поток</span>' : ''}
              ${m.punctuation ? '<span class="chip ok">пункт.</span>' : ''}
              ${m.diarization ? '<span class="chip info">диар.</span>' : ''}
              ${m.translation ? '<span class="chip">перевод</span>' : ''}
              ${m.emotion ? '<span class="chip">эмоции</span>' : ''}
              ${m.gated ? '<span class="chip warn">токен HF</span>' : ''}
            </div></td>
            <td><span class="chip badge-license ${m.commercial_use ? '' : 'err'}">${
              esc(m.license)}</span></td>
            <td><div class="row" style="gap:4px">
              <button class="ghost sm" onclick="__asrhub.showModel('${esc(m.id)}')">Подробно</button>
              <button class="ghost sm" onclick="__asrhub.useModel('${esc(m.id)}')">Выбрать</button>
              <button class="ghost sm" onclick="__asrhub.downloadModel('${esc(m.id)}')"
                title="Загрузить веса">↓</button>
            </div></td></tr>`).join('')}</tbody></table></div>
      </section>`).join('');
  },
};

window.__asrhub.useModel = (id) => {
  state.jobSettings.model = id;
  const model = modelById(id);
  if (model) state.jobSettings.engine = model.engine;
  toast(`Выбрана модель ${model ? model.name : id}`, 'ok');
  go('transcribe');
};

window.__asrhub.downloadModel = async (id) => {
  try {
    const result = await API.post(`/api/models/${encodeURIComponent(id)}/download`);
    toast('Загрузка весов запущена', 'ok', 'Следите за прогрессом в разделе «Журнал»');
  } catch (err) { fail(err); }
};

window.__asrhub.showModel = async (id) => {
  const m = modelById(id);
  if (!m) return;
  let status = null;
  try { status = await API.get(`/api/models/${encodeURIComponent(id)}/status`); } catch (e) {}
  const bench = m.benchmarks || [];
  const backdrop = h(`<div class="modal-backdrop"><div class="modal">
    <div class="modal-head"><b>${esc(m.name)}</b>
      <span class="chip badge-license">${esc(m.license)}</span>
      ${m.commercial_use ? '<span class="chip ok">коммерческое использование разрешено</span>'
        : '<span class="chip err">некоммерческая лицензия</span>'}
      <span class="spacer"></span><button class="ghost icon" id="mm-close">✕</button></div>
    <div class="modal-body">
      <div class="grid cols-4" style="margin-bottom:14px">
        ${kpi('Параметров', m.params_m ? num(m.params_m) + ' млн' : '—')}
        ${kpi('Размер', m.disk_mb ? m.disk_mb + ' МБ' : '—')}
        ${kpi('Видеопамять', m.vram_gb ? m.vram_gb + ' ГБ' : '—')}
        ${kpi('RTFx', m.rtfx ? num(m.rtfx) : '—', m.rtfx_hw || '')}
      </div>
      ${status ? `<div class="card tight"><div class="row">
        <span class="chip ${status.downloaded ? 'ok' : 'warn'}">${
          status.downloaded ? 'веса загружены' : 'веса не загружены'}</span>
        ${status.size_mb ? `<span class="chip">${status.size_mb} МБ на диске</span>` : ''}
        <span class="chip ${status.engine_available ? 'ok' : 'err'}">движок ${
          status.engine_available ? 'установлен' : 'не установлен'}</span>
        <span class="spacer"></span>
        ${!status.downloaded ? `<button class="primary sm"
          onclick="__asrhub.downloadModel('${esc(m.id)}')">Загрузить веса</button>` : ''}
      </div>${!status.engine_available ? `<div class="small dim" style="margin-top:8px">${
        esc(status.engine_reason || '')}</div>` : ''}</div>` : ''}

      <div class="grid cols-2">
        <div>
          <h4 style="margin:12px 0 6px;font-size:13px">Сильные стороны</h4>
          <ul class="small dim" style="margin:0;padding-left:18px">
            ${(m.strengths || []).map((s) => `<li>${esc(s)}</li>`).join('')}</ul>
          <h4 style="margin:14px 0 6px;font-size:13px">Ограничения</h4>
          <ul class="small" style="margin:0;padding-left:18px;color:var(--warn)">
            ${(m.weaknesses || []).map((s) => `<li>${esc(s)}</li>`).join('')}</ul>
        </div>
        <div>
          <h4 style="margin:12px 0 6px;font-size:13px">Рекомендуется для</h4>
          <div class="row wrap" style="gap:5px">${
            (m.recommended_for || []).map((s) => `<span class="chip ok">${esc(s)}</span>`).join('')}</div>
          ${(m.not_recommended_for || []).length ? `
            <h4 style="margin:14px 0 6px;font-size:13px">Не подходит для</h4>
            <div class="row wrap" style="gap:5px">${
              m.not_recommended_for.map((s) => `<span class="chip err">${esc(s)}</span>`).join('')}</div>` : ''}
          <h4 style="margin:14px 0 6px;font-size:13px">Языки</h4>
          <div class="small dim">${esc(m.languages.join(', '))}</div>
        </div>
      </div>

      ${m.notes ? `<div class="param-rec" style="margin-top:14px">${esc(m.notes)}</div>` : ''}

      ${bench.length ? `<h4 style="margin:16px 0 8px;font-size:13px">Измерения качества</h4>
        <div class="table-wrap"><table>
          <thead><tr><th>Набор данных</th><th>Метрика</th><th class="num">Значение</th>
            <th>Язык</th><th>Источник</th></tr></thead><tbody>
          ${bench.map((b) => `<tr><td>${esc(b.dataset)}</td><td>${esc(b.metric)}</td>
            <td class="num"><b>${b.value.toFixed(2)}</b></td><td>${esc(b.language)}</td>
            <td class="small faint">${esc(b.source)}${b.note ? `<br>${esc(b.note)}` : ''}</td>
          </tr>`).join('')}</tbody></table></div>
        <div class="small faint" style="margin-top:8px">
          Значения получены разными авторами на разных наборах. Сравнивать напрямую
          числа из разных строк некорректно.</div>` : ''}
    </div>
    <div class="modal-foot">
      <span class="small faint mono">${esc(m.source)}${m.revision ? ' · ' + esc(m.revision) : ''}</span>
      <span class="spacer"></span>
      <button onclick="__asrhub.useModel('${esc(m.id)}')" class="primary">
        Использовать эту модель</button>
    </div></div></div>`);
  document.body.appendChild(backdrop);
  qs('#mm-close', backdrop).onclick = () => backdrop.remove();
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
};

// ==========================================================================
// Вид: Сравнение моделей
// ==========================================================================

RENDERERS.compare = {
  render(root) {
    if (!state.compare.length) {
      state.compare = ['gigaam-v3-rnnt', 'gigaam-v3-e2e-rnnt', 'parakeet-tdt-0.6b-v3',
                       'faster-whisper-large-v3', 'tone-ru']
        .filter((id) => modelById(id));
    }
    root.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Что сравниваем</h3>
          <span class="hint">до восьми моделей</span>
          <span class="spacer"></span>
          <button class="ghost sm" id="cmp-ru">Лучшие для русского</button>
          <button class="ghost sm" id="cmp-fast">Самые быстрые</button>
          <button class="ghost sm" id="cmp-clear">Очистить</button>
        </div>
        <div class="row wrap" id="cmp-chips" style="gap:6px;margin-bottom:10px"></div>
        <select id="cmp-add" style="max-width:420px">
          <option value="">+ добавить модель…</option>
          ${state.models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('')}
        </select>
      </section>

      <div class="grid cols-2">
        ${card('Качество на русском (WER, меньше — лучше)',
               'значения из карточек моделей и независимых бенчмарков',
               '<div id="cmp-wer"></div>')}
        ${card('Скорость (RTFx, больше — лучше)',
               'во сколько раз быстрее реального времени',
               '<div id="cmp-rtfx"></div>')}
      </div>

      <div class="grid cols-2">
        ${card('Требования к видеопамяти', '', '<div id="cmp-vram"></div>')}
        ${card('Размер на диске', '', '<div id="cmp-disk"></div>')}
      </div>

      ${card('Полное сравнение', 'все характеристики рядом',
             '<div class="table-wrap full" id="cmp-table"></div>')}

      ${card('Как читать эту таблицу', '', `<div class="small dim" style="line-height:1.7">
        <p><b>Числа WER несопоставимы напрямую.</b> GigaAM измеряли на Golos, Common Voice
        и внутренних наборах Сбера; Parakeet — на FLEURS и CoVoST2; Whisper — на Common Voice
        и Open ASR Leaderboard. Один и тот же набор даёт разброс в 2–3 раза между доменами
        (студийная запись против телефонии). Используйте таблицу, чтобы отобрать двух-трёх
        кандидатов, а окончательный выбор делайте прогоном на своих файлах.</p>
        <p><b>RTFx зависит от железа.</b> Заявленные значения получены на A100 или H100
        при большом размере пакета. На RTX 3060 с пакетом 8 ожидайте в 5–15 раз меньше.</p>
        <p><b>Единственное честное сравнение — ваше собственное.</b> Загрузите 10–20 типовых
        записей, прогоните через двух-трёх кандидатов, задайте эталонный текст для
        нескольких файлов и посмотрите фактический WER в разделе «Аналитика».</p>
      </div>`)}`;

    qs('#cmp-add').onchange = (e) => {
      if (e.target.value && state.compare.length < 8 && !state.compare.includes(e.target.value)) {
        state.compare.push(e.target.value);
        renderView();
      }
    };
    qs('#cmp-ru').onclick = () => {
      state.compare = state.models
        .filter((m) => ['excellent', 'good'].includes(m.ru_quality))
        .map((m) => ({ m, w: (m.benchmarks || []).filter((b) => b.language === 'ru' && b.metric === 'WER') }))
        .filter((x) => x.w.length)
        .sort((a, b) => Math.min(...a.w.map((x) => x.value)) - Math.min(...b.w.map((x) => x.value)))
        .slice(0, 6).map((x) => x.m.id);
      renderView();
    };
    qs('#cmp-fast').onclick = () => {
      state.compare = state.models.filter((m) => m.rtfx)
        .sort((a, b) => b.rtfx - a.rtfx).slice(0, 6).map((m) => m.id);
      renderView();
    };
    qs('#cmp-clear').onclick = () => { state.compare = []; renderView(); };

    const chips = qs('#cmp-chips');
    chips.innerHTML = state.compare.map((id) => {
      const m = modelById(id);
      return m ? `<span class="chip accent">${esc(m.name)}
        <button class="ghost sm" style="padding:0 4px"
          onclick="__asrhub.cmpRemove('${esc(id)}')">✕</button></span>` : '';
    }).join('');

    const models = state.compare.map(modelById).filter(Boolean);
    if (!models.length) return;

    const werItems = models.map((m) => {
      const w = (m.benchmarks || []).filter((b) => b.language === 'ru' && b.metric === 'WER');
      const avg = w.length ? w.reduce((s, b) => s + b.value, 0) / w.length : null;
      return { label: m.name.length > 22 ? m.name.slice(0, 21) + '…' : m.name,
               value: avg, display: avg !== null ? avg.toFixed(1) + ' %' : 'нет данных',
               note: w.length ? `наборы: ${w.map((b) => b.dataset).join(', ')}` : '' };
    }).filter((x) => x.value !== null).sort((a, b) => a.value - b.value);
    Charts.hbars(qs('#cmp-wer'), { items: werItems, labelWidth: 190, unit: ' %' });

    Charts.hbars(qs('#cmp-rtfx'), {
      items: models.filter((m) => m.rtfx).map((m) => ({
        label: m.name.length > 22 ? m.name.slice(0, 21) + '…' : m.name,
        value: m.rtfx, display: '×' + num(m.rtfx), note: m.rtfx_hw }))
        .sort((a, b) => b.value - a.value),
      labelWidth: 190,
    });
    Charts.hbars(qs('#cmp-vram'), {
      items: models.filter((m) => m.vram_gb).map((m) => ({
        label: m.name.length > 22 ? m.name.slice(0, 21) + '…' : m.name,
        value: m.vram_gb, display: m.vram_gb + ' ГБ' })).sort((a, b) => a.value - b.value),
      labelWidth: 190, unit: ' ГБ',
    });
    Charts.hbars(qs('#cmp-disk'), {
      items: models.filter((m) => m.disk_mb).map((m) => ({
        label: m.name.length > 22 ? m.name.slice(0, 21) + '…' : m.name,
        value: m.disk_mb, display: m.disk_mb + ' МБ' })).sort((a, b) => a.value - b.value),
      labelWidth: 190, unit: ' МБ',
    });

    const rows = [
      ['Семейство', (m) => m.family],
      ['Движок', (m) => m.engine],
      ['Лицензия', (m) => `<span class="chip badge-license ${m.commercial_use ? '' : 'err'}">${
        esc(m.license)}</span>`],
      ['Коммерческое использование', (m) => m.commercial_use ? '✓ разрешено' : '✕ запрещено'],
      ['Качество на русском', (m) => `<span class="chip ${QUALITY_CLASS[m.ru_quality]}">${
        QUALITY_LABELS[m.ru_quality]}</span>`],
      ['Лучший WER ru', (m) => {
        const w = (m.benchmarks || []).filter((b) => b.language === 'ru' && b.metric === 'WER');
        return w.length ? `<b>${Math.min(...w.map((b) => b.value)).toFixed(1)} %</b>` : '—';
      }],
      ['Параметров, млн', (m) => m.params_m ? num(m.params_m) : '—'],
      ['Размер, МБ', (m) => m.disk_mb || '—'],
      ['Видеопамять, ГБ', (m) => m.vram_gb || '—'],
      ['RTFx', (m) => m.rtfx ? num(m.rtfx) : '—'],
      ['Потоковый режим', (m) => m.streaming ? '✓' : '—'],
      ['Пунктуация', (m) => m.punctuation ? '✓' : '—'],
      ['Диаризация', (m) => m.diarization ? '✓' : '—'],
      ['Перевод', (m) => m.translation ? '✓' : '—'],
      ['Таймкоды', (m) => ({ word: 'пословные', segment: 'по сегментам', none: 'нет' })[m.timestamps]],
      ['Макс. фрагмент', (m) => m.max_audio_s ? fmtDur(m.max_audio_s) : 'не ограничен'],
      ['Языков', (m) => m.languages.length > 3
        ? m.languages.slice(0, 3).join(', ') + `… (${m.languages.length})`
        : m.languages.join(', ')],
      ['Требует токен HF', (m) => m.gated ? '✓' : '—'],
      ['Зрелость', (m) => ({ stable: 'стабильная', new: 'новая',
                             legacy: 'устаревшая', experimental: 'экспериментальная' })[m.maturity]],
      ['Релиз', (m) => m.released || '—'],
    ];
    qs('#cmp-table').innerHTML = `<table>
      <thead><tr><th style="min-width:190px">Характеристика</th>
        ${models.map((m) => `<th>${esc(m.name)}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(([label, fn]) => `<tr>
        <td class="dim">${esc(label)}</td>
        ${models.map((m) => `<td>${fn(m)}</td>`).join('')}</tr>`).join('')}
      </tbody></table>`;
  },
};

window.__asrhub.cmpRemove = (id) => {
  state.compare = state.compare.filter((x) => x !== id);
  renderView();
};

// ==========================================================================
// Вид: Настройки
// ==========================================================================

RENDERERS.settings = {
  render(root) {
    const groups = state.catalog.groups;
    root.innerHTML = `
      <div class="settings-toolbar">
        <div class="group-nav" id="group-nav">
          ${groups.map((g) => `<button data-group="${esc(g.id)}"
            class="${state.paramGroup === g.id ? 'active' : ''}">${esc(g.title)}</button>`).join('')}
        </div>
      </div>
      <div class="settings-toolbar" style="top:106px">
        <input type="search" id="p-search" placeholder="поиск по параметрам"
          value="${esc(state.paramSearch)}" style="width:260px">
        <label class="row" style="gap:6px;cursor:pointer">
          <input type="checkbox" id="p-advanced" ${state.showAdvanced ? 'checked' : ''}
            style="width:auto"><span class="small">показывать параметры для опытных</span></label>
        <span class="spacer"></span>
        <button id="p-save" class="primary">Сохранить в конфигурацию</button>
        <button id="p-apply">Применить на сервере</button>
        <button id="p-reset" class="danger">Сбросить</button>
      </div>
      <div id="params-body"></div>`;

    qsa('#group-nav button').forEach((b) => b.addEventListener('click', () => {
      state.paramGroup = b.dataset.group;
      state.paramSearch = '';
      renderView();
    }));
    let timer;
    qs('#p-search').addEventListener('input', (e) => {
      clearTimeout(timer);
      state.paramSearch = e.target.value;
      timer = setTimeout(() => this.list(), 250);
    });
    qs('#p-advanced').addEventListener('change', (e) => {
      state.showAdvanced = e.target.checked;
      this.list();
    });
    qs('#p-apply').onclick = async () => {
      try {
        const result = await API.put('/api/settings', state.settings);
        toast(`Применено параметров: ${Object.keys(result.applied).length}`, 'ok');
      } catch (err) { fail(err); }
    };
    qs('#p-save').onclick = async () => {
      try {
        await API.put('/api/settings', state.settings);
        const result = await API.post('/api/settings/save');
        toast('Конфигурация сохранена', 'ok', result.saved);
      } catch (err) { fail(err); }
    };
    qs('#p-reset').onclick = async () => {
      if (!confirm('Сбросить все настройки к значениям по умолчанию?')) return;
      try {
        await API.post('/api/settings/reset');
        const fresh = await API.get('/api/settings');
        state.settings = fresh.values;
        toast('Настройки сброшены', 'warn');
        renderView();
      } catch (err) { fail(err); }
    };
    this.list();
  },

  list() {
    const host = qs('#params-body');
    const search = (state.paramSearch || '').toLowerCase();
    const groups = state.catalog.groups;

    let items = state.params;
    if (search) {
      items = items.filter((p) =>
        `${p.key} ${p.label} ${p.description} ${p.recommendation}`.toLowerCase().includes(search));
    } else {
      items = items.filter((p) => p.group === state.paramGroup);
    }
    if (!state.showAdvanced) items = items.filter((p) => !p.advanced);

    if (!items.length) {
      host.innerHTML = '<div class="card"><div class="empty">Параметров не найдено. ' +
        'Возможно, стоит включить показ параметров для опытных.</div></div>';
      return;
    }

    const byGroup = {};
    items.forEach((p) => { (byGroup[p.group] = byGroup[p.group] || []).push(p); });

    host.innerHTML = '';
    Object.entries(byGroup).forEach(([groupId, params]) => {
      const group = groups.find((g) => g.id === groupId) || { title: groupId, description: '' };
      const section = h(`<section class="card">
        <div class="card-head"><h3>${esc(group.title)}</h3>
          <span class="hint">${esc(group.description)}</span>
          <span class="spacer"></span>
          <span class="chip">${params.length}</span></div>
        <div class="params"></div></section>`);
      const box = qs('.params', section);
      params.forEach((spec) => {
        box.appendChild(paramCard(spec, state.settings[spec.key], (value) => {
          state.settings[spec.key] = value;
          state.jobSettings[spec.key] = value;
        }));
      });
      host.appendChild(section);
    });
  },
};

// ==========================================================================
// Вид: Сервер
// ==========================================================================

RENDERERS.system = {
  async render(root) {
    root.innerHTML = '<div class="empty">Загрузка сведений о сервере…</div>';
    let sys;
    try { sys = await API.get('/api/system'); } catch (err) { fail(err); return; }
    state.system = sys;
    const hw = sys.hardware;
    const gpu = (hw.gpus || [])[0];

    root.innerHTML = `
      <div class="grid cols-4" style="margin-bottom:16px">
        ${kpi('Ускоритель', hw.accelerator.toUpperCase(),
              gpu ? esc(gpu.name) : `${hw.cpu_cores_physical} физических ядер`)}
        ${kpi('Оперативная память', `${hw.ram_total_gb} ГБ`,
              `доступно ${hw.ram_available_gb} ГБ`)}
        ${kpi('Свободно на диске', `${hw.disk_free_gb} ГБ`,
              `база: ${sys.database.size_mb} МБ`)}
        ${kpi('Время работы', fmtDur(sys.uptime_s), `версия ${sys.version}`)}
      </div>

      ${hw.warnings.length ? `<section class="card" style="border-color:var(--warn)">
        <div class="card-head"><h3 style="color:var(--warn)">Предупреждения окружения</h3></div>
        <ul style="margin:0;padding-left:18px" class="small">
          ${hw.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></section>` : ''}

      <div class="grid cols-2">
        ${card('Оборудование', '', `<table>
          <tr><td class="dim">Операционная система</td><td>${esc(hw.os_name)} ${esc(hw.os_version)}</td></tr>
          <tr><td class="dim">Архитектура</td><td>${esc(hw.arch)}</td></tr>
          <tr><td class="dim">Процессор</td><td class="small">${esc(hw.cpu_model)}</td></tr>
          <tr><td class="dim">Ядер</td><td>${hw.cpu_cores_physical} физических / ${
            hw.cpu_cores_logical} логических</td></tr>
          <tr><td class="dim">Видеокарты</td><td>${(hw.gpus || []).length
            ? hw.gpus.map((g) => `${esc(g.name)} — ${(g.memory_total_mb / 1024).toFixed(1)} ГБ`
              ).join('<br>') : 'не обнаружены'}</td></tr>
          <tr><td class="dim">CUDA / cuDNN</td><td>${esc(hw.cuda_version || '—')} / ${
            esc(hw.cudnn_version || '—')}</td></tr>
          <tr><td class="dim">PyTorch</td><td>${esc(hw.torch_version || 'не установлен')}</td></tr>
          <tr><td class="dim">ffmpeg</td><td>${hw.ffmpeg
            ? esc(hw.ffmpeg_version) : '<span style="color:var(--err)">не найден</span>'}</td></tr>
          <tr><td class="dim">Python</td><td>${esc(hw.python_version)}</td></tr>
        </table>`)}

        ${card('Рекомендуемые настройки', 'подобраны под обнаруженное оборудование',
          `<div class="param-rec" style="margin-bottom:10px">${esc(sys.recommended._reason)}</div>
          <table>${Object.entries(sys.recommended).filter(([k]) => !k.startsWith('_'))
            .map(([k, v]) => {
              const spec = paramByKey(k);
              return `<tr><td class="dim">${esc(spec ? spec.label : k)}</td>
                <td class="num mono">${esc(String(v))}</td></tr>`;
            }).join('')}</table>
          <button class="primary sm" style="margin-top:10px" id="apply-recommended">
            Применить рекомендации</button>`)}
      </div>

      ${card('Движки распознавания', 'что установлено и что мешает',
        `<div class="table-wrap"><table>
          <thead><tr><th>Движок</th><th>Состояние</th><th>Лицензия</th>
            <th>Возможности</th><th>Замечания</th></tr></thead><tbody>
          ${state.engines.map((e) => `<tr>
            <td><b>${esc(e.name)}</b><div class="small faint mono">${esc(e.id)}</div></td>
            <td>${e.available ? '<span class="chip ok">установлен</span>'
              : '<span class="chip err">не установлен</span>'}</td>
            <td class="small">${esc(e.license)}</td>
            <td><div class="row wrap" style="gap:3px">
              ${e.supports.gpu ? '<span class="chip">GPU</span>' : ''}
              ${e.supports.cpu ? '<span class="chip">CPU</span>' : ''}
              ${e.supports.mps ? '<span class="chip">Apple</span>' : ''}
              ${e.supports.streaming ? '<span class="chip info">поток</span>' : ''}
              ${e.supports.batching ? '<span class="chip">батчинг</span>' : ''}</div></td>
            <td class="small dim">${esc(e.available ? (e.install_notes || '') : e.reason)}
              ${(e.known_issues || []).length ? `<details class="help">
                <summary>Известные проблемы (${e.known_issues.length})</summary>
                <ul style="margin:4px 0;padding-left:16px">${
                  e.known_issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul></details>` : ''}
            </td></tr>`).join('')}
        </tbody></table></div>`)}

      <div class="grid cols-2">
        ${card('Хранилище и пути', '', `<table>
          ${Object.entries(sys.paths).map(([k, v]) =>
            `<tr><td class="dim">${esc(k)}</td><td class="mono small">${esc(v)}</td></tr>`).join('')}
          <tr><td class="dim">Конфигурация</td><td class="mono small">${
            esc(sys.config_file || 'не используется')}</td></tr>
          <tr><td class="dim">Заданий в базе</td><td class="num">${sys.database.jobs}</td></tr>
          <tr><td class="dim">Сегментов</td><td class="num">${num(sys.database.segments)}</td></tr>
          <tr><td class="dim">Метрик</td><td class="num">${num(sys.database.metrics)}</td></tr>
          </table>
          <div class="row" style="margin-top:10px">
            <button class="sm" id="btn-cleanup">Очистить старые данные</button>
            <button class="sm" id="btn-unload">Выгрузить модели из памяти</button>
          </div>`)}

        ${card('Ключи доступа', 'создание и отзыв',
          '<div id="keys-body"></div>' +
          `<div class="row" style="margin-top:10px">
            <input type="text" id="key-name" placeholder="название ключа" style="flex:1">
            <select id="key-role" style="width:130px">
              <option value="user">user</option><option value="admin">admin</option>
              <option value="readonly">readonly</option></select>
            <button class="primary sm" id="key-create">Создать</button></div>`)}
      </div>`;

    qs('#apply-recommended').onclick = async () => {
      const values = {};
      Object.entries(sys.recommended).forEach(([k, v]) => { if (!k.startsWith('_')) values[k] = v; });
      try {
        await API.put('/api/settings', values);
        Object.assign(state.settings, values);
        toast('Рекомендации применены', 'ok');
      } catch (err) { fail(err); }
    };
    qs('#btn-cleanup').onclick = async () => {
      try {
        const r = await API.post('/api/maintenance/cleanup');
        toast('Очистка выполнена', 'ok', JSON.stringify(r.removed));
      } catch (err) { fail(err); }
    };
    qs('#btn-unload').onclick = async () => {
      try { await API.post('/api/maintenance/unload-models'); toast('Модели выгружены', 'ok'); }
      catch (err) { fail(err); }
    };
    qs('#key-create').onclick = async () => {
      const name = qs('#key-name').value.trim();
      if (!name) { toast('Укажите название ключа', 'warn'); return; }
      try {
        const r = await API.post('/api/keys',
          { name, role: qs('#key-role').value, rate_limit: 0 });
        prompt('Сохраните ключ — он показывается один раз:', r.key);
        this.loadKeys();
      } catch (err) { fail(err); }
    };
    this.loadKeys();
  },

  async loadKeys() {
    const host = qs('#keys-body');
    if (!host) return;
    try {
      const data = await API.get('/api/keys');
      host.innerHTML = data.items.length ? `<table>
        <thead><tr><th>Ключ</th><th>Название</th><th>Роль</th><th></th></tr></thead><tbody>
        ${data.items.map((k) => `<tr>
          <td class="mono small">${esc(k.key_preview)}</td>
          <td>${esc(k.name || '')}</td>
          <td><span class="chip ${k.role === 'admin' ? 'accent' : ''}">${esc(k.role)}</span></td>
          <td><button class="ghost sm danger"
            onclick="__asrhub.revokeKey('${esc(k.key_preview.split('…')[0])}')">отозвать</button>
          </td></tr>`).join('')}</tbody></table>`
        : '<div class="empty small">Ключей нет — аутентификация отключена</div>';
    } catch (err) {
      host.innerHTML = '<div class="empty small">Требуется ключ администратора</div>';
    }
  },
};

window.__asrhub.revokeKey = async (preview) => {
  if (!confirm('Отозвать ключ?')) return;
  try { await API.del(`/api/keys/${preview}`); toast('Ключ отозван', 'warn'); renderView(); }
  catch (err) { fail(err); }
};

// ==========================================================================
// Вид: Журнал
// ==========================================================================

RENDERERS.logs = {
  render(root) {
    root.innerHTML = `
      <div class="settings-toolbar">
        <select id="log-level" style="width:150px">
          <option value="">Все уровни</option>
          <option value="INFO">INFO и выше</option>
          <option value="WARNING">WARNING и выше</option>
          <option value="ERROR">Только ошибки</option>
        </select>
        <input type="search" id="log-search" placeholder="поиск по журналу" style="width:280px">
        <label class="row" style="gap:6px;cursor:pointer">
          <input type="checkbox" id="log-auto" checked style="width:auto">
          <span class="small">обновлять автоматически</span></label>
        <span class="spacer"></span>
        <span class="small faint" id="log-counts"></span>
      </div>
      <div class="grid cols-2">
        ${card('Журнал сервера', '', '<div class="table-wrap" id="log-table"></div>')}
        ${card('События заданий', '', '<div class="table-wrap" id="event-table"></div>')}
      </div>`;
    qs('#log-level').onchange = () => this.load();
    let timer;
    qs('#log-search').oninput = () => { clearTimeout(timer); timer = setTimeout(() => this.load(), 300); };
    this.load();
    this.interval = setInterval(() => {
      if (state.view === 'logs' && qs('#log-auto') && qs('#log-auto').checked) this.load();
      else if (state.view !== 'logs') clearInterval(this.interval);
    }, 5000);
  },

  async load() {
    try {
      const level = qs('#log-level') ? qs('#log-level').value : '';
      const search = qs('#log-search') ? qs('#log-search').value : '';
      const [logs, events] = await Promise.all([
        API.get(`/api/logs?limit=250&level=${level}&search=${encodeURIComponent(search)}`),
        API.get('/api/events?limit=150'),
      ]);
      const counts = qs('#log-counts');
      if (counts) {
        counts.innerHTML = Object.entries(logs.counts || {})
          .map(([k, v]) => `<span class="chip ${k === 'ERROR' ? 'err'
            : k === 'WARNING' ? 'warn' : ''}">${k}: ${v}</span>`).join(' ');
      }
      const table = qs('#log-table');
      if (table) {
        table.innerHTML = logs.items.length ? `<table>
          <thead><tr><th style="width:70px">Время</th><th style="width:80px">Уровень</th>
            <th>Сообщение</th></tr></thead><tbody>
          ${logs.items.slice().reverse().map((l) => `<tr>
            <td class="small faint mono nowrap">${esc(l.time.split(' ')[1] || l.time)}</td>
            <td><span class="chip ${l.level === 'ERROR' ? 'err'
              : l.level === 'WARNING' ? 'warn' : ''}">${esc(l.level)}</span></td>
            <td class="small"><span class="faint mono">${esc(l.logger.replace('asrhub.', ''))}</span>
              ${esc(l.message)}
              ${l.job_id ? `<span class="chip" style="margin-left:6px">${
                esc(String(l.job_id).slice(0, 12))}</span>` : ''}</td>
          </tr>`).join('')}</tbody></table>` : '<div class="empty small">Записей нет</div>';
      }
      const eventTable = qs('#event-table');
      if (eventTable) {
        eventTable.innerHTML = events.items.length ? `<table>
          <thead><tr><th style="width:110px">Время</th><th style="width:120px">Событие</th>
            <th>Описание</th></tr></thead><tbody>
          ${events.items.map((e) => `<tr>
            <td class="small faint nowrap">${fmtTime(e.ts)}</td>
            <td><span class="chip">${esc(e.kind)}</span></td>
            <td class="small">${esc(e.message || '')}
              ${e.job_id ? `<button class="ghost sm"
                onclick="__asrhub.openJob('${esc(e.job_id)}')">задание</button>` : ''}</td>
          </tr>`).join('')}</tbody></table>` : '<div class="empty small">Событий нет</div>';
      }
    } catch (err) { /* журнал не критичен */ }
  },
};

// ==========================================================================
// Вид: Справка
// ==========================================================================

RENDERERS.help = {
  render(root) {
    const key = localStorage.getItem('asrhub_key') || 'ВАШ_КЛЮЧ';
    root.innerHTML = `
      <div class="grid cols-2">
        ${card('С чего начать', '', `<div class="small dim" style="line-height:1.75">
          <p><b>1. Проверьте установку.</b> Раздел «Сервер» показывает, какие движки
          установлены и что мешает остальным. Для первого прогона выберите пресет
          «Проверка установки» — он использует встроенный симулятор и не требует весов.</p>
          <p><b>2. Загрузите модель.</b> В разделе «Модели» нажмите ↓ рядом с нужной.
          Для русского языка начните с <span class="mono">gigaam-v3-e2e-rnnt</span>:
          лучшая точность и готовый текст с пунктуацией.</p>
          <p><b>3. Прогоните типовые файлы.</b> Возьмите 10–20 записей, характерных
          для вашей задачи. Не оценивайте модель по одному файлу.</p>
          <p><b>4. Настройте под себя.</b> Начните с пресета, ближе всего к вашему
          сценарию, затем правьте отдельные параметры. У каждого есть описание,
          рекомендация и примеры значений.</p>
          <p><b>5. Измерьте.</b> Задайте эталонный текст для нескольких файлов —
          и раздел «Аналитика» покажет фактический WER на ваших данных.</p>
        </div>`)}

        ${card('Быстрые ответы', '', `<div class="small dim" style="line-height:1.75">
          <p><b>Модель придумывает текст, которого не было.</b> Включите детектор речи,
          отключите «Учитывать предыдущий текст», оставьте включённым каскад температур
          и фильтр типовых галлюцинаций. Это четыре независимые меры, вместе они снимают
          большую часть проблемы.</p>
          <p><b>Не хватает видеопамяти.</b> Уменьшите размер пакета вдвое, включите
          вычисления int8 или выберите модель полегче. Сервер делает это автоматически
          при повторе, если повторы разрешены.</p>
          <p><b>Слишком медленно.</b> Смотрите разбивку по этапам в «Аналитике».
          Если больше 15 % уходит на загрузку моделей — увеличьте кеш моделей.
          Если на распознавание — уменьшите ширину луча до 1 и увеличьте пакет.</p>
          <p><b>Плохо распознаются имена и термины.</b> Заполните начальную подсказку
          и ключевые слова, а систематические ошибки исправьте словарём замен.</p>
        </div>`)}
      </div>

      ${card('Программный интерфейс', 'полная документация: /api/docs', `
        <div class="small dim" style="margin-bottom:10px">Ключ передаётся заголовком
          <span class="mono">X-API-Key</span>.</div>
        <pre class="mono" style="background:var(--bg);padding:12px;border-radius:6px;
          overflow:auto;font-size:12px;line-height:1.6"># поставить файл в очередь
curl -X POST ${location.origin}/api/jobs \\
  -H "X-API-Key: ${esc(key)}" \\
  -F "file=@запись.mp3" \\
  -F 'settings={"model":"gigaam-v3-e2e-rnnt","language":"ru","diarization_enabled":true}'

# состояние задания
curl -H "X-API-Key: ${esc(key)}" ${location.origin}/api/jobs/&lt;id&gt;

# скачать субтитры
curl -H "X-API-Key: ${esc(key)}" \\
  "${location.origin}/api/jobs/&lt;id&gt;/download?fmt=srt" -o субтитры.srt

# очередь и аналитика
curl -H "X-API-Key: ${esc(key)}" ${location.origin}/api/queue
curl -H "X-API-Key: ${esc(key)}" "${location.origin}/api/analytics?period=week"</pre>
        <div class="row" style="margin-top:10px">
          <a class="btn" href="/api/reference" target="_blank">Справочник (работает офлайн)</a>
          <a class="btn" href="/api/docs" target="_blank">Swagger (нужен интернет)</a>
          <a class="btn" href="/api/redoc" target="_blank">ReDoc (нужен интернет)</a>
          <a class="btn" href="/api/openapi.json" target="_blank">Схема OpenAPI</a>
        </div>`)}

      ${card('Ключ доступа', '', `<div class="row">
        <input type="password" id="help-key" value="${esc(localStorage.getItem('asrhub_key') || '')}"
          placeholder="ah_…" style="flex:1">
        <button class="primary" id="help-key-save">Сохранить</button>
        <button class="danger" id="help-key-clear">Забыть</button></div>
        <div class="small faint" style="margin-top:8px">Ключ хранится только в этом браузере.</div>`)}

      ${card('Источники данных каталога', 'каждое число в каталоге имеет ссылку на первоисточник',
        `<div class="table-wrap"><table>
          <thead><tr><th style="width:200px">Ключ</th><th>Источник</th></tr></thead><tbody>
          ${Object.entries(state.catalog.sources).map(([k, v]) =>
            `<tr><td class="mono small">${esc(k)}</td><td class="small dim">${esc(v)}</td></tr>`
          ).join('')}</tbody></table></div>
        <div class="small faint" style="margin-top:8px">
          Каталог собран по состоянию на ${esc(state.catalog.date)}. Модели выходят
          постоянно — сверяйтесь с первоисточниками перед принятием решений.</div>`)}`;

    qs('#help-key-save').onclick = () => {
      localStorage.setItem('asrhub_key', qs('#help-key').value.trim());
      toast('Ключ сохранён', 'ok');
      location.reload();
    };
    qs('#help-key-clear').onclick = () => {
      localStorage.removeItem('asrhub_key');
      toast('Ключ забыт', 'warn');
      location.reload();
    };
  },
};

})();
