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
  //: Кто вошёл: {name, role, kind: 'user'|'key', must_change_password}.
  me: null,
  analytics: null,
  //: Раздел «Аналитика записей»: свой период и своя вкладка. Свои, а не
  //: общие с аналитикой сервера: там смотрят сутки («что сейчас с очередью»),
  //: здесь — месяц («как шли разговоры»), и один переключатель на двоих
  //: сбрасывал бы выбор при каждом переходе между разделами.
  contentPeriod: 'month',
  contentTab: 'summary',
  contentData: {},
  contentKind: 'negative',
  contentScript: null,
  contentScriptOwn: false,
  contentScriptJobs: [],
  contentScriptJob: '',
  contentScriptTimer: null,
  contentScriptRedraw: null,
  contentCoverageTimer: null,
  resultsSearch: '',
  resultsContent: '',
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
  patch(path, body) { return this.call(path, { method: 'PATCH', json: body }); },
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
/**
 * Русское склонение по числу: 1 находка, 2 находки, 5 находок.
 *
 * «Найдено: 5» вместо «5 находок» — обычный способ обойти склонение, но
 * читается он как отчёт машины, а не как ответ человеку.
 */
function plural(n, одна, две, много) {
  const число = Math.abs(Math.trunc(n)) % 100;
  const хвост = число % 10;
  if (число > 10 && число < 20) return много;
  if (хвост === 1) return одна;
  if (хвост >= 2 && хвост <= 4) return две;
  return много;
}

function pct(value, digits) {
  if (value === null || value === undefined) return '—';
  return (value * 100).toFixed(digits === undefined ? 1 : digits) + ' %';
}

/**
 * Подпись точки на оси времени по ширине корзины.
 *
 * Одна и та же на всех графиках с временной осью. Ход качества подписывал
 * точки как «день.месяц» всегда — на часовом окне это была одна и та же
 * дата двадцать четыре раза подряд, то есть ось без единой подсказки о
 * том, где на ней находишься.
 */
function подписьВремени(секунды, ширинаКорзины) {
  const d = new Date(секунды * 1000);
  if (!(ширинаКорзины > 0) || ширинаКорзины < 7200) {
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  if (ширинаКорзины < 86400 * 20) {
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
  }
  // Корзина шире трёх недель бывает только на годовом окне: там день
  // не значит ничего, а месяц с годом отвечают на вопрос «когда».
  return d.toLocaleDateString('ru-RU', { month: 'short', year: '2-digit' });
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
    // Сначала выясняем, кто мы. Раньше загрузка начиналась с каталога, а он
    // открыт всем: без ключа интерфейс отрисовывался целиком и выглядел
    // рабочим, хотя очередь, результаты и настройки отвечали отказом на
    // каждый запрос. Форма входа появлялась только при недоступном сервере.
    state.me = await API.background('/api/auth/me');
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
    applyWhoAmI();
    await Promise.all([refreshEngines(), refreshQueue()]);
    connectWs();
    render();
    state.timer = setInterval(tick, 4000);
  } catch (err) {
    if (err.status === 401) { promptKey(); return; }
    // Пароль по умолчанию: интерфейс всё равно не заработает, пока его не
    // сменят, поэтому ведём прямо к форме, а не показываем ошибку в каждом
    // разделе по очереди.
    if (err.code === 'password_change_required') { promptPasswordChange(); return; }
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
  // Форма входа. Логин и пароль — обычный путь для человека; ключ доступа
  // остаётся для программ и для тех, кто уже настроил его себе, поэтому
  // спрятан под ссылкой, а не выброшен.
  const content = qs('#content');
  content.innerHTML = '';
  const card = h(`<div class="card" style="max-width:460px;margin:60px auto">
    <div class="card-head"><h2>Вход</h2></div>
    <div class="stack" style="gap:10px">
      <label class="small dim" for="login-user">Логин</label>
      <input type="text" id="login-user" autocomplete="username" autofocus>
      <label class="small dim" for="login-pass">Пароль</label>
      <input type="password" id="login-pass" autocomplete="current-password">
      <div id="login-error" class="small" style="color:var(--err);display:none"></div>
      <button class="primary" id="login-go">Войти</button>
      <div class="small dim" style="margin-top:6px">
        <a href="#" id="login-by-key">Войти по ключу доступа</a>
      </div>
    </div></div>`);
  content.appendChild(card);

  const showError = (text, hint) => {
    const box = qs('#login-error');
    box.textContent = hint ? `${text} ${hint}` : text;
    box.style.display = '';
  };

  qs('#login-go').onclick = async () => {
    const username = qs('#login-user').value.trim();
    const password = qs('#login-pass').value;
    if (!username || !password) { showError('Введите логин и пароль.'); return; }
    const button = qs('#login-go');
    button.disabled = true;
    try {
      const result = await API.post('/api/auth/login', { username, password });
      // Ключ из прошлой жизни убираем: иначе он поедет в заголовке и
      // перекроет только что заведённую сессию — вход как будто не сработал.
      localStorage.removeItem('asrhub_key');
      if (result && result.must_change_password) { promptPasswordChange(); return; }
      location.reload();
    } catch (err) {
      showError(err.message || 'Не удалось войти.', err.hint || '');
      button.disabled = false;
    }
  };
  qs('#login-pass').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') qs('#login-go').click();
  });
  qs('#login-user').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') qs('#login-pass').focus();
  });
  qs('#login-by-key').onclick = (e) => { e.preventDefault(); promptApiKey(); };
}

function promptApiKey() {
  const content = qs('#content');
  content.innerHTML = '';
  const card = h(`<div class="card" style="max-width:520px;margin:60px auto">
    <div class="card-head"><h2>Вход по ключу доступа</h2></div>
    <p class="dim small">Ключ, созданный при первом запуске, находится в файле
    <span class="mono">api-key.txt</span> в каталоге данных сервера. Обычно он
    нужен программам — человеку проще войти логином и паролем.</p>
    <div class="stack" style="gap:10px">
      <input type="password" id="key-input" placeholder="ah_…" autocomplete="off">
      <button class="primary" id="key-save">Сохранить и войти</button>
      <div class="small dim" style="margin-top:6px">
        <a href="#" id="key-by-login">Войти по логину и паролю</a>
      </div>
    </div></div>`);
  content.appendChild(card);
  qs('#key-save').onclick = () => {
    localStorage.setItem('asrhub_key', qs('#key-input').value.trim());
    location.reload();
  };
  qs('#key-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') qs('#key-save').click();
  });
  qs('#key-by-login').onclick = (e) => { e.preventDefault(); promptKey(); };
}

function promptPasswordChange(optional) {
  // Пока пароль по умолчанию не сменён, сервер отвечает отказом на всё,
  // кроме самой смены. Показываем форму вместо интерфейса, а не поверх
  // него: иначе за ней виден пустой каркас с ошибками в каждом разделе.
  const content = qs('#content');
  content.innerHTML = '';
  const card = h(`<div class="card" style="max-width:460px;margin:60px auto">
    <div class="card-head"><h2>${optional ? 'Смена пароля' : 'Смените пароль'}</h2></div>
    ${optional ? '' : `<p class="dim small">Сейчас действует пароль, заданный при
      первом запуске. Он известен всем, у кого есть эта программа, поэтому
      работать с сервером до смены нельзя.</p>`}
    <div class="stack" style="gap:10px">
      <label class="small dim" for="pw-current">Текущий пароль</label>
      <input type="password" id="pw-current" autocomplete="current-password">
      <label class="small dim" for="pw-new">Новый пароль</label>
      <input type="password" id="pw-new" autocomplete="new-password">
      <label class="small dim" for="pw-repeat">Ещё раз</label>
      <input type="password" id="pw-repeat" autocomplete="new-password">
      <div id="pw-error" class="small" style="color:var(--err);display:none"></div>
      <button class="primary" id="pw-go">Сохранить</button>
      ${optional ? '<div class="small dim"><a href="#" id="pw-cancel">Отмена</a></div>' : ''}
    </div></div>`);
  content.appendChild(card);

  const showError = (text, hint) => {
    const box = qs('#pw-error');
    box.textContent = hint ? `${text} ${hint}` : text;
    box.style.display = '';
  };

  qs('#pw-go').onclick = async () => {
    const current = qs('#pw-current').value;
    const next = qs('#pw-new').value;
    if (next !== qs('#pw-repeat').value) { showError('Пароли не совпадают.'); return; }
    const button = qs('#pw-go');
    button.disabled = true;
    try {
      await API.post('/api/auth/password',
                     { current_password: current, new_password: next });
      toast('Пароль изменён');
      location.reload();
    } catch (err) {
      showError(err.message || 'Не удалось сменить пароль.', err.hint || '');
      button.disabled = false;
    }
  };
  qs('#pw-repeat').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') qs('#pw-go').click();
  });
  const cancel = qs('#pw-cancel');
  if (cancel) cancel.onclick = (e) => { e.preventDefault(); renderView(); };
}

function applyWhoAmI() {
  // Кто вошёл — показываем в шапке. Кнопки «Пароль» и «Выйти» появляются
  // только при входе по учётной записи: у ключа доступа пароля нет, а
  // «выйти» для него означало бы удалить ключ из браузера — это делается
  // осознанно, а не кнопкой рядом с очередью.
  if (!state.me) return;
  const chip = qs('#chip-user');
  if (chip) {
    chip.textContent = `${state.me.name}${state.me.role === 'admin' ? ' · админ' : ''}`;
    chip.hidden = false;
  }
  const isUser = state.me.kind === 'user';
  ['#btn-password', '#btn-logout'].forEach((sel) => {
    const button = qs(sel);
    if (button) button.hidden = !isUser;
  });
  if (state.me.default_password_in_use) {
    toast('Действует пароль по умолчанию', 'warn',
          'Смените его в разделе «Сервер» — сервер доступен всем, кто знает эту пару.');
  }
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
  dictation:  { title: 'Диктовка', subtitle: 'Распознавание с микрофона на лету, без ожидания конца записи' },
  queue:      { title: 'Очередь', subtitle: 'Управление заданиями, приоритетами и воркерами' },
  results:    { title: 'Результаты', subtitle: 'Выполненные задания и выгрузка' },
  analytics:  { title: 'Аналитика', subtitle: 'Показатели производительности и качества' },
  content:    { title: 'Аналитика записей', subtitle: 'О чём и как говорили: тональность, речь, темы, обязательства, скрипт' },
  models:     { title: 'Модели', subtitle: 'Каталог моделей, лицензии, требования, загрузка весов' },
  compare:    { title: 'Сравнение моделей', subtitle: 'Качество, скорость и лицензии рядом' },
  settings:   { title: 'Настройки', subtitle: 'Все параметры с описаниями, рекомендациями и примерами' },
  system:     { title: 'Сервер', subtitle: 'Оборудование, движки, хранилище, учётные записи и ключи' },
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
  const view = state.view;
  const renderer = RENDERERS[state.view];
  if (!renderer) { content.innerHTML = ''; return; }
  if (soft && renderer.soft) { renderer.soft(); return; }

  // Отменяем таймеры и подписки предыдущего раздела: без этого при каждом
  // возврате в «Журнал» добавлялся ещё один опрос, и вкладка сама себя
  // упирала в ограничение частоты запросов.
  // Раздел, который уходит, может держать что-то живое — микрофон и открытый
  // сокет диктовки. Без этого крючка запись продолжалась бы в фоне, а
  // индикатор записи в браузере горел бы после ухода со страницы.
  if (state.renderedView && state.renderedView !== view) {
    const leaving = RENDERERS[state.renderedView];
    if (leaving && typeof leaving.leave === 'function') {
      try { leaving.leave(); } catch (e) { /* уход не должен мешать приходу */ }
    }
  }
  state.renderedView = view;

  stopViewTimers();
  API.abortAll();
  // Подсказка графика прячется по mouseleave. Если узел, на котором она
  // висит, снесён перерисовкой, событие не придёт никогда — и подсказка
  // остаётся висеть поверх любых других разделов. Гасим её явно.
  const tip = qs('#chart-tip');
  if (tip) tip.style.display = 'none';
  content.innerHTML = '';
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
  qs('#btn-password').addEventListener('click', () => promptPasswordChange(true));
  qs('#btn-logout').addEventListener('click', async () => {
    try { await API.post('/api/auth/logout'); } catch (e) { /* всё равно уходим */ }
    // Ключ из браузера тоже убираем: иначе после выхода интерфейс молча
    // продолжит работать от него, и человек будет уверен, что вышел.
    localStorage.removeItem('asrhub_key');
    location.reload();
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

const HOTKEY_VIEWS = ['transcribe', 'dictation', 'queue', 'results', 'analytics', 'models',
                      'compare', 'settings', 'system', 'monitoring', 'logs'];

const HOTKEY_HELP = [
  ['1 … 0', 'переход к разделу по номеру (в порядке меню; на «Журнал» цифры не хватило)'],
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
  const numbers = HOTKEY_VIEWS.slice(0, 10)
    .map((view, index) => `${index === 9 ? '0' : index + 1} — ${(VIEWS[view] || {}).title || view}`)
    .join(', ');
  const backdrop = h(`<div class="modal-backdrop"><div class="modal" style="max-width:520px">
    <div class="modal-head"><b>Горячие клавиши</b><span class="spacer"></span>
      <button class="ghost icon" id="hk-close" aria-label="Закрыть" title="Закрыть">✕</button></div>
    <div class="modal-body"><table class="table"><tbody>${rows}</tbody></table>
      <p class="hint" style="margin-top:12px">Номера разделов: ${numbers}.</p>
      <p class="hint" style="margin-top:6px">Буквенные сокращения не срабатывают,
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
// Вид: Диктовка
// ==========================================================================

/**
 * Распознавание с микрофона на лету.
 *
 * Файл принимался целиком: чтобы увидеть первое слово, надо было дождаться
 * конца записи. Здесь звук уходит на сервер кусками по мере записи, а текст
 * возвращается по ходу — через тот же маршрут /api/stream, что и у
 * программных клиентов.
 *
 * Микрофон браузер отдаёт только в защищённом контексте: https либо
 * localhost. Это не наше ограничение и не ошибка сервера, поэтому раздел
 * говорит об этом прямо, а не показывает молчаливо неработающую кнопку.
 */
RENDERERS.dictation = {
  render(root) {
    const models = (state.models || []);
    const streaming = models.filter((m) => m.streaming);
    const current = state.jobSettings.model || (state.settings || {}).model || '';
    const secure = window.isSecureContext
      || ['localhost', '127.0.0.1', '::1'].includes(location.hostname);
    const hasMic = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
      && typeof window.MediaRecorder !== 'undefined';

    root.innerHTML = `
      <div class="split">
        <div>
          <section class="card">
            <div class="card-head"><h3>Микрофон</h3>
              <span class="hint" id="dict-mode">режим определится при запуске</span>
            </div>
            ${(secure && hasMic) ? '' : `
              <div class="card" style="border-color:var(--warn);margin-bottom:12px">
                <b>Микрофон недоступен в этой вкладке.</b>
                <div class="small" style="margin-top:6px">
                  ${secure
                    ? 'Браузер не поддерживает запись через MediaRecorder. Диктовка работает в Chrome, Firefox, Edge и Safari 14.1+.'
                    : 'Браузер отдаёт микрофон только по https или на localhost. Откройте интерфейс через https либо по адресу http://localhost:8080 на самом сервере.'}
                  Разбор без микрофона возможен всегда: раздел «Транскрибация» принимает готовые файлы, а программный клиент —
                  <span class="mono">examples/stream_microphone.py</span>.
                </div>
              </div>`}
            <div class="row" style="gap:12px;align-items:center">
              <button class="primary" id="dict-toggle" ${(secure && hasMic) ? '' : 'disabled'}>
                Начать диктовку</button>
              <span class="mono" id="dict-timer">00:00</span>
              <span class="spacer"></span>
              <span class="small dim" id="dict-status">не записывается</span>
            </div>
            <div id="dict-level" class="dict-level" aria-hidden="true"><span></span></div>
          </section>

          <section class="card">
            <div class="card-head"><h3>Расшифровка</h3>
              <span class="hint">серым — гипотеза, она ещё уточняется</span>
              <span class="spacer"></span>
              <button class="ghost sm" id="dict-copy">Копировать</button>
              <button class="ghost sm" id="dict-save">Скачать .txt</button>
              <button class="ghost sm" id="dict-clear">Очистить</button>
            </div>
            <div id="dict-text" class="dict-text" role="log" aria-live="polite"
                 aria-label="Расшифровка диктовки"><span class="faint">Здесь появится текст.</span></div>
          </section>
        </div>

        <div>
          <section class="card">
            <div class="card-head"><h3>Настройка</h3></div>
            <div class="stack" style="gap:12px">
              <div>
                <label for="dict-model">Модель</label>
                <select id="dict-model" style="margin-top:4px">
                  <option value="">серверная по умолчанию</option>
                  ${models.map((m) => `<option value="${esc(m.id)}"${m.id === current ? ' selected' : ''}>${esc(m.name)}${m.streaming ? ' · поток' : ''}</option>`).join('')}
                </select>
                <div class="small faint" style="margin-top:6px">
                  Движки с настоящим потоком держат состояние между кусками и уточняют
                  гипотезу после каждого — таких моделей в каталоге ${streaming.length}.
                  Остальные работают скользящим окном: накопленный звук распознаётся заново.
                </div>
              </div>
              <div>
                <label for="dict-window">Шаг гипотез, секунд</label>
                <input type="number" id="dict-window" class="mono" min="1" max="30" step="0.5"
                       value="3" style="margin-top:4px;width:100%">
                <div class="small faint" style="margin-top:6px">
                  Действует только для моделей без потока. Меньше — чаще обновления
                  и больше повторной работы: каждое окно распознаёт всё сказанное с начала.
                </div>
              </div>
              <div class="small dim">
                Сессия длиннее часа прерывается. Для длинных записей ставьте обычное
                задание — оно надёжнее и даёт разметку по говорящим.
              </div>
            </div>
          </section>

          <section class="card">
            <div class="card-head"><h3>Как это работает</h3></div>
            <div class="small" style="line-height:1.7">
              Звук уходит кусками по четверти секунды в WebSocket <span class="mono">/api/stream</span>.
              Сервер отвечает двумя видами сообщений: <b>гипотеза</b> — черновик, который
              заменяется следующим целиком, и <b>окончательный кусок</b> — то, что уже не изменится.
              Здесь гипотеза показана серым и дописывается в конец,
              окончательный текст — обычным цветом.
              <div style="margin-top:10px">
                Тот же обмен из своей программы:
                <span class="mono">examples/stream_microphone.py</span>.
              </div>
            </div>
          </section>
        </div>
      </div>`;

    this.session = null;
    qs('#dict-toggle').onclick = () => (this.session ? this.stop() : this.start());
    qs('#dict-copy').onclick = () => {
      const text = this.finalText();
      if (!text) { toast('Пока нечего копировать', 'warn'); return; }
      navigator.clipboard.writeText(text).then(() => toast('Текст скопирован'),
                                               () => toast('Буфер обмена недоступен', 'err'));
    };
    qs('#dict-save').onclick = () => {
      const text = this.finalText();
      if (!text) { toast('Пока нечего сохранять', 'warn'); return; }
      const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
      const link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
      link.download = `диктовка-${stamp}.txt`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 5000);
    };
    qs('#dict-clear').onclick = () => {
      this.final = [];
      this.partial = '';
      this.paint();
    };
    this.final = this.final || [];
    this.partial = '';
  },

  /** Уход из раздела обязан отпустить микрофон: индикатор записи не должен гореть. */
  leave() { this.stop(true); },

  finalText() {
    return (this.final || []).join(' ').replace(/\s+/g, ' ').trim();
  },

  paint() {
    const host = qs('#dict-text');
    if (!host) return;
    const done = this.finalText();
    if (!done && !this.partial) {
      host.innerHTML = '<span class="faint">Здесь появится текст.</span>';
      return;
    }
    host.innerHTML = `${esc(done)}${this.partial ? ` <span class="faint">${esc(this.partial)}</span>` : ''}`;
    host.scrollTop = host.scrollHeight;
  },

  setStatus(text, kind) {
    const node = qs('#dict-status');
    if (!node) return;
    node.textContent = text;
    node.className = `small ${kind || 'dim'}`;
  },

  async start() {
    const button = qs('#dict-toggle');
    button.disabled = true;
    this.setStatus('запрашиваем микрофон…');
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      button.disabled = false;
      // Отказ в доступе — самая частая причина, и звучит она в браузере
      // одинаково невнятно. Говорим, что именно произошло и что делать.
      this.setStatus('микрофон не разрешён', 'err');
      toast(err && err.name === 'NotAllowedError'
        ? 'Браузер не дал доступ к микрофону. Разрешите его в значке замка слева от адреса.'
        : `Микрофон недоступен: ${err && err.message ? err.message : err}`, 'err');
      return;
    }

    // Ключ в адресе оседает в журналах прокси — берём одноразовый билет,
    // как и лента событий.
    let ticket = '';
    try {
      const issued = await API.post('/api/auth/ticket', {});
      ticket = (issued && issued.ticket) || '';
    } catch (e) { ticket = ''; }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/api/stream${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`;
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      button.disabled = false;
      this.setStatus('соединение не открылось', 'err');
      return;
    }
    socket.binaryType = 'arraybuffer';

    const recorder = new MediaRecorder(stream, pickMime());
    this.session = { stream, socket, recorder, started: Date.now() };
    this.partial = '';
    this.watchLevel(stream);

    socket.onopen = () => {
      const model = qs('#dict-model').value;
      const window_s = parseFloat(qs('#dict-window').value) || 3;
      const config = { type: 'config', format: 'auto', stream_window_s: window_s };
      if (model) config.model = model;
      socket.send(JSON.stringify(config));
      // MediaRecorder с timeslice даёт один непрерывный контейнер, а не
      // независимые файлы: только так ffmpeg на сервере разберёт поток.
      recorder.start(250);
      button.disabled = false;
      button.textContent = 'Остановить';
      button.classList.add('danger');
      this.setStatus('идёт запись', 'ok');
      // Часы принадлежат записи, а не разделу: viewTimer гасит таймеры только
      // при смене раздела, и после остановки обработчик продолжал стучать в
      // уже обнулённую сессию — по ошибке в консоли на каждую секунду.
      this.tick = setInterval(() => {
        if (!this.session) { clearInterval(this.tick); this.tick = null; return; }
        const seconds = Math.floor((Date.now() - this.session.started) / 1000);
        const node = qs('#dict-timer');
        if (node) node.textContent =
          `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
      }, 1000);
    };

    socket.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      switch (message.type) {
        case 'ready': {
          const hint = qs('#dict-mode');
          if (hint) hint.textContent = message.mode === 'native'
            ? 'настоящий поток: гипотеза уточняется после каждого куска'
            : `скользящее окно ${message.window_s} с: движок потока не держит`;
          break;
        }
        case 'partial':
          this.partial = message.text || '';
          this.paint();
          break;
        case 'final':
          if (message.text) this.final.push(message.text);
          this.partial = '';
          this.paint();
          break;
        case 'done':
          this.setStatus('готово', 'ok');
          break;
        case 'error':
          this.setStatus('ошибка', 'err');
          toast(`${message.message || 'Поток прерван'}${message.hint ? ` ${message.hint}` : ''}`, 'err');
          break;
        default:
          break;
      }
    };

    socket.onclose = (event) => {
      // Отказ приходит кодом закрытия — без разбора он выглядит как обрыв сети.
      const reasons = {
        4401: 'Ключ доступа не принят.',
        4403: 'Ключу доступа разрешено только чтение — диктовка ему закрыта.',
        4404: 'Потоковое распознавание выключено параметром stream_enabled.',
      };
      if (reasons[event.code]) {
        this.setStatus('отказано', 'err');
        toast(reasons[event.code], 'err');
      }
      this.stop(true);
    };

    recorder.ondataavailable = (event) => {
      if (socket.readyState === WebSocket.OPEN && event.data && event.data.size) {
        event.data.arrayBuffer().then((buffer) => {
          if (socket.readyState === WebSocket.OPEN) socket.send(buffer);
        });
      }
    };
  },

  /**
   * Полоса уровня. Немой микрофон — самая частая причина «сервер молчит»:
   * запись идёт, кадры уходят, распознавать нечего. Полоса отвечает на это
   * раньше, чем пустая расшифровка.
   */
  watchLevel(stream) {
    const bar = qs('#dict-level > span');
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!bar || !Ctx) return;
    let context;
    try { context = new Ctx(); } catch (e) { return; }
    const analyser = context.createAnalyser();
    analyser.fftSize = 512;
    context.createMediaStreamSource(stream).connect(analyser);
    const data = new Uint8Array(analyser.frequencyBinCount);
    this.level = { context, stop: false };
    const draw = () => {
      if (!this.level || this.level.stop) return;
      analyser.getByteTimeDomainData(data);
      let peak = 0;
      for (let i = 0; i < data.length; i += 1) peak = Math.max(peak, Math.abs(data[i] - 128));
      bar.style.width = `${Math.min(100, (peak / 128) * 260).toFixed(0)}%`;
      requestAnimationFrame(draw);
    };
    draw();
  },

  releaseLevel() {
    if (!this.level) return;
    this.level.stop = true;
    try { this.level.context.close(); } catch (e) { /* уже закрыт */ }
    this.level = null;
    const bar = qs('#dict-level > span');
    if (bar) bar.style.width = '0';
  },

  /**
   * Останавливает запись. Тихий вариант (при уходе из раздела или закрытии
   * сокета) не трогает разметку: её уже нет.
   */
  stop(silent) {
    const session = this.session;
    this.session = null;
    if (this.tick) { clearInterval(this.tick); this.tick = null; }
    this.releaseLevel();
    if (!session) { if (!silent) this.resetButton(); return; }
    try { if (session.recorder.state !== 'inactive') session.recorder.stop(); } catch (e) { /* уже */ }
    session.stream.getTracks().forEach((track) => track.stop());
    try {
      if (session.socket.readyState === WebSocket.OPEN) {
        // Досылаем «finish»: сервер досчитает хвост и вернёт окончательный текст.
        session.socket.send(JSON.stringify({ type: 'finish' }));
        setTimeout(() => { try { session.socket.close(); } catch (e) { /* уже */ } }, 4000);
      } else {
        session.socket.close();
      }
    } catch (e) { /* уже закрыт */ }
    if (!silent) this.setStatus('запись остановлена');
    this.resetButton();
  },

  resetButton() {
    const button = qs('#dict-toggle');
    if (!button) return;
    button.disabled = false;
    button.textContent = 'Начать диктовку';
    button.classList.remove('danger');
  },
};

/** Формат записи, который поддерживает этот браузер. */
function pickMime() {
  const wanted = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
  for (const type of wanted) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return { mimeType: type };
  }
  return {};      // пусть браузер выберет сам — ffmpeg на сервере разберёт
}

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
// Выбор строк и действия над выборкой
// ==========================================================================

/* Все действия были поштучными.
 *
 * После обновления модели пятьсот разговоров переобрабатывались по одному,
 * руками. Здесь строки отмечаются, а действие уходит одной командой.
 *
 * Выбор живёт в памяти раздела, а не в разметке: таблица перерисовывается
 * при каждом обновлении списка, и отметки в разметке пропадали бы вместе с
 * ней — вместе с тем, что человек успел выбрать.
 */
const Bulk = {
  ids: new Set(),
  bar: null,
  reload: null,

  has(id) { return this.ids.has(id); },

  attach(bar, reload) {
    this.ids.clear();
    this.bar = bar;
    this.reload = reload;
    if (!bar) return;
    qsa('button[data-bulk]', bar).forEach((b) =>
      b.addEventListener('click', () => this.run(b.dataset.bulk)));
    const clear = qs('#r-bulk-clear', bar);
    if (clear) clear.addEventListener('click', () => { this.ids.clear(); this.sync(); });
    this.sync();
  },

  bind(host) {
    qsa('.pick-one', host).forEach((box) => box.addEventListener('change', () => {
      if (box.checked) this.ids.add(box.value); else this.ids.delete(box.value);
      this.sync();
    }));
    const all = qs('#r-pick-all', host);
    if (all) all.addEventListener('change', () => {
      qsa('.pick-one', host).forEach((box) => {
        box.checked = all.checked;
        if (all.checked) this.ids.add(box.value); else this.ids.delete(box.value);
      });
      this.sync();
    });
    this.sync();
  },

  sync() {
    if (!this.bar) return;
    const n = this.ids.size;
    this.bar.hidden = n === 0;
    const count = qs('#r-bulk-count', this.bar);
    if (count) {
      count.textContent = `${n} ${plural(n, 'задание', 'задания', 'заданий')} выбрано`;
    }
  },

  async run(action) {
    const ids = [...this.ids];
    if (!ids.length) return;
    const тело = { action, ids };
    if (action === 'tag') {
      const метка = prompt(
        `Метка для ${ids.length} ${plural(ids.length, 'задания', 'заданий', 'заданий')}` +
        ' (пустая строка снимет метку):', '');
      if (метка === null) return;
      тело.tags = метка.trim();
    }
    if (action === 'delete' && !confirm(
        `Удалить ${ids.length} ${plural(ids.length, 'задание', 'задания', 'заданий')} ` +
        'вместе с записями и результатами? Это не отменить.')) return;
    if (action === 'retry' && !confirm(
        `Поставить ${ids.length} ${plural(ids.length, 'задание', 'задания', 'заданий')} ` +
        'в очередь заново?')) return;
    try {
      const ответ = await API.post('/api/jobs/bulk', тело);
      const сделано = (ответ.done || []).length;
      const отказов = (ответ.failed || []).length;
      // Отказы называем по первому: список из сотни строк в всплывающем
      // сообщении не читает никто, а причина у них обычно одна.
      if (отказов) {
        toast(`Готово: ${сделано}, не удалось: ${отказов}. ` +
              `Первая причина — ${ответ.failed[0].error}`, 'warn');
      } else {
        toast(`Готово: ${сделано} ${plural(сделано, 'задание', 'задания', 'заданий')}`, 'ok');
      }
      (ответ.done || []).forEach((id) => this.ids.delete(id));
      this.sync();
      if (this.reload) this.reload();
      await refreshQueue();
    } catch (err) { fail(err); }
  },
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
            style="width:240px">
          <!-- Ширина по содержимому, а не 210 пикселей: при жёсткой ширине
               самый длинный отбор обрезался посреди слова («с упоминанием
               суда и жало»), и человек не понимал, что именно выбрано. -->
          <select id="r-content" style="max-width:280px"
            title="Отбор по содержанию разговора — считается разбором записей">
            <option value="">любое содержание</option>
            <option value="negative">отрицательные</option>
            <option value="positive">положительные</option>
            <option value="downturn">кончились хуже, чем начались</option>
            <option value="recovered">кончились лучше, чем начались</option>
            <option value="alerts">с упоминанием суда и жалоб</option>
            <option value="open_commitments">обещания без срока</option>
            <option value="interruptions">много перебиваний</option>
            <option value="silence">много молчания</option>
            <option value="script_failed">скрипт не соблюдён</option>
            <option value="money">названы суммы</option>
            <option value="monologue">монолог оператора дольше 2,5 мин</option>
            <option value="mixed">противоречивые: и резко, и тепло</option>
            <option value="dead_air">заметной тишины больше 30 с</option>
            <option value="frustrated">клиент раздражён</option>
            <option value="repeat">повторное обращение</option>
            <option value="profanity_agent">нецензурная лексика у сотрудника</option>
            <option value="profanity">нецензурная лексика в разговоре</option>
            <option value="suspect">подозрительная расшифровка</option>
            <option value="hallucination">похоже на галлюцинацию</option>
            <option value="speakers_mismatch">говорящих не столько, сколько ожидалось</option>
          </select>
          <select id="r-order" style="width:190px">
            <option value="created_at DESC">Сначала новые</option>
            <option value="created_at ASC">Сначала старые</option>
            <option value="media_duration_s DESC">Самые длинные</option>
            <option value="rtf DESC">Самые медленные</option>
            <option value="rtf ASC">Самые быстрые</option>
          </select>
        </div>
        <div class="bulk-bar" id="r-bulk" hidden>
          <span class="count" id="r-bulk-count"></span>
          <button class="btn sm" data-bulk="retry">Повторить</button>
          <button class="btn sm" data-bulk="tag">Пометить</button>
          <button class="btn sm danger" data-bulk="delete">Удалить</button>
          <span class="spacer"></span>
          <button class="ghost sm" id="r-bulk-clear">Снять выбор</button>
        </div>
        <div class="table-wrap" id="results-table"></div>
      </section>`;
    let timer;
    // Переход сюда из аналитики записей («показать разговоры про сроки»)
    // приносит запрос с собой. Забираем его один раз и гасим: иначе
    // следующий заход в раздел молча подставлял бы прошлый поиск, и список
    // выглядел бы наполовину пустым без видимой причины.
    if (state.resultsSearch) {
      qs('#r-search').value = state.resultsSearch;
      state.resultsSearch = '';
    }
    qs('#r-search').oninput = () => { clearTimeout(timer); timer = setTimeout(() => this.load(), 300); };
    qs('#r-order').onchange = () => this.load();
    // Отбор по содержанию приходит и снаружи — из «Аналитики записей»,
    // где щелчок по отбору «что послушать» ведёт сюда за самим списком.
    // Категории обращений дописываются в тот же список: их набор живёт в
    // настройках, и зашитый в разметку перечень устарел бы с первой правкой.
    const выбрать = state.resultsContent;
    state.resultsContent = '';
    if (выбрать) qs('#r-content').value = выбрать;
    this.loadCategoryOptions(выбрать);
    qs('#r-content').onchange = () => this.load();
    Bulk.attach(qs('#r-bulk'), () => this.load());
    this.load();
  },

  /** Дописывает в отбор по содержанию действующие категории обращений. */
  async loadCategoryOptions(выбрать) {
    let перечни;
    try { перечни = await API.background('/api/content/kinds'); } catch (e) { return; }
    const select = qs('#r-content');
    if (!select || !(перечни.categories || []).length) return;
    const группа = document.createElement('optgroup');
    группа.label = 'Категории обращений';
    (перечни.categories || []).forEach((к) => {
      const opt = document.createElement('option');
      opt.value = `category:${к.id}`;
      opt.textContent = к.label;
      группа.appendChild(opt);
    });
    select.appendChild(группа);
    if (выбрать && выбрать.startsWith('category:')) {
      select.value = выбрать;
      this.load();
    }
  },

  async load() {
    const host = qs('#results-table');
    const search = qs('#r-search').value.trim();
    const order = qs('#r-order').value;
    try {
      const params = new URLSearchParams({ status: 'completed', limit: '150', order });
      if (search) params.set('search', search);
      const содержание = (qs('#r-content') || {}).value;
      if (содержание) params.set('content', содержание);
      const data = await API.latest('results-table', `/api/jobs?${params}`);
      host.innerHTML = data.items.length ? `<table>
        <thead><tr><th class="pick"><input type="checkbox" id="r-pick-all"
            title="Выбрать все на странице"></th>
          <th>Файл</th><th>Модель</th><th class="num">Длит.</th>
          <th class="num">Слов</th><th class="num">Сегм.</th><th class="num">RTF</th>
          <th class="num">Уверенность</th><th>Говорящие</th><th>Готово</th>
          <th style="width:230px">Выгрузка</th></tr></thead><tbody>
        ${data.items.map((job) => `<tr data-id="${job.id}">
          <td class="pick"><input type="checkbox" class="pick-one" value="${job.id}"
            ${Bulk.has(job.id) ? 'checked' : ''}></td>
          <td><div class="truncate" style="max-width:260px">${esc(job.filename)}</div>
            ${job.match && job.match.snippet
              ? `<div class="found small truncate" style="max-width:260px"
                     title="Открыть на ${fmtDur(job.match.start_s)}"
                     onclick="__asrhub.openAt('${job.id}', ${Number(job.match.start_s) || 0})">
                   <span class="at">${fmtDur(job.match.start_s)}</span> ${markSnippet(job.match.snippet)}
                 </div>`
              : `<div class="small faint truncate" style="max-width:260px">${
                  esc((job.text || '').slice(0, 70))}</div>`}</td>
          <td class="small dim">${esc(job.model || '')}</td>
          <td class="num">${fmtDur(job.media_duration_s)}</td>
          <td class="num">${num(job.words_count)}</td>
          <td class="num">${num(job.segments_count)}</td>
          <td class="num">${job.rtf ? num(job.rtf, 3) : '—'}</td>
          <td class="num">${job.avg_confidence ? pct(job.avg_confidence, 0) : '—'}</td>
          <td class="num">${job.speakers_count || '—'}</td>
          <td class="small faint nowrap">${fmtAgo(job.finished_at)}</td>
          <td><div class="row" style="gap:3px">
            <button class="ghost sm" onclick="__asrhub.playRecording('${job.id}')"
              title="Прослушать запись">▶</button>
            ${['txt', 'srt', 'json', 'docx'].map((f) =>
              `<button class="btn sm" onclick="__asrhub.download('${job.id}','${f}')"
                 title="Скачать в формате ${f}">${f}</button>`).join('')}
            <button class="ghost sm" onclick="__asrhub.openJob('${job.id}')">Открыть</button>
          </div></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">Завершённых заданий пока нет</div>';
      Bulk.bind(host);
    } catch (err) { fail(err); }
  },
};

// ==========================================================================
// Проигрыватель записи
// ==========================================================================

/* Прослушивание исходной записи — в карточке задания и в списке результатов.
 *
 * Аутентификация здесь особая. Обычные запросы уходят с заголовком
 * X-API-Key, но <audio src="…"> заголовков не шлёт: браузер сам ходит за
 * файлом и отправляет только cookie. Тем, кто вошёл логином и паролем, этого
 * достаточно — сервер принимает сессионную cookie. Тем, кто пользуется
 * ключом, прямая ссылка ответит 401, и тогда мы забираем файл обычным
 * запросом с заголовком и подсовываем проигрывателю как blob.
 *
 * Порядок именно такой, а не наоборот: прямая ссылка даёт частичные запросы
 * (Range), то есть перемотку по большому файлу без выкачивания целиком.
 * Blob — запасной путь: он тянет запись полностью, и для часового разговора
 * это заметно.
 */
const Player = {
  url: (id) => `/api/jobs/${encodeURIComponent(id)}/audio`,

  /** Подключает источник к <audio>, с запасным путём через blob. */
  attach(el, id, onFail) {
    const direct = Player.url(id);
    // Ключ в хранилище означает, что сессионной cookie нет, а заголовок
    // <audio> не пошлёт — прямая ссылка ответит 401 гарантированно. Идти за
    // предсказуемым отказом незачем: он стоит запроса и оставляет в консоли
    // красную строку, по которой потом ищут несуществующую поломку.
    const keyOnly = !!localStorage.getItem('asrhub_key');
    let fallbackTried = false;
    const fallback = async () => {
      if (fallbackTried) return;
      fallbackTried = true;
      try {
        const headers = {};
        const key = localStorage.getItem('asrhub_key');
        if (key) headers['X-API-Key'] = key;
        const response = await fetch(direct, { headers });
        if (!response.ok) {
          let reason = `сервер ответил ${response.status}`;
          try {
            const body = await response.json();
            reason = (body.error && (body.error.message || body.error.hint)) || reason;
          } catch (e) { /* тело не разобралось — оставляем код ответа */ }
          throw new Error(reason);
        }
        const blob = await response.blob();
        el.__blobUrl = URL.createObjectURL(blob);
        el.src = el.__blobUrl;
      } catch (err) {
        if (onFail) onFail(err.message || 'запись недоступна');
      }
    };
    el.addEventListener('error', fallback);
    if (keyOnly) fallback(); else el.src = direct;
    return () => { if (el.__blobUrl) URL.revokeObjectURL(el.__blobUrl); };
  },

  /** Панель управления. Возвращает объект с seek() и подпиской на время. */
  mount(host, id, opts) {
    const options = opts || {};
    host.innerHTML = `
      <audio preload="metadata" style="display:none"></audio>
      <button class="play" type="button" title="Пуск и пауза (пробел)" disabled>▶</button>
      <button class="skip" type="button" title="Назад на 5 секунд">−5с</button>
      <span class="time">--:-- / --:--</span>
      <input class="scrub" type="range" min="0" max="1000" value="0"
             title="Перемотка" aria-label="Позиция в записи">
      <button class="skip" type="button" title="Вперёд на 5 секунд">+5с</button>
      <select title="Скорость воспроизведения" aria-label="Скорость">
        <option value="0.5">0.5×</option>
        <option value="0.75">0.75×</option>
        <option value="1" selected>1×</option>
        <option value="1.25">1.25×</option>
        <option value="1.5">1.5×</option>
        <option value="1.75">1.75×</option>
        <option value="2">2×</option>
      </select>
      <button class="skip" type="button" title="Скачать исходную запись">↓</button>
      <span class="note"></span>`;

    const el = qs('audio', host);
    const play = qs('.play', host);
    const scrub = qs('.scrub', host);
    const time = qs('.time', host);
    const note = qs('.note', host);
    const speed = qs('select', host);
    const [back, forward, save] = qsa('.skip', host);

    let dragging = false;
    const fail = (message) => {
      note.textContent = `Запись недоступна: ${message}`;
      note.classList.add('err');
      play.disabled = true;
      scrub.disabled = true;
    };
    const release = Player.attach(el, id, fail);

    const paint = () => {
      const total = el.duration && isFinite(el.duration) ? el.duration : 0;
      time.textContent = `${fmtDur(el.currentTime)} / ${total ? fmtDur(total) : '--:--'}`;
      if (!dragging && total) scrub.value = String((el.currentTime / total) * 1000);
    };

    el.addEventListener('loadedmetadata', () => { play.disabled = false; paint(); });
    el.addEventListener('timeupdate', () => {
      paint();
      if (options.onTime) options.onTime(el.currentTime);
    });
    el.addEventListener('play', () => { play.textContent = '❚❚'; });
    el.addEventListener('pause', () => { play.textContent = '▶'; });
    el.addEventListener('ended', () => { play.textContent = '▶'; });

    play.addEventListener('click', () => { el.paused ? el.play() : el.pause(); });
    back.addEventListener('click', () => { el.currentTime = Math.max(0, el.currentTime - 5); });
    forward.addEventListener('click', () => {
      const total = el.duration && isFinite(el.duration) ? el.duration : 0;
      el.currentTime = total ? Math.min(total, el.currentTime + 5) : el.currentTime + 5;
    });
    speed.addEventListener('change', () => { el.playbackRate = Number(speed.value); });
    save.addEventListener('click', () => Player.save(id));

    // Ползунок ведём по вводу, а позицию ставим по отпусканию: иначе каждое
    // движение мыши превращается в запрос куска файла, и по сети уходит
    // вся запись вместо одного перехода.
    scrub.addEventListener('input', () => {
      dragging = true;
      const total = el.duration && isFinite(el.duration) ? el.duration : 0;
      if (total) time.textContent = `${fmtDur((Number(scrub.value) / 1000) * total)} / ${fmtDur(total)}`;
    });
    const commit = () => {
      const total = el.duration && isFinite(el.duration) ? el.duration : 0;
      if (total) el.currentTime = (Number(scrub.value) / 1000) * total;
      dragging = false;
    };
    scrub.addEventListener('change', commit);

    return {
      audio: el,
      seek(seconds, andPlay) {
        if (!isFinite(seconds)) return;
        el.currentTime = Math.max(0, seconds);
        if (andPlay && el.paused) el.play().catch(() => { /* автозапуск запрещён — не беда */ });
      },
      destroy() {
        try { el.pause(); } catch (e) { /* уже удалён из документа */ }
        release();
      },
    };
  },

  /** Скачивание исходной записи — тем же способом, что и остальные файлы. */
  async save(id) {
    try {
      const headers = {};
      const key = localStorage.getItem('asrhub_key');
      if (key) headers['X-API-Key'] = key;
      const response = await fetch(Player.url(id), { headers });
      if (!response.ok) throw new Error(`сервер ответил ${response.status}`);
      const blob = await response.blob();
      // Разбор имени — общий с выгрузкой результатов. Своя копия здесь
      // искала только filename*= по RFC 5987, а его сервер шлёт лишь для
      // неascii-имён: файл «record.wav» сохранялся как «запись-job_….wav».
      const disposition = response.headers.get('Content-Disposition') || '';
      const name = parseFilename(disposition) || `запись-${id}.wav`;
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    } catch (err) {
      toast(`Не удалось скачать запись: ${err.message}`, 'err');
    }
  },
};
window.__asrhub.playRecording = (id) => window.__asrhub.openJob(id, { play: true });
/**
 * Выгрузка отчёта в таблицу.
 *
 * Через fetch, а не ссылкой: ключ доступа живёт в заголовке, а адрес с
 * ключом попадает в историю браузера и в журнал обратного прокси. Тот же
 * приём, что и у выгрузки результатов задания.
 */
window.__asrhub.exportAnalytics = (fmt) => downloadReport(
  `/api/analytics/export?period=${encodeURIComponent(state.period)}` +
  `&fmt=${encodeURIComponent(fmt)}`, fmt, 'аналитика');

window.__asrhub.exportContent = (fmt) => downloadReport(
  `/api/content/export?period=${encodeURIComponent(state.contentPeriod || state.period)}` +
  `&fmt=${encodeURIComponent(fmt)}`, fmt, 'аналитика-записей');

/**
 * Скачивание отчёта: запрос с ключом, разбор имени файла, отдача браузеру.
 *
 * Общий для обеих выгрузок сервера намеренно. Две копии этого кода
 * разошлись бы на первой правке — и объяснить человеку, почему одна
 * выгрузка сообщает причину отказа, а вторая молча ничего не делает,
 * было бы нечем.
 */
async function downloadReport(url, fmt, подпись) {
  try {
    const headers = {};
    const key = localStorage.getItem('asrhub_key');
    if (key) headers['X-API-Key'] = key;
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let причина = `сервер ответил ${response.status}`;
      try {
        const тело = await response.json();
        причина = тело.message || причина;
        if (тело.hint) причина += ` — ${тело.hint}`;
      } catch (e) { /* тело не разбирается: остаётся код ответа */ }
      throw new Error(причина);
    }
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = parseFilename(response.headers.get('Content-Disposition') || '')
                    || `${подпись}.${fmt === 'csv' ? 'zip' : 'xlsx'}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    toast('Выгрузка готова', 'ok');
  } catch (err) {
    toast(`Не удалось выгрузить: ${err.message}`, 'err');
  }
}

window.__asrhub.openAt = (id, seconds) =>
  window.__asrhub.openJob(id, { play: true, at: Number(seconds) || 0 });

/**
 * Найденная фраза с обрамлением: сервер помечает совпадение символами ‹ ›.
 *
 * Экранируем сами, а метки превращаем в разметку уже после: в расшифровке
 * встречается что угодно, включая угловые скобки, и вставлять её как HTML
 * нельзя. Метки выбраны такие, каких в русской речи не бывает, — обычные
 * кавычки-ёлочки для этого не годятся, они в тексте попадаются.
 */
function markSnippet(text) {
  return esc(String(text || ''))
    .replaceAll('\u2039', '<mark>')
    .replaceAll('\u203a', '</mark>');
}
window.__asrhub.saveRecording = (id) => Player.save(id);

// ==========================================================================
// Карточка задания
// ==========================================================================

window.__asrhub.openJob = async (id, opts) => {
  try {
    const job = await API.get(`/api/jobs/${id}?with_segments=true`);
    showJobModal(job, opts || {});
  } catch (err) { fail(err); }
};

function showJobModal(job, opts) {
  const options = opts || {};
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

      ${(job.quality_flags || []).length ? `<div class="finding warning" style="margin-bottom:12px">
        <b>Расшифровка выглядит подозрительно</b>: ${(job.quality_flags || []).map((ф) =>
          esc(ПРИЗНАКИ_КАЧЕСТВА[ф] || ф)).join(', ')}${
          job.suspect_segments ? ` — ${num(job.suspect_segments)} из ${num(job.segments_count)} сегментов` : ''}.
        ${((job.quality_detail || {}).items || []).length ? `<div class="analysis-lines" style="margin-top:8px">${
          (job.quality_detail.items || []).slice(0, 8).map((и) => `<div class="analysis-line" data-start="${и.start_s || 0}">
            <span class="ts mono">${fmtDur(и.start_s || 0)}</span>
            <span class="who">сегмент ${num((и.idx || 0) + 1)}</span>
            <span class="what">${esc(и.text || '')} ${(и.reasons || []).map((r) =>
              `<span class="chip">${esc(ПРИЗНАКИ_КАЧЕСТВА[r] || r)}</span>`).join(' ')}</span></div>`).join('')}</div>` : ''}
        ${(job.quality_detail || {}).speakers_expected && job.quality_flags.includes('speakers')
          ? `<div class="small" style="margin-top:6px">Говорящих найдено ${num(job.quality_detail.speakers)}, ожидалось ${num(job.quality_detail.speakers_expected)} — настройка quality_expected_speakers.</div>` : ''}
        <div class="small faint" style="margin-top:6px">Признаки считаются по сегментам без эталона и ничего не доказывают — это повод послушать.</div>
      </div>` : ''}

      ${job.status === 'completed' || job.status === 'failed'
        ? '<div class="player" id="job-player"></div>' : ''}

      ${(job.waveform || []).length ? `<div class="wave-block" id="job-waveform-block">
        <div class="row small dim" style="margin-bottom:6px">
          <b class="small">Громкость записи</b>
          <span class="spacer"></span>
          <span class="faint">средний уровень за ${
            num((job.params || {}).waveform_interval_s || 1, 2)} с${
            segments.length ? ' · щелчок — переход к этому месту' : ''}</span>
        </div>
        <div id="job-waveform"></div>
        <div id="job-waveform-legend"></div>
      </div>` : ''}

      <div class="tabs" id="job-tabs">
        <button class="active" data-tab="text">Текст</button>
        <button data-tab="segments">Сегменты (${segments.length})</button>
        <button data-tab="params">Параметры (${changed.length} изменено)</button>
        ${job.status === 'completed' && job.text
          ? '<button data-tab="analysis">Разбор</button>' : ''}
        <button data-tab="events">События</button>
      </div>
      <div id="job-tab-body"></div>
    </div>
    <div class="modal-foot">
      <span class="small faint mono">${esc(job.id)}</span>
      <span class="spacer"></span>
      <button class="btn sm" onclick="__asrhub.saveRecording('${job.id}')"
        title="Скачать исходную запись">запись</button>
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
    segments: () => segments.length ? `
      <div class="job-find">
        <input type="search" id="job-find-input" placeholder="Найти в разговоре"
               autocomplete="off">
        <span class="count" id="job-find-count"></span>
      </div>
      <div class="find-hits" id="job-find-hits"></div>
      <div class="transcript">${segments.map((s, i) => `
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
    analysis: () => `<div id="job-analysis"><div class="empty">Разбираем запись…</div></div>`,
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
    if (name === 'segments') setupJobFind(backdrop, job);
    // Разбор грузится по требованию, а не вместе с карточкой: открывают её
    // чаще всего ради текста, и лишний запрос на каждое открытие оплачивал
    // бы вкладку, в которую не заходят.
    if (name === 'analysis') loadJobAnalysis(backdrop, job);
  };
  qsa('#job-tabs button', backdrop).forEach((b) =>
    b.addEventListener('click', () => show(b.dataset.tab)));
  show('text');

  const player = setupJobPlayer(backdrop, job, segments, show, options);
  drawJobWaveform(backdrop, job, segments, show, player);

  // Проигрыватель продолжал бы играть из закрытого окна: узел удалён, звук
  // идёт. Поэтому останавливаем его вместе с окном.
  backdrop.addEventListener('asrhub:closed', () => { if (player) player.destroy(); });
}

/* Разбор одной записи в карточке задания.
 *
 * Отвечает на вопрос, ради которого запись и открывают повторно: что здесь
 * было. Читать часовую расшифровку ради этого — не ответ, а работа.
 *
 * Всё, что показано, кликабельно по времени: щелчок по реплике переводит
 * проигрыватель на её секунду. Без этого раздел остаётся справкой, а с ним
 * становится оглавлением разговора.
 */
async function loadJobAnalysis(backdrop, job) {
  const host = qs('#job-analysis', backdrop);
  if (!host) return;
  let данные;
  try {
    данные = await API.get(`/api/content/jobs/${job.id}`);
  } catch (err) {
    host.innerHTML = `<div class="empty">Разбор недоступен: ${esc(err.message)}</div>`;
    return;
  }
  if (!host.isConnected) return;          // окно успели закрыть
  const a = данные.analysis || {};
  const тон = a.sentiment || {};
  const речь = a.speech || {};
  const сущности = a.entities || {};
  const скрипт = a.compliance || {};
  const обещания = a.commitments || {};
  const вопросы = a.questions || {};
  const тревога = a.alerts || {};
  const раздражение = a.frustration || {};
  const повторное = a.repeat_contact || {};
  const мат = a.profanity || {};
  const категории = a.categories || { items: [], matched: [], checked: 0 };

  const реплика = (з, доп) => `<div class="analysis-line" data-start="${з.start_s || 0}">
    <span class="ts mono">${fmtDur(з.start_s || 0)}</span>
    <span class="who">${esc(з.speaker || '—')}</span>
    <span class="what">${esc(з.text || '')}${доп ? ` <span class="chip">${esc(доп)}</span>` : ''}</span>
  </div>`;

  host.innerHTML = `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${kpi('Тональность', num(тон.score, 2), esc(тон.label || ''))}
      ${kpi('Разворот', num((тон.turn || {}).shift, 2),
            esc((тон.turn || {}).shape || 'нет данных'))}
      ${kpi('Темп речи', речь.wpm ? `${num(речь.wpm)} сл/мин` : '—',
            `тишины ${речь.silence_share === null || речь.silence_share === undefined
              ? '—' : pct(речь.silence_share, 0)}`)}
      ${kpi('Скрипт', скрипт.score === null || скрипт.score === undefined
              ? '—' : pct(скрипт.score, 0),
            скрипт.checked ? `${скрипт.passed} из ${скрипт.checked} пунктов` : '')}
    </div>

    ${card('Ход тональности', 'форма разговора: упало и не поднялось, выправилось к концу, ровно',
           '<div id="analysis-traj"></div>')}

    ${(тон.by_speaker || []).length > 1 ? card('По говорящим',
      'средняя по разговору смешивает раздражённого клиента с ровным оператором',
      `<div class="table-wrap"><table>
        <thead><tr><th>Говорящий</th><th class="num">Тональность</th>
          <th class="num">Реплик</th><th class="num">Говорил</th>
          <th class="num">Темп</th><th class="num">Паразитов</th>
          <th class="num" title="самая долгая непрерывная речь">Монолог</th>
          <th class="num" title="средняя пауза перед ответом собеседнику; паузы от 2 с считаются тишиной и сюда не входят">Пауза перед ответом</th></tr></thead>
        <tbody>${(тон.by_speaker || []).map((г) => {
          const р = (речь.speakers || []).find((s) => s.speaker === г.speaker) || {};
          const стороны = речь.sides || {};
          const роль = г.speaker === стороны.agent ? ' <span class="faint small">оператор</span>'
            : g_роль(г.speaker, стороны);
          return `<tr><td>${esc(г.speaker)}${роль}</td>
            <td class="num">${toneChip(г.score, г.label)}</td>
            <td class="num mono">${num(г.segments)}</td>
            <td class="num mono">${р.share === null || р.share === undefined
              ? '—' : pct(р.share, 0)}</td>
            <td class="num mono">${num(р.wpm, 0)}</td>
            <td class="num mono">${р.filler_rate === null || р.filler_rate === undefined
              ? '—' : pct(р.filler_rate, 1)}</td>
            <td class="num mono">${р.monologue_s ? `${num(р.monologue_s, 0)} с` : '—'}</td>
            <td class="num mono">${р.reply_delay_s === null || р.reply_delay_s === undefined
              ? '—' : `${num(р.reply_delay_s, 2)} с`}</td></tr>`;
        }).join('')}</tbody></table></div>`) : ''}

    <div class="grid cols-2">
      ${card('О чём говорили', 'вес по TF-IDF: часто здесь и редко в остальных записях',
             (a.keywords || []).length
               ? `<div class="chips">${(a.keywords || []).slice(0, 24).map((к) =>
                   `<span class="chip" title="упоминаний: ${к.count}">${esc(к.word)}</span>`
                 ).join('')}</div>` +
                 ((a.phrases || []).length
                   ? `<div class="small dim" style="margin-top:10px">Сочетания: ${
                       (a.phrases || []).slice(0, 8).map((ф) =>
                         `<b>${esc(ф.phrase)}</b>`).join(', ')}</div>` : '')
               : '<div class="empty small">Значимых слов не нашлось</div>')}
      ${card('Что прозвучало', 'суммы, сроки, контакты и номера',
             `<div class="table-wrap"><table><tbody>
               ${[['Суммы', (сущности.money || []).map((с) => с.text)],
                  ['Проценты', (сущности.percents || []).map((п) => `${п}%`)],
                  ['Сроки', сущности.deadlines || []],
                  ['Даты', сущности.dates || []],
                  ['Телефоны', сущности.phones || []],
                  ['Почта', сущности.emails || []],
                  ['Номера', (сущности.numbers || []).map(
                     (н) => `${н.kind} ${н.number}`)]]
                 .filter(([, v]) => (v || []).length)
                 .map(([имя, v]) => `<tr><td class="small dim">${имя}</td>
                   <td>${v.slice(0, 12).map((x) => `<span class="chip">${esc(x)}</span>`).join(' ')}</td>
                   </tr>`).join('') ||
                 '<tr><td class="small dim">Ничего из этого в записи не прозвучало</td></tr>'}
             </tbody></table></div>`)}
    </div>

    ${(категории.items || []).length ? card('Категории обращения',
      `по правилам набора: ${категории.matched.length} из ${категории.checked} сработали`,
      `<div class="chips" style="margin-bottom:10px">${(категории.items || []).map((к) =>
        `<span class="chip ${КАТЕГОРИЯ_ЦВЕТ[к.kind] || ''}" title="${esc(КАТЕГОРИЯ_ВИД[к.kind] || к.kind)}: совпадений ${к.count}${
          к.sides === false ? '; сторона не определена — искали по всей записи' : ''}">${esc(к.label)}${
          к.count > 1 ? ` <span class="faint">×${к.count}</span>` : ''}</span>`).join('')}</div>
       <div class="analysis-lines">${(категории.items || []).flatMap((к) => {
         // Одна реплика — одна строка, сколько бы примет в ней ни совпало:
         // «Сроки: срок, сорван», а не две строки с одним и тем же текстом.
         const поРепликам = new Map();
         (к.hits || []).forEach((h) => {
           const ключ = `${h.start_s}|${h.speaker}`;
           if (!поРепликам.has(ключ)) поРепликам.set(ключ, { ...h, слова: [] });
           поРепликам.get(ключ).слова.push(h.matched);
         });
         return [...поРепликам.values()].slice(0, 2).map((h) =>
           реплика(h, `${к.label}: ${h.слова.join(', ')}`));
       }).join('')}</div>`) : ''}

    ${(тревога.items || []).length ? card('Тревожные упоминания',
      'суд, жалоба, огласка — повод послушать запись целиком',
      `<div class="analysis-lines">${(тревога.items || []).map(
         (т) => реплика(т, (т.words || []).join(', '))).join('')}</div>`) : ''}

    ${(раздражение.items || []).length ? card('Клиент раздражён',
      раздражение.speaker ? `по репликам говорящего «${раздражение.speaker}»: не «обсуждает плохое», а расстроен`
                          : 'по всем репликам: оператор не определён',
      `<div class="analysis-lines">${(раздражение.items || []).map(
         (т) => реплика(т, (т.words || []).join(', '))).join('')}</div>`) : ''}

    ${(повторное.items || []).length ? card('Признаки повторного обращения',
      'клиент говорит, что уже обращался и вопрос не решён — обратная сторона решения с первого раза',
      `<div class="analysis-lines">${(повторное.items || []).map(
         (т) => реплика(т, (т.words || []).join(', '))).join('')}</div>`) : ''}

    ${мат.enabled && (мат.items || []).length ? card(
      `Нецензурная лексика${мат.agent ? ` — у сотрудника ${мат.agent} из ${мат.count}` : ''}`,
      'по корням слов; созвучные слова возможны — проверьте по репликам',
      `<div class="analysis-lines">${(мат.items || []).map(
         (т) => реплика(т, (т.words || []).join(', '))).join('')}</div>`) : ''}

    ${(обещания.items || []).length ? card(
      `Обещания (${обещания.count}, со сроком ${обещания.with_deadline})`,
      'то, за что потом спросят: в записи это есть, а в системе учёта — нет',
      `<div class="analysis-lines">${(обещания.items || []).map(
         (о) => реплика(о, о.deadline || 'срок не назван')).join('')}</div>`) : ''}

    <div class="grid cols-2">
      ${card('Самые тяжёлые реплики', '',
             (тон.worst || []).length
               ? `<div class="analysis-lines">${(тон.worst || []).map(
                   (о) => реплика(о, num(о.score, 2))).join('')}</div>`
               : '<div class="empty small">Отрицательных реплик нет</div>')}
      ${card('Самые благополучные реплики', '',
             (тон.best || []).length
               ? `<div class="analysis-lines">${(тон.best || []).map(
                   (о) => реплика(о, num(о.score, 2))).join('')}</div>`
               : '<div class="empty small">Положительных реплик нет</div>')}
    </div>

    ${(скрипт.items || []).length ? card('Скрипт разговора',
      скрипт.speaker ? `проверен по говорящему «${скрипт.speaker}»`
                     : 'говорящий не определён — проверено по всей записи',
      `<div class="table-wrap"><table>
        <thead><tr><th></th><th>Пункт</th><th>Где искали</th><th>Что нашли</th></tr></thead>
        <tbody>${(скрипт.items || []).map((п) => `<tr>
          <td>${п.passed ? '<span class="chip ok">есть</span>'
                         : '<span class="chip err">нет</span>'}</td>
          <td>${esc(п.label)}</td>
          <td class="small dim">${esc(гдеИскали(п))}</td>
          <td class="small">${esc(п.matched || '—')}</td></tr>`).join('')}</tbody></table></div>`) : ''}

    <div class="grid cols-2">
      ${card(`Вопросы (${вопросы.count || 0})`, '',
             (вопросы.items || []).length
               ? `<div class="analysis-lines">${(вопросы.items || []).slice(0, 15)
                   .map((в) => реплика(в)).join('')}</div>`
               : '<div class="empty small">Вопросов не найдено</div>')}
      ${card('Разговор', 'паузы, перебивания, вежливость',
             `<div class="table-wrap"><table><tbody>
               <tr><td class="small dim">Перебиваний</td><td class="mono">${num(речь.interruptions)}${
                 речь.overlap_s ? ` <span class="faint">(говорили одновременно ${
                   num(речь.overlap_s, 1)} с)</span>` : ''}</td></tr>
               <tr><td class="small dim">Смен говорящего</td><td class="mono">${num(речь.switches)}${
                 речь.switches_per_min ? ` <span class="faint">(${num(речь.switches_per_min, 1)} в минуту)</span>` : ''}</td></tr>
               <tr><td class="small dim">Долгих пауз</td><td class="mono">${num(речь.pauses)}${
                 речь.longest_pause_s ? ` <span class="faint">(дольше всего ${
                   num(речь.longest_pause_s, 1)} с)</span>` : ''}</td></tr>
               <tr><td class="small dim" title="сумма пауз от трёх секунд">Заметная тишина</td><td class="mono">${
                 речь.dead_air_s ? `${num(речь.dead_air_s, 0)} с${
                   речь.dead_air_share ? ` <span class="faint">(${pct(речь.dead_air_share, 0)} записи)</span>` : ''}` : '—'}</td></tr>
               ${(речь.sides || {}).agent ? `<tr><td class="small dim">Темп оператора к темпу клиента</td><td class="mono">${
                 речь.sides.tempo_ratio === null || речь.sides.tempo_ratio === undefined
                   ? '—' : num(речь.sides.tempo_ratio, 2)}</td></tr>` : ''}
               <tr><td class="small dim">Самый долгий монолог</td><td class="mono">${
                 (речь.monologue || {}).seconds
                   ? `${num(речь.monologue.seconds, 0)} с — ${esc(речь.monologue.speaker || '—')}`
                   : '—'}</td></tr>
               <tr><td class="small dim">Слова-паразиты</td><td class="mono">${
                 речь.filler_rate === null || речь.filler_rate === undefined
                   ? '—' : pct(речь.filler_rate, 1)}</td></tr>
               <tr><td class="small dim">Вежливость</td><td>${
                 Object.entries(a.politeness || {}).map(([к, v]) =>
                   `<span class="chip ${v ? 'ok' : ''}" title="${
                     v ? 'прозвучало' : 'не прозвучало'}">${esc(к)}</span>`
                 ).join(' ') || '—'}</td></tr>
             </tbody></table></div>`)}
    </div>

    <p class="small faint" style="margin-top:12px">
      Разбор версии ${esc(String(данные.version))}, посчитан
      ${данные.computed_at ? fmtTime(данные.computed_at) : '—'}.
      <button class="ghost sm" id="analysis-recompute">Пересчитать</button>
    </p>`;

  const точки = (тон.trajectory || []).filter((v) => v !== null && v !== undefined);
  window.Charts.line(qs('#analysis-traj', backdrop), {
    height: 160, yMin: -1, yMax: 1,
    labels: (тон.trajectory || []).map((_, i) => {
      const всего = (тон.trajectory || []).length || 1;
      return fmtDur((данные.duration_s || 0) * i / всего);
    }),
    series: [{ name: 'тональность', values: тон.trajectory || [] }],
    emptyText: точки.length ? '' : 'реплик слишком мало для хода тональности',
  });

  const кнопка = qs('#analysis-recompute', backdrop);
  if (кнопка) кнопка.addEventListener('click', async () => {
    кнопка.disabled = true;
    try {
      await API.post(`/api/content/jobs/${job.id}/recompute`, {});
      loadJobAnalysis(backdrop, job);
    } catch (err) { fail(err); кнопка.disabled = false; }
  });
}

/* Поиск по репликам открытого разговора.
 *
 * Часовой разговор — это сотни реплик, и «где обсуждали сроки» поиском по
 * странице означает пролистать их все. Ищет тот же указатель, что и общий
 * поиск, поэтому находится и по началу слова, и без разницы «ещё»/«еще»;
 * щелчок по находке переводит проигрыватель на её секунду.
 *
 * Запрос уходит не на каждую букву: набирающий «договор» иначе присылает
 * семь запросов, из которых нужен последний.
 */
function setupJobFind(backdrop, job) {
  const input = qs('#job-find-input', backdrop);
  const hits = qs('#job-find-hits', backdrop);
  const count = qs('#job-find-count', backdrop);
  if (!input || !hits) return;

  let таймер = null;
  let поколение = 0;

  const искать = async () => {
    const запрос = input.value.trim();
    hits.innerHTML = '';
    if (!запрос) { count.textContent = ''; return; }
    const своё = ++поколение;
    count.textContent = 'ищем…';
    let data;
    try {
      data = await API.get(
        `/api/jobs/${job.id}/search?q=${encodeURIComponent(запрос)}`);
    } catch (err) {
      if (своё === поколение) count.textContent = 'не удалось найти';
      return;
    }
    // Ответ на устаревший запрос: пока он шёл, набрали ещё букву.
    if (своё !== поколение) return;
    const items = data.items || [];
    if (!items.length) {
      count.textContent = data.indexed ? 'ничего не найдено'
                                       : 'поиск по репликам недоступен';
      return;
    }
    count.textContent = `${items.length} ${plural(items.length, 'находка', 'находки', 'находок')}`;
    hits.innerHTML = items.map((r) => `
      <div class="found" data-start="${r.start_s}">
        <span class="at">${fmtDur(r.start_s)}</span>${
          r.speaker ? `<b>${esc(r.speaker)}:</b> ` : ''}${markSnippet(r.snippet)}
      </div>`).join('');
  };

  input.addEventListener('input', () => {
    clearTimeout(таймер);
    таймер = setTimeout(искать, 250);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { clearTimeout(таймер); искать(); }
    if (e.key === 'Escape') { input.value = ''; искать(); }
  });
  hits.addEventListener('click', (e) => {
    const node = e.target.closest('.found[data-start]');
    if (!node) return;
    // Тот же обработчик, что и у щелчка по сегменту: он живёт на теле
    // вкладок и умеет и перемотку, и подсветку.
    const сегмент = qs(`.segment[data-start="${node.dataset.start}"]`, backdrop);
    if (сегмент) сегмент.click();
  });
}


/* Проигрыватель в карточке и его связь с расшифровкой.
 *
 * Ради этой связи всё и затевалось. Просто послушать запись можно и скачав
 * файл; ценно другое — слышать и одновременно видеть, что распознал сервер.
 * Поэтому звучащий сегмент подсвечивается сам, а щелчок по сегменту
 * переводит звук на его начало.
 */
function setupJobPlayer(backdrop, job, segments, show, options) {
  const host = qs('#job-player', backdrop);
  if (!host) return null;

  let current = -1;
  const highlight = (seconds) => {
    if (!segments.length) return;
    // Перебор с начала на каждом такте. Тактов у звука около четырёх в
    // секунду, а сегментов даже у часовой записи меньше тысячи — на этом
    // поиск не виден. Умный поиск «от текущего места» здесь был бы ошибкой
    // с перемоткой назад в обмен на выигрыш, которого не измерить.
    let index = -1;
    for (let i = 0; i < segments.length; i += 1) {
      if (segments[i].start <= seconds && seconds < segments[i].end) { index = i; break; }
    }
    if (index === current) return;
    current = index;
    const active = qs('.segment.playing', backdrop);
    if (active) active.classList.remove('playing');
    if (index < 0) return;
    const node = qs(`.segment[data-index="${index}"]`, backdrop);
    if (!node) return;                       // открыта другая вкладка
    node.classList.add('playing');
    // Подводим к строке, только если она ушла из поля зрения: иначе список
    // дёргается на каждой реплике, и читать его невозможно.
    const box = node.closest('.transcript');
    if (box) {
      const top = node.offsetTop - box.scrollTop;
      if (top < 0 || top > box.clientHeight - node.offsetHeight) {
        node.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  };

  const player = Player.mount(host, job.id, { onTime: highlight });

  // Щелчок по сегменту — переход к нему. Слушаем на теле вкладок, а не на
  // самих сегментах: вкладка перерисовывается целиком, и обработчики,
  // навешанные на строки, пропали бы при первом же переключении.
  // Слушаем всё тело окна, а не только вкладки: примеры подозрительных
  // сегментов стоят над вкладками, и щелчок по ним тоже должен вести к
  // этому месту записи.
  const body = qs('.modal-body', backdrop);
  if (body) {
    body.addEventListener('click', (event) => {
      // И реплики, и строки разбора: у обеих есть секунда, и обе для того
      // и показаны — чтобы попасть в это место записи. Разбор без перехода
      // остаётся справкой; с переходом становится оглавлением разговора.
      const node = event.target.closest('.segment[data-start], .analysis-line[data-start]');
      if (!node) return;
      player.seek(Number(node.dataset.start), true);
      qsa('.segment.active', backdrop).forEach((n) => n.classList.remove('active'));
      if (node.classList.contains('segment')) node.classList.add('active');
    });
  }

  if (options && options.play) {
    show('segments');
    player.audio.addEventListener('loadedmetadata', () => {
      // Секунда из результата поиска: открывать часовой разговор с начала,
      // когда уже известно, где сказано искомое, — значит выбросить
      // единственное, что поиск и добыл.
      if (options.at) player.seek(options.at, false);
      player.audio.play().catch(() => { /* браузер запретил автозапуск */ });
    }, { once: true });
  }
  return player;
}

/* Полоса громкости в карточке: дорожка на канал или говорящего.
 *
 * Щелчок по полосе открывает вкладку сегментов и подводит к тому, что
 * говорилось в эту секунду. Без этого полоса остаётся картинкой: видно,
 * что в середине разговора кто-то долго молчал, а найти это место в
 * расшифровке всё равно приходится вручную.
 */
function drawJobWaveform(backdrop, job, segments, show, player) {
  const host = qs('#job-waveform', backdrop);
  if (!host || !window.Charts || !Charts.waveform) return;
  const curves = job.waveform || [];

  const seek = (seconds) => {
    // Полоса громкости — это карта записи, и щелчок по ней должен вести
    // звук, а не только текст.
    if (player) player.seek(seconds, false);
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
    onSeek: (segments.length || player) ? seek : null,
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
        <button class="btn sm" onclick="__asrhub.exportAnalytics('xlsx')"
          title="Тот же отчёт книгой Excel: по листу на разрез">Выгрузить в Excel</button>
        <button class="ghost sm" onclick="__asrhub.exportAnalytics('csv')"
          title="Архив CSV — если Excel под рукой нет">CSV</button>
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
      </div>

      ${card('Нагрузка по дням недели', 'день × час — планировать обслуживание по суточному профилю нельзя: он усредняет будни с выходными',
             '<div id="chart-weekly"></div>')}

      <div class="grid cols-2">
        ${card('Ожидание в очереди', 'среднее скрывает хвост, а жалуются именно на него',
               '<div id="queue-body"></div>')}
        ${card('Уверенность во времени', 'провал означает, что что-то поменялось: источник, модель или параметры',
               '<div id="chart-quality"></div>')}
      </div>

      <div class="grid cols-2">
        ${card('Ошибки распознавания во времени', 'WER и доля заданий с низкой уверенностью',
               '<div id="chart-quality2"></div>')}
        ${card('Расход ресурсов', 'память моделей и разрез по устройствам',
               '<div id="resources-body"></div>')}
      </div>

      ${card('Подозрительные расшифровки',
             'галлюцинации Whisper, невозможный темп, повторы, известные фразы, разметка говорящих — по сегментам, без эталона',
             '<div id="suspicious-body"></div>')}

      <div class="grid cols-2">
        ${card('Надёжность', 'что происходит между приёмом и выдачей результата',
               '<div id="reliability-body"></div>')}
        ${card('Повторы и экономия', 'сколько работы сняло узнавание уже виденных файлов',
               '<div id="cache-body"></div>')}
      </div>

      ${card('Каким бывает звук', 'разрез не про сервер, а про материал',
             '<div id="audio-body"></div>')}

      ${card('По меткам', 'единственный разрез, который задаёт сам пользователь — для отчётности он важнее прочих',
             '<div class="table-wrap full" id="table-tags"></div>')}`;

    const ts = data.timeseries;
    const labels = (ts.labels || []).map((t) => подписьВремени(t, ts.bucket_seconds));

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

    drawExtraAnalytics(data);
  },
};

/* Разделы, добавленные поверх исходных двенадцати.
 *
 * Общий принцип отбора: показывать то, чего нельзя получить из уже
 * имеющихся цифр. Средняя уверенность за месяц не отвечает ни на один
 * вопрос — а её ход по дням отвечает; доля успеха не отличает задание,
 * прошедшее с первой попытки, от прошедшего с третьей.
 */
function drawExtraAnalytics(data) {
  const weekly = data.weekly || {};
  if (qs('#chart-weekly') && (weekly.jobs || []).length) {
    Charts.grid(qs('#chart-weekly'), {
      rows: weekly.days, cols: (weekly.hours || []).map((h) => String(h).padStart(2, '0')),
      values: weekly.jobs, secondary: weekly.audio_hours, secondaryUnit: 'ч аудио',
      emptyText: 'Нет заданий за период',
    });
    const p = weekly.peak || {};
    if (p.jobs) {
      qs('#chart-weekly').insertAdjacentHTML('beforeend',
        `<div class="small faint" style="margin-top:8px">Пик: ${esc(weekly.days[p.day])}, ${
          String(p.hour).padStart(2, '0')}:00 — ${p.jobs} заданий. Всего за период: ${
          num(weekly.total)}.</div>`);
    }
  }

  const q = data.queue || {};
  if (qs('#queue-body') && q.overall) {
    const строка = (s) => `${num(s.p50, 1)} / ${num(s.p90, 1)} / ${num(s.p95, 1)} / ${num(s.p99, 1)}`;
    qs('#queue-body').innerHTML = `
      <div class="grid cols-3" style="margin-bottom:10px">
        ${kpi('Медиана', `${num(q.overall.p50, 1)} с`, 'половина ждала меньше')}
        ${kpi('p95', `${num(q.overall.p95, 1)} с`, 'каждое двадцатое — дольше')}
        ${kpi('Максимум', `${num(q.overall.max, 1)} с`, `дольше минуты: ${q.waited_over_minute}`)}
      </div>
      <table><thead><tr><th>Разрез</th><th class="num">Заданий</th>
        <th class="num">p50 / p90 / p95 / p99, с</th></tr></thead><tbody>
      ${(q.by_priority || []).map((r) => `<tr><td>${esc(r.name)}</td>
        <td class="num">${r.jobs}</td><td class="num mono">${строка(r)}</td></tr>`).join('')}
      ${(q.by_source || []).map((r) => `<tr><td class="dim">источник: ${esc(r.name)}</td>
        <td class="num">${r.jobs}</td><td class="num mono">${строка(r)}</td></tr>`).join('')}
      </tbody></table>
      ${q.waited_over_10_minutes ? `<div class="small faint" style="margin-top:8px">
        Дольше десяти минут ждали ${q.waited_over_10_minutes} заданий — стоит посмотреть,
        не совпадает ли это с пиком на карте нагрузки выше.</div>` : ''}`;
  }

  const qt = data.quality_trend || {};
  if (qs('#chart-quality') && !(qt.buckets || []).length) {
    // Пустой ход — это не повод оставить на странице две дырки без
    // объяснения: карточки нарисованы, а внутри ничего.
    Charts.empty(qs('#chart-quality'), 'За период нет завершённых заданий');
    if (qs('#chart-quality2')) {
      Charts.empty(qs('#chart-quality2'), 'За период нет завершённых заданий');
    }
  } else if (qs('#chart-quality')) {
    const метки = qt.buckets.map((t) => подписьВремени(t, qt.bucket_seconds));
    const уверенность = (qt.confidence || []).map((v) => v === null ? null : v * 100);
    const есть = уверенность.some((v) => v !== null);
    if (есть) {
      // Свой график и своя ось: уверенность держится у 95 %, а WER около
      // единицы. На одной оси младший ряд ложится в ноль и не читается — а
      // вторую ось рисовать нельзя, она врёт про соотношение величин.
      // Заодно поджимаем низ шкалы: разница между 93 % и 96 % — это и есть
      // всё, что здесь происходит, а от нуля она не видна.
      const мин = Math.min(...уверенность.filter((v) => v !== null));
      Charts.line(qs('#chart-quality'), {
        labels: метки,
        series: [{ name: 'Средняя уверенность', values: уверенность }],
        yMin: Math.max(0, Math.floor(мин - 3)), yMax: 100, unit: ' %',
      });
    } else {
      Charts.empty(qs('#chart-quality'), 'Уверенность за период не считалась');
    }

    const второй = qs('#chart-quality2');
    if (второй) {
      const ряды = [];
      if ((qt.low_confidence_share || []).some((v) => v !== null)) {
        ряды.push({ name: 'Доля заданий с низкой уверенностью',
                    values: qt.low_confidence_share.map((v) => v === null ? null : v * 100) });
      }
      if ((qt.wer || []).some((v) => v !== null)) {
        ряды.push({ name: 'WER', values: qt.wer.map((v) => v === null ? null : v * 100) });
      }
      if (ряды.length) {
        Charts.line(второй, { labels: метки, series: ряды, unit: ' %' });
      } else {
        Charts.empty(второй, 'Эталонных текстов за период не задавали — WER не считался');
      }
    }
  }

  const п = data.suspicious || {};
  if (qs('#suspicious-body')) {
    const тело = qs('#suspicious-body');
    if (!п.assessed) {
      тело.innerHTML = `<div class="empty small">${п.jobs
        ? 'Признаки считаются с этой версии: у записей периода их ещё нет — они появятся по мере фонового разбора архива'
        : 'За период нет завершённых заданий'}</div>`;
    } else {
      тело.innerHTML = `
        <div class="grid cols-4" style="margin-bottom:12px">
          ${kpi('Записей с признаками', num(п.flagged),
                `${num(п.flagged_share, 1)}% из ${num(п.assessed)} проверенных`)}
          ${kpi('Подозрительных сегментов', num(п.suspect_segments),
                `${num(п.suspect_share, 2)}% из ${num(п.segments)}`)}
          ${kpi('Проверено записей', num(п.assessed),
                п.jobs > п.assessed ? `ещё ${num(п.jobs - п.assessed)} сделаны до появления признаков` : 'все записи периода')}
          ${kpi('Худшая модель',
                (п.by_model || []).length && п.by_model[0].suspect_share ? esc(п.by_model[0].key) : '—',
                (п.by_model || []).length && п.by_model[0].suspect_share
                  ? `${num(п.by_model[0].suspect_share, 2)}% подозрительных сегментов` : 'подозрительных сегментов нет')}
        </div>
        <div class="grid cols-2">
          <div><div class="small dim" style="margin-bottom:6px">По признакам, записей</div>
            <div id="chart-suspect-flags"></div></div>
          <div><div class="small dim" style="margin-bottom:6px">По моделям и источникам</div>
            <div class="table-wrap"><table>
              <thead><tr><th>Модель / источник</th><th class="num">Записей</th><th class="num">С признаками</th><th class="num">Сегментов</th></tr></thead>
              <tbody>${[...(п.by_model || []), ...(п.by_source || []).map((и) => ({ ...и, key: `источник: ${и.key}` }))].map((м) => `<tr><td>${esc(м.key)}</td>
                <td class="num mono">${num(м.jobs)}</td>
                <td class="num mono">${м.flagged_share === null ? '—' : num(м.flagged_share, 1) + '%'}</td>
                <td class="num mono">${м.suspect_share === null ? '—' : num(м.suspect_share, 2) + '%'}</td></tr>`).join('')}</tbody>
            </table></div></div>
        </div>
        <div class="small dim" style="margin:12px 0 6px">Самые подозрительные записи</div>
        <div class="table-wrap full"><table>
          <thead><tr><th>Файл</th><th>Модель</th><th class="num">Подозрительных сегментов</th><th>Признаки</th><th></th></tr></thead>
          <tbody>${(п.worst || []).map((з) => `<tr>
            <td class="truncate" style="max-width:260px">${esc(з.filename || з.id)}</td>
            <td class="small dim">${esc(з.model || '—')}</td>
            <td class="num mono">${num(з.suspect_segments)} / ${num(з.segments_count)}</td>
            <td class="small dim">${esc((з.flags || []).map((ф) => ПРИЗНАКИ_КАЧЕСТВА[ф] || ф).join(', '))}</td>
            <td><button class="ghost sm" onclick="__asrhub.openJob('${esc(з.id)}')">Открыть</button></td></tr>`).join('')
            || '<tr><td colspan="5" class="empty small">Подозрительных записей нет</td></tr>'}</tbody>
        </table></div>
        <p class="small faint" style="margin-top:10px">
          Ни один признак не доказательство: это отбор «что послушать» и ход по моделям, а не приговор расшифровке.
          В «Результатах» те же записи — отбор «подозрительная расшифровка».
        </p>`;
      // Одна величина — один цвет: разные цвета читались бы как разные
      // виды признаков, а это одно и то же число записей.
      const цвет = Charts.palette()[0];
      Charts.hbars(qs('#chart-suspect-flags'), {
        items: (п.by_flag || []).map((ф) => ({ label: ф.title, value: ф.jobs, color: цвет })),
        labelWidth: 210, emptyText: 'признаков нет',
      });
    }
  }

  const r = data.reliability || {};
  if (qs('#reliability-body') && r.total !== undefined) {
    qs('#reliability-body').innerHTML = `<table>
      <tr><td class="dim">Заданий всего</td><td class="num">${num(r.total)}</td></tr>
      <tr><td class="dim">Прошло с первой попытки</td><td class="num">${
        num(r.first_attempt_success)}${r.first_attempt_rate !== null
          ? ` (${pct(r.first_attempt_rate, 1)})` : ''}</td></tr>
      <tr><td class="dim">Дошло со второй и далее</td><td class="num">${
        num(r.completed_after_retry)}</td></tr>
      <tr><td class="dim">Заданий с повторами</td><td class="num">${
        num(r.jobs_with_retries)}</td></tr>
      <tr><td class="dim">Повторов всего</td><td class="num">${num(r.retry_total)}</td></tr>
      <tr><td class="dim">Отменено</td><td class="num">${num(r.cancelled)}</td></tr>
      </table>
      ${(r.cancelled_by || []).length ? `<div class="small faint" style="margin-top:8px">
        Кто отменял: ${r.cancelled_by.map((c) => `${esc(c.who)} — ${c.jobs}`).join(', ')}</div>` : ''}
      ${(r.webhooks || []).length ? `<div class="small faint" style="margin-top:6px">
        Уведомления: ${r.webhooks.map((w) => `${esc(w.status)} — ${w.jobs}`).join(', ')}</div>` : ''}
      <div class="small faint" style="margin-top:8px">«Прошло с первой попытки» отличается от
        доли успеха: задание, дошедшее с третьего раза, для доли успеха такое же, как
        безупречное, — а для состояния сервера это разные вещи.</div>`;
  }

  const c = data.cache || {};
  if (qs('#cache-body') && c.hits !== undefined) {
    qs('#cache-body').innerHTML = `
      <div class="grid cols-3" style="margin-bottom:10px">
        ${kpi('Повторов', num(c.hits), c.hit_rate !== null ? `доля: ${pct(c.hit_rate, 1)}` : '')}
        ${kpi('Сэкономлено аудио', `${num(c.audio_hours_saved, 2)} ч`, 'не считалось заново')}
        ${kpi('Машинного времени', `${num(c.processing_seconds_saved / 3600, 2)} ч`,
              c.assumed_rtf ? `по RTF ${num(c.assumed_rtf, 3)}` : '')}
      </div>
      ${(c.repeats || []).length ? `<table><thead><tr><th>Файл</th>
        <th class="num">Повторов</th><th class="num">Аудио, ч</th></tr></thead><tbody>
      ${c.repeats.map((x) => `<tr><td class="truncate" style="max-width:200px">${
        esc(x.filename || '—')}</td><td class="num">${x.hits}</td>
        <td class="num">${num(x.audio_hours, 2)}</td></tr>`).join('')}</tbody></table>
      <div class="small faint" style="margin-top:8px">Один и тот же файл, приходящий десятки раз,
        обычно означает не бережливость, а ошибку в очереди на стороне клиента.</div>`
      : '<div class="empty small">Повторов за период не было</div>'}`;
  }

  const a = data.audio || {};
  if (qs('#audio-body') && a.formats) {
    const s = a.speech_rate_wpm || {};
    qs('#audio-body').innerHTML = `
      <div class="grid cols-3" style="margin-bottom:10px">
        ${kpi('Темп речи', s.count ? `${num(s.avg, 0)} сл/мин` : '—',
              s.count ? `p50 ${num(s.p50, 0)} · p95 ${num(s.p95, 0)}` : '')}
        ${kpi('Битрейт', (a.bitrate_kbps || {}).count ? `${num(a.bitrate_kbps.avg, 0)} кбит/с` : '—',
              'средний по записям')}
        ${kpi('Реплик в минуту', (a.segments_per_minute || {}).count
              ? num(a.segments_per_minute.avg, 1) : '—', 'плотность разговора')}
      </div>
      ${a.formats.length ? `<table><thead><tr><th>Формат</th><th class="num">Файлов</th>
        <th class="num">Аудио, ч</th><th class="num">Средний размер</th></tr></thead><tbody>
      ${a.formats.map((f) => `<tr><td class="mono">${esc(f.format)}</td>
        <td class="num">${f.jobs}</td><td class="num">${num(f.audio_hours, 2)}</td>
        <td class="num">${num(f.avg_mb, 1)} МБ</td></tr>`).join('')}</tbody></table>` : ''}
      ${(a.speakers || []).length ? `<div class="small faint" style="margin-top:8px">
        Говорящих в записи: ${a.speakers.map((x) => `${x.speakers} — ${x.jobs}`).join(', ')}</div>` : ''}`;
  }

  const res = data.resources || {};
  if (qs('#resources-body') && res.devices) {
    qs('#resources-body').innerHTML = `
      ${res.devices.length ? `<table><thead><tr><th>Устройство</th><th class="num">Заданий</th>
        <th class="num">Аудио, ч</th><th class="num">RTF</th></tr></thead><tbody>
      ${res.devices.map((d) => `<tr><td class="mono">${esc(d.device)}</td>
        <td class="num">${d.jobs}</td><td class="num">${num(d.audio_hours, 2)}</td>
        <td class="num">${d.rtf !== null ? num(d.rtf, 3) : '—'}</td></tr>`).join('')}
      </tbody></table>` : ''}
      ${(res.models || []).length ? `<table style="margin-top:10px"><thead><tr><th>Модель</th>
        <th class="num">Пик</th><th class="num">p95</th><th class="num">Среднее</th>
        </tr></thead><tbody>
      ${res.models.slice(0, 10).map((m) => `<tr><td class="mono truncate"
        style="max-width:180px">${esc(m.model)}</td>
        <td class="num">${fmtBytes(m.peak_mb * 1024 * 1024)}</td>
        <td class="num">${fmtBytes(m.p95_mb * 1024 * 1024)}</td>
        <td class="num">${fmtBytes(m.avg_mb * 1024 * 1024)}</td></tr>`).join('')}</tbody></table>
      ${(res.concurrency || []).length > 1 ? `<table style="margin-top:10px"><thead><tr>
        <th>Заданий разом</th><th class="num">Замеров</th><th class="num">Пик</th>
        <th class="num">p95</th><th class="num">Среднее</th></tr></thead><tbody>
      ${res.concurrency.map((c) => `<tr>
        <td>${c.jobs_at_once} ${plural(c.jobs_at_once, 'задание', 'задания', 'заданий')}</td>
        <td class="num">${c.measurements}</td>
        <td class="num">${fmtBytes(c.peak_mb * 1024 * 1024)}</td>
        <td class="num">${fmtBytes(c.p95_mb * 1024 * 1024)}</td>
        <td class="num">${fmtBytes(c.avg_mb * 1024 * 1024)}</td></tr>`).join('')}
      </tbody></table>
      <div class="small faint" style="margin-top:6px">Пик — величина на весь сервер, а не
        на одну модель: счётчики памяти другого не умеют. Поэтому рядом стоит, сколько
        заданий шло разом: по этой таблице видно, сколько их выдержит карта.</div>` : ''}`
      : `<div class="small faint" style="margin-top:8px">Пик памяти пишется начиная с этой
         версии — у заданий, выполненных раньше, его нет. На видеокарте это память
         ускорителя, на процессоре — резидентная память процесса.</div>`}`;
  }

  const tags = data.tags || [];
  if (qs('#table-tags')) {
    qs('#table-tags').innerHTML = tags.length ? `<table>
      <thead><tr><th>Метка</th><th class="num">Заданий</th><th class="num">Готово</th>
        <th class="num">Ошибок</th><th class="num">Аудио, ч</th>
        <th class="num">Машинное время</th><th class="num">RTF</th>
        <th class="num">Слов</th></tr></thead><tbody>
      ${tags.map((t) => `<tr><td><b>${esc(t.tag)}</b></td>
        <td class="num">${t.jobs}</td><td class="num">${t.completed}</td>
        <td class="num">${t.failed || 0}</td><td class="num">${num(t.audio_hours, 2)}</td>
        <td class="num">${fmtDur(t.processing_s)}</td>
        <td class="num">${t.rtf !== null ? num(t.rtf, 3) : '—'}</td>
        <td class="num">${num(t.words)}</td></tr>`).join('')}</tbody></table>`
      : '<div class="empty small">Метки заданиям не присваивались</div>';
  }
}

// ==========================================================================
// Вид: Модели
// ==========================================================================

// ==========================================================================
// Аналитика записей
// ==========================================================================

/* Раздел отвечает не на те вопросы, что «Аналитика». Та — про сервер:
 * сколько сделано, с какой скоростью, что падало. Этот — про разговоры:
 * какими они были, чем отличаются друг от друга и что из этого следует.
 *
 * Разделы грузятся по отдельности, а не одним отчётом. Полный отчёт на
 * архиве в сотню тысяч записей считается несколько секунд — это нормально
 * для выгрузки, которую делают раз в месяц, и неприемлемо для страницы,
 * которую открывают между делом. Каждая вкладка забирает своё за четверть
 * секунды, и, пока человек читает свод, остальные разделы ему не нужны.
 */

/* Какой отбор «что послушать» каким фильтром открывается в «Результатах».
 * Перечни близки, но не совпадают: в разделе есть отборы, которых в списке
 * заданий нет (самые долгие паузы, самая быстрая речь) — для них перехода
 * не будет, и кнопка не показывается вовсе. Молча уводить на «все записи»
 * хуже, чем не уводить никуда. */
const ОТБОР_В_РЕЗУЛЬТАТЫ = {
  negative: 'negative', downturn: 'downturn', recovered: 'recovered',
  alerts: 'alerts', open_commitments: 'open_commitments',
  interruptions: 'interruptions', silence: 'silence',
  script: 'script_failed', money: 'money',
  monologue: 'monologue', mixed: 'mixed', dead_air: 'dead_air',
  frustrated: 'frustrated', repeat: 'repeat',
  profanity_agent: 'profanity_agent', profanity: 'profanity',
};

const CONTENT_TABS = [
  { key: 'summary',    title: 'Свод' },
  { key: 'categories', title: 'Категории' },
  { key: 'groups',     title: 'Разрезы' },
  { key: 'topics',   title: 'Темы' },
  { key: 'links',    title: 'Связи' },
  { key: 'records',  title: 'Что послушать' },
  { key: 'script',   title: 'Скрипт разговора' },
];

/** Виды категорий: подпись и цвет фишки. Один перечень на карточку,
 *  вкладку и редактор — чтобы «нарушение» везде было красным. */
const КАТЕГОРИЯ_ВИД = {
  topic: 'категория обращения',
  violation: 'нарушение оператора',
  objection: 'возражение клиента',
  handling: 'отработка возражения',
};
const КАТЕГОРИЯ_ЦВЕТ = { violation: 'err', objection: 'warn', handling: 'ok' };
const КАТЕГОРИЯ_КТО = { any: 'любой', agent: 'оператор', customer: 'клиент' };

/** Подпись тональности с цветом: одно число читается плохо, слово — сразу. */
function toneChip(score, label) {
  if (score === null || score === undefined) return '<span class="chip">нет оценки</span>';
  const cls = score < -0.15 ? 'err' : score > 0.15 ? 'ok' : '';
  return `<span class="chip ${cls}">${esc(label || '')} ${num(score, 2)}</span>`;
}

/** Как называть признаки подозрительной расшифровки — тот же перечень, что в quality.py. */
const ПРИЗНАКИ_КАЧЕСТВА = {
  compression: 'сжимаемый текст',
  silence: 'текст на тишине',
  temperature: 'перебор температур',
  tempo: 'невозможный темп',
  repeat: 'повторы фраз',
  phrase: 'известная фраза-галлюцинация',
  fragments: 'осколки разметки говорящих',
  speakers: 'говорящих не столько, сколько ожидалось',
};

/** Подпись роли говорящего в карточке: клиент — самый говорливый из не-операторов. */
function g_роль(кто, стороны) {
  return кто === (стороны || {}).customer ? ' <span class="faint small">клиент</span>' : '';
}

/** Полоска долей: отрицательные / нейтральные / положительные. */
function toneBar(host, свод) {
  const s = window.Charts.status();
  window.Charts.stacked(host, {
    height: 26,
    parts: [
      { label: 'отрицательные', value: свод.negative || 0, color: s.err },
      { label: 'нейтральные', value: свод.neutral || 0, color: s.idle },
      { label: 'положительные', value: свод.positive || 0, color: s.ok },
    ],
    emptyText: 'нет оценённых записей',
  });
}

/** Изменение показателя к прошлому периоду — со знаком и направлением. */
function delta(сейчас, раньше, признак) {
  if (сейчас === null || сейчас === undefined ||
      раньше === null || раньше === undefined) return '';
  const знаков = признак && признак.digits !== undefined ? признак.digits : 2;
  const d = сейчас - раньше;
  // Изменение, неразличимое в показанной точности, — это не изменение.
  // Без проверки таблица пестрела строками «−0» и «+0.000»: разница в
  // седьмом знаке подавалась как новость, а глаз цеплялся за знак.
  if (Math.abs(d) < Math.pow(10, -знаков) / 2) return '';
  const лучше = (признак && признак.good) ? признак.good * Math.sign(d) : 0;
  const dir = лучше > 0 ? 'up' : лучше < 0 ? 'down' : '';
  const знак = d > 0 ? '+' : '−';
  return `<div class="kpi-trend ${dir}">${знак}${num(Math.abs(d), знаков)} к прошлому периоду</div>`;
}

RENDERERS.content = {
  async render(root) {
    root.innerHTML = `
      <div class="settings-toolbar">
        <span class="small dim">Период:</span>
        <div class="group-nav" id="content-period">
          ${Object.entries(PERIOD_LABELS).map(([k, v]) =>
            `<button data-period="${k}" class="${state.contentPeriod === k ? 'active' : ''}">${v}</button>`
          ).join('')}
        </div>
        <span class="spacer"></span>
        <button class="btn sm" onclick="__asrhub.exportContent('xlsx')"
          title="Весь отчёт книгой Excel: по листу на раздел">Выгрузить в Excel</button>
        <button class="ghost sm" onclick="__asrhub.exportContent('csv')"
          title="Архив CSV — если Excel под рукой нет">CSV</button>
        <button class="ghost sm" id="content-recompute"
          title="Пересчитать разбор всего архива — после смены словарей или скрипта">Пересчитать</button>
      </div>
      <div id="content-coverage"></div>
      <div class="tabs" id="content-tabs">
        ${CONTENT_TABS.map((t) => `<button data-tab="${t.key}"
          class="${state.contentTab === t.key ? 'active' : ''}">${esc(t.title)}</button>`).join('')}
      </div>
      <div id="content-body"><div class="empty">Загрузка…</div></div>`;

    // Отрисовка раздела всегда начинается с чистого таймера: renderView
    // гасит таймеры при каждой перерисовке, а leave() зовёт только при
    // смене раздела. Смена периода перерисовывает тот же раздел — интервал
    // погашен, а метка о нём осталась, и полоса разбора больше не
    // обновлялась никогда, замирая на числе, с которым открыли страницу.
    state.contentCoverageTimer = null;
    qsa('#content-period button').forEach((b) => b.addEventListener('click', () => {
      state.contentPeriod = b.dataset.period;
      state.contentData = {};           // период сменился — прошлые ответы не про него
      renderView();
    }));
    qsa('#content-tabs button').forEach((b) => b.addEventListener('click', () => {
      state.contentTab = b.dataset.tab;
      qsa('#content-tabs button').forEach((x) =>
        x.classList.toggle('active', x.dataset.tab === state.contentTab));
      this.showTab();
    }));
    qs('#content-recompute').addEventListener('click', () => this.recompute());

    this.loadCoverage();
    return this.showTab();
  },

  /** Полоса состояния разбора: пока архив не разобран, свод неполон. */
  async loadCoverage() {
    const host = qs('#content-coverage');
    if (!host) return;
    let с;
    try { с = await API.background('/api/content/status'); } catch (e) { return; }
    if (!host.isConnected) return;
    state.contentData.coverage = с;
    if (!с.total) {
      host.innerHTML = `<div class="card tight"><b>Записей ещё нет</b>
        <div class="small dim" style="margin-top:4px">Раздел наполнится, как только
        появятся завершённые задания с расшифровкой.</div></div>`;
      return;
    }
    if (!с.pending) {
      host.innerHTML = `<p class="small dim" style="margin:0 0 10px">
        Разобрано записей: ${num(с.analyzed)} из ${num(с.total)} · версия разбора
        ${с.version} · словарь основ: ${num(с.vocabulary)}${
        с.last_error ? ` · последняя ошибка: ${esc(с.last_error)}` : ''}</p>`;
      return;
    }
    const доля = с.total ? с.analyzed / с.total : 0;
    host.innerHTML = `<div class="card tight" style="border-color:var(--warn)">
      <div class="row"><b>Архив ещё разбирается</b><span class="spacer"></span>
        <span class="small dim">${num(с.analyzed)} из ${num(с.total)} · ${pct(доля, 0)}</span></div>
      <div class="progress warn" style="margin-top:8px"><span style="width:${(доля * 100).toFixed(1)}%"></span></div>
      <div class="small dim" style="margin-top:6px">Показатели ниже посчитаны по
        разобранной части и ещё сдвинутся. Разбор идёт в фоне порциями и на
        очередь не влияет.</div></div>`;
    // Пока идёт разбор, полосу обновляем: иначе она замирает на числе,
    // с которым человек открыл страницу, и выглядит как зависшая работа.
    // Одного таймера довольно: следующий заход перезаведёт его сам, а до
    // тех пор лишние обновления только шумят запросами.
    if (!state.contentCoverageTimer) {
      state.contentCoverageTimer = viewTimer(() => this.loadCoverage(), 15000);
    }
  },

  /** Уход из раздела: таймер полосы разбора гасится общим механизмом;
   *  черновик категорий сбрасывается — как и черновик скрипта, он живёт
   *  только на экране, пока его не сохранили. */
  leave() {
    state.contentCoverageTimer = null;
    state.contentCategories = null;
    state.contentCategoriesDirty = false;
  },

  async recompute() {
    if (!confirm('Пересчитать разбор всего архива?\n\nСчитается в фоне порциями; ' +
                 'на большом архиве это часы. Нужно после смены словарей, ' +
                 'скрипта разговора или обновления сервера.')) return;
    try {
      const r = await API.post('/api/content/recompute', {});
      toast(`К пересчёту помечено записей: ${num(r.queued)}`, 'ok');
      this.loadCoverage();
    } catch (err) { fail(err); }
  },

  async showTab() {
    const host = qs('#content-body');
    if (!host) return;
    host.innerHTML = '<div class="empty">Загрузка…</div>';
    const вкладка = state.contentTab;
    try {
      await this[`tab_${вкладка}`](host);
      // Проверка стоит и здесь, а не только в catch. Разрезы отвечают
      // дольше остальных вкладок, и на успешном пути `host.innerHTML`
      // выполнялся безусловно: подсвечены «Темы», показаны «Разрезы», и
      // понять это можно было только по содержимому таблицы.
      if (state.contentTab !== вкладка) { this.showTab(); return; }
    } catch (err) {
      if (err && err.silent) return;
      if (state.contentTab !== вкладка) return;
      host.innerHTML = `<div class="empty">Не удалось загрузить: ${esc(err.message)}</div>`;
    }
  },

  // --- свод ---------------------------------------------------------------

  async tab_summary(host) {
    const период = state.contentPeriod;
    const [свод, лента, выводы] = await Promise.all([
      API.latest('content-summary', `/api/content/summary?period=${период}`),
      API.latest('content-timeline', `/api/content/timeline?period=${период}`),
      API.latest('content-findings', `/api/content/findings?period=${период}`),
    ]);
    const c = свод.current || {};
    const p = свод.previous || {};
    const признак = (k) => (свод.features || []).find((f) => f.key === k) || {};
    if (!c.records) {
      host.innerHTML = '<div class="empty">За период разобранных записей нет</div>';
      return;
    }

    host.innerHTML = `
      <div class="grid cols-6" style="margin-bottom:16px">
        ${kpi('Записей', num(c.records), `${num(c.hours)} ч звука`,
              p.records ? { dir: c.records >= p.records ? 'up' : 'down',
                            text: `было ${num(p.records)}` } : null)}
        ${kpi('Тональность', num(c.sentiment, 2),
              `разворот ${num(c.sentiment_shift, 2)}` +
              delta(c.sentiment, p.sentiment, признак('sentiment')))}
        ${kpi('Отрицательных',
              c.negative_share === null ? '—' : `${num(c.negative_share, 1)}%`,
              `${num(c.negative)} из ${num(c.scored)}` +
              delta(c.negative_share, p.negative_share, { good: -1, digits: 1 }))}
        ${kpi('Тревожных', num(c.alert_records),
              c.alert_share === null ? 'записей с упоминанием суда и жалоб'
                : `${num(c.alert_share, 1)}% записей — суд, жалобы, огласка`)}
        ${kpi('Обещаний без срока', num(c.commitments_open),
              `всего обещаний ${num(c.commitments)}`)}
        ${kpi('Скрипт', c.compliance !== null ? pct(c.compliance, 0) : '—',
              'средняя доля выполненных пунктов' +
              delta(c.compliance, p.compliance, признак('compliance')))}
      </div>

      ${card('Как распределились разговоры', 'по оценке тональности',
             `<div id="tone-bar"></div>
              <div class="row small dim" style="margin-top:10px;gap:16px">
                <span>отрицательных: <b>${num(c.negative)}</b></span>
                <span>нейтральных: <b>${num(c.neutral)}</b></span>
                <span>положительных: <b>${num(c.positive)}</b></span>
                ${c.mixed ? `<span title="и резкие, и тёплые реплики — средняя по ним ничего не говорит">из нейтральных противоречивых: <b>${num(c.mixed)}</b></span>` : ''}
              </div>`)}

      <div class="grid cols-4" style="margin-bottom:16px">
        ${kpi('Клиент раздражён', num(c.frustrated),
              c.frustrated_share === null ? 'по репликам клиента'
                : `${num(c.frustrated_share, 1)}% записей — «сколько можно», «позовите руководителя»`)}
        ${kpi('Повторные обращения', num(c.repeat),
              c.repeat_share === null ? 'по репликам клиента'
                : `${num(c.repeat_share, 1)}% записей — «уже звонил», «до сих пор не»`)}
        ${kpi('Противоречивых', num(c.mixed),
              'и резкие, и тёплые реплики в одном разговоре')}
        ${kpi('Мат у сотрудника',
              c.profanity_checked ? num(c.profanity_agent_records) : '—',
              c.profanity_checked
                ? `в разговоре вообще: ${num(c.profanity_records)} из ${num(c.profanity_checked)} проверенных`
                : 'словарь выключен — настройка content_profanity')}
      </div>

      ${(выводы.items || []).length ? card('Выводы',
        'правила с порогами, а не пересказ цифр — каждый вывод называет числа, из которых сделан',
        `<div class="findings">${(выводы.items || []).map((в) => `
          <div class="finding ${esc(в.severity)}">${esc(в.text)}</div>`).join('')}</div>`) : ''}

      <div class="grid cols-3">
        ${card('Тональность во времени', 'форма важнее среднего',
               '<div id="chart-tone-time"></div>')}
        ${card('Доля отрицательных', 'проценты по тем же корзинам времени',
               '<div id="chart-neg-time"></div>')}
        ${card('Сколько записей', 'чтобы видеть, на чём посчитаны две соседние кривые',
               '<div id="chart-vol-time"></div>')}
      </div>

      ${c.mono ? `<div class="finding info" style="margin-bottom:14px">
        В ${num(c.mono_share, 1)} % записей периода (${num(c.mono)} из ${num(c.records)})
        говорящий не разделён. Перебивания, монологи, разрез по операторам и ход
        тональности по ним не считаются — ноль в этих строках означает «нечем
        считать», а не «этого не было». Включается настройкой диаризации.
      </div>` : ''}

      ${card('Речь и разговор', 'усреднённые характеристики записей периода',
             `<div class="table-wrap full"><table>
               <thead><tr><th>Показатель</th><th class="num">За период</th>
                 <th class="num">Прошлый период</th><th class="num">Изменение</th></tr></thead>
               <tbody>${(свод.features || []).map((f) => {
                 const a = c[f.key], b = p[f.key];
                 if (a === null || a === undefined) return '';
                 let d = (b === null || b === undefined) ? null : a - b;
                 // Разница мельче показанной точности — это ноль, а не
                 // изменение: иначе столбец пестрит «−0» и «+0.000».
                 if (d !== null && Math.abs(d) < Math.pow(10, -f.digits) / 2) d = 0;
                 return `<tr><td${f.hint ? ` title="${esc(f.hint)}"` : ''}>${esc(f.title)}${f.unit ? ` <span class="faint small">${esc(f.unit)}</span>` : ''}${
                   f.hint ? `<div class="faint small">${esc(f.hint)}</div>` : ''}</td>
                   <td class="num mono">${num(a, f.digits)}</td>
                   <td class="num mono faint">${b === null || b === undefined ? '—' : num(b, f.digits)}</td>
                   <td class="num mono ${d && f.good ? (Math.sign(d) * f.good > 0 ? 'ok-text' : 'err-text') : ''}">${
                     d === null ? '—' : d === 0 ? 'без изменений'
                       : (d > 0 ? '+' : '') + num(d, f.digits)}</td></tr>`;
               }).join('')}</tbody></table></div>`)}`;

    toneBar(qs('#tone-bar'), c);
    const точки = (лента.buckets || []).filter((т) => т.records);
    const метки = точки.map((т) => fmtTime(т.ts).slice(0, 5));
    window.Charts.line(qs('#chart-tone-time'), {
      height: 220, labels: метки, yMin: -1, yMax: 1,
      series: [{ name: 'тональность', values: точки.map((т) => т.sentiment) }],
      emptyText: 'нет данных за период',
    });
    // Проценты и штуки — на разных графиках. На одной оси это вторая шкала
    // в маскировке: кривые сходятся и расходятся не потому, что связаны, а
    // потому, что у них случайно похожие числа.
    window.Charts.line(qs('#chart-neg-time'), {
      height: 220, labels: метки, yMin: 0, unit: '%',
      series: [{ name: 'доля отрицательных, %',
                 values: точки.map((т) => т.negative_share) }],
      emptyText: 'нет данных за период',
    });
    window.Charts.bars(qs('#chart-vol-time'), {
      height: 220, values: точки.map((т) => т.records), labels: метки,
      emptyText: 'нет данных за период',
    });
  },

  // --- разрезы -------------------------------------------------------------

  async tab_groups(host) {
    const период = state.contentPeriod;
    const разрезы = ['owner', 'speaker', 'tag', 'model', 'source', 'weekday', 'hour'];
    const данные = await Promise.all(разрезы.map((d) =>
      API.latest(`content-b-${d}`, `/api/content/breakdown/${d}?period=${период}`)
        .catch(() => ({ items: [] }))));
    const по_ключу = Object.fromEntries(разрезы.map((d, i) => [d, данные[i]]));

    const таблица = (данные) => {
      const items = данные.items || [];
      if (!items.length) return '<div class="empty small">Групп с достаточным числом записей нет</div>';
      return `<div class="table-wrap full"><table>
        <thead><tr><th>${esc(данные.title)}</th><th class="num">Записей</th>
          <th class="num">Тональность</th><th class="num">Отрицательных</th>
          <th class="num">Тревожных</th><th class="num">Скрипт</th>
          <th class="num">Темп</th><th class="num">Перебиваний</th>
          <th class="num">Тишина</th><th class="num">Обещаний без срока</th>
          <th class="num" title="доля времени речи оператора; ориентир 40–60 %">Речь опер.</th>
          <th class="num" title="средний самый долгий монолог оператора, секунд">Монолог</th></tr></thead>
        <tbody>${items.map((г) => `<tr>
          <td>${esc(г.label)}</td>
          <td class="num mono">${num(г.records)}</td>
          <td class="num">${toneChip(г.sentiment)}</td>
          <td class="num mono">${г.negative_share === null ? '—'
            : num(г.negative_share, 1) + '%'}</td>
          <td class="num mono">${num(г.alert_records)}</td>
          <td class="num mono">${г.compliance === null ? '—' : pct(г.compliance, 0)}</td>
          <td class="num mono">${num(г.wpm, 0)}</td>
          <td class="num mono">${num(г.interruptions, 1)}</td>
          <td class="num mono">${г.silence_share === null ? '—' : pct(г.silence_share, 0)}</td>
          <td class="num mono">${num(г.commitments_open)}</td>
          <td class="num mono">${г.talk_share === null || г.talk_share === undefined ? '—' : pct(г.talk_share, 0)}</td>
          <td class="num mono">${г.monologue_s === null || г.monologue_s === undefined ? '—' : num(г.monologue_s, 0) + ' с'}</td></tr>`).join('')}</tbody></table>
        ${данные.hidden ? `<p class="small faint" style="margin:8px 12px">
          Скрыто групп с числом записей меньше пяти: ${данные.hidden}. На двух
          разговорах группа всегда либо лучшая, либо худшая, и оба раза
          это ничего не значит.</p>` : ''}</div>`;
    };

    host.innerHTML = `
      <div class="grid cols-2">
        ${card('Тональность по владельцам', 'сравнение с общим средним',
               '<div id="chart-by-owner"></div>')}
        ${card('Тональность по меткам', 'на что жалуются и о чём договариваются',
               '<div id="chart-by-tag"></div>')}
      </div>
      <div class="grid cols-2">
        ${card('По часам суток', 'когда разговоры даются тяжелее',
               '<div id="chart-by-hour"></div>')}
        ${card('По дням недели', '',
               '<div id="chart-by-weekday"></div>')}
      </div>
      ${card('По владельцам', 'все показатели рядом', таблица(по_ключу.owner))}
      ${card('По операторам',
             'кто из говорящих вёл разговор; записи без разделения по говорящим ' +
             'попадают в одну группу «—»', таблица(по_ключу.speaker))}
      ${card('По меткам', 'метка задания', таблица(по_ключу.tag))}
      <div class="grid cols-2">
        ${card('По моделям', 'разбор зависит от качества расшифровки', таблица(по_ключу.model))}
        ${card('По источникам', '', таблица(по_ключу.source))}
      </div>`;

    // Тональность — величина со знаком, поэтому столбцы рисуются от нуля
    // посередине: цвет и сторона сразу показывают, кто ушёл в минус.
    const столбики = (узел, данные, поле) => {
      const items = (данные.items || []).filter((г) => г[поле] !== null &&
                                                       г[поле] !== undefined);
      window.Charts.hbars(qs(узел), {
        diverging: true, absMin: 0.5, labelWidth: 130, emptyText: 'нет групп',
        items: items.map((г) => ({
          label: г.label, value: г[поле],
          display: num(г[поле], 2),
          note: `${num(г.records)} записей` + (г.negative_share === null ? ''
            : ` · отрицательных ${num(г.negative_share, 1)} %`),
        })),
      });
    };
    столбики('#chart-by-owner', по_ключу.owner, 'sentiment');
    столбики('#chart-by-tag', по_ключу.tag, 'sentiment');
    const часы = (по_ключу.hour.items || []);
    window.Charts.line(qs('#chart-by-hour'), {
      height: 220, labels: часы.map((г) => г.label), yMin: -1, yMax: 1,
      series: [{ name: 'тональность', values: часы.map((г) => г.sentiment) }],
      emptyText: 'нет данных',
    });
    const дни = (по_ключу.weekday.items || []);
    window.Charts.bars(qs('#chart-by-weekday'), {
      height: 220, values: дни.map((г) => г.records),
      labels: дни.map((г) => г.label.slice(0, 2)),
      emptyText: 'нет данных',
    });
  },

  // --- темы ----------------------------------------------------------------

  async tab_topics(host) {
    const период = state.contentPeriod;
    const данные = await API.latest('content-topics',
      `/api/content/topics?period=${период}`);
    const темы = данные.items || [];
    const тренд = данные.trend || [];
    if (!темы.length) {
      host.innerHTML = `<div class="empty">Тем пока нет: ни одна основа не
        встретилась в двух записях. Появятся, когда разберётся архив.</div>`;
      return;
    }
    host.innerHTML = `
      ${card('О чём говорят', `по ${num(данные.corpus)} разобранным записям периода`,
             '<div id="chart-topics"></div>')}
      <div class="grid cols-2">
        ${card('Стали звучать чаще', 'сравнение с предыдущим таким же периодом',
               `<div class="table-wrap"><table>
                 <thead><tr><th>Тема</th><th class="num">Сейчас</th>
                   <th class="num">Было</th><th class="num">Изменение</th></tr></thead>
                 <tbody>${тренд.filter((т) => т.delta > 0).slice(0, 12).map((т) => `<tr>
                   <td>${esc(т.word)}</td>
                   <td class="num mono">${num(т.share_now, 1)}%</td>
                   <td class="num mono faint">${num(т.share_before, 1)}%</td>
                   <td class="num mono ok-text">+${num(т.delta, 1)} п.п.</td></tr>`).join('')
                 || '<tr><td colspan="4" class="small dim">Заметного роста нет</td></tr>'}
                 </tbody></table></div>`)}
        ${card('Стали звучать реже', '',
               `<div class="table-wrap"><table>
                 <thead><tr><th>Тема</th><th class="num">Сейчас</th>
                   <th class="num">Было</th><th class="num">Изменение</th></tr></thead>
                 <tbody>${тренд.filter((т) => т.delta < 0).slice(0, 12).map((т) => `<tr>
                   <td>${esc(т.word)}</td>
                   <td class="num mono">${num(т.share_now, 1)}%</td>
                   <td class="num mono faint">${num(т.share_before, 1)}%</td>
                   <td class="num mono err-text">${num(т.delta, 1)} п.п.</td></tr>`).join('')
                 || '<tr><td colspan="4" class="small dim">Заметного спада нет</td></tr>'}
                 </tbody></table></div>`)}
      </div>
      ${(данные.new || []).length ? card('Новые слова периода',
             'в прошлом таком же периоде не звучали вовсе — новая акция, новый сбой, новая модель; сырьё для категорий',
             `<div class="chips">${(данные.new || []).map((т) =>
               `<span class="chip" title="в ${т.now} записях; найти в результатах" data-search="${esc(т.word)}"
                      style="cursor:pointer">${esc(т.word)} <span class="faint">${num(т.now)}</span></span>`).join('')}</div>`) : ''}
      ${card('Все темы периода', 'вес — редкость темы: слово из девяти записей ' +
             'из десяти это фон, а не тема',
             `<div class="table-wrap full"><table>
               <thead><tr><th>Тема</th><th class="num">Записей</th><th class="num">Доля</th>
                 <th class="num">Упоминаний</th><th class="num">Вес</th>
                 <th>Найти</th></tr></thead>
               <tbody>${темы.map((т) => `<tr>
                 <td>${esc(т.word)}<span class="faint small mono"> ${esc(т.stem)}</span></td>
                 <td class="num mono">${num(т.records)}</td>
                 <td class="num mono">${num(т.share, 1)}%</td>
                 <td class="num mono">${num(т.mentions)}</td>
                 <td class="num mono">${num(т.weight, 2)}</td>
                 <td><button class="ghost sm" data-search="${esc(т.word)}"
                       title="Найти эти разговоры в результатах">записи</button></td>
                 </tr>`).join('')}</tbody></table></div>`)}`;

    // Все столбцы — одна и та же величина (в скольких записях встретилось
    // слово), поэтому и цвет один: разные цвета читались бы как разные
    // виды тем, которых нет.
    const цвет = window.Charts.palette()[0];
    window.Charts.hbars(qs('#chart-topics'), {
      items: темы.slice(0, 18).map((т) => ({
        label: т.word, value: т.records, color: цвет,
        display: `${num(т.records)} · ${num(т.share, 1)} %`,
        note: `упоминаний: ${num(т.mentions)}` })),
      labelWidth: 150, emptyText: 'нет тем',
    });
    qsa('#content-body [data-search]').forEach((b) =>
      b.addEventListener('click', () => {
        state.resultsSearch = b.dataset.search;
        go('results');
      }));
  },

  // --- связи ---------------------------------------------------------------

  async tab_links(host) {
    const данные = await API.latest('content-corr',
      `/api/content/correlations?period=${state.contentPeriod}`);
    const items = данные.items || [];
    host.innerHTML = card('Связи между признаками',
      `посчитано по ${num(данные.sampled)} записям`,
      items.length ? `<div class="table-wrap full"><table>
        <thead><tr><th>Наблюдение</th>
          <th class="num" title="Знак — направление связи, а не оценка: «чем длиннее разговор, тем меньше перебиваний» — тоже минус">Коэффициент</th>
          <th>Сила</th><th class="num">Записей</th></tr></thead>
        <tbody>${items.map((с) => `<tr>
          <td>${esc(с.text)}<div class="small faint">${esc(с.x_title)} ↔ ${esc(с.y_title)}</div></td>
          <td class="num mono">${с.r > 0 ? '+' : ''}${num(с.r, 2)}</td>
          <td><span class="chip">${esc(с.strength)}</span></td>
          <td class="num mono">${num(с.n)}</td></tr>`).join('')}</tbody></table></div>
        <p class="small dim" style="margin:12px">${esc(данные.note)}</p>`
      : `<div class="empty">Заметных связей за период не нашлось.<br>
         <span class="small">Пары со слабой связью (меньше 0,2) и посчитанные
         меньше чем по пятидесяти записям не показываются: отличить такую
         цифру от случайности нельзя, а прочитана она будет как факт.</span></div>`);
  },

  // --- что послушать -------------------------------------------------------

  async tab_records(host) {
    const период = state.contentPeriod;
    const перечень = await API.latest('content-kinds', '/api/content/kinds');
    const виды = перечень.kinds || [];
    state.contentKind = state.contentKind && виды.some((к) => к.key === state.contentKind)
      ? state.contentKind : (виды[0] || {}).key;
    host.innerHTML = `
      <div class="settings-toolbar">
        <span class="small dim">Отбор:</span>
        <div class="group-nav" id="content-kind">
          ${виды.map((к) => `<button data-kind="${esc(к.key)}"
            class="${state.contentKind === к.key ? 'active' : ''}">${esc(к.title)}</button>`).join('')}
        </div>
      </div>
      <div id="content-records"><div class="empty">Загрузка…</div></div>`;
    qsa('#content-kind button').forEach((b) => b.addEventListener('click', () => {
      state.contentKind = b.dataset.kind;
      qsa('#content-kind button').forEach((x) =>
        x.classList.toggle('active', x.dataset.kind === state.contentKind));
      this.loadRecords();
    }));
    return this.loadRecords();
  },

  async loadRecords() {
    const host = qs('#content-records');
    if (!host) return;
    const данные = await API.latest('content-records',
      `/api/content/records?kind=${encodeURIComponent(state.contentKind)}` +
      `&period=${state.contentPeriod}&limit=50`);
    const items = данные.items || [];
    // Отбор показывает верхние пятьдесят, а «Результаты» — весь список с
    // поиском и листалкой. Ключи отборов там те же, поэтому переход
    // сохраняет выбор, а не сбрасывает его на «все записи».
    const весь = ОТБОР_В_РЕЗУЛЬТАТЫ[state.contentKind];
    const действия = весь
      ? `<button class="ghost sm" id="content-all"
           title="Открыть тот же отбор в разделе «Результаты» — с поиском и листалкой"
           >Все такие записи</button>` : '';
    host.innerHTML = card(данные.title, `${items.length} записей`,
      items.length ? `<div class="table-wrap full"><table>
        <thead><tr><th>Запись</th><th>Когда</th><th>Владелец</th>
          <th class="num">Тональность</th><th class="num">Разворот</th>
          <th class="num">Тревожных</th><th class="num">Обещаний</th>
          <th class="num">Перебиваний</th><th class="num">Скрипт</th>
          <th class="num">Длит.</th><th></th></tr></thead>
        <tbody>${items.map((з) => `<tr>
          <td class="truncate" style="max-width:240px">${esc(з.filename || з.job_id)}</td>
          <td class="small faint nowrap">${fmtTime(з.created_at)}</td>
          <td class="small">${esc(з.owner || '—')}</td>
          <td class="num">${toneChip(з.sentiment, з.sentiment_label)}</td>
          <td class="num mono">${з.sentiment_shift === null ? '—' : num(з.sentiment_shift, 2)}</td>
          <td class="num mono">${num(з.alerts)}</td>
          <td class="num mono">${num(з.commitments)}${
            з.commitments > з.commitments_dated
              ? `<span class="faint"> (${num(з.commitments - з.commitments_dated)} без срока)</span>` : ''}</td>
          <td class="num mono">${num(з.interruptions)}</td>
          <td class="num mono">${з.compliance === null ? '—' : pct(з.compliance, 0)}</td>
          <td class="num mono nowrap">${fmtDur(з.media_duration_s)}</td>
          <td><button class="ghost sm" onclick="__asrhub.openJob('${esc(з.job_id)}')"
                >Открыть</button></td></tr>`).join('')}</tbody></table></div>`
      : '<div class="empty">По этому отбору записей за период нет</div>', действия);
    const кнопка = qs('#content-all');
    if (кнопка) кнопка.addEventListener('click', () => {
      state.resultsContent = весь;
      go('results');
    });
  },
};

/* Категории обращений: счёт за период и редактор правил.
 *
 * Вкладка отвечает на вопрос «о чём звонят» — по правилам, которые тут же
 * и правят. Верхняя таблица считается по сохранённому набору, редактор
 * ниже держит черновик; проверка на записи гоняет черновик, ничего не
 * сохраняя. Так у человека перед глазами и результат набора на архиве,
 * и то, как правило сработает на одной настоящей записи, — второе без
 * первого рождает категории, покрывающие весь архив, первое без второго
 * — категории, не совпадающие ни с чем.
 */

RENDERERS.content.tab_categories = async function (host) {
  const период = state.contentPeriod;
  const [свод, перечни, перечень] = await Promise.all([
    API.latest('content-categories', `/api/content/categories?period=${период}`),
    API.latest('content-kinds', '/api/content/kinds'),
    API.latest('content-script-jobs',
      '/api/jobs?status=completed&limit=25&light=true&order=created_at DESC'),
  ]);
  state.contentCategoriesOwn = !!перечни.categories_own;
  state.contentCategoriesReady = перечни.default_categories || [];
  // Черновик заводится один раз на заход в раздел: смена периода
  // перерисовывает вкладку, и терять при этом полчаса правки нельзя.
  if (!state.contentCategories) {
    state.contentCategories = JSON.parse(JSON.stringify(
      (перечни.categories || []).map((к) => ({
        id: к.id, label: к.label, rule: к.rule, kind: к.kind, who: к.who,
        where: к.where, within_s: к.within_s || undefined, notify: !!к.notify,
        penalty: к.penalty || 0, weight: к.weight === undefined ? 1 : к.weight }))));
    state.contentCategoriesDirty = false;
  }
  state.contentScriptJobs = (перечень.items || []);
  state.contentScriptJob = state.contentScriptJob ||
    (state.contentScriptJobs[0] || {}).id || '';
  this.drawCategories(host, свод);
  if (state.contentScriptJob) this.checkCategories();
};

RENDERERS.content.drawCategories = function (host, свод) {
  const набор = state.contentCategories || [];
  const свой = state.contentCategoriesOwn;
  const items = свод.items || [];
  const без = свод.uncategorized || {};
  const растут = new Set(свод.rising || []);
  const угасают = new Set(свод.fading || []);
  const сКатегорией = свод.corpus && без.records !== undefined
    ? свод.corpus - без.records : null;
  host.innerHTML = `
    <div class="settings-toolbar">
      <span class="small dim">Проверить на записи:</span>
      <select id="cat-job" style="width:320px">
        ${state.contentScriptJobs.map((j) => `<option value="${esc(j.id)}"
          ${j.id === state.contentScriptJob ? 'selected' : ''}>${
          esc(j.filename || j.id)}</option>`).join('') ||
          '<option value="">завершённых записей пока нет</option>'}
      </select>
      <span class="spacer"></span>
      ${свой ? `<button class="ghost sm" id="cat-default"
        title="Вернуться к готовому набору из десяти категорий">Вернуть готовый набор</button>` : ''}
      <select id="cat-ready" class="sm" style="width:200px" title="Добавить готовую категорию в набор">
        <option value="">Добавить готовую…</option>
        ${(state.contentCategoriesReady || [])
          .filter((г) => !набор.some((к) => к.id === г.id))
          .map((г) => `<option value="${esc(г.id)}">${esc(г.label)}</option>`).join('')}
      </select>
      <button class="btn sm" id="cat-add">Добавить категорию</button>
      <button class="primary sm" id="cat-save" ${state.contentCategoriesDirty ? '' : 'disabled'}>Сохранить</button>
    </div>

    ${свой ? '' : `<div class="finding info" style="margin-bottom:14px">
      Сейчас действует готовый набор — ${items.length} категорий, с которыми
      сталкивается почти любая служба поддержки. Сохранение делает набор вашим:
      с этого момента сервер размечает записи только по тому, что здесь написано.
    </div>`}

    <div class="grid cols-4" style="margin-bottom:14px">
      ${kpi('Записей с категорией', сКатегорией === null ? '—' : num(сКатегорией),
            свод.corpus ? `из ${num(свод.corpus)} разобранных за период` : 'за период записей нет')}
      ${kpi('Без категории', без.records === undefined ? '—' : num(без.records),
            без.share === undefined || без.share === null ? '' :
              `${num(без.share, 1)} % — довод завести новую категорию`)}
      ${kpi('Растут', num(растут.size), 'доля выросла на 5 п.п. и больше')}
      ${kpi('Угасают', num(угасают.size), 'доля упала на 5 п.п. и больше')}
    </div>

    ${card('Категории за период',
           'доля — от разобранных записей периода; изменение — к прошлому такому же периоду',
           items.length ? `<div class="table-wrap full"><table id="cat-table">
        <thead><tr><th>Категория</th><th>Вид</th><th>Кто</th>
          <th class="num">Записей</th><th class="num">Доля</th>
          <th class="num" title="записей в прошлом периоде">Было</th>
          <th class="num" title="изменение доли, процентных пунктов">Изменение</th>
          <th class="num" title="совпадений всего, по всем записям">Упоминаний</th>
          <th></th></tr></thead>
        <tbody>${items.map((к) => `<tr class="${к.error ? 'faint' : ''}">
          <td><b>${esc(к.label)}</b>${к.stale ? ' <span class="chip">нет в наборе</span>' : ''}${
            к.error ? ` <span class="chip err" title="${esc(к.error)}">ошибка правила</span>` : ''}${
            растут.has(к.id) ? ' <span class="chip warn">растёт</span>' : ''}${
            угасают.has(к.id) ? ' <span class="chip">угасает</span>' : ''}</td>
          <td class="small"><span class="chip ${КАТЕГОРИЯ_ЦВЕТ[к.kind] || ''}">${
            esc(КАТЕГОРИЯ_ВИД[к.kind] || к.kind)}</span></td>
          <td class="small dim">${esc(КАТЕГОРИЯ_КТО[к.who] || к.who || '—')}</td>
          <td class="num mono">${num(к.records)}</td>
          <td class="num mono">${к.share === null || к.share === undefined ? '—' : `${num(к.share, 1)} %`}</td>
          <td class="num mono faint">${num(к.previous)}</td>
          <td class="num mono">${к.delta === null || к.delta === undefined ? '—' :
            `<span class="${к.delta > 0 ? (к.kind === 'violation' ? 'err' : '') : ''}">${
              к.delta > 0 ? '+' : ''}${num(к.delta, 1)}</span>`}</td>
          <td class="num mono">${num(к.mentions)}</td>
          <td>${к.records ? `<button class="ghost sm" data-category="${esc(к.id)}"
                title="Все записи этой категории в разделе «Результаты»">Записи</button>` : ''}</td>
        </tr>`).join('')}</tbody></table></div>`
           : '<div class="empty small">За период разобранных записей нет</div>')}

    ${card('Набор категорий',
           'правило: слова и фразы с И, ИЛИ, НЕ, РЯДОМ(N) и скобками; без кавычек — по основам ' +
           '(«уточнить» найдёт «уточню»), в кавычках — точно. Операторы — заглавными',
           '<div class="script-list" id="cat-list"></div>')}

    ${card('Что нашлось в выбранной записи',
           'правило, совпадающее с частым словом, покрывает весь архив — здесь это видно сразу',
           '<div id="cat-check"><div class="empty small">Выберите запись</div></div>')}`;

  const список = qs('#cat-list', host);
  const тронуто = () => {
    state.contentCategoriesDirty = true;
    qs('#cat-save').disabled = false;
    clearTimeout(state.contentScriptTimer);
    state.contentScriptTimer = setTimeout(() => RENDERERS.content.checkCategories(), 500);
  };
  const рисовать = () => {
    список.innerHTML = набор.map((к, i) => `
      <div class="script-item" data-index="${i}">
        <div class="row" style="gap:8px">
          <input type="text" class="cat-title" value="${esc(к.label || '')}"
                 placeholder="Название категории" style="flex:1">
          <select class="cat-kind" style="width:190px" title="Что означает срабатывание">
            ${Object.entries(КАТЕГОРИЯ_ВИД).map(([v, t]) =>
              `<option value="${v}" ${(к.kind || 'topic') === v ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
          <select class="cat-who" style="width:130px" title="Чьи реплики смотреть">
            ${Object.entries(КАТЕГОРИЯ_КТО).map(([v, t]) =>
              `<option value="${v}" ${(к.who || 'any') === v ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
          <select class="cat-where" style="width:170px">
            ${Object.entries(ГДЕ_ИСКАТЬ).map(([v, t]) =>
              `<option value="${v}" ${(к.where || 'any') === v ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
          <input type="number" class="cat-within" min="0" step="5" style="width:92px"
                 value="${к.within_s ? esc(String(к.within_s)) : ''}"
                 placeholder="секунд" ${(к.where || 'any') === 'any' ? 'hidden' : ''}
                 title="Окно в секундах от начала или до конца записи; пусто — пятая часть реплик">
          <button class="ghost icon cat-del" title="Убрать категорию">✕</button>
        </div>
        <input type="text" class="cat-rule mono" style="margin-top:6px"
               value="${esc(к.rule || '')}"
               placeholder='правило: оплата ИЛИ платёж ИЛИ "не прошла оплата"'>
        <div class="script-hit small" data-hit="${esc(к.id || '')}"></div>
      </div>`).join('') ||
      '<div class="empty small">Категорий нет: записи размечаться не будут</div>';
    qsa('.script-item', список).forEach((узел) => {
      const i = Number(узел.dataset.index);
      const менять = () => {
        набор[i].label = qs('.cat-title', узел).value.trim();
        набор[i].kind = qs('.cat-kind', узел).value;
        набор[i].who = qs('.cat-who', узел).value;
        набор[i].where = qs('.cat-where', узел).value;
        const окно = qs('.cat-within', узел);
        окно.hidden = набор[i].where === 'any';
        const секунд = Number(окно.value);
        if (секунд > 0 && набор[i].where !== 'any') набор[i].within_s = секунд;
        else delete набор[i].within_s;
        набор[i].rule = qs('.cat-rule', узел).value.trim();
        тронуто();
      };
      qsa('input, select', узел).forEach((поле) => {
        поле.addEventListener('change', менять);
        if (поле.tagName === 'INPUT') поле.addEventListener('input', менять);
      });
      qs('.cat-del', узел).addEventListener('click', () => {
        набор.splice(i, 1);
        тронуто();
        рисовать();
      });
    });
  };
  рисовать();

  qs('#cat-job').addEventListener('change', (e) => {
    state.contentScriptJob = e.target.value;
    this.checkCategories();
  });
  qs('#cat-add').addEventListener('click', () => {
    набор.push({ id: `к${Date.now().toString(36)}`, label: '', rule: '',
                 kind: 'topic', who: 'any', where: 'any' });
    тронуто();
    рисовать();
    const последний = qs('.script-item:last-child .cat-title', список);
    if (последний) последний.focus();
  });
  qs('#cat-ready').addEventListener('change', (e) => {
    const готовая = (state.contentCategoriesReady || []).find((г) => г.id === e.target.value);
    if (!готовая) return;
    набор.push(JSON.parse(JSON.stringify(готовая)));
    e.target.value = '';
    e.target.querySelector(`option[value="${CSS.escape(готовая.id)}"]`).remove();
    тронуто();
    рисовать();
  });
  const вернуть = qs('#cat-default');
  if (вернуть) вернуть.addEventListener('click', () => this.saveCategories([]));
  qs('#cat-save').addEventListener('click', () => this.saveCategories(набор));
  qsa('[data-category]', host).forEach((b) => b.addEventListener('click', () => {
    state.resultsContent = `category:${b.dataset.category}`;
    go('results');
  }));
};

/** Прогоняет черновик набора по выбранной записи и показывает, что нашлось. */
RENDERERS.content.checkCategories = async function () {
  const host = qs('#cat-check');
  if (!host || !state.contentScriptJob) return;
  let ответ;
  try {
    ответ = await API.post('/api/content/categories/check', {
      job_id: state.contentScriptJob,
      categories: state.contentCategories || [],
    });
  } catch (err) {
    host.innerHTML = `<div class="empty small">Проверить не удалось: ${esc(err.message)}</div>`;
    return;
  }
  const итог = ответ.result || {};
  const items = итог.items || [];
  // Ошибки правил — под самими правилами в редакторе, там их и правят.
  qsa('#cat-list .script-item').forEach((узел, i) => {
    const к = items[i] || {};
    const строка = qs('.script-hit', узел);
    if (!строка) return;
    строка.textContent = к.error ? `Ошибка правила: ${к.error}`
      : (к.count ? `В выбранной записи: ${к.count} совп., первое на ${fmtDur(к.first_s || 0)}` : '');
    строка.classList.toggle('err', !!к.error);
  });
  host.innerHTML = `
    <div class="row small dim" style="margin-bottom:10px">
      <span>Сработало категорий: <b>${(итог.matched || []).length}</b> из ${итог.checked || 0}${
        итог.errors ? `, с ошибкой правила: ${итог.errors}` : ''}</span>
      <span class="spacer"></span>
      <span>${ответ.agent ? `оператор — «${esc(ответ.agent)}»${
        ответ.customer ? `, клиент — «${esc(ответ.customer)}»` : ''}`
        : 'стороны не определены — «только оператор» и «только клиент» ищут по всей записи'}</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>Категория</th><th class="num">Совпадений</th><th>Первое</th><th>Что нашли</th></tr></thead>
      <tbody>${items.map((к) => `<tr>
        <td>${к.error ? '<span class="chip err">ошибка</span>' : к.count
          ? '<span class="chip ok">есть</span>' : '<span class="chip">нет</span>'}</td>
        <td>${esc(к.label || '—')} <span class="faint small">${esc(КАТЕГОРИЯ_ВИД[к.kind] || '')}</span></td>
        <td class="num mono">${к.error ? '—' : num(к.count)}</td>
        <td class="mono small">${к.first_s === null || к.first_s === undefined ? '—' : fmtDur(к.first_s)}</td>
        <td class="small">${к.error ? `<span class="err">${esc(к.error)}</span>`
          : (к.hits || []).slice(0, 3).map((h) =>
              `<div><b>${esc(h.matched)}</b> — <span class="dim">${esc(h.speaker || '—')}:</span> ${esc(h.text)}</div>`
            ).join('') || '—'}</td></tr>`).join('')}</tbody></table></div>
    ${(ответ.suspicious || []).length ? `<div class="finding warning" style="margin-top:12px">
      Операнды, совпадающие с частыми словами:
      ${(ответ.suspicious || []).map((с) =>
        `<b>${esc(с.word)}</b> (в категории «${esc(с.label)}»)`).join(', ')}.
      Такая категория покроет почти весь архив — уберите слово или уточните его фразой.
    </div>` : ''}
    ${(ответ.errors || []).length ? `<div class="finding warning" style="margin-top:12px">
      Набор не сохранится, пока есть ошибки: ${(ответ.errors || []).map(esc).join('; ')}
    </div>` : ''}`;
};

/** Сохраняет набор категорий и предлагает пересчитать архив. */
RENDERERS.content.saveCategories = async function (набор) {
  const плохие = набор.filter((к) => !к.label || !к.rule);
  if (плохие.length) {
    toast('У каждой категории должно быть название и правило', 'err');
    return;
  }
  try {
    await API.put('/api/settings', { content_categories: набор });
  } catch (err) { fail(err); return; }
  toast(набор.length ? 'Набор категорий сохранён' : 'Возвращён готовый набор', 'ok');
  if (confirm('Набор категорий изменился — пересчитать разбор архива?\n\n' +
              'Без пересчёта старые записи останутся размечены прежним набором, ' +
              'и счёт по категориям будет смешивать два набора.')) {
    try { await API.post('/api/content/recompute', {}); } catch (err) { fail(err); }
  }
  state.contentData = {};
  state.contentCategories = null;
  state.contentCategoriesDirty = false;
  this.showTab();
};

/* Редактор скрипта разговора.
 *
 * Скрипт — единственная настройка раздела, которую правят руками и не по
 * одному разу: у каждого он свой, и подбирается он итерациями. В общем
 * списке настроек он лежит как поле `json`, куда надо вписать массив
 * объектов, — то есть ровно та настройка, которую меняют чаще всего,
 * сделана хуже всех остальных.
 *
 * Главное здесь не форма, а проверка: примета «это» или «то есть»
 * совпадает с одним из самых частых слов языка и делает пункт выполненным
 * всегда. Понять это по списку слов нельзя — только увидев, на чём пункт
 * сработал в настоящей записи. Поэтому рядом с каждым пунктом стоит
 * результат проверки на выбранном разговоре.
 */

const ГДЕ_ИСКАТЬ = {
  start: 'в начале разговора',
  end: 'в конце разговора',
  any: 'в любом месте',
};

/** Подпись «где искали» с окном в секундах, если оно задано. */
function гдеИскали(п) {
  if (п.within_s && п.where === 'start') return `в первые ${п.within_s} с`;
  if (п.within_s && п.where === 'end') return `в последние ${п.within_s} с`;
  return ГДЕ_ИСКАТЬ[п.where] || п.where;
}

RENDERERS.content.tab_script = async function (host) {
  const [настройки, перечень, перечни] = await Promise.all([
    API.latest('content-settings', '/api/settings'),
    API.latest('content-script-jobs',
      '/api/jobs?status=completed&limit=25&light=true&order=created_at DESC'),
    API.latest('content-kinds', '/api/content/kinds'),
  ]);
  const свои = (настройки.values || {}).content_script;
  state.contentScriptOwn = Array.isArray(свои) && свои.length;
  // Показываем набор по умолчанию, когда своего нет: с пустым списком
  // человек не видит, что же сервер проверяет сейчас, и начинать правку
  // ему приходится с чистого листа. Своим набор становится в тот момент,
  // когда его сохранили, — до этого правки живут только на экране.
  state.contentScript = JSON.parse(JSON.stringify(
    state.contentScriptOwn ? свои : (перечни.default_script || [])));
  state.contentScriptJobs = (перечень.items || []);
  state.contentScriptJob = state.contentScriptJob ||
    (state.contentScriptJobs[0] || {}).id || '';
  this.drawScript(host);
  if (state.contentScriptJob) this.checkScript();
};

RENDERERS.content.drawScript = function (host) {
  const пункты = state.contentScript || [];
  const свой = state.contentScriptOwn;
  host.innerHTML = `
    <div class="settings-toolbar">
      <span class="small dim">Проверить на записи:</span>
      <select id="script-job" style="width:320px">
        ${state.contentScriptJobs.map((j) => `<option value="${esc(j.id)}"
          ${j.id === state.contentScriptJob ? 'selected' : ''}>${
          esc(j.filename || j.id)}</option>`).join('') ||
          '<option value="">завершённых записей пока нет</option>'}
      </select>
      <span class="spacer"></span>
      ${свой ? `<button class="ghost sm" id="script-default"
        title="Вернуться к набору по умолчанию">Вернуть набор по умолчанию</button>` : ''}
      <button class="btn sm" id="script-add">Добавить пункт</button>
      <button class="primary sm" id="script-save" disabled>Сохранить</button>
    </div>

    ${свой ? '' : `<div class="finding info" style="margin-bottom:14px">
      Сейчас действует набор по умолчанию — ${пункты.length} пунктов, которые
      спрашивают почти в любой службе поддержки. Он показан ниже целиком.
      Сохранение делает скрипт вашим: с этого момента сервер проверяет только
      то, что здесь написано, и обновления сервера этот список больше не трогают.
    </div>`}

    ${card('Пункты скрипта',
           'слова-приметы задавайте в начальной форме: сравнение идёт по основам, ' +
           'и «уточнить» найдёт «уточню» и «уточнили»',
           `<div class="script-list" id="script-list"></div>`)}

    ${card('Что нашлось в выбранной записи',
           'примета, совпадающая с частым словом, делает пункт выполненным всегда — ' +
           'здесь это видно сразу',
           '<div id="script-check"><div class="empty small">Выберите запись</div></div>')}`;

  const список = qs('#script-list', host);
  const рисовать = () => {
    список.innerHTML = пункты.map((п, i) => `
      <div class="script-item" data-index="${i}">
        <div class="row" style="gap:8px">
          <input type="text" class="script-title" value="${esc(п.label || '')}"
                 placeholder="Как называть пункт в отчёте" style="flex:1">
          <select class="script-where" style="width:190px">
            ${Object.entries(ГДЕ_ИСКАТЬ).map(([k, v]) =>
              `<option value="${k}" ${(п.where || 'any') === k ? 'selected' : ''}>${v}</option>`
            ).join('')}
          </select>
          <input type="number" class="script-within" min="0" step="5" style="width:92px"
                 value="${п.within_s ? esc(String(п.within_s)) : ''}"
                 placeholder="секунд" ${(п.where || 'any') === 'any' ? 'hidden' : ''}
                 title="Окно в секундах от начала или до конца записи. Пусто — пятая часть реплик, как раньше. «Разговор записывается» обязано прозвучать в первые 30 секунд — это и есть такое окно">
          <button class="ghost icon script-del" title="Убрать пункт">✕</button>
        </div>
        <input type="text" class="script-any" style="margin-top:6px"
               value="${esc((п.any || []).join(', '))}"
               placeholder="слова-приметы через запятую: здравствуйте, добрый день">
        <div class="script-hit small" data-hit="${esc(п.id || '')}"></div>
      </div>`).join('') ||
      '<div class="empty small">Пунктов нет: сервер проверять не будет ничего</div>';
    qsa('.script-item', список).forEach((узел) => {
      const i = Number(узел.dataset.index);
      const менять = () => {
        пункты[i].label = qs('.script-title', узел).value.trim();
        пункты[i].where = qs('.script-where', узел).value;
        const окно = qs('.script-within', узел);
        окно.hidden = пункты[i].where === 'any';
        const секунд = Number(окно.value);
        if (секунд > 0 && пункты[i].where !== 'any') пункты[i].within_s = секунд;
        else delete пункты[i].within_s;
        пункты[i].any = qs('.script-any', узел).value
          .split(',').map((w) => w.trim()).filter(Boolean);
        qs('#script-save').disabled = false;
        clearTimeout(state.contentScriptTimer);
        state.contentScriptTimer = setTimeout(() => RENDERERS.content.checkScript(), 500);
      };
      qsa('input, select', узел).forEach((поле) => poleOn(поле, менять));
      qs('.script-del', узел).addEventListener('click', () => {
        пункты.splice(i, 1);
        qs('#script-save').disabled = false;
        рисовать();
        RENDERERS.content.checkScript();
      });
    });
  };
  const poleOn = (поле, fn) => {
    поле.addEventListener('change', fn);
    if (поле.tagName === 'INPUT') поле.addEventListener('input', fn);
  };
  state.contentScriptRedraw = рисовать;
  рисовать();

  qs('#script-job').addEventListener('change', (e) => {
    state.contentScriptJob = e.target.value;
    this.checkScript();
  });
  qs('#script-add').addEventListener('click', () => {
    state.contentScript = state.contentScript || [];
    state.contentScript.push({ id: `п${Date.now().toString(36)}`,
                               label: '', any: [], where: 'any' });
    qs('#script-save').disabled = false;
    this.showTab();
  });
  const вернуть = qs('#script-default');
  if (вернуть) вернуть.addEventListener('click', () => this.saveScript([]));
  qs('#script-save').addEventListener('click', () =>
    this.saveScript(state.contentScript || []));
};

/** Прогоняет скрипт по выбранной записи и показывает, что нашлось. */
RENDERERS.content.checkScript = async function () {
  const host = qs('#script-check');
  if (!host || !state.contentScriptJob) return;
  let ответ;
  try {
    ответ = await API.post('/api/content/script/check', {
      job_id: state.contentScriptJob,
      script: state.contentScript || undefined,
    });
  } catch (err) {
    host.innerHTML = `<div class="empty small">Проверить не удалось: ${esc(err.message)}</div>`;
    return;
  }
  const итог = ответ.compliance || {};
  const пункты = итог.items || [];
  host.innerHTML = `
    <div class="row small dim" style="margin-bottom:10px">
      <span>Выполнено пунктов: <b>${итог.passed || 0}</b> из ${итог.checked || 0}</span>
      <span class="spacer"></span>
      <span>${итог.speaker ? `проверено по говорящему «${esc(итог.speaker)}»`
        : 'говорящий не определён — проверено по всей записи'}</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>Пункт</th><th>Где искали</th><th>Что нашли</th></tr></thead>
      <tbody>${пункты.map((п) => `<tr>
        <td>${п.passed ? '<span class="chip ok">есть</span>'
                       : '<span class="chip err">нет</span>'}</td>
        <td>${esc(п.label || '—')}</td>
        <td class="small dim">${esc(гдеИскали(п))}</td>
        <td class="small">${п.matched
          ? `<b>${esc(п.matched)}</b>` : '—'}</td></tr>`).join('')}</tbody></table></div>
    ${(ответ.suspicious || []).length ? `<div class="finding warning" style="margin-top:12px">
      Приметы, совпадающие с частыми словами языка:
      ${(ответ.suspicious || []).map((с) =>
        `<b>${esc(с.word)}</b> (в пункте «${esc(с.label)}»)`).join(', ')}.
      Такая примета делает пункт выполненным почти всегда — на ней и попался
      пункт «Представился», искавший голое «это».
    </div>` : ''}`;
};

/** Сохраняет скрипт и пересчитывает разбор: старые числа считаны другим. */
RENDERERS.content.saveScript = async function (пункты) {
  const плохие = пункты.filter((п) => !п.label || !(п.any || []).length);
  if (плохие.length) {
    toast('У каждого пункта должно быть название и хотя бы одна примета', 'err');
    return;
  }
  try {
    await API.put('/api/settings', { content_script: пункты });
  } catch (err) { fail(err); return; }
  toast(пункты.length ? 'Скрипт сохранён' : 'Возвращён набор по умолчанию', 'ok');
  if (confirm('Скрипт изменился — пересчитать разбор архива?\n\n' +
              'Без пересчёта соблюдение скрипта в отчётах останется посчитанным ' +
              'по прежним пунктам, и сравнивать эти числа с новыми нельзя.')) {
    try { await API.post('/api/content/recompute', {}); } catch (err) { fail(err); }
  }
  state.contentData = {};
  state.contentScriptOwn = пункты.length > 0;
  this.showTab();
};

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
// Раздел «Доступ» в настройках
// ==========================================================================
//
// Ключ доступа и токен Hugging Face — не параметры каталога: у них нет ни
// значения по умолчанию, ни диапазона, а показывать их целиком нельзя.
// Поэтому отдельный раздел со своими правилами вместо карточек параметров.

const ACCESS_GROUP = '_access';

function maskKey(value) {
  if (!value) return '';
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : `${value.slice(0, 4)}…`;
}

async function renderAccessSection(host) {
  const stored = localStorage.getItem('asrhub_key') || '';
  const me = state.me || {};
  const isAdmin = me.role === 'admin';

  host.innerHTML = `
    <section class="card">
      <div class="card-head"><h3>Этот браузер</h3>
        <span class="hint">чем интерфейс подписывает свои запросы</span></div>
      <div class="params" style="padding:14px 16px">
        <p class="small dim" id="access-current"></p>
        <div class="row wrap" style="gap:8px">
          <input type="password" id="access-key" placeholder="ah_…" autocomplete="off"
                 style="flex:1;min-width:220px">
          <button class="primary sm" id="access-key-save">Использовать этот ключ</button>
          <button class="ghost sm" id="access-key-forget">Забыть ключ</button>
        </div>
        <p class="small dim" style="margin-top:8px">Ключ хранится только в этом
          браузере и уходит заголовком <span class="mono">X-API-Key</span>. Если вы вошли
          логином и паролем, ключ здесь не нужен.</p>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h3>Ключи доступа</h3>
        <span class="hint">для программ: curl, asrctl, интеграции</span></div>
      <div class="params" style="padding:14px 16px">
        <div id="access-keys"></div>
        ${isAdmin ? `<div class="row wrap" style="margin-top:10px;gap:6px">
          <input type="text" id="access-new-name" placeholder="название ключа"
                 style="flex:1;min-width:160px">
          <select id="access-new-role" style="width:130px">
            <option value="user">user</option><option value="admin">admin</option>
            <option value="readonly">readonly</option></select>
          <button class="primary sm" id="access-new">Создать</button></div>
          <p class="small dim" style="margin-top:8px">Полное значение ключа
            показывается один раз при создании — сохраните его сразу.</p>`
        : '<p class="small dim" style="margin-top:8px">Создавать и отзывать ключи может администратор.</p>'}
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h3>Токен Hugging Face</h3>
        <span class="hint">модели с ограниченным доступом и pyannote</span></div>
      <div class="params" style="padding:14px 16px">
        <div id="access-hf"></div>
      </div>
    </section>`;

  // --- ключ этого браузера ---
  const current = qs('#access-current');
  if (me.kind === 'user') {
    current.textContent = `Вход по учётной записи «${me.name}» — запросы подписывает сессия.`;
  } else if (stored) {
    current.textContent = `Сейчас используется ключ ${maskKey(stored)}.`;
  } else {
    current.textContent = 'Ключ не задан: сервер разрешает работу без аутентификации.';
  }
  qs('#access-key-save').onclick = () => {
    const value = qs('#access-key').value.trim();
    if (!value) { toast('Введите ключ', 'warn'); return; }
    localStorage.setItem('asrhub_key', value);
    toast('Ключ сохранён', 'ok', 'Страница перезагрузится');
    setTimeout(() => location.reload(), 600);
  };
  qs('#access-key-forget').onclick = () => {
    localStorage.removeItem('asrhub_key');
    toast('Ключ убран из браузера', 'warn');
    setTimeout(() => location.reload(), 600);
  };

  // --- список ключей ---
  await loadAccessKeys();
  if (isAdmin) {
    qs('#access-new').onclick = async () => {
      const name = qs('#access-new-name').value.trim();
      if (!name) { toast('Укажите название ключа', 'warn'); return; }
      try {
        const created = await API.post('/api/keys',
          { name, role: qs('#access-new-role').value, rate_limit: 0 });
        prompt('Сохраните ключ — он показывается один раз:', created.key);
        qs('#access-new-name').value = '';
        loadAccessKeys();
      } catch (err) { fail(err); }
    };
  }

  // --- токен Hugging Face ---
  await loadHfToken(isAdmin);
}

async function loadAccessKeys() {
  const box = qs('#access-keys');
  if (!box) return;
  try {
    const data = await API.get('/api/keys');
    box.innerHTML = data.items.length ? `<table>
      <thead><tr><th>Ключ</th><th>Название</th><th>Роль</th><th></th></tr></thead><tbody>
      ${data.items.map((k) => `<tr>
        <td class="mono small">${esc(k.key_preview)}</td>
        <td>${esc(k.name || '')}</td>
        <td><span class="chip ${k.role === 'admin' ? 'accent' : ''}">${esc(k.role)}</span></td>
        <td><button class="ghost sm danger"
          onclick="__asrhub.revokeKey('${esc(k.key_id || '')}')">отозвать</button></td>
      </tr>`).join('')}</tbody></table>` : '<div class="empty small">Ключей нет</div>';
  } catch (err) {
    box.innerHTML = '<div class="empty small">Список ключей доступен администратору</div>';
  }
}

async function loadHfToken(isAdmin) {
  const box = qs('#access-hf');
  if (!box) return;
  if (!isAdmin) {
    box.innerHTML = '<div class="empty small">Токен задаёт администратор</div>';
    return;
  }
  let info;
  try {
    info = await API.get('/api/settings/hf-token');
  } catch (err) {
    box.innerHTML = '<div class="empty small">Не удалось получить состояние токена</div>';
    return;
  }
  box.innerHTML = `
    <p class="small dim" style="margin:0 0 10px">${info.configured
      ? `Задан: <span class="mono">${esc(info.preview)}</span> (${info.length} знаков).`
      : 'Не задан. Без него не скачаются pyannote для разделения по говорящим и модели с ограниченным доступом.'}</p>
    <div class="row wrap" style="gap:8px">
      <input type="password" id="hf-input" placeholder="hf_…" autocomplete="off"
             style="flex:1;min-width:220px">
      <button class="primary sm" id="hf-save">Сохранить</button>
      ${info.configured ? '<button class="ghost sm danger" id="hf-clear">Убрать</button>' : ''}
    </div>
    <p class="small dim" style="margin-top:8px">Токен сохраняется в
      <span class="mono">${esc(info.config_file || 'config.yaml')}</span> и на экран целиком
      не выводится. Взять его: huggingface.co/settings/tokens, прав «read» достаточно.</p>`;

  qs('#hf-save').onclick = async () => {
    const token = qs('#hf-input').value.trim();
    if (!token) { toast('Введите токен', 'warn'); return; }
    try {
      await API.put('/api/settings/hf-token', { token });
      toast('Токен сохранён', 'ok', 'Движки подхватят его при следующем задании');
      loadHfToken(true);
    } catch (err) { fail(err); }
  };
  const clear = qs('#hf-clear');
  if (clear) clear.onclick = async () => {
    if (!confirm('Убрать токен Hugging Face?')) return;
    try {
      await API.put('/api/settings/hf-token', { token: '' });
      toast('Токен убран', 'warn');
      loadHfToken(true);
    } catch (err) { fail(err); }
  };
}

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
          <!-- Доступ — не группа каталога, а отдельный раздел: ключ доступа и
               токен Hugging Face это не параметры со значением по умолчанию и
               диапазоном, а секреты, и правятся они иначе. Место здесь,
               потому что искать их идут в «Настройки», а не в «Сервер». -->
          <button data-group="${ACCESS_GROUP}"
            class="${state.paramGroup === ACCESS_GROUP ? 'active' : ''}">Доступ</button>
        </div>
      </div>
      <div class="settings-toolbar" style="top:106px"
           ${state.paramGroup === ACCESS_GROUP ? 'hidden' : ''}>
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
    // Панель параметров в разделе «Доступ» скрыта: «Сбросить» там сбросил бы
    // не то, о чём человек думает, а «Применить» относится к параметрам
    // каталога, которых в этом разделе нет.
    let timer;
    if (state.paramGroup !== ACCESS_GROUP) {
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
    }
    this.list();
  },

  list() {
    const host = qs('#params-body');
    const search = (state.paramSearch || '').toLowerCase();
    const groups = state.catalog.groups;

    // Поиск идёт по параметрам каталога, поэтому раздел «Доступ» показываем
    // только когда он выбран явно и в поиске пусто.
    if (state.paramGroup === ACCESS_GROUP && !search) { renderAccessSection(host); return; }

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

        ${card('Ключи доступа', 'для программ: curl, asrctl, интеграции',
          '<div id="keys-body"></div>' +
          `<div class="row" style="margin-top:10px">
            <input type="text" id="key-name" placeholder="название ключа" style="flex:1">
            <select id="key-role" style="width:130px">
              <option value="user">user</option><option value="admin">admin</option>
              <option value="readonly">readonly</option></select>
            <button class="primary sm" id="key-create">Создать</button></div>`)}

        ${card('Учётные записи', 'для людей: вход по логину и паролю',
          '<div id="users-body"></div>' +
          `<div class="row wrap" style="margin-top:10px;gap:6px">
            <input type="text" id="user-name" placeholder="логин" style="flex:1;min-width:120px">
            <input type="password" id="user-pass" placeholder="пароль" style="flex:1;min-width:120px">
            <select id="user-role" style="width:120px">
              <option value="user">user</option><option value="admin">admin</option>
              <option value="readonly">readonly</option></select>
            <button class="primary sm" id="user-create">Завести</button></div>
          <div class="small dim" style="margin-top:6px">
            Новый пользователь меняет пароль при первом входе.</div>`)}
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
    qs('#user-create').onclick = async () => {
      const username = qs('#user-name').value.trim();
      const password = qs('#user-pass').value;
      if (!username || !password) { toast('Укажите логин и пароль', 'warn'); return; }
      try {
        await API.post('/api/users', { username, password,
                                       role: qs('#user-role').value,
                                       must_change_password: true });
        qs('#user-name').value = ''; qs('#user-pass').value = '';
        toast('Учётная запись заведена', 'ok');
        this.loadUsers();
      } catch (err) { fail(err); }
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
    this.loadUsers();
  },

  async loadUsers() {
    const host = qs('#users-body');
    if (!host) return;
    try {
      const data = await API.get('/api/users');
      const rows = data.users.map((u) => `<tr>
        <td><b>${esc(u.username)}</b>${u.must_change_password
            ? '<div class="small faint">пароль не сменён</div>' : ''}</td>
        <td><span class="chip ${u.role === 'admin' ? 'accent' : ''}">${esc(u.role)}</span></td>
        <td>${u.enabled ? '' : '<span class="chip err">отключён</span>'}</td>
        <td class="row" style="gap:4px">
          <button class="ghost sm"
            onclick="__asrhub.resetPassword('${esc(u.id)}','${esc(u.username)}')">пароль</button>
          <button class="ghost sm danger"
            onclick="__asrhub.deleteUser('${esc(u.id)}','${esc(u.username)}')">удалить</button>
        </td></tr>`).join('');
      host.innerHTML = `${data.default_password_in_use ? `<div class="banner warn small">
        Действует пароль по умолчанию (${esc(data.default_username)}/admin123).
        Смените его — сервер доступен всем, кто знает эту пару.</div>` : ''}
        <table><thead><tr><th>Логин</th><th>Роль</th><th></th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    } catch (err) {
      host.innerHTML = '<div class="empty small">Список доступен администратору</div>';
    }
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

window.__asrhub.resetPassword = async (id, username) => {
  const password = prompt(`Новый пароль для «${username}»:`);
  if (!password) return;
  try {
    await API.patch(`/api/users/${id}`, { password, must_change_password: true });
    toast('Пароль сброшен', 'ok', 'Пользователь сменит его при следующем входе');
    renderView();
  } catch (err) { fail(err); }
};

window.__asrhub.deleteUser = async (id, username) => {
  if (!confirm(`Удалить учётную запись «${username}»?`)) return;
  try { await API.del(`/api/users/${id}`); toast('Учётная запись удалена', 'warn'); renderView(); }
  catch (err) { fail(err); }
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

      ${card('Нагрузка сервера', 'ряды за выбранное окно — таблица показывает текущее значение, а нужен ход',
             `<div class="settings-toolbar" style="position:static;padding:0 0 10px">
                <span class="small dim">Окно:</span>
                <div class="group-nav" id="mon-window">
                  ${[[15, '15 мин'], [60, 'час'], [360, '6 часов'], [1440, 'сутки'],
                     [10080, 'неделя']].map(([m, л]) =>
                    `<button data-minutes="${m}"${m === 60 ? ' class="active"' : ''}>${л}</button>`).join('')}
                </div>
                <span class="spacer"></span>
                <span class="small faint" id="mon-sampled"></span>
              </div>
              <div id="mon-charts"><div class="empty">Загрузка рядов…</div></div>`)}

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
    qsa('#mon-window button').forEach((b) => b.addEventListener('click', () => {
      qsa('#mon-window button').forEach((x) => x.classList.toggle('active', x === b));
      this.loadResources(Number(b.dataset.minutes));
    }));
    this.loadResources(60);
    this.loadCatalog();
  },

  /* Графики нагрузки: сервер и каждая видеокарта отдельно.
   *
   * «Средняя загрузка видеокарты» — величина, из которой ничего не следует,
   * поэтому карты никогда не складываются в один ряд, даже когда их две.
   */
  async loadResources(minutes) {
    const host = qs('#mon-charts');
    if (!host) return;
    let data;
    try {
      data = await API.latest('mon-res', `/api/monitoring/resources?minutes=${minutes}`);
    } catch (err) {
      if (!(err && err.silent)) host.innerHTML =
        `<div class="empty">Не удалось получить ряды: ${esc(err.message)}</div>`;
      return;
    }
    const счётчик = qs('#mon-sampled');
    if (счётчик) счётчик.textContent = `замеров: ${num(data.sampled)}`;
    if (!data.sampled) {
      host.innerHTML = '<div class="empty">За это окно замеров ещё нет — сервер их пишет раз в несколько секунд</div>';
      return;
    }

    const s = data.system || {};
    const метки = (s.ts || []).map((t) => new Date(t * 1000).toLocaleTimeString('ru-RU',
      { hour: '2-digit', minute: '2-digit' }));
    const памятьВсего = s.ram_total_mb || 0;

    host.innerHTML = `
      <div class="grid cols-2">
        <div><b class="small">Процессор</b><div id="mon-cpu"></div></div>
        <div><b class="small">Оперативная память${памятьВсего
          ? `, всего ${num(памятьВсего / 1024, 1)} ГБ` : ''}</b><div id="mon-ram"></div></div>
      </div>
      <div class="grid cols-2">
        <div><b class="small">Очередь и работа</b><div id="mon-queue"></div></div>
        <div><b class="small">Свободно на диске</b><div id="mon-disk"></div></div>
      </div>
      ${(data.gpus || []).map((g) => `
        <div class="card tight" style="margin-top:12px">
          <div class="row" style="margin-bottom:8px">
            <b>${esc(g.name)}</b>
            <span class="chip">GPU ${g.gpu}</span>
            ${g.mem_total_mb ? `<span class="chip">${num(g.mem_total_mb / 1024, 1)} ГБ</span>` : ''}
            ${g.power_limit_w ? `<span class="chip">предел ${num(g.power_limit_w, 0)} Вт</span>` : ''}
          </div>
          <div class="grid cols-2">
            <div><b class="small">Загрузка и память</b><div id="mon-gpu-${g.gpu}"></div></div>
            <div><b class="small">Температура и потребление</b>
                 <div id="mon-gpu-t-${g.gpu}"></div></div>
          </div>
        </div>`).join('')
      || '<div class="empty small" style="margin-top:12px">Видеокарты не обнаружены — рядов по ним нет</div>'}`;

    Charts.line(qs('#mon-cpu'), { labels: метки, area: true, unit: ' %', yMax: 100,
      series: [{ name: 'Загрузка', values: s.cpu_percent || [] }] });
    Charts.line(qs('#mon-ram'), { labels: метки, area: true, unit: ' МБ',
      yMax: памятьВсего || undefined,
      series: [{ name: 'Занято', values: s.ram_used_mb || [] }] });
    Charts.line(qs('#mon-queue'), { labels: метки,
      series: [{ name: 'В очереди', values: s.queue_depth || [] },
               { name: 'В работе', values: s.active_jobs || [] }] });
    Charts.line(qs('#mon-disk'), { labels: метки, area: true, unit: ' ГБ',
      series: [{ name: 'Свободно', values: s.disk_free_gb || [] }] });

    (data.gpus || []).forEach((g) => {
      const их = (g.ts || []).map((t) => new Date(t * 1000).toLocaleTimeString('ru-RU',
        { hour: '2-digit', minute: '2-digit' }));
      // Загрузка в процентах и память в мегабайтах — величины разного
      // порядка, поэтому память переводим в проценты от установленной. Так
      // обе линии живут на одной шкале честно, без второй оси.
      const памятьДоля = g.mem_total_mb
        ? (g.mem_used_mb || []).map((v) => v === null ? null : (v / g.mem_total_mb) * 100)
        : null;
      Charts.line(qs(`#mon-gpu-${g.gpu}`), {
        labels: их, unit: ' %', yMax: 100,
        series: [{ name: 'Загрузка', values: g.util_percent || [] },
                 ...(памятьДоля ? [{ name: 'Память занята', values: памятьДоля }] : [])],
      });
      const ряды = [];
      if ((g.temperature_c || []).some((v) => v !== null)) {
        ряды.push({ name: 'Температура, °C', values: g.temperature_c });
      }
      if ((g.power_w || []).some((v) => v !== null)) {
        ряды.push({ name: 'Потребление, Вт', values: g.power_w });
      }
      const узел = qs(`#mon-gpu-t-${g.gpu}`);
      if (ряды.length) {
        Charts.line(узел, { labels: их, series: ряды });
      } else {
        Charts.empty(узел, 'Карта не отдаёт телеметрию по температуре и мощности');
      }
    });
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

      ${card('Ключ доступа', 'задаётся в одном месте', `<div class="small dim">
        Ключ этого браузера, список ключей для программ и токен Hugging Face —
        в разделе <b>Настройки → Доступ</b>. Раньше поле для ключа стояло ещё и
        здесь: два поля показывали одно значение, и сохранив ключ в одном,
        человек видел в другом прежний.</div>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="help-to-access">Открыть «Доступ»</button></div>`)}

      ${card('Источники данных каталога', 'каждое число в каталоге имеет ссылку на первоисточник',
        `<div class="table-wrap"><table>
          <thead><tr><th style="width:200px">Ключ</th><th>Источник</th></tr></thead><tbody>
          ${Object.entries(state.catalog.sources).map(([k, v]) =>
            `<tr><td class="mono small">${esc(k)}</td><td class="small dim">${esc(v)}</td></tr>`
          ).join('')}</tbody></table></div>
        <div class="small faint" style="margin-top:8px">
          Каталог собран по состоянию на ${esc(state.catalog.date)}. Модели выходят
          постоянно — сверяйтесь с первоисточниками перед принятием решений.</div>`)}`;

    qs('#help-to-access').onclick = () => {
      state.paramGroup = ACCESS_GROUP;
      state.paramSearch = '';
      go('settings');
    };
  },
};

})();
