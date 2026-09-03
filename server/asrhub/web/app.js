/* ASR Hub — веб-интерфейс. Без сборки и без внешних зависимостей. */
(function () {
'use strict';

// ==========================================================================
// Состояние и утилиты
// ==========================================================================

const state = {
  view: 'transcribe',
  viewTimers: [],
  hashGoing: false,
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
  /** Контроллеры отмены по ключу: новый запрос отменяет предыдущий такой же. */
  _inflight: new Map(),
  /** Все незавершённые запросы на чтение — их снимает смена раздела. */
  _pending: new Set(),

  /**
   * Снимает все незавершённые запросы на чтение.
   *
   * Вызывается при уходе из раздела. Без этого медленный ответ приходил уже
   * после переключения и переписывал содержимое поверх нового раздела: на
   * экране «Журнал», а в теле — таблица моделей. Запросы на изменение
   * (POST, PUT, DELETE) не трогаем: постановка задания в очередь не должна
   * срываться от того, что пользователь переключил вкладку.
   */
  abortAll() {
    this._inflight.forEach((controller) => controller.abort());
    this._inflight.clear();
    this._pending.forEach((controller) => controller.abort());
    this._pending.clear();
  },

  /**
   * Запрос, который отменяет предыдущий с тем же ключом.
   * Нужен для поиска и фильтров: без отмены ответ на «alpha» мог прийти
   * после ответа на «beta» и перезаписать таблицу устаревшими данными.
   */
  latest(key, path, options) {
    const previous = this._inflight.get(key);
    if (previous) previous.abort();
    const controller = new AbortController();
    this._inflight.set(key, controller);
    return this.call(path, Object.assign({ signal: controller.signal }, options || {}))
      .finally(() => {
        if (this._inflight.get(key) === controller) this._inflight.delete(key);
      });
  },

  async call(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    const key = localStorage.getItem('asrhub_key');
    if (key) opts.headers['X-API-Key'] = key;
    if (opts.json !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    // Чтение получает свой контроллер отмены, если вызывающий не передал
    // свой. Исключение — фоновые запросы: счётчики в меню и состояние связи
    // живут вне разделов, и смена раздела не должна их снимать. Раньше
    // счётчик тревог гас в ноль при каждом переходе именно поэтому.
    const method = (opts.method || 'GET').toUpperCase();
    const background = opts.background === true;
    delete opts.background;
    let own = null;
    if (!opts.signal && method === 'GET' && !background) {
      own = new AbortController();
      opts.signal = own.signal;
      this._pending.add(own);
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      if (err && err.name === 'AbortError') {
        // Запрос отменён более свежим — это не сбой, а штатный ход.
        throw { code: 'aborted', message: 'Запрос отменён', silent: true };
      }
      throw { code: 'network', message: 'Сервер недоступен',
              hint: 'Проверьте, что служба asrhub запущена и доступна по сети.' };
    } finally {
      if (own) this._pending.delete(own);
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
  /** Запрос вне разделов: смена раздела его не отменяет. */
  background(path) { return this.call(path, { background: true }); },
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
/** Страница уходит: браузер рвёт незавершённые запросы, и это не сбой. */
let unloading = false;
window.addEventListener('pagehide', () => { unloading = true; });
window.addEventListener('beforeunload', () => { unloading = true; });

function fail(err) {
  // Отменённый запрос — не сбой: пользователь просто набрал следующий символ.
  if (err && err.silent) return;
  // При закрытии или перезагрузке вкладки все запросы падают разом, и на
  // прощание пользователь получал стопку красных плашек «Сервер недоступен».
  if (unloading) return;
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
    // Запуск идёт вне разделов, поэтому запросы фоновые: смена раздела не
    // должна их снимать. Иначе переход по меню в первую секунду после
    // загрузки отменял загрузку каталога, и вместо интерфейса появлялась
    // карточка «Не удалось связаться с сервером. Запрос отменён».
    const [catalog, settings] = await Promise.all([
      API.background('/api/catalog'),
      API.background('/api/settings').catch(() => ({ values: {} })),
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
    // Отменённый запрос — не сбой связи, и рисовать по нему заглушку нельзя.
    if (err && err.silent) return;
    fail(err);
    qs('#content').innerHTML = `<div class="card"><div class="empty">
      <b>Не удалось связаться с сервером</b><div class="small" style="margin-top:8px">
      ${esc(err.message)}<br>${esc(err.hint || '')}</div>
      <button class="primary" id="boot-retry" style="margin-top:12px">Повторить</button>
      </div></div>`;
    const retry = qs('#boot-retry');
    if (retry) retry.onclick = () => bootstrap();
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
  try { state.engines = (await API.background('/api/engines')).items; } catch (e) { /* не критично */ }
}
async function refreshQueue() {
  try {
    state.queue = await API.background('/api/queue');
    const depth = state.queue.queue_depth || 0;
    qs('#badge-queue').textContent = depth;
    updateAlertBadge();
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

async function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const key = localStorage.getItem('asrhub_key');

  // Ключ в адресе оседает в истории браузера, в журнале обратного прокси и
  // в заголовке Referer. Поэтому берём одноразовый билет на минуту: он
  // гасится при первом подключении и ничего больше не открывает.
  let ticket = '';
  if (key) {
    try {
      const issued = await API.post('/api/auth/ticket', {});
      ticket = (issued && issued.ticket) || '';
    } catch (e) {
      // Старый сервер без /api/auth/ticket или нет связи — не рвём ленту
      // событий: ниже сработает обычный цикл переподключения.
      ticket = '';
    }
  }
  const url = `${proto}://${location.host}/ws${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`;
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
      refreshQueue(); refreshLiveViews();
      break;
    case 'job.failed':
      toast('Задание завершилось ошибкой', 'err',
            (message.error && message.error.message) || '');
      refreshQueue(); refreshLiveViews();
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

function refreshLiveViews() {
  // Перерисовываем только те разделы, у которых есть мягкое обновление.
  // Прежний renderView(true) для остальных означал полную перерисовку с
  // нуля: в «Результатах» стиралась строка поиска и сортировка, в
  // «Журнале» — уровень и поиск, в «Моделях» — пять фильтров. При очереди
  // из десятка файлов это происходило каждые несколько секунд, и набрать
  // запрос было физически невозможно.
  const renderer = RENDERERS[state.view];
  if (renderer && typeof renderer.soft === 'function') renderView(true);
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
  monitoring: { title: 'Мониторинг', subtitle: 'Метрики наружу, пороги тревог, приёмники телеметрии' },
  logs:       { title: 'Журнал', subtitle: 'События сервера и заданий' },
  help:       { title: 'Справка', subtitle: 'Как пользоваться, программный интерфейс, устранение неполадок' },
};

function go(view) {
  state.view = view;
  qsa('.nav-item').forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle('active', active);
    // Кроме подсветки нужен и признак для диктора: без него активный раздел
    // на слух ничем не отличался от прочих.
    if (active) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  closeNav();                       // на узком экране меню уезжает после выбора
  const meta = VIEWS[view] || { title: view, subtitle: '' };
  qs('#view-title').textContent = meta.title;
  qs('#view-subtitle').textContent = meta.subtitle;
  // Смена hash сама по себе вызовет render(); флаг гасит повторную отрисовку,
  // иначе каждый переход слал все запросы раздела дважды.
  if (location.hash.replace('#', '') !== view) {
    state.hashGoing = true;
    location.hash = view;
  }
  renderView();
}

function render() {
  if (state.hashGoing) { state.hashGoing = false; return; }
  const hash = location.hash.replace('#', '');
  go(VIEWS[hash] ? hash : 'transcribe');
}

/**
 * Счётчик тревог рядом с пунктом «Мониторинг».
 *
 * В разметке он стоял с нулём и нигде не обновлялся: при семи горящих
 * тревогах меню показывало «0», и пользователь, привыкший к живому счётчику
 * очереди, читал это как «тревог нет».
 */
async function updateAlertBadge() {
  const badge = qs('#badge-alerts');
  if (!badge) return;
  try {
    const data = await API.background('/api/monitoring/alerts?only_firing=true');
    const summary = data.summary || {};
    const firing = summary.firing ?? (data.items || []).length;
    badge.textContent = firing;
    badge.classList.toggle('err', firing > 0);
  } catch (err) {
    // Мониторинг может быть закрыт ключом или выключен — счётчик просто
    // не показываем, шуметь об этом не о чем.
    badge.textContent = '0';
  }
}

/**
 * Список файлов, которые сервер не принял, с причиной по каждому.
 * Всплывашка живёт девять секунд и вмещает одну строку — для разбора
 * отказов этого мало.
 */
function showRejected(errors) {
  const host = qs('#file-list');
  if (!host) return;
  const box = h(`<div class="card" style="border-color:var(--err);margin-top:12px">
    <div class="card-head"><b style="color:var(--err)">Не принято: ${errors.length}</b>
      <span class="spacer"></span>
      <button class="ghost sm" id="rejected-close">Скрыть</button></div>
    <div class="table-wrap"><table><thead><tr><th>Файл</th><th>Почему</th></tr></thead>
      <tbody>${errors.map((e) => `<tr>
        <td class="truncate" style="max-width:260px">${esc(e.filename || '—')}</td>
        <td class="small">${esc(e.error || '—')}</td></tr>`).join('')}</tbody></table></div>
    <p class="small dim" style="margin-top:8px">Эти файлы остались в списке —
      поправьте формат или размер и отправьте снова.</p>
  </div>`);
  const previous = qs('#rejected-box');
  if (previous) previous.remove();
  box.id = 'rejected-box';
  host.parentNode.insertBefore(box, host.nextSibling);
  qs('#rejected-close', box).onclick = () => box.remove();
}

function renderView(soft) {
  const content = qs('#content');
  const renderer = RENDERERS[state.view];
  if (!renderer) { content.innerHTML = ''; return; }
  if (soft && renderer.soft) { renderer.soft(); return; }

  // Отменяем таймеры и подписки предыдущего раздела: без этого при каждом
  // возврате в «Журнал» добавлялся ещё один опрос, и вкладка сама себя
  // упирала в ограничение частоты запросов.
  stopViewTimers();
  API.abortAll();
  // Подсказка графика прячется по mouseleave. Если узел, на котором она
  // висит, снесён перерисовкой, событие не придёт никогда — и подсказка
  // остаётся висеть поверх любых других разделов. Гасим её явно.
  const tip = qs('#chart-tip');
  if (tip) tip.style.display = 'none';
  content.innerHTML = '';
  const view = state.view;
  try {
    const result = renderer.render(content);
    if (result && typeof result.catch === 'function') {
      result.catch((err) => {
        // Отменённый запрос и раздел, который успели сменить, — не ошибка:
        // рисовать поверх нового раздела карточку «не загрузилось» нельзя.
        if (err && err.silent) return;
        if (state.view !== view) return;
        showViewFailure(content, err);
      });
    }
  } catch (err) {
    showViewFailure(content, err);
  }
}

/** Таймеры текущего раздела: заводятся через viewTimer, гасятся при уходе. */
function viewTimer(fn, intervalMs) {
  const handle = setInterval(fn, intervalMs);
  state.viewTimers.push(handle);
  return handle;
}

function stopViewTimers() {
  (state.viewTimers || []).forEach(clearInterval);
  state.viewTimers = [];
}

function showViewFailure(content, err) {
  const message = (err && (err.message || err.detail)) || 'Не удалось загрузить раздел';
  const hint = (err && err.hint) || 'Проверьте, что сервер запущен и доступен по сети.';
  content.innerHTML = `<section class="card">
    <div class="card-head"><h3 style="color:var(--err)">Раздел не загрузился</h3></div>
    <p>${esc(String(message))}</p>
    <p class="small dim">${esc(String(hint))}</p>
    <button class="primary" id="view-retry">Повторить</button>
  </section>`;
  const retry = qs('#view-retry');
  if (retry) retry.onclick = () => renderView();
}

window.addEventListener('hashchange', render);

/** Выдвижное меню на узких экранах. */
function toggleNav(force) {
  const open = force === undefined ? !document.body.classList.contains('nav-open') : force;
  document.body.classList.toggle('nav-open', open);
  const button = qs('#nav-toggle');
  if (button) {
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.setAttribute('aria-label', open ? 'Закрыть меню разделов' : 'Открыть меню разделов');
  }
  if (open) {
    const first = qs('.nav-item');
    if (first) first.focus();
  }
}

function closeNav() {
  if (document.body.classList.contains('nav-open')) toggleNav(false);
}

document.addEventListener('DOMContentLoaded', () => {
  qsa('.nav-item').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  const navToggle = qs('#nav-toggle');
  if (navToggle) navToggle.addEventListener('click', () => toggleNav());
  // Нажатие по затемнению закрывает меню: попасть в узкую кнопку на телефоне
  // сложнее, чем просто ткнуть в сторону.
  document.addEventListener('click', (e) => {
    if (!document.body.classList.contains('nav-open')) return;
    if (e.target.closest('.sidebar') || e.target.closest('#nav-toggle')) return;
    closeNav();
  });
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
    // Графики берут цвета из темы в момент отрисовки, а renderView открытое
    // окно не трогает: без этого события карточка после смены темы остаётся
    // с цветами прежней.
    document.body.dispatchEvent(new CustomEvent('asrhub:theme'));
  });
  installHotkeys();
  bootstrap();
});

// --------------------------------------------------------------------------
// Горячие клавиши
// --------------------------------------------------------------------------

const HOTKEY_VIEWS = ['transcribe', 'queue', 'results', 'analytics', 'models',
                      'compare', 'settings', 'system', 'monitoring', 'logs'];

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
  closeModal(modals[modals.length - 1]);
  return true;
}

/** Элементы, до которых можно добраться клавишей Tab. */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),'
  + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Показывает модальное окно и делает его доступным с клавиатуры.
 *
 * Без этого окно оставалось «картинкой»: Tab уводил фокус на элементы под
 * ним, экранный диктор продолжал читать спрятанный за подложкой список, а
 * после закрытия фокус терялся в начале страницы.
 */
function mountModal(backdrop, options) {
  const opts = options || {};
  const dialog = qs('.modal', backdrop) || backdrop;
  backdrop.setAttribute('role', 'presentation');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('tabindex', '-1');

  // Заголовок окна — первый <b> в шапке; его и озвучит диктор.
  const heading = qs('.modal-head b', dialog);
  if (heading) {
    if (!heading.id) heading.id = `modal-title-${Math.random().toString(36).slice(2, 9)}`;
    dialog.setAttribute('aria-labelledby', heading.id);
  } else if (opts.label) {
    dialog.setAttribute('aria-label', opts.label);
  }

  backdrop.__returnFocus = document.activeElement;
  // Пока окно открыто, остальная страница скрыта от диктора.
  const app = qs('.app');
  if (app && qsa('.modal-backdrop').length === 0) app.setAttribute('aria-hidden', 'true');

  document.body.appendChild(backdrop);
  document.body.classList.add('modal-open');

  // Ловушка фокуса: Tab по кругу внутри окна.
  backdrop.__trap = (e) => {
    if (e.key !== 'Tab') return;
    const items = qsa(FOCUSABLE, dialog).filter((el) => el.offsetParent !== null);
    if (!items.length) { e.preventDefault(); dialog.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  };
  backdrop.addEventListener('keydown', backdrop.__trap);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closeModal(backdrop); });

  const initial = qsa(FOCUSABLE, dialog).filter((el) => el.offsetParent !== null)[0];
  (initial || dialog).focus();
  return backdrop;
}

/** Закрывает окно и возвращает фокус туда, откуда его открыли. */
function closeModal(backdrop) {
  if (!backdrop || !backdrop.parentNode) return;
  const back = backdrop.__returnFocus;
  // Событие до удаления из документа: по нему содержимое окна снимает
  // подписки на window — иначе каждое открытие карточки оставляет
  // обработчик, который дальше дёргает уже несуществующие узлы.
  backdrop.dispatchEvent(new CustomEvent('asrhub:closed'));
  backdrop.remove();
  if (!qsa('.modal-backdrop').length) {
    document.body.classList.remove('modal-open');
    const app = qs('.app');
    if (app) app.removeAttribute('aria-hidden');
  }
  if (back && typeof back.focus === 'function' && document.contains(back)) back.focus();
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
      <button class="ghost icon" id="hk-close" aria-label="Закрыть" title="Закрыть">✕</button></div>
    <div class="modal-body"><table class="table"><tbody>${rows}</tbody></table>
      <p class="hint" style="margin-top:12px">Буквенные сокращения не срабатывают,
        пока курсор находится в поле ввода.</p></div>
  </div></div>`);
  mountModal(backdrop, { label: 'Горячие клавиши' });
  qs('#hk-close', backdrop).onclick = () => closeModal(backdrop);
}

function installHotkeys() {
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (e.key === 'Escape') {
      if (closeTopModal()) e.preventDefault();
      else if (document.body.classList.contains('nav-open')) { closeNav(); e.preventDefault(); }
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
            <div class="dropzone" id="dropzone" role="button" tabindex="0"
                 aria-label="Выбрать файлы для распознавания: нажмите Enter или перетащите файлы">
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
    // Область была доступна только мышью: с клавиатуры до выбора файлов было
    // не добраться совсем. Теперь она получает фокус и открывается по Enter
    // и пробелу, как обычная кнопка.
    zone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        input.click();
      }
    });
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
      <button class="ghost sm" aria-label="Убрать файл ${esc(f.name)}"
            title="Убрать из списка" onclick="__asrhub.removeFile(${i})">✕</button>
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
  // Отправляем ровно тот набор, что был на момент нажатия. Список очищался
  // целиком, поэтому файлы, перетащенные во время загрузки — а на сотнях
  // мегабайт это минуты, — пропадали из списка, не попав в очередь, и без
  // единого сообщения.
  const batch = state.files.slice();
  const drop = (accepted) => {
    state.files = state.files.filter((f) => !accepted.includes(f));
  };
  try {
    if (batch.length === 1) {
      const form = new FormData();
      form.append('file', batch[0]);
      form.append('settings', settings);
      form.append('priority', String(priority));
      await API.call('/api/jobs', { method: 'POST', body: form });
      toast('Задание поставлено в очередь', 'ok');
    } else {
      const form = new FormData();
      batch.forEach((f) => form.append('files', f));
      form.append('settings', settings);
      form.append('priority', String(priority));
      const result = await API.call('/api/jobs/batch', { method: 'POST', body: form });
      const rejected = result.errors || [];
      toast(`Поставлено заданий: ${result.created}`, rejected.length ? 'warn' : 'ok',
        rejected.length ? `Не принято: ${rejected.length}` : '');
      if (rejected.length) {
        // Сервер называет каждый отклонённый файл и причину, а интерфейс
        // показывал только их число и тут же очищал список — узнать, какой
        // файл не принят и почему, было негде. Оставляем отказы на виду и
        // не трогаем их в списке: их можно поправить и отправить снова.
        showRejected(rejected);
        const names = new Set(rejected.map((e) => e.filename));
        drop(batch.filter((f) => !names.has(f.name)));
        renderFileList();
        await refreshQueue();
        renderView(true);
        return;
      }
    }
    drop(batch);
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
        // Состояние берём свежее, а не то, что было при отрисовке раздела:
        // подпись обновляется автообновлением каждые четыре секунды, и после
        // паузы, поставленной из другой вкладки, кнопка «Возобновить» слала
        // ещё одну паузу — очередь не запускалась, и ничего об этом не
        // сообщалось.
        const paused = !!(state.queue && state.queue.paused);
        state.queue = await API.post(paused ? '/api/queue/resume' : '/api/queue/pause');
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
      const data = await API.latest('queue-table', `/api/jobs?${params}`);
      // Таблица перерисовывается каждые четыре секунды. Если в ней стоит
      // фокус клавиатуры, замена innerHTML его теряет — до кнопок дальних
      // строк было просто не добраться: Tab возвращался в начало страницы.
      // Запоминаем, на чём стоял фокус, и возвращаем его на то же место.
      const focused = document.activeElement;
      const restore = host.contains(focused)
        ? { row: focused.closest('tr') ? focused.closest('tr').dataset.jobId : '',
            action: focused.dataset ? focused.dataset.action || '' : '',
            scroll: host.scrollTop }
        : null;

      host.innerHTML = data.items.length ? `<table>
        <thead><tr>
          <th style="width:26px"></th><th>Файл</th><th>Модель</th>
          <th class="num">Приор.</th><th class="num">Длит.</th>
          <th>Состояние</th><th style="width:150px">Прогресс</th>
          <th class="num">RTF</th><th>Создано</th><th style="width:210px">Действия</th>
        </tr></thead><tbody>
        ${data.items.map((job) => this.row(job)).join('')}
      </tbody></table>` : '<div class="empty">Нет заданий по выбранному фильтру</div>';

      if (restore && restore.row) {
        const row = host.querySelector(`tr[data-job-id="${restore.row}"]`);
        const again = row && (restore.action
          ? row.querySelector(`[data-action="${restore.action}"]`)
          : row.querySelector('button'));
        if (again) {
          again.focus({ preventScroll: true });
          host.scrollTop = restore.scroll;
        }
      }
    } catch (err) { fail(err); }
  },

  row(job) {
    const active = ['queued', 'running', 'retry', 'paused'].includes(job.status);
    // data-job-id и data-action нужны, чтобы вернуть фокус на ту же кнопку
    // после автообновления таблицы.
    return `<tr class="queue-row ${job.status}" data-job-id="${esc(job.id)}">
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
            data-action="top" onclick="__asrhub.jobAction('${job.id}','top')">▲</button>
          <button class="ghost sm" title="Опустить"
            data-action="bottom" onclick="__asrhub.jobAction('${job.id}','bottom')">▼</button>` : ''}
        ${job.status === 'queued' ? `<button class="ghost sm" title="Приостановить"
            data-action="pause" onclick="__asrhub.jobAction('${job.id}','pause')">⏸</button>` : ''}
        ${job.status === 'paused' ? `<button class="ghost sm" title="Возобновить"
            data-action="resume" onclick="__asrhub.jobAction('${job.id}','resume')">▶</button>` : ''}
        ${active ? `<button class="ghost sm danger" title="Отменить"
            data-action="cancel" onclick="__asrhub.jobAction('${job.id}','cancel')">✕</button>` : ''}
        ${job.status === 'failed' ? `<button class="ghost sm" title="Повторить"
            data-action="retry" onclick="__asrhub.jobAction('${job.id}','retry')">↻</button>` : ''}
        <button class="ghost sm" data-action="open"
          onclick="__asrhub.openJob('${job.id}')">Открыть</button>
      </div></td>
    </tr>`;
  },
};

/**
 * Скачивание результата без ключа в адресе.
 *
 * Ссылка <a href="…?api_key=…"> удобна, но адрес с ключом попадает в историю
 * браузера, в журнал обратного прокси и в заголовок Referer при переходе на
 * сторонний сайт. Забираем файл обычным запросом с заголовком X-API-Key и
 * отдаём его через объектную ссылку — ключ не покидает заголовков.
 */
/**
 * Имя файла из заголовка Content-Disposition.
 * Предпочитает filename*= (RFC 5987) и не падает на неверном кодировании.
 */
function parseFilename(disposition) {
  const extended = /filename\*\s*=\s*([^;]+)/i.exec(disposition);
  if (extended) {
    const raw = extended[1].trim();
    // Формат: кодировка'язык'значение — например utf-8''%D1%84.txt
    const parts = raw.split("'");
    const encoded = parts.length >= 3 ? parts.slice(2).join("'") : raw;
    try { return decodeURIComponent(encoded); } catch (e) { /* ниже запасной путь */ }
  }
  const plain = /filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)/i.exec(disposition);
  if (plain) return (plain[1] !== undefined ? plain[1] : plain[2]).trim();
  return '';
}

window.__asrhub.download = async (id, fmt) => {
  const url = `/api/jobs/${id}/download?fmt=${encodeURIComponent(fmt)}`;
  try {
    const headers = {};
    const key = localStorage.getItem('asrhub_key');
    if (key) headers['X-API-Key'] = key;
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let detail = {};
      try { detail = (await response.json()).detail || {}; } catch (e) { detail = {}; }
      throw { code: detail.code || 'http_error',
              message: detail.message || `Не удалось скачать файл (HTTP ${response.status})`,
              hint: detail.hint };
    }
    // Имя берём из Content-Disposition. Сервер шлёт два поля: запасное
    // filename= в ASCII (кириллица в нём заменена подчёркиваниями) и
    // filename*= по RFC 5987 с настоящим именем. Брать надо второе.
    // Разбор «первое совпадение плюс decodeURIComponent» и портил имена,
    // и падал целиком: у файла «отчёт 100% готово» запасное имя содержит
    // знак процента, и декодирование бросало «URI malformed» — скачивание
    // не начиналось вовсе.
    const disposition = response.headers.get('Content-Disposition') || '';
    const name = parseFilename(disposition) || `${id}.${fmt}`;

    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Освобождаем память не сразу: Safari отменяет загрузку, если ссылку
    // отозвать в том же кадре.
    setTimeout(() => URL.revokeObjectURL(href), 30000);
  } catch (err) {
    fail(err);
  }
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
      const data = await API.latest('results-table', `/api/jobs?${params}`);
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
              `<button class="btn sm" onclick="__asrhub.download('${job.id}','${f}')"
                 title="Скачать в формате ${f}">${f}</button>`).join('')}
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
      <button class="ghost icon" id="modal-close" aria-label="Закрыть" title="Закрыть">✕</button>
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

      ${(job.waveform || []).length ? `<div class="wave-block" id="job-waveform-block">
        <div class="row small dim" style="margin-bottom:6px">
          <b class="small">Громкость записи</b>
          <span class="spacer"></span>
          <span class="faint">средний уровень за ${
            num((job.params || {}).waveform_interval_s || 1, 2)} с${
            segments.length ? ' · щелчок — переход к месту в расшифровке' : ''}</span>
        </div>
        <div id="job-waveform"></div>
        <div id="job-waveform-legend"></div>
      </div>` : ''}

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
        `<button class="btn sm" onclick="__asrhub.download('${job.id}','${f}')"
           title="Скачать в формате ${f}">${f}</button>`).join('') : ''}
      ${job.status === 'failed'
        ? `<button class="primary" data-action="retry" onclick="__asrhub.jobAction('${job.id}','retry')">
             Повторить</button>` : ''}
    </div></div></div>`);

  mountModal(backdrop);
  const close = () => closeModal(backdrop);
  qs('#modal-close', backdrop).onclick = close;

  const body = qs('#job-tab-body', backdrop);
  const tabs = {
    text: () => `<div class="transcript" style="white-space:pre-wrap;line-height:1.7">${
      esc(job.text || '—')}</div>`,
    segments: () => segments.length ? `<div class="transcript">${segments.map((s, i) => `
      <div class="segment" data-index="${i}" data-start="${s.start}" data-end="${s.end}">
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
  drawJobWaveform(backdrop, job, segments, show);
}

/* Полоса громкости в карточке: дорожка на канал или говорящего.
 *
 * Щелчок по полосе открывает вкладку сегментов и подводит к тому, что
 * говорилось в эту секунду. Без этого полоса остаётся картинкой: видно,
 * что в середине разговора кто-то долго молчал, а найти это место в
 * расшифровке всё равно приходится вручную.
 */
function drawJobWaveform(backdrop, job, segments, show) {
  const host = qs('#job-waveform', backdrop);
  if (!host || !window.Charts || !Charts.waveform) return;
  const curves = job.waveform || [];

  const seek = (seconds) => {
    if (!segments.length) return;
    show('segments');
    let index = segments.findIndex((s) => s.start <= seconds && seconds < s.end);
    if (index < 0) {         // щелчок пришёлся на паузу — берём ближайшую реплику
      let best = Infinity;
      segments.forEach((s, i) => {
        const distance = seconds < s.start ? s.start - seconds : seconds - s.end;
        if (distance < best) { best = distance; index = i; }
      });
    }
    if (index < 0) return;
    const node = qs(`.segment[data-index="${index}"]`, backdrop);
    if (!node) return;
    qsa('.segment.active', backdrop).forEach((n) => n.classList.remove('active'));
    node.classList.add('active');
    node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };

  const draw = () => Charts.waveform(host, {
    curves,
    duration: job.media_duration_s || 0,
    interval: (job.params || {}).waveform_interval_s || 1,
    timeFormat: fmtDur,
    onSeek: segments.length ? seek : null,
  });
  draw();

  if (curves.length > 1) {
    const colors = Charts.palette();
    Charts.legend(qs('#job-waveform-legend', backdrop), curves.map((c, i) => ({
      name: c.label || `Дорожка ${i + 1}`, color: colors[i % colors.length],
    })));
  }
  // Ширина известна только после вставки в документ; и размер окна, и тема
  // меняются, пока карточка открыта.
  const redraw = () => draw();
  window.addEventListener('resize', redraw);
  document.body.addEventListener('asrhub:theme', redraw);
  backdrop.addEventListener('asrhub:closed', () => {
    window.removeEventListener('resize', redraw);
    document.body.removeEventListener('asrhub:theme', redraw);
  }, { once: true });
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
      const data = await API.latest('analytics', `/api/analytics?period=${state.period}`);
      state.analytics = data;
      this.draw(host, data);
    } catch (err) {
      fail(err);
      if (err && err.silent) return;    // пришёл ответ посвежее — он и отрисуется
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
      <span class="spacer"></span><button class="ghost icon" id="mm-close" aria-label="Закрыть" title="Закрыть">✕</button></div>
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
  mountModal(backdrop);
  qs('#mm-close', backdrop).onclick = () => closeModal(backdrop);
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
          aria-label="Убрать модель из сравнения"
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
    // Ошибку не гасим: renderView поймает её и покажет карточку с кнопкой
    // «Повторить». Раньше здесь стоял catch с return, и раздел навсегда
    // оставался на строке «Загрузка сведений о сервере…» — единственным
    // признаком сбоя была всплывашка, исчезавшая через девять секунд.
    sys = await API.get('/api/system');
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
            onclick="__asrhub.revokeKey('${esc(k.key_id || '')}')">отозвать</button>
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


// ==========================================================================
// Мониторинг
// ==========================================================================

const ALERT_STATE_LABEL = { ok: 'норма', pending: 'наблюдение', firing: 'тревога' };
const ALERT_STATE_CLASS = { ok: 'ok', pending: 'warn', firing: 'err' };
const TARGET_KIND_LABEL = {
  prometheus_pushgateway: 'Prometheus Pushgateway',
  influxdb: 'InfluxDB',
  otlp: 'OpenTelemetry (OTLP)',
  statsd: 'StatsD / Graphite',
  webhook: 'Webhook (JSON)',
};

RENDERERS.monitoring = {
  async render(root) {
    root.innerHTML = '<div class="empty">Опрос метрик…</div>';
    let health;
    let info;
    let alerts;
    let targets;
    // Ошибку пробрасываем: её покажет renderView карточкой с кнопкой
    // «Повторить», а не оставит раздел на «Опрос метрик…» насовсем.
    [health, info, alerts, targets] = await Promise.all([
      API.get('/api/monitoring/health'),
      API.get('/api/monitoring/info'),
      API.get('/api/monitoring/alerts'),
      API.get('/api/monitoring/targets'),
    ]);

    state.monitoring = { health, info, alerts, targets };
    const summary = alerts.summary || {};
    const worstClass = summary.worst === 'critical' ? 'err'
      : summary.worst === 'warning' ? 'warn' : 'ok';

    root.innerHTML = `
      <div class="grid cols-4" style="margin-bottom:16px">
        ${kpi('Состояние', `<span class="chip ${worstClass}">${
          esc(healthLabel(health.status))}</span>`, `работает ${fmtDur(health.uptime_s)}`)}
        ${kpi('Тревог сейчас', summary.firing || 0,
              `${summary.critical || 0} критических, ${summary.warning || 0} предупреждений`)}
        ${kpi('Метрик в снимке', info.samples || 0, `правил: ${summary.rules || 0}`)}
        ${kpi('Опросов', info.scrapes || 0, `кеш ${info.cache_ttl_s} с`)}
      </div>

      ${(info.collection_errors || []).length ? `<section class="card" style="border-color:var(--warn)">
        <div class="card-head"><h3 style="color:var(--warn)">Источники, которые не опрашиваются</h3>
          <span class="hint">остальные метрики собираются как обычно</span></div>
        <ul style="margin:0;padding-left:18px" class="small">
          ${info.collection_errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul></section>` : ''}

      ${card('Пробы состояния',
             'liveness — перезапустить контейнер; readiness — снять нагрузку',
             `<div class="grid cols-3">${
               ['liveness', 'readiness', 'startup'].map((probe) => probeCard(probe, health[probe])).join('')
             }</div>`)}

      ${card('Тревоги', 'пороги берутся из каталога метрик и правятся в настройках',
             alertsTable(alerts.alerts || []),
             `<button class="ghost" id="mon-reset-rules">Вернуть пороги по умолчанию</button>`)}

      ${card('Куда отправляются метрики',
             'нужно там, где до сервера не достучаться снаружи',
             targetsTable(targets),
             `<button class="ghost" id="mon-add-target">Добавить приёмник</button>`)}

      ${card('Как забрать метрики', 'адреса относительно этого сервера',
             endpointsTable())}

      ${card('Готовые настройки для систем мониторинга',
             'собираются из каталога метрик, поэтому не расходятся с ним',
             `<div class="row" style="gap:8px;flex-wrap:wrap">
                <a class="btn ghost" href="/api/monitoring/config/prometheus" download>Правила Prometheus</a>
                <a class="btn ghost" href="/api/monitoring/config/prometheus-scrape" download>Блок scrape_configs</a>
                <a class="btn ghost" href="/api/monitoring/config/grafana" download>Панель Grafana</a>
                <a class="btn ghost" href="/api/monitoring/config/zabbix" download>Шаблон Zabbix</a>
              </div>
              <p class="small dim" style="margin-top:10px">Пороги в этих файлах — отправная
              точка. Подгоняйте под свой поток: очередь из ста заданий бывает и нормой,
              и аварией.</p>`)}

      ${card('Справочник метрик', `${info.samples || 0} значений в снимке`,
             `<div class="settings-toolbar" style="position:static;padding:0 0 10px">
                <input type="search" id="mon-search" placeholder="поиск по метрикам"
                       style="width:280px">
                <span class="spacer"></span>
                <span class="small faint" id="mon-count"></span>
              </div>
              <div id="mon-catalog"><div class="empty">Загрузка справочника…</div></div>`)}
    `;

    qs('#mon-reset-rules').onclick = async () => {
      try {
        await API.post('/api/monitoring/alerts/rules/reset');
        toast('Пороги возвращены к значениям каталога');
        renderView();
      } catch (err) { fail(err); }
    };
    qs('#mon-add-target').onclick = () => targetDialog(targets);
    this.loadCatalog();
  },

  async loadCatalog() {
    let data;
    try {
      data = await API.latest('mon-catalog', '/api/monitoring/catalog');
    } catch (err) { return; }
    state.metricCatalog = data;
    const box = qs('#mon-catalog');
    const search = qs('#mon-search');
    // Ответ мог прийти после ухода из раздела: тогда этих элементов уже нет,
    // и попытка навесить обработчик роняла скрипт целиком («Cannot set
    // properties of null»), а вместе с ним и обновление всех разделов.
    if (!box || !search) return;
    const draw = () => {
      const needle = (search.value || '').toLowerCase().trim();
      const items = data.metrics.filter((m) => !needle
        || m.name.toLowerCase().includes(needle)
        || m.label.toLowerCase().includes(needle)
        || m.description.toLowerCase().includes(needle));
      const counter = qs('#mon-count');
      if (counter) counter.textContent = `${items.length} из ${data.metrics.length}`;
      box.innerHTML = data.groups.map((group) => {
        const inGroup = items.filter((m) => m.group === group.id);
        if (!inGroup.length) return '';
        return `<h4 style="margin:16px 0 8px">${esc(group.title)}
            <span class="small dim" style="font-weight:400">${esc(group.description)}</span></h4>
          ${inGroup.map(metricCard).join('')}`;
      }).join('') || '<div class="empty">Ничего не найдено</div>';
    };
    search.oninput = draw;
    draw();
  },
};

function healthLabel(status) {
  return { ok: 'норма', warning: 'внимание', degraded: 'деградация',
           critical: 'авария' }[status] || status;
}

function probeCard(name, probe) {
  if (!probe) return '';
  const title = { liveness: 'Живость', readiness: 'Готовность',
                  startup: 'Запуск' }[name] || name;
  const cls = probe.status === 'ok' ? 'ok' : probe.status === 'warn' ? 'warn' : 'err';
  return `<div class="card" style="margin:0">
    <div class="card-head"><h3>${title}</h3><span class="spacer"></span>
      <span class="chip ${cls}">${esc(probe.status)}</span></div>
    <table class="small">${(probe.checks || []).map((c) => `<tr>
      <td class="dim">${esc(c.name)}</td>
      <td><span class="chip ${c.status === 'ok' ? 'ok' : c.status === 'warn' ? 'warn' : 'err'}"
          >${esc(c.status)}</span></td>
      <td>${esc(c.detail)}${c.hint ? `<div class="small dim">${esc(c.hint)}</div>` : ''}</td>
    </tr>`).join('')}</table></div>`;
}

/**
 * Значение метрики в человеческих единицах.
 *
 * Каталог хранит метрики в базовых единицах Prometheus — байтах и секундах.
 * Это правильно для сбора, но в таблице тревог получалось «31.6 млрд Б»
 * вместо «29.5 ГБ» и «5400 с» вместо «1:30:00»: разобрать, много это или
 * мало, было нельзя.
 */
function metricValue(value, unit) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const v = Number(value);
  if (unit === 'Б' || unit === 'B') return fmtBytes(v);
  if (unit === 'с' || unit === 's') {
    if (Math.abs(v) < 1) return `${(v * 1000).toFixed(0)} мс`;
    if (Math.abs(v) < 60) return `${num(v, v < 10 ? 2 : 1)} с`;
    return fmtDur(v);
  }
  return `${num(v, 3)}${unit ? ' ' + unit : ''}`;
}

/** Байты в КБ/МБ/ГБ/ТБ по основанию 1024. */
function fmtBytes(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  const sign = v < 0 ? '-' : '';
  let rest = Math.abs(v);
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ', 'ПБ'];
  let i = 0;
  while (rest >= 1024 && i < units.length - 1) { rest /= 1024; i += 1; }
  const digits = i === 0 ? 0 : (rest < 10 ? 2 : rest < 100 ? 1 : 0);
  return `${sign}${rest.toFixed(digits)} ${units[i]}`;
}

function alertsTable(alerts) {
  const active = alerts.filter((a) => a.state !== 'ok');
  const rows = (active.length ? active : alerts.slice(0, 12)).map((a) => `<tr>
    <td><span class="chip ${ALERT_STATE_CLASS[a.state] || ''}">${
      esc(ALERT_STATE_LABEL[a.state] || a.state)}</span></td>
    <td><b>${esc(a.label)}</b><div class="small dim">${esc(a.metric)}</div></td>
    <td class="nowrap">${esc(metricValue(a.value, a.unit))}</td>
    <td class="dim nowrap">${a.direction === 'above' ? '>' : '<'} ${
      esc(metricValue(a.threshold, a.unit))}</td>
    <td class="small">${esc(a.severity)}</td>
    <td class="small dim">${esc(a.hint || '')}</td></tr>`).join('');
  return `<div class="table-wrap"><table>
    <thead><tr><th>Состояние</th><th>Метрика</th><th>Значение</th><th>Порог</th>
      <th>Важность</th><th>Что делать</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="empty">Тревог нет</td></tr>'}</tbody>
  </table></div>${active.length ? '' :
    '<p class="small dim" style="margin-top:8px">Показаны первые правила; все они в норме.</p>'}`;
}

function targetsTable(data) {
  const rows = (data.targets || []).map((t) => `<tr>
    <td><b>${esc(t.name)}</b></td>
    <td>${esc(TARGET_KIND_LABEL[t.kind] || t.kind)}</td>
    <td class="small">${esc(t.url)}</td>
    <td>${t.interval_s} с</td>
    <td><span class="chip ${t.healthy ? 'ok' : 'err'}">${t.healthy ? 'доставляется' : 'нет'}</span>
      ${t.last_error ? `<div class="small dim">${esc(t.last_error)}</div>` : ''}</td>
    <td class="small dim">отправлено ${t.sent}, ошибок ${t.failed}</td></tr>`).join('');
  return `<div class="table-wrap"><table>
    <thead><tr><th>Имя</th><th>Тип</th><th>Адрес</th><th>Интервал</th>
      <th>Доставка</th><th>Счётчики</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="6" class="empty">
      Приёмники не настроены — метрики забирает система сбора сама
      </td></tr>`}</tbody></table></div>`;
}

function endpointsTable() {
  const rows = [
    ['/api/monitoring/metrics', 'Все метрики. Формат задаётся ?format=prometheus|openmetrics|json|otlp|influx|graphite|zabbix|csv'],
    ['/api/monitoring/metrics.json', 'Снимок с описанием и порогами каждой метрики'],
    ['/api/monitoring/health', 'Сводное состояние: живость, готовность, тревоги'],
    ['/api/monitoring/live', 'Проба живости — провал означает «перезапусти контейнер»'],
    ['/api/monitoring/ready', 'Проба готовности — провал означает «не шли запросы»'],
    ['/api/monitoring/catalog', 'Справочник метрик с рекомендациями'],
    ['/api/monitoring/alerts', 'Состояние тревог'],
  ];
  return `<div class="table-wrap"><table>
    <thead><tr><th>Адрес</th><th>Что отдаёт</th></tr></thead>
    <tbody>${rows.map(([path, what]) => `<tr>
      <td><a href="${path}" target="_blank"><code>${path}</code></a></td>
      <td class="small">${esc(what)}</td></tr>`).join('')}</tbody></table></div>`;
}

function metricCard(m) {
  const threshold = m.threshold;
  return `<details class="help" style="margin-bottom:6px">
    <summary><b>${esc(m.label)}</b> <code class="small">${esc(m.name)}</code>
      <span class="small dim">${esc(m.type)}${m.unit ? ', ' + esc(m.unit) : ''}</span></summary>
    <div class="small" style="padding:8px 0 4px">
      <p>${esc(m.description)}</p>
      ${m.normal ? `<p class="dim">Обычное значение: ${esc(m.normal)}</p>` : ''}
      ${m.recommendation ? `<div class="param-rec"><b>Рекомендация.</b> ${esc(m.recommendation)}</div>` : ''}
      ${threshold ? `<p class="dim">Порог: ${threshold.direction === 'above' ? 'выше' : 'ниже'}
        ${esc(metricValue(threshold.warning, m.unit))} — предупреждение,
        ${esc(metricValue(threshold.critical, m.unit))} — критично,
        выдержка ${threshold.for_seconds} с.
        ${threshold.note ? esc(threshold.note) : ''}</p>` : ''}
      ${m.troubleshooting ? `<p><b>Что делать:</b> ${esc(m.troubleshooting)}</p>` : ''}
      ${(m.labels || []).length ? `<p class="dim">Метки: ${m.labels.map(
        (l) => `<code>${esc(l)}</code>`).join(', ')}</p>` : ''}
    </div></details>`;
}

function targetDialog(existing) {
  const backdrop = h(`<div class="modal-backdrop"><div class="modal" style="max-width:560px">
    <div class="modal-head"><b>Новый приёмник метрик</b><span class="spacer"></span>
      <button class="ghost icon" id="tg-close" aria-label="Закрыть" title="Закрыть">✕</button></div>
    <div class="modal-body">
      <label class="mon-field"><span>Тип</span>
        <select id="tg-kind">${Object.entries(TARGET_KIND_LABEL).map(
          ([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}</select></label>
      <label class="mon-field"><span>Адрес</span>
        <input type="text" id="tg-url" placeholder="http://pushgw:9091"></label>
      <label class="mon-field"><span>Интервал, секунд</span>
        <input type="number" id="tg-interval" value="60" min="10"></label>
      <p class="small dim">Проверка отправляет текущий снимок немедленно и показывает
        результат — настройку видно до того, как она сохранена.</p>
      <div id="tg-result" class="small"></div>
    </div>
    <div class="modal-foot">
      <button class="ghost" id="tg-test">Проверить</button>
      <span class="spacer"></span>
      <button class="primary" id="tg-save">Добавить</button>
    </div></div></div>`);
  mountModal(backdrop);
  const close = () => closeModal(backdrop);
  qs('#tg-close', backdrop).onclick = close;

  const collect = () => ({
    kind: qs('#tg-kind', backdrop).value,
    url: qs('#tg-url', backdrop).value.trim(),
    interval_s: Number(qs('#tg-interval', backdrop).value) || 60,
  });

  qs('#tg-test', backdrop).onclick = async () => {
    const box = qs('#tg-result', backdrop);
    box.innerHTML = 'Отправка…';
    try {
      const result = await API.post('/api/monitoring/targets/test', collect());
      box.innerHTML = result.ok
        ? `<span class="chip ok">доставлено</span> метрик: ${result.sent_metrics}`
        : `<span class="chip err">не доставлено</span> ${esc(result.error || '')}`;
    } catch (err) {
      box.innerHTML = `<span class="chip err">ошибка</span> ${esc(err.message || '')}`;
    }
  };

  qs('#tg-save', backdrop).onclick = async () => {
    const list = (existing.targets || []).map((t) => ({
      kind: t.kind, url: t.url, interval_s: t.interval_s, name: t.name,
    }));
    list.push(collect());
    try {
      await API.put('/api/monitoring/targets', list);
      toast('Приёмник добавлен');
      close();
      renderView();
    } catch (err) { fail(err); }
  };
}

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
    // Таймер регистрируется за разделом: при уходе он гасится сам, иначе при
    // каждом возврате добавлялся ещё один опрос.
    viewTimer(() => {
      if (qs('#log-auto') && qs('#log-auto').checked) this.load();
    }, 5000);
  },

  async load() {
    try {
      const level = qs('#log-level') ? qs('#log-level').value : '';
      const search = qs('#log-search') ? qs('#log-search').value : '';
      // allSettled, а не all: журнал сервера открыт только администратору,
      // и его 403 отменял загрузку целиком — обычный пользователь видел
      // пустую панель событий вместо своих, и так каждые пять секунд.
      const [logsResult, eventsResult] = await Promise.allSettled([
        API.latest('logs', `/api/logs?limit=250&level=${level}&search=${encodeURIComponent(search)}`),
        API.latest('log-events', '/api/events?limit=150'),
      ]);
      const logsDenied = logsResult.status === 'rejected';
      const logs = logsDenied ? { items: [], counts: {} } : logsResult.value;
      const events = eventsResult.status === 'fulfilled' ? eventsResult.value : { items: [] };
      const counts = qs('#log-counts');
      if (counts) {
        counts.innerHTML = Object.entries(logs.counts || {})
          .map(([k, v]) => `<span class="chip ${k === 'ERROR' ? 'err'
            : k === 'WARNING' ? 'warn' : ''}">${k}: ${v}</span>`).join(' ');
      }
      const table = qs('#log-table');
      if (table && logsDenied) {
        table.innerHTML = '<div class="empty">Журнал сервера доступен только ключу '
          + 'с ролью администратора.</div>';
      } else if (table) {
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
    } catch (err) {
      // Молча пустой журнал выглядит как «ошибок нет», хотя на деле мы просто
      // не смогли их получить. Показываем причину.
      const table = qs('#log-table');
      if (table) {
        table.innerHTML = `<div class="empty small">Журнал недоступен: ${
          esc(String(err.message || err))}</div>`;
      }
    }
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
