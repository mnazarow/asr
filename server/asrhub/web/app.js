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
  //: Разрез, в котором смотрят напряжение и NPS. По сотрудникам — потому
  //: что первый вопрос к такому показателю всегда «у кого».
  emoDim: 'agent',
  npsDim: 'agent',
  //: Чем считать сотрудника в разделе «Аналитика по сотрудникам»: именем
  //: из журнала АТС, меткой говорящего, ключом доступа, очередью, станцией.
  employeeBy: 'agent',
  employeeCols: 'main',
  employeeSort: 'records',
  employeeKey: '',
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
    let text;
    try {
      response = await fetch(path, opts);
      // Тело читается под тем же сигналом: отмена приходит и во время
      // чтения — на большом ответе чаще, чем до него, — и без этого
      // AbortError вылетал сырым исключением DOMException мимо разбора
      // ниже: красная плашка «The user aborted a request» и ошибка в
      // консоли при обычном переключении вкладок.
      text = await response.text();
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
/* Экранирование для разметки.
 *
 * Апостроф — не украшение: половина обработчиков в этом файле написана как
 * onclick="…('${esc(id)}')", то есть значение попадает внутрь строки JS,
 * ограниченной апострофом. Кавычка там не спасает — разбор ломает именно
 * апостроф, и сегодня от этого держит только проверка имени пользователя
 * в питоне. Безопасность разметки не должна зависеть от чужой регулярки.
 */
function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
  trends:     { title: 'Тренды', subtitle: 'Как менялось со временем всё, что сервер измеряет: объём, скорость, качество, звук, содержание, железо' },
  employees:  { title: 'Аналитика по сотрудникам', subtitle: 'Все показатели по каждому: речь, клиенты, скрипт, звонки и что разобрать' },
  content:    { title: 'Аналитика записей', subtitle: 'О чём и как говорили: тональность, речь, темы, обязательства, скрипт' },
  pbx:        { title: 'АТС', subtitle: 'Все подключённые станции: состояние, нагрузка, очереди и операторы, забор записей' },
  telephony:  { title: 'Телефония', subtitle: 'Журнал звонков: кто, кому, когда, чем закончилось и что распознано' },
  models:     { title: 'Модели', subtitle: 'Каталог моделей, лицензии, требования, загрузка весов' },
  compare:    { title: 'Сравнение моделей', subtitle: 'Качество, скорость и лицензии рядом' },
  settings:   { title: 'Настройки', subtitle: 'Все параметры с описаниями, рекомендациями и примерами' },
  system:     { title: 'Сервер', subtitle: 'Оборудование, движки, хранилище, учётные записи и ключи' },
  monitoring: { title: 'Мониторинг', subtitle: 'Метрики наружу, пороги тревог, приёмники телеметрии' },
  backup:     { title: 'Резервные копии', subtitle: 'Копии настроек и данных, восстановление, расписание и срок хранения' },
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

const HOTKEY_VIEWS = ['transcribe', 'dictation', 'queue', 'results', 'analytics', 'trends',
                      'pbx', 'telephony', 'models', 'compare', 'settings', 'system', 'monitoring'];

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
window.__asrhub = { state, API, RENDERERS, go, toast, renderView, showHotkeys, fail };

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
    }).catch((err) => {
      // Пустая карточка без объяснения неотличима от «заданий ещё не было»,
      // а обновляется она каждые четыре секунды — то есть молчала бы вечно.
      if (err && err.silent) return;
      const host = qs('#recent-jobs');
      if (host && !host.children.length) {
        host.innerHTML = `<div class="empty small">Список не получен: ${
          esc((err && err.message) || 'ошибка запроса')}</div>`;
      }
    });
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

    // Раздел мог перерисоваться поверх идущей записи (кнопка «Обновить»,
    // смена темы): `leave()` вызывается только при СМЕНЕ раздела, поэтому
    // без этой строки ссылка на сессию терялась, а микрофон, MediaRecorder
    // и веб-сокет продолжали работать без всякой возможности их остановить
    // — и следующее нажатие открывало вторую запись поверх первой.
    if (this.session) { try { this.stop(true); } catch (err) { /* уже закрыта */ } }
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
            <option value="bad_audio">плохой звук: шум или клиппинг</option>
            <option value="llm_unresolved">вопрос не решён (по оценке модели)</option>
            <option value="llm_actions">есть действия к исполнению (модель)</option>
            <option value="noisy">шумная запись (SNR ниже 10 дБ)</option>
            <option value="clipped">клиппинг от 1 % отсчётов</option>
            <option value="objection_unhandled">возражение без отработки</option>
            <option value="objection">с возражениями клиента</option>
            <option value="violation">с нарушениями оператора</option>
            <option value="low_score">балл оператора ниже 60</option>
            <option value="impolite">невежливый оператор</option>
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
    // Запрос идёт через API.background, то есть переживает уход из раздела:
    // ответ прошлого захода дописывал категории в НОВЫЙ список, и каждый
    // заход-уход добавлял ещё одну копию каждой категории. Метка на самом
    // элементе списка отвечает на вопрос «это тот список или уже другой»
    // надёжнее любого счётчика поколений.
    let перечни;
    try { перечни = await API.background('/api/content/kinds'); } catch (e) { return; }
    const select = qs('#r-content');
    if (!select || !(перечни.categories || []).length) return;
    if (select.dataset.categories === 'да') return;
    select.dataset.categories = 'да';
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
    // Поиск запускается по таймеру в 300 мс, и таймер не привязан к
    // разделу: уйти сразу после набора — и обработчик срабатывает уже без
    // разметки, а `qs('#r-search').value` падает TypeError мимо всех
    // ловушек (метод async, вызывают без await).
    const поле = qs('#r-search');
    const порядок = qs('#r-order');
    if (!host || !поле || !порядок) return;
    const search = поле.value.trim();
    const order = порядок.value;
    try {
      const params = new URLSearchParams({ status: 'completed', limit: '150', order });
      if (search) params.set('search', search);
      const содержание = (qs('#r-content') || {}).value;
      if (содержание) params.set('content', содержание);
      const data = await API.latest('results-table', `/api/jobs?${params}`);
      // Поиск по расшифровкам берёт не больше SEARCH_LIMIT совпадений.
      // Молчаливая обрезка читается как «старых разговоров на эту тему
      // нет», а это совсем другое утверждение — говорим прямо.
      const обрезано = data.search_truncated
        ? `<div class="banner warn" style="margin-bottom:10px">Показаны
             ${num(data.search_limit || 0)} самых свежих совпадений — их больше.
             Уточните запрос или сузьте период, чтобы увидеть остальные.</div>`
        : '';
      host.innerHTML = data.items.length ? обрезано + `<table>
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
      ${job.snr_db !== null && job.snr_db !== undefined ? `<div class="small dim" style="margin:-6px 0 12px">
        <b class="small">Звук на входе</b>: речь к шуму ${num(job.snr_db, 0)} дБ${job.snr_db < 10 ? ' <span class="chip warn">шумно</span>' : ''}
        · пик ${num(job.peak_dbfs, 1)} dBFS${job.clipping_share >= 0.01 ? ` · клиппинг ${pct(job.clipping_share, 1)} <span class="chip warn">срезано</span>` : job.clipping_share > 0 ? ` · клиппинг ${pct(job.clipping_share, 2)}` : ''}
        ${job.loudness_lufs !== null && job.loudness_lufs !== undefined ? ` · громкость ${num(job.loudness_lufs, 0)} LUFS` : ''}
        ${job.silence_share !== null && job.silence_share !== undefined ? ` · тишины ${pct(job.silence_share, 0)}` : ''}</div>` : ''}

      ${job.call ? `<div class="card tight" style="margin-bottom:12px">
        <b class="small">Звонок с АТС</b>
        <div class="small dim" style="margin-top:4px">
          ${esc(job.call.direction || 'направление неизвестно')} ·
          ${esc(job.call.src || '—')} → ${esc(job.call.dst || '—')}
          ${job.call.clid ? ` · ${esc(job.call.clid)}` : ''}
          ${job.call.queue ? ` · очередь ${esc(job.call.queue)}` : ''}
          ${job.call.agent ? ` · оператор ${esc(job.call.agent)}` : ''}
        </div>
        <div class="small dim" style="margin-top:2px">
          начало ${esc(fmtTime(job.call.started_at))} · разговор ${fmtDur(job.call.billsec || 0)}
          из ${fmtDur(job.call.duration || 0)} ·
          ${job.call.answered ? 'ответили' : esc((job.call.disposition || '').toLowerCase() || 'без ответа')}
          · идентификатор <span class="mono">${esc(job.call.pbx_uid || job.call.uniqueid || '')}</span>
        </div>
      </div>` : ''}

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
        ${job.status === 'completed' && job.text
          ? `<button data-tab="reference">Эталон${job.wer !== null && job.wer !== undefined ? ` (WER ${pct(job.wer, 1)})` : ''}</button>` : ''}
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
    reference: () => referenceTab(job, segments),
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
    if (name === 'reference') bindReferenceTab(backdrop, job, () => show('reference'));
  };
  qsa('#job-tabs button', backdrop).forEach((b) =>
    b.addEventListener('click', () => show(b.dataset.tab)));
  show(options.tab && tabs[options.tab] ? options.tab : 'text');

  const player = setupJobPlayer(backdrop, job, segments, show, options);
  drawJobWaveform(backdrop, job, segments, show, player);

  // Проигрыватель продолжал бы играть из закрытого окна: узел удалён, звук
  // идёт. Поэтому останавливаем его вместе с окном.
  backdrop.addEventListener('asrhub:closed', () => { if (player) player.destroy(); });
}

/* Очередь ручной проверки — карточка в «Аналитике».
 *
 * Очередь пополняется сервером раз в сутки, а закрывается человеком:
 * «Открыть» ведёт в карточку записи сразу на вкладку «Эталон», и
 * сохранённый эталон закрывает строку сам. «Пропустить» — для записей,
 * которые слушать незачем (пустые, чужие, служебные).
 */
async function drawReviewQueue() {
  const тело = qs('#review-body');
  if (!тело) return;
  let данные;
  try {
    данные = await API.latest('review-queue', '/api/review?status=pending&limit=50');
  } catch (err) {
    if (err && err.silent) return;
    тело.innerHTML = `<div class="empty small">Очередь недоступна: ${esc(err.message || '')}</div>`;
    return;
  }
  if (!тело.isConnected) return;
  const счёт = данные.counts || {};
  const причины = данные.reasons || {};
  const строки = данные.items || [];
  const админ = (state.me || {}).role === 'admin' || !(state.me || {}).role;
  тело.innerHTML = `<div class="grid cols-3" style="margin-bottom:10px">
      ${kpi('Ожидают', num(счёт.pending || 0), данные.enabled ? 'пополняется раз в сутки' : 'очередь выключена (review_enabled)')}
      ${kpi('Проверено за неделю', num(счёт.done || 0), 'эталон задан')}
      ${kpi('Пропущено за неделю', num(счёт.skipped || 0), '')}
    </div>
    ${строки.length ? `<div class="table-wrap"><table><thead><tr><th>Запись</th><th>Модель</th>
      <th class="num" title="уверенность модели">Увер.</th><th class="num" title="речь к шуму, дБ">SNR</th><th>Почему</th><th></th></tr></thead><tbody>
      ${строки.map((з) => `<tr data-review="${esc(з.job_id)}">
        <td class="truncate" style="max-width:180px" title="${esc(з.filename || '')}">${esc(з.filename || з.job_id)}<div class="small faint">${fmtDur(з.media_duration_s)} · ${fmtTime(з.created_at).slice(0, 5)}</div></td>
        <td class="small">${esc(з.model || '—')}</td>
        <td class="num mono">${з.avg_confidence === null || з.avg_confidence === undefined ? '—' : pct(з.avg_confidence, 0)}</td>
        <td class="num mono">${з.snr_db === null || з.snr_db === undefined ? '—' : num(з.snr_db, 0)}</td>
        <td class="small" title="${esc(причины[з.reason] || з.reason)}">${esc({ random: 'случайная', low_confidence: 'неуверенная', manual: 'вручную', bad_audio: 'плохой звук' }[з.reason] || з.reason)}</td>
        <td class="nowrap"><button class="btn sm" data-review-open="${esc(з.job_id)}">Открыть</button>
          <button class="ghost sm" data-review-skip="${esc(з.job_id)}" title="Убрать из очереди без эталона">Пропустить</button></td></tr>`).join('')}
      </tbody></table></div>` : `<div class="empty small">Очередь пуста${данные.last_sampled_at ? `: последний отбор ${fmtTime(данные.last_sampled_at)}` : ' — первый отбор будет через сутки после запуска'}.</div>`}
    <div class="row" style="gap:8px;margin-top:8px">
      ${админ ? '<button class="ghost sm" id="review-sample-now" title="Отобрать записи за последние сутки, не дожидаясь суточного захода">Пополнить сейчас</button>' : ''}
      <span class="small faint">Случайные записи дают честный WER; неуверенные — больше исправлений на час прослушивания.</span>
    </div>`;
  qsa('[data-review-open]', тело).forEach((кнопка) => {
    кнопка.onclick = () => window.__asrhub.openJob(кнопка.dataset.reviewOpen, { tab: 'reference' });
  });
  qsa('[data-review-skip]', тело).forEach((кнопка) => {
    кнопка.onclick = async () => {
      кнопка.disabled = true;
      try {
        await API.call(`/api/review/${кнопка.dataset.reviewSkip}`, { method: 'PUT', json: { status: 'skipped' } });
        drawReviewQueue();
      } catch (err) { fail(err); кнопка.disabled = false; }
    };
  });
  const пополнить = qs('#review-sample-now', тело);
  if (пополнить) {
    пополнить.onclick = async () => {
      пополнить.disabled = true;
      try {
        const итог = await API.post('/api/review/sample');
        toast(`Отобрано записей: ${итог.added} из ${итог.candidates} за сутки`, 'ok');
        drawReviewQueue();
      } catch (err) { fail(err); пополнить.disabled = false; }
    };
  }
}

/* Вкладка «Эталон» в карточке задания.
 *
 * Эталон — расшифровка, сделанная человеком; по ней считаются WER, MER, WIL
 * и калибровка уверенности. Задать его можно было только через API, и
 * раздел «Точность по эталону» у большинства установок оставался пустым.
 * Здесь текст задания уже подставлен: править — быстрее, чем набирать.
 */
function referenceTab(job, segments) {
  const есть = job.wer !== null && job.wer !== undefined;
  const проц = (v, d) => (v === null || v === undefined ? '—' : pct(v, d === undefined ? 1 : d));
  const текст = job.reference_text || (segments.length
    ? segments.map((s) => s.text).join('\n') : (job.text || ''));
  const к = job.calibration || null;
  return `<div id="job-reference">
    ${есть ? `<div class="grid cols-4" style="margin-bottom:12px">
      ${kpi('WER', проц(job.wer), `${num(job.ref_words)} слов эталона`)}
      ${kpi('MER', проц(job.mer), 'ограничен единицей')}
      ${kpi('WIL', проц(job.wil), 'потерянная информация')}
      ${kpi('Ошибок', `${num(job.sub_words)} / ${num(job.del_words)} / ${num(job.ins_words)}`, 'замен / пропусков / вставок')}
    </div>
    ${к && к.words ? `<div class="small faint" style="margin-bottom:10px">Калибровка: ${num(к.words)} слов с уверенностью${
      к.source === 'word' ? ' по словам' : ' по сегментам'} — учтены в диаграмме надёжности раздела «Аналитика».</div>` : ''}` : `<div class="small dim" style="margin-bottom:10px">
      Эталона у записи нет. Поправьте текст ниже так, как было сказано на самом деле, и сохраните —
      сервер посчитает WER, MER, WIL и калибровку уверенности, а запись попадёт в срезы точности.</div>`}
    <textarea id="job-reference-text" rows="10" style="width:100%;font-family:inherit;line-height:1.5"
      placeholder="Эталонная расшифровка">${esc(текст)}</textarea>
    <div class="row" style="gap:8px;margin-top:8px">
      <button class="primary" id="job-reference-save">${есть ? 'Пересчитать по эталону' : 'Сохранить эталон и посчитать точность'}</button>
      <span class="small faint">Метки говорящих в эталоне не нужны — сравнение идёт по словам.</span>
    </div>
    <div id="job-reference-result" style="margin-top:10px"></div>
  </div>`;
}

function bindReferenceTab(backdrop, job, redraw) {
  const кнопка = qs('#job-reference-save', backdrop);
  if (!кнопка) return;
  кнопка.onclick = async () => {
    const текст = (qs('#job-reference-text', backdrop) || {}).value || '';
    if (!текст.trim()) { toast('Эталон пуст', 'warn'); return; }
    кнопка.disabled = true;
    try {
      const итог = await API.post(`/api/jobs/${job.id}/reference`, { text: текст });
      Object.assign(job, { reference_text: текст, wer: итог.wer, mer: итог.mer, wil: итог.wil,
        ref_words: итог.reference_words, sub_words: итог.words.substitutions,
        del_words: итог.words.deletions, ins_words: итог.words.insertions });
      redraw();
      const хост = qs('#job-reference-result', backdrop);
      const цвет = { ok: '', sub: 'var(--warn)', del: 'var(--err)', ins: 'var(--accent)' };
      const подпись = { sub: 'замена', del: 'пропуск', ins: 'вставка' };
      if (хост && (итог.diff || []).length) {
        хост.innerHTML = `<div class="small dim" style="margin-bottom:6px">Расхождения: <span style="color:var(--warn)">замена</span>,
          <span style="color:var(--err)">пропуск</span> (есть в эталоне, нет в расшифровке),
          <span style="color:var(--accent)">вставка</span> (есть в расшифровке, нет в эталоне)</div>
          <div class="transcript" style="line-height:1.9">${итог.diff.map((ш) => ш.op === 'ok'
            ? esc(ш.hyp)
            : `<span style="color:${цвет[ш.op]};border-bottom:1px dotted" title="${подпись[ш.op]}">${
                ш.op === 'sub' ? `${esc(ш.hyp)}→${esc(ш.ref)}` : ш.op === 'del' ? `[${esc(ш.ref)}]` : `+${esc(ш.hyp)}`}</span>`).join(' ')}</div>`;
      }
      toast(`Эталон сохранён: WER ${pct(итог.wer, 1)}`, 'ok');
      // Пересчитанный балл в заголовке вкладки — без перезагрузки карточки.
      const вкладка = qs('#job-tabs button[data-tab="reference"]', backdrop);
      if (вкладка) вкладка.textContent = `Эталон (WER ${pct(итог.wer, 1)})`;
    } catch (err) { fail(err); } finally { кнопка.disabled = false; }
  };
}

/* Смысл разговора в карточке: ответ языковой модели по записи.
 *
 * Отдельным запросом после разбора по правилам: слой необязательный, и
 * карточка без него обязана открываться так же быстро. Когда ответа нет,
 * а модель подключена, — кнопка «Разобрать моделью»: вызов синхронный,
 * секунды, и ответ встаёт на место без перезагрузки.
 */
async function loadJobLlm(backdrop, job) {
  const host = qs('#job-llm', backdrop);
  if (!host) return;
  let д;
  try {
    д = await API.get(`/api/content/jobs/${job.id}/llm`);
  } catch (err) { return; }
  if (!host.isConnected) return;
  const рисовать = (р, stale) => {
    if (!р) {
      host.innerHTML = д.enabled ? `<div class="card tight" style="margin-bottom:12px">
        <div class="row" style="gap:8px"><b class="small">Смысл разговора</b>
          <span class="small faint">ответа языковой модели пока нет</span><span class="spacer"></span>
          <button class="btn sm" id="job-llm-run">Разобрать моделью</button></div></div>` : '';
      const кнопка = qs('#job-llm-run', host);
      if (кнопка) кнопка.onclick = () => запустить(кнопка);
      return;
    }
    const решено = р.resolved === true ? '<span class="chip ok">вопрос решён</span>'
      : р.resolved === false ? '<span class="chip warn">не решён</span>' : '';
    host.innerHTML = `<div class="card tight" style="margin-bottom:12px">
      <div class="row wrap" style="gap:8px;margin-bottom:6px"><b class="small">Смысл разговора</b>
        ${р.reason ? `<span class="chip info" title="${esc(р.reason_quote ? `причина обращения, по цитате: «${р.reason_quote}»` : 'причина обращения (из списка)')}">${esc(р.reason)}</span>` : ''}
        ${р.outcome ? `<span class="chip accent" title="${esc(р.outcome_quote ? `исход, по цитате: «${р.outcome_quote}»` : 'исход (из списка)')}">${esc(р.outcome)}</span>` : ''}
        ${решено}
        <span class="spacer"></span>
        <button class="ghost sm" id="job-llm-run" title="Спросить модель заново">Заново</button></div>
      ${р.error ? `<div class="small" style="color:var(--err)">Модель не ответила: ${esc(р.error)}</div>` : ''}
      ${р.summary ? `<div style="line-height:1.6">${esc(р.summary)}</div>` : ''}
      ${(р.reason_quote || р.outcome_quote) ? `<div class="small dim" style="margin-top:6px">По цитатам: ${[р.reason_quote && `причина — «${esc(р.reason_quote)}»`, р.outcome_quote && `исход — «${esc(р.outcome_quote)}»`].filter(Boolean).join('; ')}</div>` : ''}
      ${(р.actions || []).length ? `<div class="small dim" style="margin-top:8px"><b>Действия к исполнению</b></div>
        <ul style="margin:4px 0 0 18px;padding:0">${р.actions.map((а) => `<li>${esc(а.what)} <span class="faint">— ${esc(а.who || '')}${а.when ? `, ${esc(а.when)}` : ''}</span></li>`).join('')}</ul>` : ''}
      ${(р.trackers || []).some((т) => т.fired) ? `<div class="small" style="margin-top:8px"><b>Умные трекеры</b>: ${р.trackers.filter((т) => т.fired).map((т) => `<span class="chip warn" title="${esc(т.quote || '')}">${esc(т.label)}</span>`).join(' ')}</div>` : ''}
      ${(р.scorecard || []).length ? `<div class="small" style="margin-top:8px"><b>Скоркарта</b>: ${р.scorecard.map((в) => `<span class="chip ${в.answer === 'да' ? 'ok' : в.answer === 'нет' ? 'err' : ''}" title="${esc(в.quote || '')}">${esc(в.question)} — ${esc(в.answer)}</span>`).join(' ')}</div>` : ''}
      ${(р.warnings || []).length ? `<div class="small" style="margin-top:6px;color:var(--warn)">Замечания разбора: ${esc(р.warnings.join('; '))}</div>` : ''}
      <div class="small faint" style="margin-top:8px">Сгенерировано моделью ${esc(р.model || '')}${р.latency_ms ? ` за ${num(р.latency_ms / 1000, 1)} с` : ''}${р.chunks > 1 ? ` по пересказам ${num(р.chunks)} частей` : ''}${stale ? ' · подсказки с тех пор менялись' : ''} — может ошибаться; причина и исход выбраны из закрытых списков.</div>
    </div>`;
    const кнопка = qs('#job-llm-run', host);
    if (кнопка) кнопка.onclick = () => запустить(кнопка);
  };
  const запустить = async (кнопка) => {
    кнопка.disabled = true; кнопка.textContent = 'Модель думает…';
    try {
      const ответ = await API.post(`/api/content/jobs/${job.id}/llm?force=true`);
      д = { ...д, result: ответ.result, stale: false };
      рисовать(ответ.result, false);
    } catch (err) { fail(err); кнопка.disabled = false; кнопка.textContent = 'Разобрать моделью'; }
  };
  рисовать(д.result, д.stale);
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
  const возражения = категории.objections || { items: [], count: 0, unhandled: null };
  const балл = a.scorecard || {};
  const эмпатия = a.empathy || {};

  const реплика = (з, доп) => `<div class="analysis-line" data-start="${з.start_s || 0}">
    <span class="ts mono">${fmtDur(з.start_s || 0)}</span>
    <span class="who">${esc(з.speaker || '—')}</span>
    <span class="what">${esc(з.text || '')}${доп ? ` <span class="chip">${esc(доп)}</span>` : ''}</span>
  </div>`;

  host.innerHTML = `<div id="job-llm"></div>
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
    <div class="grid cols-4" style="margin-bottom:14px">
      ${kpi('Балл оператора', балл.score === null || балл.score === undefined ? '—' : num(балл.score, 0),
            балл.score === null || балл.score === undefined
              ? 'без пунктов скрипта балла нет'
              : `скрипт ${num(балл.base, 0)}${балл.penalty ? ` − штраф ${num(балл.penalty, 0)} (${
                  (балл.penalties || []).map((ш) => esc(ш.label)).join(', ')})` : ''}`)}
      ${kpi('Индекс эмпатии', эмпатия.index === null || эмпатия.index === undefined ? '—'
              : `${эмпатия.index > 0 ? '+' : ''}${num(эмпатия.index, 0)}`,
            эмпатия.index === null || эмпатия.index === undefined
              ? 'ни одного вежливого или невежливого оборота'
              : `вежливых ${num(эмпатия.polite)}, невежливых ${num(эмпатия.impolite)}${
                  эмпатия.speaker ? ` — по репликам «${esc(эмпатия.speaker)}»` : ' — по всем репликам'}`)}
      ${kpi('Нарушения оператора', num((категории.violations || []).length),
            (категории.violations || []).length
              ? (категории.violations || []).map((н) => `${esc(н.label)} ×${н.count}`).join(', ')
              : 'стоп-слов и других нарушений не найдено')}
      ${kpi('Возражения', num(возражения.count || 0),
            возражения.unhandled === null || возражения.unhandled === undefined
              ? (возражения.count ? 'отработку считать нечем' : 'возражений клиента нет')
              : `без отработки: ${num(возражения.unhandled)}`)}
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

    ${(эмпатия.items || []).length ? card('Невежливые обороты оператора',
      '«подождите», «вы должны», «я вам уже сказал» — то, что снижает индекс эмпатии',
      `<div class="analysis-lines">${(эмпатия.items || []).map(
         (т) => реплика(т, (т.words || []).join(', '))).join('')}</div>`) : ''}

    ${(возражения.items || []).length ? card(
      `Возражения клиента${возражения.unhandled === null || возражения.unhandled === undefined
        ? '' : ` — без отработки ${возражения.unhandled} из ${возражения.count}`}`,
      возражения.unhandled === null || возражения.unhandled === undefined
        ? 'в наборе нет категорий «отработка возражения» — считать, было ли отвечено, нечем'
        : 'отработкой считается реплика из категории «отработка» в следующих трёх репликах',
      `<div class="analysis-lines">${(возражения.items || []).map((в) => реплика(в,
        в.handled === false ? 'без отработки' : в.handled ? 'отработано' : в.matched)).join('')}</div>`) : ''}

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

  // Смысл разговора — после разбора по правилам и своим запросом: место
  // под него уже размечено, а ответ модели грузится отдельно.
  loadJobLlm(backdrop, job);
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
        ${card('Дрейф уверенности',
               'распределение уверенности за период против четырёх недель до него — по моделям; критерий Колмогорова — Смирнова',
               '<div id="drift-body"></div>')}
        ${card('Контрольные карты распознавания',
               'по дням: уверенность, доля заданий с низкой уверенностью, доля подозрительных сегментов; пределы 2σ и 3σ по базе',
               '<div id="control-body"></div>')}
      </div>

      ${card('Точность по эталону',
             'записи с эталонной расшифровкой: WER, MER и WIL по моделям и длительности — сложением слов, а не усреднением записей',
             '<div id="accuracy-body"></div>')}

      <div class="grid cols-2">
        ${card('Очередь ручной проверки',
               'раз в сутки: случайная доля плюс нижняя четверть по уверенности; правка текста во вкладке «Эталон» и есть проверка',
               '<div id="review-body"><div class="empty small">Загрузка…</div></div>')}
        ${card('Согласие моделей',
               'контрольный прогон второй моделью: расхождение двух расшифровок одной записи — по дням и по парам моделей',
               '<div id="agreement-body"></div>')}
      </div>

      <div class="grid cols-2">
        ${card('Калибровка уверенности',
               'верны ли слова, которым модель дала такую уверенность: диаграмма надёжности, ECE и AUC по записям с эталоном',
               '<div id="calibration-body"></div>')}
        ${card('Латентность',
               'p50 / p95 / p99 времени обработки и RTF по моделям и по длительности; для потока — секунды до первого текста',
               '<div id="latency-body"></div>')}
      </div>

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

  const д = data.drift || {};
  if (qs('#drift-body')) {
    const тело = qs('#drift-body');
    const вердикт = (v) => ({ critical: '<span class="chip err">критично</span>',
      warning: '<span class="chip warn">предупреждение</span>', ok: '<span class="chip ok">без дрейфа</span>' }[v]
      || '<span class="chip">мало данных</span>');
    const строка = (м) => `<tr><td>${м.key === 'all' ? '<b>все модели</b>' : esc(м.key)}</td>
      <td>${вердикт(м.verdict)}</td>
      <td class="num mono">${num((м.baseline || {}).n)} / ${num((м.current || {}).n)}</td>
      <td class="num mono">${(м.baseline || {}).p50 === null || (м.baseline || {}).p50 === undefined ? '—' : num(м.baseline.p50, 3)}
        → ${(м.current || {}).p50 === null || (м.current || {}).p50 === undefined ? '—' : num(м.current.p50, 3)}</td>
      <td class="num mono">${м.shift_relative === null || м.shift_relative === undefined ? '—' : (м.shift_relative > 0 ? '+' : '') + num(м.shift_relative * 100, 1) + '%'}</td>
      <td class="num mono">${(м.ks || {}).p === null || (м.ks || {}).p === undefined ? '—' : num(м.ks.p, 3)}</td>
      <td class="num mono">${м.low_share === null || м.low_share === undefined ? '—' : num(м.low_share, 1) + '%'}${
        м.low_share_baseline !== null && м.low_share_baseline !== undefined ? ` <span class="faint">/ ${num(м.low_share_baseline, 1)}%</span>` : ''}</td></tr>`;
    const общий = д.overall || {};
    if (!(общий.current || {}).n) {
      тело.innerHTML = '<div class="empty small">За период нет заданий с уверенностью</div>';
    } else {
      тело.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>Модель</th><th>Вердикт</th><th class="num" title="заданий в базе / за период">База / период</th>
          <th class="num" title="медиана уверенности: база → период">Медиана</th>
          <th class="num" title="сдвиг среднего, относительный">Сдвиг</th>
          <th class="num" title="p-значение критерия Колмогорова — Смирнова">p</th>
          <th class="num" title="доля заданий с уверенностью ниже 0,75: период / база">Низких</th></tr></thead>
        <tbody>${[общий, ...(д.models || [])].map(строка).join('')}</tbody></table></div>
        <p class="small faint" style="margin-top:8px">${esc(д.note || '')}</p>`;
    }
  }
  const кк = data.control || {};
  if (qs('#control-body')) {
    const тело = qs('#control-body');
    const карты = кк.charts || [];
    if (!карты.length || !карты.some((к) => (к.points || []).some((т) => т.value !== null))) {
      тело.innerHTML = '<div class="empty small">За период нет данных по дням</div>';
    } else {
      тело.innerHTML = карты.map((к) => `<div style="margin-bottom:10px">
        <div class="row" style="gap:8px;margin-bottom:4px"><span class="small"><b>${esc(к.title)}</b></span>
          ${к.worst === 'critical' ? '<span class="chip err">за 3σ</span>'
            : к.worst === 'warning' ? '<span class="chip warn">за 2σ или серия</span>'
            : к.limits && к.limits.enough ? '<span class="chip ok">в пределах</span>'
            : '<span class="chip">мало дней в базе</span>'}</div>
        <div id="spc-a-${esc(к.key)}"></div>
        ${(к.flags || []).length ? `<div class="small dim" style="margin-top:4px">${(к.flags || []).slice(0, 3).map((ф) =>
          `${fmtTime((к.points[ф.index] || {}).ts).slice(0, 5)}: ${num(ф.value, 3)} — ${esc(ф.why)}`).join('; ')}</div>` : ''}
      </div>`).join('');
      карты.forEach((к) => {
        const точки = к.points || [];
        const пределы = к.limits || {};
        const ряд = (v) => точки.map(() => v);
        Charts.line(qs(`#spc-a-${к.key}`), {
          height: 140, labels: точки.map((т) => fmtTime(т.ts).slice(0, 5)),
          series: [
            { name: к.title, values: точки.map((т) => т.value) },
            ...(пределы.enough ? [
              { name: 'среднее базы', values: ряд(пределы.mean) },
              { name: '+2σ', values: ряд(пределы.warn_high) },
              { name: '−2σ', values: ряд(пределы.warn_low) },
            ] : []),
          ],
          emptyText: 'нет данных',
        });
      });
    }
  }

  const т = data.accuracy || {};
  if (qs('#accuracy-body')) {
    const тело = qs('#accuracy-body');
    const общий = т.overall || {};
    const проц = (v, d) => (v === null || v === undefined ? '—' : num(v * 100, d === undefined ? 1 : d) + '%');
    const строка = (р) => `<tr><td>${esc(String(р.key))}</td>
      <td class="num">${num(р.jobs)}</td><td class="num${р.enough ? '' : ' faint'}" title="${р.enough ? 'слов эталона достаточно' : 'меньше десяти тысяч слов эталона — около часа речи; число показано, но значит мало'}">${num(р.words)}${р.enough ? '' : ' <span class="faint">†</span>'}</td>
      <td class="num mono">${проц(р.wer)}</td><td class="num mono">${проц(р.mer)}</td>
      <td class="num mono">${проц(р.wil)}</td>
      <td class="num mono">${р.gap === null || р.gap === undefined ? '—' : (р.gap > 0.02 ? '<span class="warn">' : '') + проц(р.gap) + (р.gap > 0.02 ? '</span>' : '')}</td>
      <td class="num mono">${проц(р.insertion_share)}</td></tr>`;
    const таблица = (заголовок, строки) => (строки || []).length ? `<div class="table-wrap"><table>
      <thead><tr><th>${заголовок}</th><th class="num">Записей</th><th class="num" title="слов эталона">Слов</th>
        <th class="num" title="доля ошибок по словам эталона; не ограничена единицей">WER</th>
        <th class="num" title="доля ошибок среди пар выравнивания; в пределах 0–1">MER</th>
        <th class="num" title="потерянная информация: 1 − H²/(N·M)">WIL</th>
        <th class="num" title="разрыв WER − MER: избыток вставок, то есть галлюцинации">WER − MER</th>
        <th class="num" title="вставок на слово эталона">Вставок</th></tr></thead>
      <tbody>${строки.map(строка).join('')}</tbody></table></div>` : '';
    if (!общий.jobs) {
      тело.innerHTML = `<div class="empty small">За период нет записей с эталоном.
        Эталон приходит полем reference_text при постановке задания или задаётся в карточке записи —
        по нему считаются WER, MER, WIL и калибровка уверенности.</div>`;
    } else {
      тело.innerHTML = `<div class="grid cols-4" style="margin-bottom:10px">
        ${kpi('WER', проц(общий.wer), `${num(общий.jobs)} записей · ${num(общий.words)} слов${общий.enough ? '' : ' · мало слов'}`)}
        ${kpi('MER', проц(общий.mer), 'ограничен единицей')}
        ${kpi('WIL', проц(общий.wil), 'потерянная информация')}
        ${kpi('Вставок', проц(общий.insertion_share), общий.gap > 0.02 ? 'разрыв WER − MER заметен' : 'разрыв WER − MER мал')}
      </div>
      <div class="grid cols-2">
        <div>${таблица('Модель', т.by_model)}${(т.by_language || []).length > 1 ? `<div style="margin-top:8px">${таблица('Язык', т.by_language)}</div>` : ''}</div>
        <div>${таблица('Длительность', т.by_duration)}${(т.by_source || []).length > 1 ? `<div style="margin-top:8px">${таблица('Источник', т.by_source)}</div>` : ''}</div>
      </div>
      ${(т.worst || []).length ? `<div class="small" style="margin-top:8px"><b>Хуже всего</b>: ${т.worst.slice(0, 5).map((з) =>
        `<a href="#" class="link" onclick="window.__asrhub.openJob('${esc(з.id)}');return false">${esc(з.filename || з.id)}</a> — ${проц(з.wer)}`).join('; ')}</div>` : ''}
      <p class="small faint" style="margin-top:8px">${esc(т.note || '')}${
        [...(т.by_model || []), ...(т.by_duration || [])].some((р) => !р.enough) ? ' † — срезу не хватает слов.' : ''}</p>`;
    }
  }

  const сг = data.agreement || {};
  if (qs('#agreement-body')) {
    const тело = qs('#agreement-body');
    const проц = (v, d) => (v === null || v === undefined ? '—' : num(v * 100, d === undefined ? 1 : d) + '%');
    const вердикт = { critical: '<span class="chip err">расхождение выросло вдвое</span>',
      warning: '<span class="chip warn">расхождение растёт</span>',
      ok: '<span class="chip ok">ровно</span>' }[сг.verdict] || '<span class="chip">мало прогонов</span>';
    if (!сг.checks) {
      тело.innerHTML = `<div class="empty small">За период контрольных прогонов не было.
        Задайте контрольную модель в настройках (control_model) — модель другого семейства, —
        и сервер раз в сутки будет заново распознавать несколько случайных записей.</div>`;
    } else {
      тело.innerHTML = `<div class="grid cols-3" style="margin-bottom:10px">
        ${kpi('Расхождение', проц(сг.wer_avg), `p50 ${проц(сг.wer_p50)} · p90 ${проц(сг.wer_p90)}`)}
        ${kpi('Прогонов', num(сг.checks), сг.previous_checks ? `в прошлом периоде ${num(сг.previous_checks)}` : 'первый период')}
        ${kpi('К прошлому периоду', сг.growth === null || сг.growth === undefined ? '—' : (сг.growth > 0 ? '+' : '') + num(сг.growth * 100, 0) + '%',
              сг.previous_wer_avg === null || сг.previous_wer_avg === undefined ? 'сравнивать не с чем' : `было ${проц(сг.previous_wer_avg)}`)}
      </div>
      <div class="row" style="gap:8px;margin-bottom:6px">${вердикт}</div>
      <div id="chart-agreement"></div>
      ${(сг.by_pair || []).length ? `<table style="margin-top:8px"><thead><tr><th>Модель</th><th>Контрольная</th>
        <th class="num">Прогонов</th><th class="num">Расхождение</th><th class="num">MER</th></tr></thead><tbody>
        ${сг.by_pair.map((п) => `<tr><td>${esc(п.model)}</td><td>${esc(п.control_model)}</td>
          <td class="num">${num(п.checks)}</td><td class="num mono">${проц(п.wer_avg)}</td>
          <td class="num mono">${проц(п.mer_avg)}</td></tr>`).join('')}</tbody></table>` : ''}
      ${(сг.worst || []).length ? `<div class="small" style="margin-top:8px"><b>Сильнее всего разошлись</b>: ${сг.worst.slice(0, 5).map((з) =>
        `<a href="#" onclick="window.__asrhub.openJob('${esc(з.job_id)}');return false">${esc(з.filename || з.job_id)}</a> — ${проц(з.wer)}${
          з.snr_db !== null && з.snr_db !== undefined && з.snr_db < 10 ? ' <span class="chip warn" title="шумная запись">шум</span>' : ''}`).join('; ')}</div>` : ''}
      <p class="small faint" style="margin-top:8px">${esc(сг.note || '')}</p>`;
      const дни = сг.by_day || [];
      Charts.line(qs('#chart-agreement'), {
        height: 150, labels: дни.map((д) => fmtTime(д.ts).slice(0, 5)),
        series: [{ name: 'расхождение, %', values: дни.map((д) => (д.wer_avg === null ? null : д.wer_avg * 100)) }],
        emptyText: 'нет прогонов',
      });
    }
  }
  drawReviewQueue();

  const кл = data.calibration || {};
  if (qs('#calibration-body')) {
    const тело = qs('#calibration-body');
    if (!кл.words) {
      тело.innerHTML = '<div class="empty small">Нет записей с эталоном и уверенностью — калибровку считать не по чему</div>';
    } else {
      const корзины = (кл.bins || []).filter((к) => к.words);
      const пункты = (v) => (v === null || v === undefined ? '—'
        : Math.abs(v) < 0.0005 ? '0 п.' : (v > 0 ? '+' : '−') + num(Math.abs(v) * 100, 1) + ' п.');
      тело.innerHTML = `<div class="grid cols-3" style="margin-bottom:10px">
        ${kpi('ECE', num(кл.ece, 3), кл.ece < 0.05 ? 'уверенности можно верить' : кл.ece < 0.1 ? 'небольшое расхождение' : 'уверенность врёт')}
        ${kpi('Переоценка', пункты(кл.overconfidence), Math.abs(кл.overconfidence) < 0.0005 ? 'обещает ровно столько, сколько даёт' : кл.overconfidence > 0 ? 'обещает больше, чем даёт' : 'скромнее, чем есть')}
        ${kpi('AUC', кл.auc === null || кл.auc === undefined ? '—' : num(кл.auc, 3), 'верные от неверных')}
      </div>
      <div id="chart-calibration"></div>
      ${(кл.by_model || []).length > 1 ? `<table style="margin-top:8px"><thead><tr><th>Модель</th><th class="num">Слов</th>
        <th class="num">ECE</th><th class="num">Переоценка</th><th class="num">AUC</th></tr></thead><tbody>
        ${кл.by_model.map((м) => `<tr><td>${esc(м.key)}</td><td class="num">${num(м.words)}</td>
          <td class="num mono">${num(м.ece, 3)}</td><td class="num mono">${пункты(м.overconfidence)}</td>
          <td class="num mono">${м.auc === null ? '—' : num(м.auc, 3)}</td></tr>`).join('')}</tbody></table>` : ''}
      <p class="small faint" style="margin-top:8px">${esc(кл.note || '')} Слов: ${num(кл.words)}, записей: ${num(кл.jobs)}${
        (кл.sources || {}).word && (кл.sources || {}).segment ? `; уверенность по словам у ${num(кл.sources.word)} записей, по сегментам у ${num(кл.sources.segment)}`
        : (кл.sources || {}).word ? '; уверенность по словам' : '; уверенность по сегментам'}.</p>`;
      Charts.line(qs('#chart-calibration'), {
        height: 170, labels: корзины.map((к) => `${num(к.from, 1)}–${num(к.to, 1)}`),
        series: [
          { name: 'доля верных слов', values: корзины.map((к) => к.accuracy) },
          { name: 'средняя уверенность', values: корзины.map((к) => к.confidence) },
        ],
        emptyText: 'нет данных',
      });
    }
  }

  const лт = data.latency || {};
  if (qs('#latency-body')) {
    const тело = qs('#latency-body');
    const общий = лт.overall || {};
    const строка = (р) => `<tr><td>${esc(String(р.key))}</td><td class="num">${num(р.jobs)}</td>
      <td class="num mono">${num(р.processing_p50, 1)} / ${num(р.processing_p95, 1)} / ${num(р.processing_p99, 1)}</td>
      <td class="num mono">${р.rtf_p50 === null ? '—' : num(р.rtf_p50, 3)} / ${р.rtf_p95 === null ? '—' : num(р.rtf_p95, 3)}</td>
      <td class="num mono">${р.queue_p95 === null || р.queue_p95 === undefined ? '—' : num(р.queue_p95, 1)}</td></tr>`;
    const таблица = (заголовок, строки) => (строки || []).length ? `<table>
      <thead><tr><th>${заголовок}</th><th class="num">Заданий</th>
        <th class="num" title="время обработки: p50 / p95 / p99, с">Обработка, с</th>
        <th class="num" title="RTF: p50 / p95">RTF</th>
        <th class="num" title="ожидание в очереди, p95, с">Очередь p95</th></tr></thead>
      <tbody>${строки.map(строка).join('')}</tbody></table>` : '';
    const поток = лт.stream || {};
    if (!общий.jobs && !поток.sessions) {
      тело.innerHTML = '<div class="empty small">За период нет заданий, посчитанных сервером самим</div>';
    } else {
      тело.innerHTML = `<div class="grid cols-3" style="margin-bottom:10px">
        ${kpi('Обработка p95', общий.processing_p95 === null || общий.processing_p95 === undefined ? '—' : fmtDur(общий.processing_p95), `p50 ${fmtDur(общий.processing_p50 || 0)} · p99 ${fmtDur(общий.processing_p99 || 0)}`)}
        ${kpi('RTF p95', общий.rtf_p95 === null || общий.rtf_p95 === undefined ? '—' : num(общий.rtf_p95, 3), `p50 ${общий.rtf_p50 === null || общий.rtf_p50 === undefined ? '—' : num(общий.rtf_p50, 3)}`)}
        ${kpi('Поток: первый текст', поток.first_text_p95 === null || поток.first_text_p95 === undefined ? '—' : `${num(поток.first_text_p95, 1)} с`,
              поток.sessions ? `p95 · p50 ${num(поток.first_text_p50, 1)} с · сессий ${num(поток.sessions)}` : 'сессий не было')}
      </div>
      ${таблица('Модель', лт.by_model)}
      <div style="margin-top:8px">${таблица('Длительность', лт.by_duration)}</div>
      ${(поток.by_model || []).length ? `<table style="margin-top:8px"><thead><tr><th>Поток: модель</th><th class="num">Сессий</th>
        <th class="num" title="секунды до первого текста: p50 / p95">До первого текста, с</th></tr></thead><tbody>
        ${поток.by_model.map((м) => `<tr><td>${esc(м.key)}</td><td class="num">${num(м.sessions)}</td>
          <td class="num mono">${num(м.first_text_p50, 1)} / ${num(м.first_text_p95, 1)}</td></tr>`).join('')}</tbody></table>` : ''}
      <p class="small faint" style="margin-top:8px">${esc(лт.note || '')}</p>`;
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
        Говорящих в записи: ${a.speakers.map((x) => `${x.speakers} — ${x.jobs}`).join(', ')}</div>` : ''}
      ${a.measured_jobs ? `<div style="margin-top:12px">
        <div class="grid cols-4" style="margin-bottom:10px">
          ${kpi('Речь к шуму', `${num((a.snr_db || {}).p50, 0)} дБ`, `p10 ${num((a.snr_db || {}).p10, 0)} · p90 ${num((a.snr_db || {}).p90, 0)} дБ`)}
          ${kpi('Плохой звук', a.bad_audio_share === null ? '—' : pct(a.bad_audio_share, 0),
                `шумных ${num(a.noisy_jobs)}, с клиппингом ${num(a.clipped_jobs)} из ${num(a.measured_jobs)}`)}
          ${kpi('Громкость', (a.loudness_lufs || {}).count ? `${num(a.loudness_lufs.p50, 0)} LUFS` : '—',
                (a.loudness_lufs || {}).count ? `p10 ${num(a.loudness_lufs.p10, 0)} · p90 ${num(a.loudness_lufs.p90, 0)}` : '')}
          ${kpi('Тишины', (a.silence_share || {}).count ? pct(a.silence_share.avg, 0) : '—', 'в среднем по записи')}
        </div>
        <table><thead><tr><th title="отношение речи к шуму, оценка по кадрам">Речь к шуму</th><th class="num">Записей</th>
          <th class="num" title="средняя уверенность модели">Уверенность</th>
          <th class="num" title="доля записей с уверенностью ниже 0,75">Низкой</th>
          <th class="num" title="WER по записям с эталоном">WER</th>
          <th class="num" title="средняя доля подозрительных сегментов">Подозр.</th></tr></thead><tbody>
          ${(a.snr_bands || []).map((б) => `<tr><td>${esc(б.key)}</td><td class="num">${num(б.jobs)}</td>
            <td class="num mono">${б.confidence_avg === null ? '—' : pct(б.confidence_avg, 1)}</td>
            <td class="num mono">${б.low_confidence_share === null ? '—' : pct(б.low_confidence_share, 0)}</td>
            <td class="num mono">${б.wer_avg === null ? '—' : pct(б.wer_avg, 1) + `<span class="faint"> (${num(б.wer_jobs)})</span>`}</td>
            <td class="num mono">${б.suspect_share_avg === null ? '—' : pct(б.suspect_share_avg, 1)}</td></tr>`).join('')}</tbody></table>
        ${(a.bad_audio_by_source || []).some((и) => и.bad) ? `<div class="small faint" style="margin-top:8px">Плохой звук по источникам: ${
          a.bad_audio_by_source.filter((и) => и.bad).map((и) => `${esc(и.key)} — ${pct(и.share, 0)} (${num(и.bad)} из ${num(и.jobs)})`).join(', ')}</div>` : ''}
        <div class="small faint" style="margin-top:6px">SNR — оценка по кадрам без эталона: громкие кадры считаются речью, тихие — шумом; плохой звук — ниже 10 дБ или клиппинг от 1 % отсчётов (пороги Deepgram). WER при 20 дБ обычно около 3–4 %, при 10 дБ — 15 %, при 5 дБ — за 30 %.</div>
      </div>` : ''}`;
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
  objections: 'objection_unhandled', violations: 'violation',
  low_score: 'low_score', impolite: 'impolite',
};


/* Показатель со шкалой, подсказкой и сравнением с прошлым периодом — тем
 * же видом, что и остальные карточки раздела. Пусто показывается прочерком,
 * а не нулём: «не считали» и «ноль» — разные утверждения, и для стресса
 * второе значит «спокойно», то есть прямо противоположное первому. */
function пкпи(имя, сейчас, раньше, ключ, знаков, признак) {
  const значение = сейчас[ключ];
  const есть = значение !== null && значение !== undefined;
  const единица = (признак || {}).unit || '';
  return kpi(имя, есть ? num(значение, знаков) : '—',
             `${esc(единица)}${(признак || {}).hint
               ? `<span class="hint" title="${esc(признак.hint)}"> ⓘ</span>` : ''}`
             + delta(значение, (раньше || {})[ключ], признак));
}

/* Полоса «сколько из скольких» с долей. Доля и знаменатель рядом с числом
 * обязательны: «42 записи» не говорит ничего, пока не сказано, из скольких,
 * а показатель считается не по всякой записи. */
function полоса(имя, сколько, из, вид) {
  const всего = Number(из || 0);
  const часть = Number(сколько || 0);
  const доля = всего > 0 ? (часть / всего) * 100 : null;
  const цвет = вид === 'ok' ? 'var(--ok)' : доля >= 25 ? 'var(--warn)' : 'var(--accent)';
  return `<div style="margin-bottom:10px">
    <div class="row small" style="gap:8px">
      <span>${esc(имя)}</span><span class="spacer"></span>
      <b>${доля === null ? '—' : `${num(доля, 1)} %`}</b>
      <span class="dim">${num(часть, 0)} из ${num(всего, 0)}</span>
    </div>
    <div style="height:6px;border-radius:3px;background:var(--border-soft);margin-top:4px">
      <div style="height:6px;border-radius:3px;width:${Math.max(0, Math.min(100, доля || 0))}%;
                  background:${цвет}"></div>
    </div></div>`;
}

/* Разрезы, в которых имеет смысл смотреть напряжение и NPS. Перечень общий
 * для обеих вкладок: вопрос «где тяжелее» и вопрос «где хуже оценка» — это
 * один и тот же вопрос про одни и те же группы. */
const ЭМОЦИЯ_РАЗРЕЗЫ = [
  ['agent', 'По сотрудникам'], ['queue', 'По очередям'], ['station', 'По АТС'],
  ['category', 'По категориям'], ['direction', 'По направлению'],
  ['hour', 'По часам'], ['weekday', 'По дням недели'], ['owner', 'По владельцу'],
];

const CONTENT_TABS = [
  { key: 'summary',    title: 'Свод' },
  { key: 'emotion',    title: 'Эмоциональный фон и стресс' },
  { key: 'clarity',    title: 'Понятность и точность' },
  { key: 'nps',        title: 'NPS' },
  { key: 'categories', title: 'Категории' },
  { key: 'agents',     title: 'Операторы' },
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

/** Фишка «относительно нормы»: по статусу и по тому, куда лучше. */
function НОРМА_ФИШКА(status, good) {
  if (!status) return '<span class="faint small">—</span>';
  const выше = status === 'above' || status === 'outlier_high';
  const выброс = status.startsWith('outlier');
  if (status === 'inside') return '<span class="chip ok">в норме</span>';
  const плохо = good ? (выше ? good < 0 : good > 0) : false;
  const cls = выброс ? (плохо ? 'err' : good ? 'ok' : 'warn') : (плохо ? 'warn' : good ? 'ok' : '');
  return `<span class="chip ${cls}">${выброс ? 'выброс: ' : ''}${выше ? 'выше нормы' : 'ниже нормы'}</span>`;
}

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

// ==========================================================================
// Вид: Тренды
// ==========================================================================

/* Тренды: все измеримые величины сервера на одной оси времени.
 *
 * Остальные разделы отвечают «как дела сейчас» и сравнивают период с
 * предыдущим одним числом. Здесь отвечают на вопрос «когда это началось»:
 * полсотни рядов с общими корзинами, сравнение с прошлым периодом,
 * разложение по часам недели и связи между рядами.
 *
 * Один запрос на всё: ряды для мелких графиков, свод для таблицы и данные
 * главного графика приходят вместе — иначе полсотни показателей означали бы
 * полсотни запросов.
 */
RENDERERS.trends = {
  async render(root) {
    if (!state.trendsPeriod) state.trendsPeriod = 'month';
    if (!state.trendsBucket) state.trendsBucket = 'auto';
    if (!state.trendsSelected) state.trendsSelected = ['jobs', 'rtf', 'confidence'];
    if (!state.trendsSmooth) state.trendsSmooth = '0';
    if (!state.trendsNorm) state.trendsNorm = 'auto';
    if (state.trendsCompare === undefined) state.trendsCompare = true;
    if (!state.trendsGroup) state.trendsGroup = '';

    root.innerHTML = `
      <div class="settings-toolbar">
        <select id="tr-period" style="width:150px">
          <option value="day">Сутки</option>
          <option value="week">Неделя</option>
          <option value="month">Месяц</option>
          <option value="quarter">Квартал</option>
          <option value="year">Год</option>
        </select>
        <select id="tr-bucket" style="width:140px">
          <option value="auto">Шаг: авто</option>
          <option value="hour">Шаг: час</option>
          <option value="day">Шаг: сутки</option>
          <option value="week">Шаг: неделя</option>
          <option value="month">Шаг: месяц</option>
        </select>
        <select id="tr-norm" style="width:190px">
          <option value="auto">Шкала: по единицам</option>
          <option value="index">Шкала: индекс (среднее = 100)</option>
          <option value="z">Шкала: отклонения</option>
        </select>
        <select id="tr-smooth" style="width:170px">
          <option value="0">Без сглаживания</option>
          <option value="3">Сглаживание по 3</option>
          <option value="7">Сглаживание по 7</option>
        </select>
        <label class="row small" style="gap:6px;cursor:pointer"><input type="checkbox" id="tr-compare"
          ${state.trendsCompare ? 'checked' : ''} style="width:auto">сравнить с прошлым периодом</label>
        <span class="spacer"></span>
        <input type="search" id="tr-search" placeholder="поиск показателя" value="${esc(state.trendsSearch || '')}"
          style="width:200px">
        <button id="tr-xlsx" class="ghost sm">В Excel</button>
        <button id="tr-csv" class="ghost sm">В CSV</button>
      </div>
      <div id="trends-body"><div class="empty">Считаем ряды…</div></div>`;

    qs('#tr-period').value = state.trendsPeriod;
    qs('#tr-bucket').value = state.trendsBucket;
    qs('#tr-norm').value = state.trendsNorm;
    qs('#tr-smooth').value = state.trendsSmooth;
    qs('#tr-period').onchange = (e) => { state.trendsPeriod = e.target.value; this.load(); };
    qs('#tr-bucket').onchange = (e) => { state.trendsBucket = e.target.value; this.load(); };
    qs('#tr-norm').onchange = (e) => { state.trendsNorm = e.target.value; this.draw(); };
    qs('#tr-smooth').onchange = (e) => { state.trendsSmooth = e.target.value; this.draw(); };
    qs('#tr-compare').onchange = (e) => { state.trendsCompare = e.target.checked; this.load(); };
    let таймер;
    qs('#tr-search').addEventListener('input', (e) => {
      clearTimeout(таймер);
      state.trendsSearch = e.target.value;
      таймер = setTimeout(() => this.draw(), 250);
    });
    const выгрузить = (fmt) => {
      const params = new URLSearchParams({ period: state.trendsPeriod, bucket: state.trendsBucket, fmt });
      if (state.trendsSelected.length) params.set('metrics', state.trendsSelected.join(','));
      downloadReport(`/api/trends/export?${params}`, fmt, `asrhub-тренды-${state.trendsPeriod}`);
    };
    qs('#tr-xlsx').onclick = () => выгрузить('xlsx');
    qs('#tr-csv').onclick = () => выгрузить('csv');
    await this.load();
  },

  async load() {
    const host = qs('#trends-body');
    if (!host) return;
    const params = new URLSearchParams({
      period: state.trendsPeriod, bucket: state.trendsBucket,
      compare: state.trendsCompare ? 'true' : 'false' });
    try {
      state.trendsData = await API.latest('trends', `/api/trends?${params}`);
    } catch (err) {
      if (err && err.silent) return;
      if (host.isConnected) {
        host.innerHTML = `<div class="card"><div class="empty">Ряды не получены: ${
          esc((err && err.message) || 'ошибка запроса')}</div></div>`;
      }
      return;
    }
    if (!host.isConnected) return;
    // Соседние карточки наполняет сам draw(): он пересоздаёт их разметку,
    // и звать их отсюда значило бы делать по два запроса на каждый заход.
    this.draw();
  },

  /* Сглаживание и приведение к общей шкале — в интерфейсе, а не на сервере:
   * это способ смотреть, а не то, что измерено. Сервер отдаёт числа как
   * есть, и выгрузка совпадает с тем, что посчитано. */
  prepare(values) {
    let ряд = (values || []).slice();
    const окно = parseInt(state.trendsSmooth, 10) || 0;
    if (окно > 1) {
      const сглажено = ряд.map((_, i) => {
        const кусок = ряд.slice(Math.max(0, i - окно + 1), i + 1).filter((v) => v !== null);
        return кусок.length ? кусок.reduce((a, b) => a + b, 0) / кусок.length : null;
      });
      ряд = сглажено;
    }
    return ряд;
  },

  normalize(ряд, режим) {
    const есть = ряд.filter((v) => v !== null);
    if (!есть.length) return ряд;
    if (режим === 'index') {
      // Опора — среднее по периоду, а не первая точка. Первая точка была
      // именно тем, что ломало график: корзина с одним заданием давала
      // базу «1», и ряд заданий улетал к шести тысячам, прижимая RTF и
      // уверенность к нулю — то есть ровно к той картинке, ради ухода от
      // которой шкалу и переводят в индекс.
      // Опора — модуль среднего. Деление на отрицательное среднее
      // переворачивает знак вместе с направлением: падающая тональность
      // (−0,1 → −0,5) в индексе РОСЛА со 33 до 167, и линия убедительно
      // показывала улучшение там, где всё стало хуже. А режим включается
      // сам, стоит взять на график два показателя в разных единицах.
      const среднее = есть.reduce((a, b) => a + b, 0) / есть.length;
      const опора = Math.abs(среднее) > 1e-9 ? Math.abs(среднее) : 1;
      return ряд.map((v) => (v === null ? null : (v / опора) * 100));
    }
    if (режим === 'z') {
      const среднее = есть.reduce((a, b) => a + b, 0) / есть.length;
      const дисп = есть.reduce((a, b) => a + (b - среднее) ** 2, 0) / есть.length;
      const сигма = Math.sqrt(дисп) || 1;
      return ряд.map((v) => (v === null ? null : (v - среднее) / сигма));
    }
    return ряд;
  },

  draw() {
    const host = qs('#trends-body');
    const д = state.trendsData;
    if (!host || !д) return;
    const поиск = (state.trendsSearch || '').toLowerCase();
    const все = д.summary || [];
    const выбраны = state.trendsSelected.filter((и) => все.some((с) => с.id === и));
    const ряды = new Map((д.series || []).map((р) => [р.id, р]));
    const метки = (д.buckets || []).map((ts) => fmtBucket(ts, д.step_s));

    // Разные единицы на одной оси врут о соотношении величин, поэтому при
    // смешанном наборе шкала сама переходит в индекс — и об этом сказано.
    const единицы = new Set(выбраны.map((и) => (ряды.get(и) || {}).unit || ''));
    const режим = state.trendsNorm === 'auto'
      ? (единицы.size > 1 ? 'index' : 'none') : state.trendsNorm;

    host.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Выбранные показатели</h3>
          <span class="hint">${esc(периодПодпись(д))}${режим === 'index' && state.trendsNorm === 'auto'
            ? ' · единицы разные, поэтому шкала переведена в индекс (среднее за период = 100)' : ''}</span>
          <span class="spacer"></span>
          <span class="chip">${num(выбраны.length)} из ${num(все.length)}</span></div>
        <div id="tr-main"></div>
        <div id="tr-legend" class="row wrap" style="gap:10px"></div>
        ${выбраны.length ? '' : '<div class="empty small">Отметьте показатели ниже — они появятся здесь.</div>'}
      </section>

      <div class="grid cols-2" style="margin-bottom:16px;align-items:start">
        <section class="card">
          <div class="card-head"><h3>По часам недели</h3>
            <span class="hint">когда именно это происходит</span>
            <span class="spacer"></span>
            <select id="tr-heat-metric" style="width:220px">
              ${все.map((с) => `<option value="${esc(с.id)}" ${с.id === (state.trendsHeat || выбраны[0]) ? 'selected' : ''}>${esc(с.label)}</option>`).join('')}
            </select></div>
          <div id="tr-heat"><div class="empty small">Считаем…</div></div>
        </section>
        <section class="card">
          <div class="card-head"><h3>Движутся вместе</h3>
            <span class="hint">связь, а не причина: повод посмотреть глазами</span></div>
          <div id="tr-corr"><div class="empty small">Считаем…</div></div>
        </section>
      </div>

      <div id="tr-groups"></div>

      <section class="card">
        <div class="card-head"><h3>Все показатели за период</h3>
          <span class="hint">среднее по корзинам, крайние значения и изменение к прошлому периоду</span></div>
        <div class="table-wrap full"><table><thead><tr>
          <th></th><th>Показатель</th><th>Группа</th><th class="num">Среднее</th>
          <th class="num">Минимум</th><th class="num">Максимум</th><th class="num">Последнее</th>
          <th class="num">Изменение</th><th>Куда идёт</th></tr></thead><tbody id="tr-table"></tbody></table></div>
      </section>`;

    // Главный график.
    const место = qs('#tr-main', host);
    const серии = выбраны.map((и) => {
      const р = ряды.get(и) || {};
      return { name: р.label || и,
               values: this.normalize(this.prepare(р.values), режим) };
    });
    if (серии.length) {
      // Высота задаётся графику, а не коробке вокруг него: Charts.line
      // рисует 220 пикселей по умолчанию и не растягивается под контейнер,
      // поэтому «height:300px» на обёртке давал не большой график, а
      // прежний график и восемьдесят пикселей пустоты под ним.
      window.Charts.line(место, { series: серии, labels: метки, tip: true, height: 300 });
      // Своей легенды здесь нет: Charts.line рисует её сам для двух рядов и
      // больше. Вторая — та, что стояла тут раньше, — просто дублировала
      // первую под графиком, и на снимке это выглядело как ошибка вёрстки.
      // Один ряд легенды не требует: его называет заголовок карточки.
      const легенда = qs('#tr-legend', host);
      if (легенда && серии.length === 1) {
        легенда.innerHTML = `<span class="small dim">${esc(серии[0].name)}</span>`;
      }
    } else {
      window.Charts.empty(место, 'Показатели не выбраны');
    }

    // Мелкие графики по группам — то, ради чего раздел и заводится: полсотни
    // рядов рядом, каждый со своим направлением «лучше».
    const группы = {};
    все.forEach((с) => {
      if (поиск && !`${с.label} ${с.id} ${с.group}`.toLowerCase().includes(поиск)) return;
      (группы[с.group] = группы[с.group] || []).push(с);
    });
    const коробка = qs('#tr-groups', host);
    заполнитьГруппы(коробка, группы, ряды, выбраны, (id) => this.toggle(id), (р) => this.prepare(р));
    if (!Object.keys(группы).length) {
      коробка.innerHTML = '<div class="card"><div class="empty">Ничего не нашлось по этому запросу.</div></div>';
    }

    // Обе соседние карточки живут в разметке, которую только что собрал
    // этот же метод, — значит, и наполнять их надо здесь. Раньше их звал
    // только `load()`, а `draw()` без него вызывают смена шкалы, смена
    // сглаживания, поиск, галочка показателя и щелчок по паре: разметка
    // пересоздавалась, и обе карточки навсегда оставались на «Считаем…».
    this.loadHeatmap();
    this.loadCorrelations();

    // Таблица.
    const тело = qs('#tr-table', host);
    тело.innerHTML = все.filter((с) => !поиск
      || `${с.label} ${с.id} ${с.group}`.toLowerCase().includes(поиск)).map((с) => `<tr>
        <td class="pick"><input type="checkbox" data-metric="${esc(с.id)}"
          ${выбраны.includes(с.id) ? 'checked' : ''} style="width:auto"></td>
        <td><b>${esc(с.label)}</b>${с.hint ? `<div class="small faint">${esc(с.hint)}</div>` : ''}</td>
        <td class="small dim">${esc(с.group)}</td>
        <td class="num mono">${значение(с, с.avg)}</td>
        <td class="num mono">${значение(с, с.min)}</td>
        <td class="num mono">${значение(с, с.max)}</td>
        <td class="num mono">${значение(с, с.last)}</td>
        <td class="num mono">${изменение(с)}</td>
        <td>${вердикт(с)}</td></tr>`).join('');
    qsa('input[data-metric]', host).forEach((кн) => {
      кн.onchange = () => this.toggle(кн.dataset.metric);
    });
    const выбор = qs('#tr-heat-metric', host);
    if (выбор) выбор.onchange = () => { state.trendsHeat = выбор.value; this.loadHeatmap(); };
  },

  toggle(id) {
    const набор = new Set(state.trendsSelected);
    if (набор.has(id)) набор.delete(id);
    else if (набор.size >= 8) { toast('На одном графике больше восьми рядов не читаются', 'warn'); return; }
    else набор.add(id);
    state.trendsSelected = [...набор];
    this.draw();
  },

  async loadHeatmap() {
    const host = qs('#tr-heat');
    if (!host) return;
    const метрика = state.trendsHeat || state.trendsSelected[0] || 'jobs';
    let д;
    try {
      д = await API.latest('trends-heat',
        `/api/trends/heatmap?metric=${encodeURIComponent(метрика)}&period=${state.trendsPeriod}`);
    } catch (err) {
      if (err && err.silent) return;
      if (host.isConnected) host.innerHTML = `<div class="empty small">Карта не получена: ${
        esc((err && err.message) || 'ошибка запроса')}</div>`;
      return;
    }
    if (!host.isConnected) return;
    // Пустые клетки передаём как есть. Подстановка нуля стоила двух вещей
    // сразу: у показателя с отрицательными значениями ноль сам по себе
    // значение (и картина искажалась), а у показателя без данных вообще
    // вся карта заливалась одним тоном вместо честной пустоты — при
    // подписи «от — до —» под ней.
    window.Charts.grid(host, {
      rows: д.days || [], cols: Array.from({ length: 24 }, (_, i) => (i % 3 === 0 ? String(i) : '')),
      values: д.grid || [], emptyText: 'За период нет данных' });
    const подпись = document.createElement('div');
    подпись.className = 'small faint';
    подпись.style.marginTop = '6px';
    // Говорим, ЧТО именно в клетке. Для счётных показателей это среднее за
    // такой час недели, а не сумма по периоду: иначе одна и та же клетка
    // показывает 3 за неделю и 27 за квартал, и число отвечает на вопрос
    // про длину периода, а не про час недели.
    подпись.textContent = `${д.label}: от ${fmtNumSafe(д.min)} до ${fmtNumSafe(д.max)}${
      д.unit ? ` ${д.unit}` : ''}, среднее ${fmtNumSafe(д.avg)}${
      д.per ? ` · ${д.per}` : ''}`;
    host.appendChild(подпись);
  },

  async loadCorrelations() {
    const host = qs('#tr-corr');
    if (!host) return;
    let д;
    try {
      д = await API.latest('trends-corr',
        `/api/trends/correlations?period=${state.trendsPeriod}&bucket=${state.trendsBucket}&limit=10`);
    } catch (err) {
      if (err && err.silent) return;
      if (host.isConnected) host.innerHTML = `<div class="empty small">Связи не посчитаны: ${
        esc((err && err.message) || 'ошибка запроса')}</div>`;
      return;
    }
    if (!host.isConnected) return;
    const пары = д.pairs || [];
    // Список ограничен по высоте и прокручивается: карточка стоит рядом с
    // сеткой часов недели, и без предела десять пар растягивали строку
    // сетки вдвое, оставляя под ней пустое поле в пол-экрана.
    host.innerHTML = пары.length ? `<div class="analysis-lines pairs"
        style="max-height:330px;overflow:auto">${пары.map((п) => `
      <div class="analysis-line">
        <span class="ts mono" style="white-space:nowrap">${п.r > 0 ? '+' : ''}${num(п.r, 2)}</span>
        <span class="what">
          <a href="#" data-pair="${esc(п.a)},${esc(п.b)}"
             title="Показать оба ряда на графике">${esc(п.a_label)} ↔ ${esc(п.b_label)}</a>
          <span class="faint small" style="white-space:nowrap">по ${num(п.points)} ${
            plural(п.points, 'общей точке', 'общим точкам', 'общим точкам')}</span>
        </span>
      </div>`).join('')}</div>`
      : '<div class="empty small">Пар с устойчивой связью не нашлось: нужно хотя бы восемь общих точек.</div>';
    qsa('a[data-pair]', host).forEach((ссылка) => {
      ссылка.onclick = (e) => {
        e.preventDefault();
        state.trendsSelected = ссылка.dataset.pair.split(',');
        this.draw();
      };
    });
  },
};

/* Подписи и мелкая арифметика раздела трендов. */
function fmtBucket(ts, step) {
  const d = new Date(ts * 1000);
  const дата = `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`;
  if (step <= 3600) return `${дата} ${String(d.getHours()).padStart(2, '0')}:00`;
  return дата;
}

function fmtNumSafe(value) {
  return (value === null || value === undefined) ? '—' : num(value, 2);
}

function периодПодпись(д) {
  const шаги = { hour: 'по часам', day: 'по суткам', week: 'по неделям', month: 'по месяцам' };
  return `${(д.buckets || []).length} точек ${шаги[д.bucket] || ''}`;
}

function значение(с, v) {
  if (v === null || v === undefined) return '—';
  return `${num(v, с.digits)}${с.unit ? ` ${esc(с.unit)}` : ''}`;
}

function изменение(с) {
  if (с.change_percent === null || с.change_percent === undefined) return '—';
  const знак = с.change_percent > 0 ? '+' : '';
  return `${знак}${num(с.change_percent, 1)} %`;
}

function вердикт(с) {
  if (!с.verdict) return '';
  const класс = с.verdict.includes('хуже') ? 'err' : с.verdict.includes('лучше') ? 'ok' : '';
  return `<span class="chip ${класс}">${esc(с.verdict)}</span>`;
}

/* Мелкие графики по группам: строка на показатель, спарклайн, последнее
 * значение и изменение. Клик добавляет ряд на главный график. */
function заполнитьГруппы(коробка, группы, ряды, выбраны, переключить, готовить) {
  if (!коробка) return;
  коробка.innerHTML = '';
  Object.entries(группы).forEach(([имя, элементы]) => {
    const section = h(`<section class="card">
      <div class="card-head"><h3>${esc(имя[0].toUpperCase() + имя.slice(1))}</h3>
        <span class="spacer"></span><span class="chip">${элементы.length}</span></div>
      <div class="grid cols-3" data-cells></div></section>`);
    const сетка = qs('[data-cells]', section);
    элементы.forEach((с) => {
      const карточка = h(`<div class="kpi" style="cursor:pointer" title="${esc(с.hint || с.label)}">
        <div class="row"><span class="kpi-label">${esc(с.label)}</span><span class="spacer"></span>
          ${выбраны.includes(с.id) ? '<span class="chip ok">на графике</span>' : ''}</div>
        <div class="row" style="align-items:baseline;gap:8px">
          <span class="kpi-value">${значение(с, с.last === null ? с.avg : с.last)}</span>
          <span class="kpi-trend ${с.change_percent > 0 ? 'up' : с.change_percent < 0 ? 'down' : ''}">${изменение(с)}</span>
        </div>
        <div data-spark style="height:34px"></div></div>`);
      const ряд = ряды.get(с.id) || {};
      // Пустые корзины передаём как есть: `Charts.spark` умеет их
      // пропускать (splitRuns), а подстановка нуля рисовала в одной
      // карточке линию, четырежды падающую в ноль, рядом с подписью
      // «Минимум 0.058» — то есть график спорил с числом под ним.
      window.Charts.spark(qs('[data-spark]', карточка), готовить(ряд.values || []));
      карточка.onclick = () => переключить(с.id);
      сетка.appendChild(карточка);
    });
    коробка.appendChild(section);
  });
}

/* ===========================================================================
 * Раздел «Телефония»: разговоры с АТС Asterisk
 * =========================================================================*/

const ТЕЛЕФОНИЯ_ПЕРИОДЫ = { day: 'Сутки', week: 'Неделя', month: 'Месяц',
                            quarter: 'Квартал', year: 'Год', all: 'Всё время' };

/* Итоги звонка, как их называет Asterisk. Английские слова в русском
 * журнале — не строгость, а лень: «BUSY» и «занято» читаются по-разному. */
const ИТОГ_ЗВОНКА = {
  ANSWERED: 'ответили', 'NO ANSWER': 'не ответили', BUSY: 'занято',
  FAILED: 'не дозвонились', CONGESTION: 'сеть занята',
};

RENDERERS.telephony = {
  async render(root) {
    if (!state.telPeriod) state.telPeriod = 'week';
    if (!state.telDirection) state.telDirection = '';
    if (!state.telQueue) state.telQueue = '';
    if (!state.telAgent) state.telAgent = '';
    if (state.telOnlyQueued === undefined) state.telOnlyQueued = false;
    state.telOffset = 0;

    root.innerHTML = `
      <div id="tel-status"></div>
      <div class="settings-toolbar">
        <div class="group-nav" id="tel-period">
          ${Object.entries(ТЕЛЕФОНИЯ_ПЕРИОДЫ).map(([k, v]) =>
            `<button data-period="${k}" class="${state.telPeriod === k ? 'active' : ''}">${v}</button>`
          ).join('')}
        </div>
        <select id="tel-direction" style="width:150px">
          <option value="">Все направления</option>
          <option value="входящий">Входящие</option>
          <option value="исходящий">Исходящие</option>
          <option value="внутренний">Внутренние</option>
        </select>
        <select id="tel-station" style="width:180px"><option value="">Все АТС</option></select>
        <select id="tel-queue" style="width:170px"><option value="">Все очереди</option></select>
        <select id="tel-agent" style="width:170px"><option value="">Все операторы</option></select>
        <label class="row small" style="gap:6px;cursor:pointer"><input type="checkbox" id="tel-queued"
          ${state.telOnlyQueued ? 'checked' : ''} style="width:auto">только распознанные</label>
        <span class="spacer"></span>
        <input type="search" id="tel-search" placeholder="номер или идентификатор"
          value="${esc(state.telSearch || '')}" style="width:220px">
      </div>
      <div id="tel-calls"><div class="empty">Загрузка…</div></div>`;

    qsa('#tel-period button').forEach((b) => b.addEventListener('click', () => {
      state.telPeriod = b.dataset.period;
      state.telOffset = 0;
      qsa('#tel-period button').forEach((x) => x.classList.toggle('active', x === b));
      this.loadCalls();
    }));
    ['tel-direction', 'tel-station', 'tel-queue', 'tel-agent'].forEach((ид) => {
      const поле = qs(`#${ид}`);
      if (поле) поле.addEventListener('change', () => {
        state[{ 'tel-direction': 'telDirection', 'tel-station': 'telStation',
                'tel-queue': 'telQueue', 'tel-agent': 'telAgent' }[ид]] = поле.value;
        state.telOffset = 0;
        this.loadCalls();
      });
    });
    const только = qs('#tel-queued');
    if (только) только.addEventListener('change', () => {
      state.telOnlyQueued = только.checked;
      state.telOffset = 0;
      this.loadCalls();
    });
    const поиск = qs('#tel-search');
    let таймер = null;
    if (поиск) поиск.addEventListener('input', () => {
      clearTimeout(таймер);
      таймер = setTimeout(() => {
        state.telSearch = поиск.value.trim();
        state.telOffset = 0;
        this.loadCalls();
      }, 300);
    });

    await this.loadStatus();
    await this.loadDimensions();
    await this.loadCalls();
  },

  /* Карточка состояния: включено ли, что за источник, что мешает.
   * Ошибка источника показывается прямо здесь и первой строкой: молчащий
   * импорт выглядит точно так же, как «звонков не было». */
  async loadStatus() {
    const коробка = qs('#tel-status');
    if (!коробка) return;
    let свод;
    try {
      свод = await API.get('/api/telephony/status');
    } catch (err) {
      if (err && err.code === 'aborted') return;
      коробка.innerHTML = `<div class="empty">Состояние телефонии недоступно: ${esc(err.message || '')}</div>`;
      return;
    }
    state.telStatus = свод;
    const звонки = свод.calls || {};
    const архив = свод.archive || звонки;
    const админ = (state.me || {}).role === 'admin';
    const станции = свод.stations || [];
    const беда = станции.filter((с) => с.enabled && свод.enabled
                                       && (с.last_error || !с.running));
    const причины = Object.entries(архив.reasons || {})
      .map(([п, n]) => `<span class="chip" title="звонков пропущено по этой причине">${esc(п)}: ${n}</span>`)
      .join(' ');
    // Шапка журнала отвечает коротко: доезжают ли записи вообще. Подробности
    // по каждой станции — в разделе «АТС», и вести туда честнее, чем
    // пересказывать их здесь мелким шрифтом.
    коробка.innerHTML = `
      <section class="card" style="margin-bottom:14px">
        <div class="card-head">
          <h3>Забор записей с АТС</h3>
          <span class="chip ${свод.enabled ? (свод.running ? 'ok' : 'warn') : ''}">${
            !свод.enabled ? 'выключено'
              : свод.running ? `в работе ${свод.running} из ${свод.configured}`
              : 'включено, но потоки стоят'}</span>
          ${беда.length ? `<span class="chip err" title="${esc(беда.map((с) =>
            `${с.name}: ${с.last_error || 'поток не запущен'}`).join('; '))}">${
            беда.length} ${plural(беда.length, 'станция требует', 'станции требуют',
            'станций требуют')} внимания</span>` : ''}
          <span class="spacer"></span>
          ${админ ? `<button class="btn sm" id="tel-scan"
            title="Заход за новыми звонками по всем включённым станциям">Забрать сейчас</button>` : ''}
          <button class="ghost sm" onclick="__asrhub.go('pbx')"
            title="Состояние станций, нагрузка, очереди и операторы">Раздел «АТС» →</button>
        </div>
        <div class="grid cols-4" style="padding:14px 16px">
          <div class="kpi"><span class="kpi-label">Всего звонков</span>
            <span class="kpi-value">${num(архив.total || 0, 0)}</span>
            <span class="small dim">за всё время</span></div>
          <div class="kpi"><span class="kpi-label">Распознано</span>
            <span class="kpi-value">${num(архив.queued || 0, 0)}</span>
            <span class="small dim">поставлено в очередь</span></div>
          <div class="kpi"><span class="kpi-label">Пропущено</span>
            <span class="kpi-value">${num(архив.skipped || 0, 0)}</span>
            <span class="small dim">короткие, без ответа, без записи</span></div>
          <div class="kpi"><span class="kpi-label">Наговорено</span>
            <span class="kpi-value">${fmtDur(архив.talk_s || 0)}</span>
            <span class="small dim">${num(архив.inbound || 0, 0)} вх · ${
              num(архив.outbound || 0, 0)} исх</span></div>
        </div>
        ${причины ? `<div class="row wrap" style="padding:0 16px 12px;gap:6px">${причины}</div>` : ''}
        ${станции.length ? `<div class="row wrap" style="padding:0 16px 14px;gap:6px">${
          станции.map((с) => `<button class="chip ${
            !с.enabled || !свод.enabled ? ''
              : с.last_error ? 'err' : с.running ? 'ok' : 'warn'}"
            data-station-chip="${esc(с.id)}"
            title="Показать звонки только этой станции · ${esc(с.last_error || (
              !с.enabled ? 'станция выключена'
                : !свод.enabled ? 'забор записей выключен целиком'
                : с.running ? 'работает' : 'поток стоит'))}"
            >${esc(с.name)}: ${num((с.calls || {}).total || 0, 0)}</button>`).join('')}</div>` : ''}
      </section>`;
    qsa('[data-station-chip]').forEach((чип) => чип.addEventListener('click', () => {
      state.telStation = state.telStation === чип.dataset.stationChip
        ? '' : чип.dataset.stationChip;
      state.telOffset = 0;
      const поле = qs('#tel-station');
      if (поле) поле.value = state.telStation;
      this.loadCalls();
    }));
    const заход = qs('#tel-scan');
    if (заход) заход.addEventListener('click', () => this.scan());
  },

  async scan() {
    const кнопка = qs('#tel-scan');
    if (кнопка) { кнопка.disabled = true; кнопка.textContent = 'Забираю…'; }
    try {
      const итог = await API.post('/api/telephony/scan');
      const причины = Object.entries(итог.reasons || {}).map(([п, n]) => `${п}: ${n}`).join(', ');
      const сбои = (итог.errors || []).map((о) => `${о.name || о.station}: ${о.error}`).join('; ');
      toast(`Просмотрено ${итог.seen}, поставлено ${итог.imported}, пропущено ${итог.skipped}`,
            сбои ? 'warn' : итог.imported ? 'ok' : '', сбои || причины);
      await this.loadStatus();
      await this.loadCalls();
    } catch (err) {
      toast(err.message || 'Заход не удался', 'err', err.hint || '');
    } finally {
      if (кнопка) { кнопка.disabled = false; кнопка.textContent = 'Забрать сейчас'; }
    }
  },

  /* Очереди и операторы для отбора: список из того, что встречалось, а не
   * из того, что настроено. Настроенного может не быть ни в одном звонке. */
  async loadDimensions() {
    let оси;
    try {
      оси = await API.get('/api/telephony/dimensions');
    } catch (err) { return; }
    const заполнить = (ид, значения, выбрано, пусто) => {
      const поле = qs(`#${ид}`);
      if (!поле) return;
      поле.innerHTML = `<option value="">${пусто}</option>` + значения
        .map((з) => `<option value="${esc(з)}"${з === выбрано ? ' selected' : ''}>${esc(з)}</option>`)
        .join('');
    };
    заполнить('tel-queue', оси.queues || [], state.telQueue, 'Все очереди');
    заполнить('tel-agent', оси.agents || [], state.telAgent, 'Все операторы');
    // Станции подписываем именами из настроек: в звонке лежит
    // идентификатор («filial-yug»), а человек знает «Филиал „Юг“».
    const имена = new Map(((state.telStatus || {}).stations || []).map((с) => [с.id, с.name]));
    const поле = qs('#tel-station');
    if (поле) поле.innerHTML = '<option value="">Все АТС</option>' + (оси.stations || [])
      .map((ид) => `<option value="${esc(ид)}"${ид === state.telStation ? ' selected' : ''}>${
        esc(имена.get(ид) || ид)}</option>`).join('');
  },

  async loadCalls() {
    const коробка = qs('#tel-calls');
    if (!коробка) return;
    const пар = new URLSearchParams({
      period: state.telPeriod, limit: '50', offset: String(state.telOffset || 0),
    });
    if (state.telDirection) пар.set('direction', state.telDirection);
    if (state.telStation) пар.set('station', state.telStation);
    if (state.telQueue) пар.set('queue', state.telQueue);
    if (state.telAgent) пар.set('agent', state.telAgent);
    if (state.telSearch) пар.set('search', state.telSearch);
    if (state.telOnlyQueued) пар.set('only_queued', 'true');
    let свод;
    try {
      свод = await API.get(`/api/telephony/calls?${пар}`);
    } catch (err) {
      if (err.code === 'aborted') return;
      коробка.innerHTML = `<div class="empty">Журнал недоступен: ${esc(err.message || '')}</div>`;
      return;
    }
    this.drawCalls(коробка, свод);
  },

  drawCalls(коробка, свод) {
    const звонки = свод.calls || [];
    if (!звонки.length) {
      коробка.innerHTML = `<div class="empty">
        Звонков за выбранный период нет.
        ${state.telStatus && !state.telStatus.enabled
          ? 'Забор записей выключен — включите его в настройках, раздел «Телефония».'
          : 'Проверьте период и отбор либо нажмите «Забрать сейчас».'}</div>`;
      return;
    }
    const строки = звонки.map((з) => {
      const статус = з.job_id
        ? `<a href="#" onclick="__asrhub.openJob('${esc(з.job_id)}');return false"
             title="Открыть карточку задания">${esc(STATUS_LABELS[з.job_status] || з.job_status || 'в очереди')}</a>`
        : `<span class="dim" title="почему не распознан">${esc(з.skipped || '—')}</span>`;
      const направление = з.direction
        ? `<span class="chip ${з.direction === 'входящий' ? 'ok' : ''}">${esc(з.direction)}</span>`
        : '<span class="dim">—</span>';
      const имена = new Map(((state.telStatus || {}).stations || []).map((с) => [с.id, с.name]));
      return `<tr>
        <td class="small">${esc(fmtTime(з.started_at))}</td>
        <td class="small">${з.station
          ? esc(имена.get(з.station) || з.station) : '<span class="dim">—</span>'}</td>
        <td>${направление}</td>
        <td class="mono small">${esc(з.src || '—')}</td>
        <td class="mono small">${esc(з.dst || '—')}</td>
        <td class="small">${esc(з.queue || '—')}</td>
        <td class="small">${esc(з.agent || '—')}</td>
        <td class="small" title="${з.duration ? `всего с гудками ${esc(fmtDur(з.duration))}` : ''}">${
          з.billsec ? fmtDur(з.billsec) : '<span class="dim">—</span>'}</td>
        <td class="small">${з.answered ? 'ответили'
          : esc(ИТОГ_ЗВОНКА[з.disposition] || (з.disposition || '').toLowerCase() || '—')}</td>
        <td class="small">${статус}</td>
        <td class="small dim" title="${esc(з.preview || '')}">${esc((з.preview || '').slice(0, 80))}</td>
      </tr>`;
    }).join('');
    const страниц = Math.ceil((свод.total || 0) / (свод.limit || 50));
    const текущая = Math.floor((свод.offset || 0) / (свод.limit || 50)) + 1;
    коробка.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Журнал звонков</h3>
          <span class="chip">${num(свод.total || 0, 0)} ${plural(свод.total || 0, 'звонок', 'звонка', 'звонков')}</span>
          <span class="spacer"></span>
          ${страниц > 1 ? `<span class="small dim">страница ${текущая} из ${страниц}</span>
            <button class="ghost sm" id="tel-prev" ${текущая <= 1 ? 'disabled' : ''}>←</button>
            <button class="ghost sm" id="tel-next" ${текущая >= страниц ? 'disabled' : ''}>→</button>` : ''}
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Начало</th><th>АТС</th><th>Направление</th><th>Кто</th><th>Кому</th>
            <th>Очередь</th><th>Оператор</th><th>Разговор</th><th>Итог</th>
            <th>Распознавание</th><th>Начало расшифровки</th></tr></thead>
          <tbody>${строки}</tbody>
        </table></div>
      </section>`;
    const назад = qs('#tel-prev');
    const вперёд = qs('#tel-next');
    if (назад) назад.addEventListener('click', () => {
      state.telOffset = Math.max(0, (свод.offset || 0) - (свод.limit || 50));
      this.loadCalls();
    });
    if (вперёд) вперёд.addEventListener('click', () => {
      state.telOffset = (свод.offset || 0) + (свод.limit || 50);
      this.loadCalls();
    });
  },
};

/* ========================================================================
 * Раздел «АТС»: всё про телефонные станции разом и про каждую отдельно.
 *
 * Раздел «Телефония» отвечает на вопрос «что было в этом разговоре» — там
 * журнал звонков. Здесь вопрос другой: «как живут станции» — сколько их,
 * доезжают ли записи, когда приходит нагрузка, кто и сколько разговаривает.
 * Поэтому и разделы разные: смешать их значит получить страницу, на
 * которой ни настройку не найти, ни звонок.
 * ===================================================================== */

const ПАТС_ВКЛАДКИ = [
  { key: 'overview', title: 'Обзор', hint: 'Станции, их состояние и вклад каждой' },
  { key: 'load', title: 'Нагрузка', hint: 'Когда звонят: по времени, по дням недели, по длительности' },
  { key: 'people', title: 'Очереди и операторы', hint: 'Кто принимает звонки и сколько разговаривает' },
  { key: 'numbers', title: 'Номера и направления', hint: 'Откуда и куда звонят, чем заканчивается' },
  { key: 'intake', title: 'Забор записей', hint: 'Что доехало, что пропущено и почему' },
];

const ПАТС_ИСТОЧНИКИ = {
  cdr_csv: 'журнал CDR', ami: 'интерфейс AMI', folder: 'каталог записей',
};

/* Поля станции с описанием, рекомендацией и примерами — тем же набором,
 * что и параметры сервера в разделе «Настройки». Форма без объяснений
 * заставляет человека угадывать, что такое «контекст» и чем «журнал CDR»
 * отличается от «каталога записей», а угадывают обычно неверно. */
const ПАТС_ПОЛЯ = [
  { key: 'name', label: 'Название', type: 'text', required: true,
    desc: 'Как станция называется у вас: «Головной офис», «Филиал Юг», «Склад».',
    rec: 'Пишите так, как её называют люди — это имя будет в разрезах отчётов.',
    examples: ['Головной офис', 'Филиал «Юг»', 'Склад и логистика'] },
  { key: 'source', label: 'Источник', type: 'select', required: true,
    options: [['cdr_csv', 'журнал CDR (Master.csv)'], ['ami', 'интерфейс AMI'],
              ['folder', 'каталог записей']],
    desc: 'Откуда сервер узнаёт о звонках. Журнал CDR — файл, который Asterisk '
        + 'пишет сам; AMI — живое соединение с управляющим интерфейсом; каталог '
        + 'записей — просто папка с файлами, без сведений о звонке.',
    rec: 'Журнал CDR — самый спокойный способ: он не держит соединение и '
       + 'переживает перезапуск АТС. AMI нужен, когда записи требуются сразу '
       + 'после разговора.',
    examples: ['cdr_csv — для большинства установок Asterisk',
               'ami — когда нужен разбор в течение минуты',
               'folder — когда записи складывает стороннее решение'] },
  { key: 'enabled', label: 'Забирать записи', type: 'bool',
    desc: 'Выключенная станция остаётся в настройках, но сервер к ней не ходит.',
    rec: 'Выключайте на время работ на АТС: позиция чтения журнала сохранится, '
       + 'и после включения сервер продолжит с того места, где остановился.',
    examples: ['включено — обычный режим', 'выключено — АТС на обслуживании'] },
  { key: 'host', label: 'Адрес АТС', type: 'text', only: 'ami',
    desc: 'Адрес или имя узла, на котором работает Asterisk.',
    rec: 'Для АТС на этом же сервере — 127.0.0.1: соединение не выйдет в сеть.',
    examples: ['127.0.0.1', '10.0.0.5', 'pbx.example.ru'] },
  { key: 'port', label: 'Порт AMI', type: 'number', only: 'ami',
    desc: 'Порт управляющего интерфейса из manager.conf.',
    rec: 'По умолчанию 5038. Менять стоит, только если его сменили на АТС.',
    examples: ['5038 — значение по умолчанию', '15038 — если порт перенесли'] },
  { key: 'username', label: 'Учётная запись AMI', type: 'text', only: 'ami',
    desc: 'Имя из manager.conf, под которым сервер подключается к АТС.',
    rec: 'Заведите отдельную запись только на чтение: read = call,cdr и '
       + 'write = <пусто>. Полные права серверу распознавания не нужны.',
    examples: ['asrhub', 'monitoring'] },
  { key: 'secret', label: 'Пароль AMI', type: 'password', only: 'ami',
    desc: 'Пароль этой учётной записи. В ответах интерфейса он не показывается.',
    rec: 'Не переиспользуйте пароль администратора АТС.',
    examples: ['длинная случайная строка'] },
  { key: 'cdr_file', label: 'Журнал звонков', type: 'text', only: 'cdr_csv',
    desc: 'Путь к файлу Master.csv, который Asterisk пишет после каждого звонка.',
    rec: 'Обычно /var/log/asterisk/cdr-csv/Master.csv. Нужен доступ на чтение '
       + 'пользователю, от которого работает сервер.',
    examples: ['/var/log/asterisk/cdr-csv/Master.csv',
               '/mnt/pbx-filial/cdr-csv/Master.csv'] },
  { key: 'recordings_dir', label: 'Каталог записей', type: 'text',
    desc: 'Где лежат файлы разговоров. Сервер ищет запись по идентификатору '
        + 'звонка, затем по номерам и времени.',
    rec: 'Обычно /var/spool/asterisk/monitor. Каталог филиала монтируйте '
       + 'только на чтение — серверу распознавания писать туда незачем.',
    examples: ['/var/spool/asterisk/monitor',
               '/mnt/pbx-filial/monitor'] },
  { key: 'filename', label: 'Шаблон имени файла', type: 'text',
    desc: 'Если MixMonitor зовут с особым именем, опишите его здесь: '
        + '${UNIQUEID}, ${SRC}, ${DST}, ${YYYY}, ${MM}, ${DD}.',
    rec: 'Оставьте пустым, если имена обычные: поиск по идентификатору '
       + 'находит запись и без шаблона.',
    examples: ['${YYYY}/${MM}/${UNIQUEID}.wav',
               'out-${DST}-${SRC}-${YYYY}${MM}${DD}-${UNIQUEID}'] },
  { key: 'internal_digits', label: 'Длина внутренних номеров', type: 'text',
    desc: 'Сколько цифр во внутреннем номере. Можно несколько значений через '
        + 'запятую — в организации, которая росла или объединялась, рядом живут '
        + 'трёхзначные и четырёхзначные добавочные.',
    rec: 'Перечислите все длины, которые встречаются: по ним сервер отличает '
       + 'внутренний звонок от внешнего, а значит и входящий от исходящего.',
    examples: ['3', '3, 4', '3, 4, 6'] },
  { key: 'contexts', label: 'Контексты и направления', type: 'text',
    desc: 'Правила «контекст диалплана = направление», через запятую. '
        + 'Порядок важен: первое подходящее правило выигрывает.',
    rec: 'Частные правила пишите выше общих: from-internal-out=исходящий, '
       + 'from-internal=внутренний. Иначе общее правило перехватит частный случай.',
    examples: ['from-trunk=входящий, from-internal=исходящий',
               'from-pstn=входящий, from-internal-out=исходящий, from-internal=внутренний'] },
  { key: 'min_duration_s', label: 'Минимальная длительность, с', type: 'number',
    desc: 'Разговоры короче этого не распознаются.',
    rec: '10–15 секунд: за это время не успевают сказать ничего, что стоит '
       + 'расшифровки, а задание в очереди занимает место.',
    examples: ['10 — обычное значение', '0 — распознавать всё подряд'] },
  { key: 'skip_unanswered', label: 'Пропускать без ответа', type: 'bool',
    desc: 'Не заводить задания для звонков, на которые не ответили.',
    rec: 'Включено: в неотвеченном звонке нечего распознавать, кроме гудков.',
    examples: ['включено — обычный режим',
               'выключено — когда нужен разбор автоответчика'] },
  { key: 'settle_s', label: 'Выдержка перед забором, с', type: 'number',
    desc: 'Сколько ждать после конца разговора, прежде чем брать файл: запись '
        + 'дописывается и конвертируется уже после того, как положили трубку.',
    rec: '30 секунд хватает почти всегда. 0 — брать сразу, годится только '
       + 'когда записи кладут в каталог уже готовыми.',
    examples: ['30 — обычное значение', '120 — если АТС перекодирует в mp3'] },
  { key: 'lookback_days', label: 'Глубина первого захода, дней', type: 'number',
    desc: 'Насколько далеко в прошлое смотреть при первом подключении станции.',
    rec: '7 дней: свежий архив приедет сразу, а годовой не забьёт очередь в '
       + 'первый же час. Увеличьте разово, если нужен весь архив.',
    examples: ['7 — обычное значение', '90 — разовый перенос архива'] },
  { key: 'poll_s', label: 'Интервал опроса, с', type: 'number',
    desc: 'Как часто заглядывать на станцию за новыми звонками.',
    rec: '60 секунд. Чаще имеет смысл только при AMI и требовании «расшифровка '
       + 'в течение минуты»; реже — для архивных станций.',
    examples: ['60 — обычное значение', '600 — архивная станция'] },
  { key: 'owner', label: 'Владелец звонков', type: 'text',
    desc: 'Под каким владельцем заводить задания. По нему работает разграничение '
        + 'доступа: ключ видит свои записи и записи своей группы.',
    rec: 'Заведите отдельного владельца на каждую станцию, если филиалы не '
       + 'должны видеть разговоры друг друга.',
    examples: ['telephony', 'filial-yug', 'sales'] },
  { key: 'priority', label: 'Приоритет заданий', type: 'number',
    desc: 'С каким приоритетом ставить звонки этой станции в очередь распознавания.',
    rec: '40 — ниже ручных загрузок (50), чтобы поток с АТС не задвигал '
       + 'человека, который ждёт результат у экрана.',
    examples: ['40 — обычное значение', '60 — когда звонки важнее всего'] },
  { key: 'tags', label: 'Метки', type: 'text',
    desc: 'Метки, которые получат задания этой станции, через запятую.',
    rec: 'Ставьте метку филиала: по ней потом отбираются результаты и отчёты.',
    examples: ['филиал-юг', 'склад, логистика'] },
];

RENDERERS.pbx = {
  async render(root) {
    if (!state.pbxPeriod) state.pbxPeriod = 'week';
    if (!state.pbxTab) state.pbxTab = 'overview';
    if (state.pbxStation === undefined) state.pbxStation = '';
    const админ = (state.me || {}).role === 'admin';

    root.innerHTML = `
      <div class="settings-toolbar">
        <span class="small dim">Период:</span>
        <div class="group-nav" id="pbx-period">
          ${Object.entries(ТЕЛЕФОНИЯ_ПЕРИОДЫ).map(([k, v]) =>
            `<button data-period="${k}" class="${state.pbxPeriod === k ? 'active' : ''}"
               title="Показатели и графики за ${v.toLowerCase()}">${v}</button>`).join('')}
        </div>
        <select id="pbx-station" style="width:210px"
          title="Разрез по одной станции или свод по всем"><option value="">Все станции</option></select>
        <span class="spacer"></span>
        ${админ ? `<button class="btn sm" id="pbx-scan-all"
            title="Заход за новыми звонками по всем включённым станциям прямо сейчас">Забрать со всех</button>
          <button class="primary sm" id="pbx-add"
            title="Подключить ещё одну АТС">+ Добавить АТС</button>` : ''}
        <button class="ghost sm" id="pbx-refresh" title="Обновить данные раздела">Обновить</button>
      </div>
      <div id="pbx-top"></div>
      <div class="tabs" id="pbx-tabs">
        ${ПАТС_ВКЛАДКИ.map((в) => `<button data-tab="${в.key}" title="${esc(в.hint)}"
          class="${state.pbxTab === в.key ? 'active' : ''}">${esc(в.title)}</button>`).join('')}
      </div>
      <div id="pbx-body"><div class="empty">Загрузка…</div></div>`;

    qsa('#pbx-period button').forEach((b) => b.addEventListener('click', () => {
      state.pbxPeriod = b.dataset.period;
      qsa('#pbx-period button').forEach((x) => x.classList.toggle('active', x === b));
      this.load();
    }));
    qsa('#pbx-tabs button').forEach((b) => b.addEventListener('click', () => {
      state.pbxTab = b.dataset.tab;
      qsa('#pbx-tabs button').forEach((x) =>
        x.classList.toggle('active', x.dataset.tab === state.pbxTab));
      this.draw();
    }));
    const выбор = qs('#pbx-station');
    if (выбор) выбор.addEventListener('change', () => {
      state.pbxStation = выбор.value;
      this.load();
    });
    const обновить = qs('#pbx-refresh');
    if (обновить) обновить.addEventListener('click', () => this.load());
    const заход = qs('#pbx-scan-all');
    if (заход) заход.addEventListener('click', () => this.scan(''));
    const добавить = qs('#pbx-add');
    if (добавить) добавить.addEventListener('click', () => this.edit(null));

    await this.load();
  },

  /* Весь раздел приезжает одним ответом: восемь графиков — это восемь
   * разрезов одного и того же отбора, и восемь запросов подряд показали бы
   * на одном экране части картины от разных мгновений. */
  async load() {
    const тело = qs('#pbx-body');
    if (тело && !state.pbxData) тело.innerHTML = '<div class="empty">Загрузка…</div>';
    const пар = new URLSearchParams({ period: state.pbxPeriod });
    if (state.pbxStation) пар.set('station', state.pbxStation);
    try {
      state.pbxData = await API.latest('pbx-overview', `/api/telephony/overview?${пар}`);
    } catch (err) {
      if (err.code === 'aborted') return;
      state.pbxData = null;
      if (тело) тело.innerHTML = `<div class="empty">Раздел недоступен: ${
        esc(err.message || '')}${err.hint ? `<div class="small dim" style="margin-top:6px">${
        esc(err.hint)}</div>` : ''}</div>`;
      const верх = qs('#pbx-top');
      if (верх) верх.innerHTML = '';
      return;
    }
    this.fillStations();
    this.drawTop();
    this.draw();
  },

  fillStations() {
    const поле = qs('#pbx-station');
    const данные = state.pbxData || {};
    if (!поле) return;
    const имена = new Map((данные.stations || []).map((с) => [с.id, с.name]));
    // В списке и станции из настроек, и те, что встречаются только в архиве:
    // станцию сняли со стойки, а её прошлогодние разговоры остались, и
    // посмотреть их разрез — законное желание.
    (данные.by_station || []).forEach((с) => {
      if (с.station && !имена.has(с.station)) имена.set(с.station, `${с.station} (в архиве)`);
    });
    поле.innerHTML = '<option value="">Все станции</option>' + [...имена.entries()]
      .map(([ид, имя]) => `<option value="${esc(ид)}"${
        ид === state.pbxStation ? ' selected' : ''}>${esc(имя)}</option>`).join('');
  },

  /* Шапка: сколько станций и что с ними прямо сейчас. Она одна и та же на
   * всех вкладках — это ответ на вопрос «всё ли работает», а его задают
   * независимо от того, какой график открыт. */
  drawTop() {
    const место = qs('#pbx-top');
    const д = state.pbxData || {};
    if (!место) return;
    const за = д.period_calls || {};
    const станции = д.stations || [];
    // «Требует внимания» — только то, что чинится на уровне станции.
    // При общем выключателе не работает вообще ничего, и об этом говорит
    // соседний признак; дублировать его тремя тревожными чипами незачем.
    const беда = станции.filter((с) => с.enabled && д.enabled
                                       && (с.last_error || !с.running));
    const среднее = за.answered ? за.talk_s / за.answered : 0;
    const доля = за.total ? (за.queued / за.total) * 100 : 0;
    const ответ = за.total ? (за.answered / за.total) * 100 : 0;
    место.innerHTML = `
      <section class="card" style="margin-bottom:12px">
        <div class="card-head">
          <h3>Станции</h3>
          <span class="chip ${д.enabled ? (д.running ? 'ok' : 'warn') : ''}">${
            !д.enabled ? 'забор выключен'
              : д.running ? `в работе ${д.running} из ${д.configured}`
              : 'включено, но потоки стоят'}</span>
          ${беда.length ? `<span class="chip err" title="${esc(беда.map((с) =>
            `${с.name}: ${с.last_error || 'поток не запущен'}`).join('; '))}">${
            беда.length} ${plural(беда.length, 'станция требует', 'станции требуют',
            'станций требуют')} внимания</span>` : ''}
          <span class="spacer"></span>
          <span class="small dim">${esc(ТЕЛЕФОНИЯ_ПЕРИОДЫ[д.period] || '')}${
            state.pbxStation ? ` · ${esc((станции.find((с) => с.id === state.pbxStation) || {}).name
              || state.pbxStation)}` : ''}</span>
        </div>
        <div class="grid cols-6" style="padding:14px 16px">
          ${kpi('Звонков за период', num(за.total || 0, 0),
                `всего в архиве ${num((д.calls || {}).total || 0, 0)}`)}
          ${kpi('Ответили', `${num(ответ, 1)} %`,
                `${num(за.answered || 0, 0)} из ${num(за.total || 0, 0)}`)}
          ${kpi('Распознано', `${num(доля, 1)} %`,
                `${num(за.queued || 0, 0)} ${plural(за.queued || 0, 'задание', 'задания', 'заданий')}`)}
          ${kpi('Наговорено', fmtDur(за.talk_s || 0),
                `в среднем ${fmtDur(среднее)} на разговор`)}
          ${kpi('Ожидание', fmtDur(за.answered ? (за.wait_s || 0) / за.answered : 0),
                'среднее до ответа')}
          ${kpi('Пропущено', num(за.skipped || 0, 0), 'короткие, без ответа, без записи')}
        </div>
      </section>`;
  },

  draw() {
    const тело = qs('#pbx-body');
    const д = state.pbxData;
    if (!тело || !д) return;
    const вкладка = state.pbxTab;
    if (вкладка === 'overview') return this.drawOverview(тело, д);
    if (вкладка === 'load') return this.drawLoad(тело, д);
    if (вкладка === 'people') return this.drawPeople(тело, д);
    if (вкладка === 'numbers') return this.drawNumbers(тело, д);
    return this.drawIntake(тело, д);
  },

  /* --- Обзор ---------------------------------------------------------- */

  drawOverview(тело, д) {
    const станции = д.stations || [];
    const архивные = (д.by_station || []).filter((с) =>
      с.station && !станции.some((н) => н.id === с.station));
    const карточки = станции.map((с) => this.stationCard(с, д)).join('');
    тело.innerHTML = `
      ${станции.length ? `<div class="grid cols-3" id="pbx-cards">${карточки}</div>`
        : `<div class="empty">Ни одной АТС не подключено.
             ${(state.me || {}).role === 'admin'
               ? 'Нажмите «Добавить АТС» — сервер проверит связь ещё до сохранения.'
               : 'Обратитесь к администратору сервера.'}</div>`}
      ${архивные.length ? `<section class="card" style="margin-top:14px">
        <div class="card-head"><h3>Станции только в архиве</h3>
          <span class="hint">этих АТС нет в настройках, но их разговоры сохранились</span></div>
        <div class="row wrap" style="padding:12px 16px;gap:6px">${архивные.map((с) =>
          `<span class="chip" title="звонков в архиве">${esc(с.station)}: ${num(с.total, 0)}</span>`)
          .join('')}</div></section>` : ''}
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Вклад станций</h3>
          <span class="hint">звонки за период</span></div>
          <div id="pbx-by-station" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Направления</h3>
          <span class="hint">кто кому звонит</span></div>
          <div id="pbx-directions" style="padding:12px 16px"></div></section>
      </div>
      <section class="card" style="margin-top:14px">
        <div class="card-head"><h3>Станции рядом</h3>
          <span class="hint">одни и те же показатели у всех АТС — так видно, какая выпадает</span></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Станция</th><th>Источник</th><th class="num">Звонков</th>
            <th class="num">Ответили</th><th class="num">Распознано</th>
            <th class="num">Наговорено</th><th class="num">Средний разговор</th>
            <th class="num">Пропущено</th><th>Состояние</th></tr></thead>
          <tbody>${this.stationRows(д)}</tbody></table></div>
      </section>`;

    this.bindCards();
    const по_станциям = (д.by_station || []).filter((с) => с.total > 0);
    const имя = (ид) => (станции.find((с) => с.id === ид) || {}).name || ид || 'без станции';
    // Один цвет на все столбцы: величина у них одна и та же — звонки за
    // период. Разные цвета читались бы как разные виды звонков.
    const цвет = Charts.palette()[0];
    Charts.hbars(qs('#pbx-by-station'), {
      items: по_станциям.slice(0, 12).map((с) => ({
        label: имя(с.station), value: с.total, color: цвет,
        display: `${num(с.total, 0)} · ${fmtDur(с.talk_s)}`,
      })),
      emptyText: 'За период звонков не было',
    });
    const за = д.period_calls || {};
    Charts.donut(qs('#pbx-directions'), {
      parts: [
        { label: "входящие", value: за.inbound || 0 },
        { label: "исходящие", value: за.outbound || 0 },
        { label: "внутренние", value: за.internal || 0 },
        { label: 'без направления',
          value: Math.max(0, (за.total || 0) - (за.inbound || 0)
                             - (за.outbound || 0) - (за.internal || 0)) },
      ],
      centerLabel: 'звонков за период',
      emptyText: 'За период звонков не было',
    });
  },

  /* Карточка станции: состояние, счётчики, линия последних суток и все
   * действия рядом. Действия именно здесь, а не в отдельном списке: когда
   * станция молчит, человек смотрит на неё, и «проверить связь» должно
   * быть под рукой в этот момент. */
  stationCard(с, д) {
    const админ = (state.me || {}).role === 'admin';
    const свои = (д.by_station || []).find((x) => x.station === с.id) || {};
    const звонки = с.calls || {};
    // Когда забор выключен целиком, «поток стоит» у каждой станции — правда,
    // но не ответ: чинить нужно один общий выключатель, а не три станции.
    const состояние = !с.enabled ? ['', 'выключена']
      : с.last_error ? ['err', 'ошибка источника']
      : с.running ? ['ok', 'работает']
      : !д.enabled ? ['', 'забор выключен'] : ['warn', 'поток стоит'];
    return `<section class="card tight pbx-card" data-station="${esc(с.id)}">
      <div class="card-head">
        <h3 title="${esc(с.id)}">${esc(с.name)}</h3>
        <span class="chip ${состояние[0]}">${состояние[1]}</span>
        <span class="spacer"></span>
        <span class="chip" title="откуда сервер узнаёт о звонках">${
          esc(ПАТС_ИСТОЧНИКИ[с.source] || с.source || '—')}</span>
      </div>
      <div style="padding:10px 14px 4px">
        <div class="row" style="gap:16px;align-items:flex-end">
          <div><div class="kpi-label">За период</div>
            <div class="kpi-value" style="font-size:22px">${num(свои.total || 0, 0)}</div></div>
          <div><div class="kpi-label">Распознано</div>
            <div class="kpi-value" style="font-size:22px">${num(свои.queued || 0, 0)}</div></div>
          <div><div class="kpi-label">Наговорено</div>
            <div class="kpi-value" style="font-size:22px">${fmtDur(свои.talk_s || 0)}</div></div>
          <span class="spacer"></span>
          <div class="pbx-spark" data-station="${esc(с.id)}"></div>
        </div>
        <div class="row wrap small dim" style="gap:6px;margin-top:8px">
          <span title="всего в архиве этой станции">архив: ${num(звонки.total || 0, 0)}</span>
          ${звонки.skipped ? `<span title="пропущено: короткие, без ответа, без записи">
            · пропущено ${num(звонки.skipped, 0)}</span>` : ''}
          ${звонки.deferred ? `<span title="записи ещё дописываются — вернёмся к ним">
            · отложено ${num(звонки.deferred, 0)}</span>` : ''}
          ${с.last_run ? `<span>· заход ${esc(fmtTime(с.last_run))}</span>`
            : '<span>· заходов ещё не было</span>'}
        </div>
        ${с.last_error ? `<div class="banner err" style="margin:10px 0 0">
          <b>Источник отвечает ошибкой.</b> ${esc(с.last_error)}</div>` : ''}
      </div>
      <div class="row wrap" style="gap:6px;padding:10px 14px 12px">
        <button class="ghost sm" data-act="calls" title="Журнал звонков этой станции">Звонки</button>
        ${админ ? `
          <button class="ghost sm" data-act="test" title="Достучаться до источника и сказать, что именно не так">Проверить</button>
          <button class="btn sm" data-act="scan" title="Заход за новыми звонками прямо сейчас">Забрать</button>
          <button class="ghost sm" data-act="edit" title="Изменить поля станции">Изменить</button>
          <button class="ghost sm" data-act="toggle" title="${с.enabled
            ? 'Перестать ходить на эту АТС; настройки и позиция чтения сохранятся'
            : 'Снова забирать записи с этой АТС'}">${с.enabled ? 'Выключить' : 'Включить'}</button>
          <button class="ghost sm danger" data-act="remove"
            title="Убрать станцию из настроек; её звонки останутся в архиве">Убрать</button>` : ''}
      </div>
    </section>`;
  },

  stationRows(д) {
    const станции = д.stations || [];
    const строки = (д.by_station || []).slice().sort((a, b) => b.total - a.total);
    if (!строки.length) return '<tr><td colspan="9" class="dim">За период звонков не было</td></tr>';
    return строки.map((с) => {
      const настроена = станции.find((н) => н.id === с.station);
      const среднее = с.answered ? с.talk_s / с.answered : 0;
      const состояние = !настроена ? '<span class="dim">только в архиве</span>'
        : !настроена.enabled ? '<span class="chip">выключена</span>'
        : настроена.last_error ? `<span class="chip err" title="${esc(настроена.last_error)}">ошибка</span>`
        : настроена.running ? '<span class="chip ok">работает</span>'
        : !д.enabled ? '<span class="chip">забор выключен</span>'
        : '<span class="chip warn">поток стоит</span>';
      return `<tr>
        <td>${esc((настроена || {}).name || с.station || 'без станции')}</td>
        <td class="small dim">${esc(ПАТС_ИСТОЧНИКИ[(настроена || {}).source] || '—')}</td>
        <td class="num">${num(с.total, 0)}</td>
        <td class="num">${с.total ? num((с.answered / с.total) * 100, 1) + ' %' : '—'}</td>
        <td class="num">${с.total ? num((с.queued / с.total) * 100, 1) + ' %' : '—'}</td>
        <td class="num">${fmtDur(с.talk_s)}</td>
        <td class="num">${fmtDur(среднее)}</td>
        <td class="num">${num(с.skipped, 0)}</td>
        <td>${состояние}</td></tr>`;
    }).join('');
  },

  /* Линия на карточке станции строится из общей ленты: отдельный запрос на
   * каждую станцию — это десяток запросов при десятке АТС. */
  bindCards() {
    qsa('.pbx-card').forEach((карточка) => {
      const ид = карточка.dataset.station;
      qsa('button[data-act]', карточка).forEach((кнопка) =>
        кнопка.addEventListener('click', () => this.act(кнопка.dataset.act, ид, кнопка)));
    });
    const д = state.pbxData || {};
    qsa('.pbx-spark').forEach((место) => {
      const ид = место.dataset.station;
      const лента = (д.station_timelines || {})[ид];
      if (лента && лента.length) Charts.spark(место, лента, { width: 120, height: 30 });
      else if (!state.pbxStation) место.innerHTML = '';
    });
  },

  /* --- Нагрузка ------------------------------------------------------- */

  drawLoad(тело, д) {
    const шаг = { hour: 'по часам', day: 'по суткам', week: 'по неделям',
                  month: 'по месяцам' }[д.bucket] || '';
    тело.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Звонки во времени</h3>
          <span class="hint">${esc(шаг)} · разложено по направлениям</span>
          <span class="spacer"></span>
          <label class="row small" style="gap:6px;cursor:pointer">
            <input type="checkbox" id="pbx-load-talk" ${state.pbxLoadTalk ? 'checked' : ''}
              style="width:auto">показывать наговоренное время</label>
        </div>
        <div id="pbx-timeline" style="padding:12px 16px"></div>
      </section>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>День недели и час</h3>
          <span class="hint">когда приходит нагрузка — основание для расписания смен</span></div>
          <div id="pbx-heat" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Длительность разговоров</h3>
          <span class="hint">среднее без этого обманывает</span></div>
          <div id="pbx-durations" style="padding:12px 16px"></div></section>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Часы суток</h3>
          <span class="hint">сумма по всем дням периода</span></div>
          <div id="pbx-hours" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Дни недели</h3>
          <span class="hint">сумма по всем неделям периода</span></div>
          <div id="pbx-days" style="padding:12px 16px"></div></section>
      </div>`;

    const лента = д.timeline || [];
    const шагСек = { hour: 3600, day: 86400, week: 604800, month: 2592000 }[д.bucket] || 86400;
    const ряды = state.pbxLoadTalk
      ? [{ name: 'наговорено, мин', values: лента.map((т) => Math.round(т.talk_s / 60)) }]
      : [
        { name: 'входящие', values: лента.map((т) => т.inbound) },
        { name: 'исходящие', values: лента.map((т) => т.outbound) },
        { name: 'внутренние', values: лента.map((т) => т.internal) },
      ];
    Charts.line(qs('#pbx-timeline'), {
      labels: лента.map((т) => fmtBucket(т.t, шагСек)), series: ряды, height: 260, yMin: 0,
      emptyText: 'За период звонков не было',
    });
    const переключатель = qs('#pbx-load-talk');
    if (переключатель) переключатель.addEventListener('change', () => {
      state.pbxLoadTalk = переключатель.checked;
      this.draw();
    });

    const карта = д.heatmap || {};
    Charts.grid(qs('#pbx-heat'), {
      rows: карта.days || [], cols: (карта.hours || []).map((ч) => String(ч).padStart(2, '0')),
      values: карта.calls || [], cell: 20,
      secondary: (карта.talk_s || []).map((строка) =>
        (строка || []).map((с) => Math.round((с || 0) / 60))),
      secondaryUnit: 'мин разговора',
      emptyText: 'За период звонков не было',
    });
    Charts.bars(qs('#pbx-durations'), {
      labels: (д.durations || []).map((к) => к.label),
      values: (д.durations || []).map((к) => к.count),
      height: 220, emptyText: 'За период отвеченных разговоров не было',
    });

    const по_часам = new Array(24).fill(0);
    const по_дням = new Array(7).fill(0);
    (карта.calls || []).forEach((строка, день) => (строка || []).forEach((v, час) => {
      по_часам[час] += v || 0;
      по_дням[день] += v || 0;
    }));
    Charts.bars(qs('#pbx-hours'), {
      labels: по_часам.map((_, ч) => String(ч)), values: по_часам, height: 200,
      showValues: false, emptyText: 'За период звонков не было',
    });
    Charts.bars(qs('#pbx-days'), {
      labels: карта.days || [], values: по_дням, height: 200,
      emptyText: 'За период звонков не было',
    });
  },

  /* --- Очереди и операторы -------------------------------------------- */

  drawPeople(тело, д) {
    const очереди = (д.tops || {}).queue || [];
    const операторы = (д.tops || {}).agent || [];
    тело.innerHTML = `
      <div class="grid cols-2">
        <section class="card"><div class="card-head"><h3>Очереди</h3>
          <span class="hint">звонков за период</span></div>
          <div id="pbx-queues" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Операторы</h3>
          <span class="hint">звонков за период</span></div>
          <div id="pbx-agents" style="padding:12px 16px"></div></section>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Средний разговор по очередям</h3>
          <span class="hint">длинная очередь — либо сложные вопросы, либо неудачный скрипт</span></div>
          <div id="pbx-queue-avg" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Средний разговор по операторам</h3>
          <span class="hint">сравнивать стоит внутри одной очереди</span></div>
          <div id="pbx-agent-avg" style="padding:12px 16px"></div></section>
      </div>
      <section class="card" style="margin-top:14px">
        <div class="card-head"><h3>Операторы: всё вместе</h3>
          <span class="hint">звонки, наговоренное время, доля отвеченных</span>
          <span class="spacer"></span>
          <button class="ghost sm" onclick="__asrhub.go('employees')"
            title="Показатели речи, вежливости и скрипта по каждому сотруднику">Аналитика по сотрудникам →</button>
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Оператор</th><th class="num">Звонков</th><th class="num">Ответили</th>
            <th class="num">Наговорено</th><th class="num">Средний разговор</th>
            <th class="num">Доля периода</th></tr></thead>
          <tbody>${this.peopleRows(операторы, (д.period_calls || {}).total)}</tbody></table></div>
      </section>
      <section class="card" style="margin-top:14px">
        <div class="card-head"><h3>Очереди: всё вместе</h3></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Очередь</th><th class="num">Звонков</th><th class="num">Ответили</th>
            <th class="num">Наговорено</th><th class="num">Средний разговор</th>
            <th class="num">Доля периода</th></tr></thead>
          <tbody>${this.peopleRows(очереди, (д.period_calls || {}).total)}</tbody></table></div>
      </section>`;

    const цвет = Charts.palette()[0];
    const столбики = (место, данные, поле, формат) => Charts.hbars(qs(место), {
      items: данные.slice(0, 12).map((з) => ({
        label: з.value, value: поле === 'count' ? з.count : з.avg_s,
        display: формат(з), color: цвет,
        note: `ответили ${з.count ? num((з.answered / з.count) * 100, 1) : 0} % · `
            + `наговорено ${fmtDur(з.talk_s)}`,
      })),
      labelWidth: 150, emptyText: 'За период данных нет',
    });
    столбики('#pbx-queues', очереди, 'count',
             (з) => `${num(з.count, 0)} · ${fmtDur(з.talk_s)}`);
    столбики('#pbx-agents', операторы, 'count',
             (з) => `${num(з.count, 0)} · ${fmtDur(з.talk_s)}`);
    столбики('#pbx-queue-avg', очереди.slice().sort((a, b) => b.avg_s - a.avg_s),
             'avg', (з) => fmtDur(з.avg_s));
    столбики('#pbx-agent-avg', операторы.slice().sort((a, b) => b.avg_s - a.avg_s),
             'avg', (з) => fmtDur(з.avg_s));
  },

  peopleRows(строки, всего_периода) {
    if (!строки.length) return '<tr><td colspan="6" class="dim">За период данных нет</td></tr>';
    // Знаменатель — все звонки периода, а не сумма показанных строк: сервер
    // отдаёт только верхушку (пятнадцать), и доли по ней всегда складывались
    // ровно в сто процентов, завышая вклад каждого.
    const всего = Number(всего_периода) > 0
      ? Number(всего_периода)
      : (строки.reduce((с, з) => с + з.count, 0) || 1);
    return строки.map((з) => `<tr>
      <td>${esc(з.value)}</td>
      <td class="num">${num(з.count, 0)}</td>
      <td class="num">${з.count ? num((з.answered / з.count) * 100, 1) + ' %' : '—'}</td>
      <td class="num">${fmtDur(з.talk_s)}</td>
      <td class="num">${fmtDur(з.avg_s)}</td>
      <td class="num">${num((з.count / всего) * 100, 1)} %</td></tr>`).join('');
  },

  /* --- Номера и направления ------------------------------------------- */

  drawNumbers(тело, д) {
    const топ = д.tops || {};
    тело.innerHTML = `
      <div class="grid cols-2">
        <section class="card"><div class="card-head"><h3>Кто звонит чаще всего</h3>
          <span class="hint">исходящий номер звонка</span></div>
          <div id="pbx-src" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Кому звонят чаще всего</h3>
          <span class="hint">номер назначения</span></div>
          <div id="pbx-dst" style="padding:12px 16px"></div></section>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Контексты диалплана</h3>
          <span class="hint">по ним определяется направление; незнакомый контекст — повод дописать правило</span></div>
          <div id="pbx-context" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Чем кончился звонок</h3>
          <span class="hint">как это называет Asterisk</span></div>
          <div id="pbx-disposition" style="padding:12px 16px"></div></section>
      </div>`;
    const цвет = Charts.palette()[0];
    const нарисовать = (место, данные, подпись) => Charts.hbars(qs(место), {
      items: (данные || []).slice(0, 12).map((з) => ({
        label: подпись ? подпись(з.value) : з.value, value: з.count, color: цвет,
        display: `${num(з.count, 0)} · ${fmtDur(з.talk_s)}`,
        note: `средний разговор ${fmtDur(з.avg_s)}`,
      })),
      labelWidth: 150, emptyText: 'За период данных нет',
    });
    нарисовать('#pbx-src', топ.src);
    нарисовать('#pbx-dst', топ.dst);
    нарисовать('#pbx-context', топ.context);
    нарисовать('#pbx-disposition', топ.disposition,
               (з) => ИТОГ_ЗВОНКА[з] || String(з).toLowerCase());
  },

  /* --- Забор записей --------------------------------------------------- */

  drawIntake(тело, д) {
    const станции = д.stations || [];
    const причины = (д.tops || {}).skipped || [];
    тело.innerHTML = `
      <div class="grid cols-2">
        <section class="card"><div class="card-head"><h3>Почему звонок не распознан</h3>
          <span class="hint">причина важнее счётчика: «нет записи ×48» и «короткий ×48» — разные поломки</span></div>
          <div id="pbx-reasons" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Что доехало</h3>
          <span class="hint">за период</span></div>
          <div id="pbx-funnel" style="padding:12px 16px"></div>
          <div class="small dim" style="padding:0 16px 14px">
            Отложенные — не пропуск: запись ещё дописывается, сервер вернётся к ней
            следующим заходом.</div>
        </section>
      </div>
      <section class="card" style="margin-top:14px">
        <div class="card-head"><h3>Заходы по станциям</h3>
          <span class="hint">когда последний раз ходили и что принесли</span>
          <span class="spacer"></span>
          ${(state.me || {}).role === 'admin' ? `<button class="btn sm" id="pbx-scan-2"
            title="Заход по всем включённым станциям">Забрать со всех</button>` : ''}
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Станция</th><th>Источник</th><th>Последний заход</th>
            <th class="num">Взято</th><th class="num">Пропущено</th><th class="num">Отложено</th>
            <th>Что мешает</th><th></th></tr></thead>
          <tbody>${станции.length ? станции.map((с) => {
            const з = с.calls || {};
            return `<tr>
              <td>${esc(с.name)}<div class="small dim mono">${esc(с.id)}</div></td>
              <td class="small">${esc(ПАТС_ИСТОЧНИКИ[с.source] || с.source || '—')}
                ${(() => { const путь = с.source === 'folder' ? с.recordings_dir : с.cdr_file;
                   return путь ? `<div class="small dim mono" title="${esc(путь)}">${
                     esc(String(путь).slice(-38))}</div>` : ''; })()}</td>
              <td class="small">${с.last_run ? esc(fmtTime(с.last_run)) : '<span class="dim">не было</span>'}
                ${(с.last_scan || {}).at ? `<div class="small dim">осмотр ${
                  esc(fmtAgo(с.last_scan.at))}</div>` : ''}</td>
              <td class="num">${num(з.queued || 0, 0)}</td>
              <td class="num">${num(з.skipped || 0, 0)}</td>
              <td class="num">${num(з.deferred || 0, 0)}</td>
              <td class="small">${с.last_error
                ? `<span class="chip err" title="${esc(с.last_error)}">${
                    esc(String(с.last_error).slice(0, 42))}</span>`
                : !с.enabled ? '<span class="chip">выключена</span>'
                : с.running ? '<span class="dim">—</span>'
                : '<span class="chip warn">поток стоит</span>'}</td>
              <td>${(state.me || {}).role === 'admin' ? `<button class="ghost sm"
                data-scan="${esc(с.id)}" title="Заход только по этой станции">Забрать</button>` : ''}</td>
            </tr>`;
          }).join('') : '<tr><td colspan="8" class="dim">Ни одной АТС не подключено</td></tr>'}</tbody>
        </table></div>
      </section>`;

    Charts.hbars(qs('#pbx-reasons'), {
      items: причины.slice(0, 12).map((п) => ({
        label: п.value, value: п.count, color: Charts.status().warn })),
      labelWidth: 170, emptyText: 'Все звонки периода доехали до распознавания',
    });
    const за = д.period_calls || {};
    const пропущено = за.skipped || 0;
    Charts.stacked(qs('#pbx-funnel'), {
      parts: [
        { label: 'распознано', value: за.queued || 0 },
        { label: 'пропущено', value: пропущено },
        { label: 'остальное', value: Math.max(0, (за.total || 0) - (за.queued || 0) - пропущено) },
      ],
      emptyText: 'За период звонков не было',
    });
    const заход = qs('#pbx-scan-2');
    if (заход) заход.addEventListener('click', () => this.scan(''));
    qsa('button[data-scan]').forEach((кнопка) =>
      кнопка.addEventListener('click', () => this.scan(кнопка.dataset.scan, кнопка)));
  },

  /* --- Действия -------------------------------------------------------- */

  act(действие, ид, кнопка) {
    if (действие === 'calls') {
      state.telStation = ид;
      go('telephony');
      return null;
    }
    if (действие === 'test') return this.test(ид, кнопка);
    if (действие === 'scan') return this.scan(ид, кнопка);
    if (действие === 'edit') return this.edit(ид);
    if (действие === 'toggle') return this.toggle(ид);
    if (действие === 'remove') return this.remove(ид);
    return null;
  },

  async test(ид, кнопка) {
    const прежний = кнопка ? кнопка.textContent : '';
    if (кнопка) { кнопка.disabled = true; кнопка.textContent = 'Проверяю…'; }
    try {
      const итог = await API.post(`/api/telephony/test?station=${encodeURIComponent(ид)}`);
      toast(`Связь есть: ${ПАТС_ОТВЕТ(итог)}`, 'ok', `Ответ за ${num(итог.ms || 0, 0)} мс`);
    } catch (err) {
      toast(err.message || 'Источник недоступен', 'err', err.hint || '');
    } finally {
      if (кнопка) { кнопка.disabled = false; кнопка.textContent = прежний; }
    }
  },

  async scan(ид, кнопка) {
    const прежний = кнопка ? кнопка.textContent : '';
    if (кнопка) { кнопка.disabled = true; кнопка.textContent = 'Забираю…'; }
    try {
      const пар = ид ? `?station=${encodeURIComponent(ид)}` : '';
      const итог = await API.post(`/api/telephony/scan${пар}`);
      const причины = Object.entries(итог.reasons || {}).map(([п, n]) => `${п}: ${n}`).join(', ');
      const сбои = (итог.errors || []).map((о) => `${о.name || о.station}: ${о.error}`).join('; ');
      toast(`Просмотрено ${итог.seen}, поставлено ${итог.imported}, пропущено ${итог.skipped}`,
            сбои ? 'warn' : итог.imported ? 'ok' : '', сбои || причины);
      await this.load();
    } catch (err) {
      toast(err.message || 'Заход не удался', 'err', err.hint || '');
    } finally {
      if (кнопка) { кнопка.disabled = false; кнопка.textContent = прежний; }
    }
  },

  async toggle(ид) {
    const станция = (state.pbxData.stations || []).find((с) => с.id === ид);
    if (!станция) return;
    try {
      await API.post(`/api/telephony/stations/${encodeURIComponent(ид)}/enabled?enabled=${
        станция.enabled ? 'false' : 'true'}`);
      toast(станция.enabled ? `Станция «${станция.name}» выключена`
                            : `Станция «${станция.name}» включена`, 'ok',
            станция.enabled ? 'Настройки и позиция чтения журнала сохранены' : '');
      await this.load();
    } catch (err) { fail(err); }
  },

  async remove(ид) {
    const станция = (state.pbxData.stations || []).find((с) => с.id === ид);
    if (!станция) return;
    const архив = (станция.calls || {}).total || 0;
    // Подтверждение спрашиваем, но не пугаем: звонки остаются, и человеку
    // важно это знать до нажатия, а не после.
    const ответ = confirm(`Убрать станцию «${станция.name}»?\n\n`
      + `Её ${num(архив, 0)} ${plural(архив, 'звонок', 'звонка', 'звонков')} останутся в архиве `
      + 'и в отчётах: убирается только подключение. Чтобы убрать и записи, '
      + 'пользуйтесь сроком хранения в разделе «Сервер».');
    if (!ответ) return;
    try {
      const итог = await API.del(`/api/telephony/stations/${encodeURIComponent(ид)}`);
      toast(`Станция «${станция.name}» убрана`, 'ok',
            `Звонков осталось в архиве: ${num(итог.kept_calls || 0, 0)}`);
      if (state.pbxStation === ид) state.pbxStation = '';
      await this.load();
    } catch (err) { fail(err); }
  },
};

/* Короткий человеческий ответ на «проверить связь» — один и тот же и в
 * карточке станции, и в форме её настройки. */
function ПАТС_ОТВЕТ(итог) {
  if (итог.version) return `Asterisk ${итог.version}`;
  if (итог.files !== undefined) return `файлов записей: ${num(итог.files, 0)}`;
  const строки = (итог.sample || []).length;
  return `журнал ${num((итог.bytes || 0) / 1048576, 1)} МБ, прочитано до ${
    num((итог.offset || 0) / 1048576, 1)} МБ${строки ? `, разобрано строк-образцов: ${строки}` : ''}`;
}

/* Форма станции: все поля с описанием, рекомендацией и примерами, а рядом
 * кнопка «Проверить подключение», которая работает ДО сохранения. Проверка
 * после сохранения — это предложение сначала завести в настройках станцию
 * неизвестно куда, а потом выяснять, доедет ли до неё сервер. */
RENDERERS.pbx.edit = function (ид) {
  const станция = ид
    ? ((state.pbxData || {}).stations || []).find((с) => с.id === ид)
    : null;
  if (ид && !станция) return;
  const значения = станция ? ПАТС_ИЗ_СТАНЦИИ(станция) : ПАТС_ПО_УМОЛЧАНИЮ();

  const поле = (п) => {
    const значение = значения[п.key];
    if (п.type === 'bool') {
      return `<label class="row" style="gap:8px;cursor:pointer">
        <input type="checkbox" data-field="${п.key}" ${значение ? 'checked' : ''}
          style="width:auto"><span class="small">${значение ? 'включено' : 'выключено'}</span></label>`;
    }
    if (п.type === 'select') {
      return `<select data-field="${п.key}">${п.options.map(([з, имя]) =>
        `<option value="${esc(з)}"${з === значение ? ' selected' : ''}>${esc(имя)}</option>`)
        .join('')}</select>`;
    }
    const тип = п.type === 'number' ? 'number' : п.type === 'password' ? 'password' : 'text';
    return `<input type="${тип}" data-field="${п.key}" value="${esc(String(значение ?? ''))}"
      ${п.type === 'password' && станция ? 'placeholder="оставьте пустым — пароль не изменится"' : ''}>`;
  };

  const карточка = (п) => `<div class="param" data-only="${esc(п.only || '')}">
    <div>
      <div class="param-head"><span class="param-label">${esc(п.label)}</span>
        <span class="param-key">${esc(п.key)}</span>
        ${п.required ? '<span class="chip warn">обязательное</span>' : ''}
        ${п.only ? `<span class="chip">только для источника «${
          esc(ПАТС_ИСТОЧНИКИ[п.only] || п.only)}»</span>` : ''}</div>
      <div class="param-desc">${esc(п.desc)}</div>
      ${п.rec ? `<div class="param-rec"><b>Рекомендация.</b> ${esc(п.rec)}</div>` : ''}
      ${(п.examples || []).length ? `<details class="help"><summary>Примеры (${
        п.examples.length})</summary><ul class="small dim" style="margin:6px 0 0;padding-left:18px">${
        п.examples.map((пр) => `<li class="mono">${esc(пр)}</li>`).join('')}</ul></details>` : ''}
    </div>
    <div class="param-control"><div class="control-slot">${поле(п)}</div></div>
  </div>`;

  const backdrop = h(`<div class="modal-backdrop"><div class="modal" style="max-width:860px">
    <div class="modal-head"><b>${ид ? `Станция «${esc(станция.name)}»` : 'Новая АТС'}</b>
      ${ид ? `<span class="chip mono" title="идентификатор станции; к нему привязан её архив">${
        esc(ид)}</span>` : ''}
      <span class="spacer"></span>
      <button class="ghost icon" id="pbx-close" aria-label="Закрыть" title="Закрыть">✕</button></div>
    <div class="modal-body">
      <div id="pbx-check"></div>
      <div class="params" id="pbx-fields">${ПАТС_ПОЛЯ.map(карточка).join('')}</div>
    </div>
    <div class="modal-foot">
      <span class="small dim" id="pbx-form-hint">Проверка связи работает и до сохранения.</span>
      <span class="spacer"></span>
      <button class="ghost" id="pbx-check-btn"
        title="Достучаться до источника с этими полями, ничего не сохраняя">Проверить подключение</button>
      <button class="primary" id="pbx-save">${ид ? 'Сохранить' : 'Добавить станцию'}</button>
    </div>
  </div></div>`);
  // Через mountModal, как остальные окна: он вешает ловушку фокуса,
  // гасит прокрутку фона и возвращает фокус на кнопку. Форма из двух
  // десятков карточек параметров без этого прокручивала страницу под
  // подложкой, а Tab уводил в разделы за ней.
  document.body.appendChild(backdrop);
  mountModal(backdrop, { label: ид ? `Станция «${станция.name}»` : 'Новая АТС' });

  const собрать = () => {
    const данные = ид ? { id: ид } : {};
    qsa('[data-field]', backdrop).forEach((вход) => {
      const имя = вход.dataset.field;
      const описание = ПАТС_ПОЛЯ.find((п) => п.key === имя) || {};
      if (описание.type === 'bool') данные[имя] = вход.checked;
      else if (описание.type === 'number') данные[имя] = Number(вход.value || 0);
      else данные[имя] = вход.value.trim();
    });
    // Пустой пароль у существующей станции — «не менять», а не «стереть»:
    // интерфейс не показывает сохранённый пароль, и пустое поле здесь
    // означает только то, что его не трогали.
    if (ид && !данные.secret) delete данные.secret;
    return данные;
  };

  const показать_нужные = () => {
    const источник = (qs('[data-field="source"]', backdrop) || {}).value || 'cdr_csv';
    qsa('.param[data-only]', backdrop).forEach((узел) => {
      const только = узел.dataset.only;
      узел.style.display = !только || только === источник ? '' : 'none';
    });
  };
  показать_нужные();
  const выбор = qs('[data-field="source"]', backdrop);
  if (выбор) выбор.addEventListener('change', показать_нужные);
  qsa('input[type="checkbox"][data-field]', backdrop).forEach((вход) =>
    вход.addEventListener('change', () => {
      const подпись = вход.parentElement.querySelector('span');
      if (подпись) подпись.textContent = вход.checked ? 'включено' : 'выключено';
    }));

  const закрыть = () => closeModal(backdrop);
  qs('#pbx-close', backdrop).addEventListener('click', закрыть);
  backdrop.addEventListener('click', (ев) => { if (ев.target === backdrop) закрыть(); });

  qs('#pbx-check-btn', backdrop).addEventListener('click', async (ев) => {
    const кнопка = ев.currentTarget;
    const место = qs('#pbx-check', backdrop);
    кнопка.disabled = true;
    кнопка.textContent = 'Проверяю…';
    место.innerHTML = '<div class="small dim" style="margin-bottom:10px">Проверяю связь…</div>';
    try {
      const итог = await API.post('/api/telephony/test', собрать());
      const образцы = (итог.sample || []).map((з) => `<tr>
        <td class="mono small">${esc(з.pbx_uid || з.uniqueid || '')}</td>
        <td class="mono small">${esc(з.src || '')}</td>
        <td class="mono small">${esc(з.dst || '')}</td>
        <td class="small">${esc(з.direction || '—')}</td>
        <td class="small">${з.billsec ? fmtDur(з.billsec) : '—'}</td>
        <td class="small">${esc(ИТОГ_ЗВОНКА[з.disposition] || з.disposition || '')}</td></tr>`).join('');
      место.innerHTML = `<div class="banner ok" style="margin-bottom:12px">
        <b>Связь есть.</b> ${esc(ПАТС_ОТВЕТ(итог))} · ответ за ${num(итог.ms || 0, 0)} мс
        ${образцы ? `<div class="table-wrap" style="margin-top:10px"><table class="table">
          <thead><tr><th>Идентификатор</th><th>Кто</th><th>Кому</th><th>Направление</th>
            <th>Разговор</th><th>Итог</th></tr></thead><tbody>${образцы}</tbody></table></div>
          <div class="small dim" style="margin-top:6px">Так сервер прочитает последние строки
          журнала. Если направление определилось неверно — поправьте длины внутренних
          номеров и контексты.</div>` : ''}</div>`;
    } catch (err) {
      место.innerHTML = `<div class="banner err" style="margin-bottom:12px">
        <b>${esc(err.message || 'Источник недоступен')}</b>
        ${err.hint ? `<div class="small" style="margin-top:6px">${esc(err.hint)}</div>` : ''}</div>`;
      // Ответ проверки — наверху формы, а кнопка внизу: без прокрутки
      // человек нажимает «Проверить» и видит, что ничего не произошло.
      место.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } finally {
      кнопка.disabled = false;
      кнопка.textContent = 'Проверить подключение';
    }
  });

  qs('#pbx-save', backdrop).addEventListener('click', async (ев) => {
    const кнопка = ев.currentTarget;
    кнопка.disabled = true;
    try {
      await API.post('/api/telephony/stations', собрать());
      toast(ид ? 'Станция сохранена' : 'Станция добавлена', 'ok',
            ид ? '' : 'Первый заход пройдёт в ближайшую минуту — или нажмите «Забрать»');
      закрыть();
      await RENDERERS.pbx.load();
    } catch (err) {
      toast(err.message || 'Сохранить не удалось', 'err', err.hint || '');
    } finally {
      кнопка.disabled = false;
    }
  });
};

function ПАТС_ПО_УМОЛЧАНИЮ() {
  return {
    name: '', source: 'cdr_csv', enabled: true, host: '127.0.0.1', port: 5038,
    username: '', secret: '', cdr_file: '/var/log/asterisk/cdr-csv/Master.csv',
    recordings_dir: '/var/spool/asterisk/monitor', filename: '',
    internal_digits: '3, 4', contexts: 'from-trunk=входящий, from-internal=исходящий',
    min_duration_s: 10, skip_unanswered: true, settle_s: 30, lookback_days: 7,
    poll_s: 60, owner: 'telephony', priority: 40, tags: '',
  };
}

/* Станция из ответа сервера — в поля формы. Длины и правила приходят
 * разобранными (список и пары), а человеку привычнее строка. */
function ПАТС_ИЗ_СТАНЦИИ(с) {
  return {
    ...ПАТС_ПО_УМОЛЧАНИЮ(), ...с, secret: '',
    internal_digits: (с.internal_digits || []).join(', '),
    contexts: (с.contexts || []).map((п) => `${п.context}=${п.direction}`).join(', '),
  };
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
    state.contentAgent = '';
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


  // --- эмоциональный фон и стресс -----------------------------------------

  /* Тональность отвечает «хорошо или плохо». Этого мало: разговор бывает
   * ровным по тону и невыносимым по усилию, спокойным у оператора и
   * взвинченным у клиента. Здесь то, что в отраслевых методиках стоит
   * отдельными шкалами: знак эмоции, её сила, напряжение и усилие. */
  async tab_emotion(host) {
    const период = state.contentPeriod;
    const [свод, лента] = await Promise.all([
      API.latest('content-summary', `/api/content/summary?period=${период}`),
      API.latest('content-timeline', `/api/content/timeline?period=${период}`),
    ]);
    const c = свод.current || {};
    const p = свод.previous || {};
    const признак = (k) => (свод.features || []).find((f) => f.key === k) || {};
    const шкалы = [
      ['mood', 'Настроение клиента', 2], ['intensity', 'Сила эмоции', 1],
      ['stress', 'Напряжение', 0], ['effort', 'Усилие клиента', 1],
      ['fatigue', 'Усталость оператора', 0],
    ];
    host.innerHTML = `
      <div class="grid cols-5">
        ${шкалы.map(([k, имя, знаков]) => пкпи(имя, c, p, k, знаков, признак(k))).join('')}
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Настроение и напряжение во времени</h3>
          <span class="hint">по корзинам периода</span></div>
          <div id="emo-line" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Чем кончаются разговоры</h3>
          <span class="hint">сдвиг настроения от начала к концу</span></div>
          <div id="emo-shift" style="padding:12px 16px"></div>
          <div class="small dim" style="padding:0 16px 14px">
            Разговор с тяжёлым началом и тёплым концом сделан хорошо — по средней
            тональности он неотличим от ровно-никакого.</div></section>
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Напряжение по разрезам</h3>
          <span class="hint">где разговоры тяжелее</span>
          <span class="spacer"></span>
          <select id="emo-dim" style="width:180px">${ЭМОЦИЯ_РАЗРЕЗЫ.map(([к, и]) =>
            `<option value="${к}"${state.emoDim === к ? ' selected' : ''}>${esc(и)}</option>`).join('')}</select>
        </div>
          <div id="emo-breakdown" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Разговоры на нервах</h3>
          <span class="hint">напряжение 55 и выше</span></div>
          <div style="padding:12px 16px">
            ${полоса('С высоким напряжением', c.stress_high, c.stress_checked)}
            ${полоса('Клиенту пришлось пробиваться', c.effort_high, c.records)}
            ${полоса('Клиент раздражён', c.frustrated, c.records)}
            ${полоса('Повторное обращение', c.repeat, c.records)}
            ${полоса('Разговор выправился к концу', c.recovered, c.records, 'ok')}
            ${полоса('Разговор испортился к концу', c.worsened, c.records)}
          </div></section>
      </div>`;

    const точки = лента.buckets || [];
    Charts.line(qs('#emo-line'), {
      labels: точки.map((т) => fmtBucket(т.ts, лента.step_s || 86400)),
      series: [
        { name: 'настроение ×100', values: точки.map((т) =>
          (т.mood === null || т.mood === undefined ? null : Math.round(т.mood * 100))) },
        { name: 'напряжение', values: точки.map((т) => т.stress ?? null) },
      ],
      height: 250, emptyText: 'За период разобранных записей нет',
    });
    Charts.donut(qs('#emo-shift'), {
      parts: [
        { label: 'выправился', value: c.recovered || 0 },
        { label: 'испортился', value: c.worsened || 0 },
        { label: 'ровно', value: Math.max(0, (c.records || 0) - (c.recovered || 0)
                                             - (c.worsened || 0)) },
      ],
      centerLabel: 'разговоров', emptyText: 'За период разобранных записей нет',
    });
    const нарисовать = async () => {
      let разрез;
      try {
        разрез = await API.latest('content-emo-dim',
          `/api/content/breakdown/${state.emoDim}?period=${период}`);
      } catch (err) {
        if (!err || err.code !== 'aborted') fail(err);
        return;
      }
      // Вкладку могли сменить, пока ответ ехал: узла больше нет, и рисовать
      // в него — это TypeError в консоли и пустое место на экране.
      const место = qs('#emo-breakdown');
      if (!место) return;
      Charts.hbars(место, {
        items: (разрез.items || []).filter((и) => и.stress !== null && и.stress !== undefined)
          .slice(0, 12).map((и) => ({
            label: и.label || и.key, value: и.stress,
            color: Charts.status().warn,
            display: `${num(и.stress, 0)} · ${num(и.records, 0)} зап.`,
            note: `настроение ${fmtNumSafe(и.mood)} · усилие ${fmtNumSafe(и.effort)}`,
          })),
        labelWidth: 150, emptyText: 'В этом разрезе напряжение не считалось',
      });
    };
    const выбор = qs('#emo-dim');
    if (выбор) выбор.addEventListener('change', () => {
      state.emoDim = выбор.value;
      нарисовать();
    });
    await нарисовать();
  },

  // --- понятность и точность ----------------------------------------------

  async tab_clarity(host) {
    const период = state.contentPeriod;
    const [свод, лента] = await Promise.all([
      API.latest('content-summary', `/api/content/summary?period=${период}`),
      API.latest('content-timeline', `/api/content/timeline?period=${период}`),
    ]);
    const c = свод.current || {};
    const p = свод.previous || {};
    const признак = (k) => (свод.features || []).find((f) => f.key === k) || {};
    const шкалы = [
      ['clarity', 'Понятность речи', 0], ['accuracy', 'Точность ответов', 0],
      ['rhythm', 'Ритмичность речи', 0], ['politeness', 'Вежливость', 0],
      ['personalization', 'Персонализация', 0],
    ];
    host.innerHTML = `
      <div class="grid cols-5">
        ${шкалы.map(([k, имя, знаков]) => пкпи(имя, c, p, k, знаков, признак(k))).join('')}
      </div>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Понятность и точность во времени</h3></div>
          <div id="clr-line" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Что мешает слушать</h3>
          <span class="hint">доли записей с признаком</span></div>
          <div style="padding:12px 16px">
            ${полоса('Речь тяжело слушать (понятность ниже 35)', c.clarity_low, c.clarity_checked)}
            ${полоса('Уменьшительно-ласкательные', c.diminutive_records, c.records)}
            ${полоса('Долгий монолог оператора', c.long_monologues, c.records)}
            ${полоса('Клиент раздражён', c.frustrated, c.records)}
          </div>
          <div class="small dim" style="padding:0 16px 14px">
            Понятность считается по речи оператора: длина фраз, канцелярит, темп и
            слова-паразиты. Точность — конкретика против «наверное» и «где-то так».
          </div></section>
      </div>
      <div class="grid cols-3" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>Слова-паразиты</h3>
          <span class="hint">доля слов в речи</span></div>
          <div id="clr-fillers" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Уменьшительно-ласкательные</h3>
          <span class="hint">доля слов в речи</span></div>
          <div id="clr-dim" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>Ритмичность</h3>
          <span class="hint">ровность темпа и пауз</span></div>
          <div id="clr-rhythm" style="padding:12px 16px"></div></section>
      </div>`;

    const точки = лента.buckets || [];
    Charts.line(qs('#clr-line'), {
      labels: точки.map((т) => fmtBucket(т.ts, лента.step_s || 86400)),
      series: [
        { name: 'понятность', values: точки.map((т) => т.clarity ?? null) },
        { name: 'точность', values: точки.map((т) => т.accuracy ?? null) },
        { name: 'вежливость', values: точки.map((т) => т.politeness ?? null) },
      ],
      height: 250, yMin: 0, yMax: 100,
      emptyText: 'За период разобранных записей нет',
    });
    const по_людям = await API.latest('content-clarity-agents',
      `/api/content/employees?by=${state.contentAgentBy || 'agent'}&period=${период}`)
      .catch(() => ({ items: [] }));
    if (!qs('#clr-fillers')) return;          // вкладку сменили, пока ответ ехал
    const люди = (по_людям.items || []).filter((ч) => (ч.records || 0) > 0);
    const столбики = (место, поле, знаков, единица) => Charts.hbars(qs(место), {
      items: люди.filter((ч) => ч[поле] !== null && ч[поле] !== undefined)
        .sort((a, b) => b[поле] - a[поле]).slice(0, 10)
        .map((ч) => ({ label: ч.label || ч.key, value: ч[поле],
                       color: Charts.palette()[0],
                       display: `${num(ч[поле], знаков)}${единица}`,
                       note: `записей ${num(ч.records, 0)}` })),
      labelWidth: 140, emptyText: 'Разбор по сотрудникам пока пуст',
    });
    столбики('#clr-fillers', 'filler_rate', 4, '');
    столбики('#clr-dim', 'diminutive_rate', 4, '');
    столбики('#clr-rhythm', 'rhythm', 0, '');
  },

  // --- NPS ----------------------------------------------------------------

  async tab_nps(host) {
    const период = state.contentPeriod;
    const [свод, лента] = await Promise.all([
      API.latest('content-summary', `/api/content/summary?period=${период}`),
      API.latest('content-timeline', `/api/content/timeline?period=${период}`),
    ]);
    const c = свод.current || {};
    const p = свод.previous || {};
    const всего = c.nps_checked || 0;
    const индекс = c.nps_index;
    host.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Индекс NPS</h3>
          <span class="hint">доля промоутеров минус доля критиков</span>
          <span class="spacer"></span>
          ${c.nps_stated_count ? `<span class="chip ok" title="балл, который клиент назвал вслух">
            назвали балл: ${num(c.nps_stated_count, 0)}</span>` : ''}
          <span class="chip" title="по скольким разговорам вообще есть оценка">${
            num(всего, 0)} ${plural(всего, 'разговор', 'разговора', 'разговоров')}</span>
        </div>
        <div class="grid cols-4" style="padding:14px 16px">
          ${kpi('Индекс NPS', индекс === null || индекс === undefined ? '—' : num(индекс, 0),
                p.nps_index !== null && p.nps_index !== undefined
                  ? `было ${num(p.nps_index, 0)}` : 'от −100 до +100')}
          ${kpi('Средний балл', c.nps === null || c.nps === undefined ? '—' : num(c.nps, 1),
                'по шкале 0–10')}
          ${kpi('Названный балл', c.nps_stated_avg === null || c.nps_stated_avg === undefined
                  ? '—' : num(c.nps_stated_avg, 1),
                c.nps_stated_count ? `индекс ${c.nps_stated_index ?? '—'} по ${
                  num(c.nps_stated_count, 0)} ответам` : 'клиентов не спрашивали')}
          ${kpi('Промоутеры', num(c.promoters || 0, 0),
                `нейтралы ${num(c.passives || 0, 0)} · критики ${num(c.detractors || 0, 0)}`)}
        </div>
        <div style="padding:0 16px 16px"><div id="nps-bar"></div></div>
        <div class="banner" style="margin:0 16px 16px">
          <b>Предсказанный балл — не опрос.</b> Там, где клиента прямо спросили
          «оцените по шкале», сервер берёт названное число и помечает его как
          названное. Где не спрашивали — считает балл из настроения к концу
          разговора, усилия клиента и напряжения. Смешивать эти два числа в
          отчёте наружу нельзя: первое — факт, второе — оценка.
        </div>
      </section>
      <div class="grid cols-2" style="margin-top:14px">
        <section class="card"><div class="card-head"><h3>NPS во времени</h3></div>
          <div id="nps-line" style="padding:12px 16px"></div></section>
        <section class="card"><div class="card-head"><h3>NPS по разрезам</h3>
          <span class="spacer"></span>
          <select id="nps-dim" style="width:180px">${ЭМОЦИЯ_РАЗРЕЗЫ.map(([к, и]) =>
            `<option value="${к}"${state.npsDim === к ? ' selected' : ''}>${esc(и)}</option>`).join('')}</select>
        </div>
          <div id="nps-breakdown" style="padding:12px 16px"></div></section>
      </div>`;

    Charts.stacked(qs('#nps-bar'), {
      parts: [
        { label: 'промоутеры', value: c.promoters || 0, color: Charts.status().ok },
        { label: 'нейтралы', value: c.passives || 0, color: Charts.status().idle },
        { label: 'критики', value: c.detractors || 0, color: Charts.status().err },
      ],
      height: 30, emptyText: 'За период оценок нет',
    });
    const точки = лента.buckets || [];
    Charts.line(qs('#nps-line'), {
      labels: точки.map((т) => fmtBucket(т.ts, лента.step_s || 86400)),
      series: [{ name: 'средний балл', values: точки.map((т) => т.nps ?? null) }],
      height: 250, yMin: 0, yMax: 10,
      emptyText: 'За период разобранных записей нет',
    });
    const нарисовать = async () => {
      let разрез;
      try {
        разрез = await API.latest('content-nps-dim',
          `/api/content/breakdown/${state.npsDim}?period=${период}`);
      } catch (err) {
        if (!err || err.code !== 'aborted') fail(err);
        return;
      }
      const место = qs('#nps-breakdown');
      if (!место) return;
      Charts.hbars(место, {
        items: (разрез.items || []).filter((и) => и.nps_index !== null
                                                  && и.nps_index !== undefined)
          .sort((a, b) => b.nps_index - a.nps_index).slice(0, 12)
          .map((и) => ({
            label: и.label || и.key, value: и.nps_index,
            display: `${num(и.nps_index, 0)} · ${num(и.records, 0)} зап.`,
            note: `промоутеров ${num(и.promoters, 0)}, критиков ${num(и.detractors, 0)}`,
          })),
        // Двусторонний вид не навязываем: когда критиков больше везде,
        // все значения отрицательные, и деление оси пополам оставляет
        // половину картинки пустой.
        labelWidth: 160, emptyText: 'В этом разрезе оценок нет',
      });
    };
    const выбор = qs('#nps-dim');
    if (выбор) выбор.addEventListener('change', () => {
      state.npsDim = выбор.value;
      нарисовать();
    });
    await нарисовать();
  },

  // --- свод ---------------------------------------------------------------

  async tab_summary(host) {
    const период = state.contentPeriod;
    const [свод, лента, выводы, нормы, карты] = await Promise.all([
      API.latest('content-summary', `/api/content/summary?period=${период}`),
      API.latest('content-timeline', `/api/content/timeline?period=${период}`),
      API.latest('content-findings', `/api/content/findings?period=${период}`),
      API.latest('content-norms', `/api/content/norms?period=${период}`)
        .catch(() => ({ items: [] })),
      API.latest('content-control', `/api/content/control?period=${период}`)
        .catch(() => ({ charts: [] })),
    ]);
    const c = свод.current || {};
    const p = свод.previous || {};
    const признак = (k) => (свод.features || []).find((f) => f.key === k) || {};
    const норма = (k) => (нормы.items || []).find((n) => n.key === k) || {};
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
        ${kpi('Балл оператора', c.agent_score === null || c.agent_score === undefined ? '—' : num(c.agent_score, 0),
              c.agent_score === null || c.agent_score === undefined
                ? 'скрипт с весами минус штрафы; без пунктов скрипта балла нет'
                : `из 100 по ${num(c.scored_agents)} записям — скрипт с весами минус штрафы стоп-слов`,
              delta(c.agent_score, p.agent_score, признак('agent_score')))}
        ${kpi('Нарушения оператора', num(c.violation_records),
              c.violation_share === null || c.violation_share === undefined ? 'стоп-слова и другие категории вида «нарушение»'
                : `${num(c.violation_share, 1)}% записей — стоп-слова и другие категории вида «нарушение»`)}
        ${kpi('Индекс эмпатии', c.empathy === null || c.empathy === undefined ? '—' : `${c.empathy > 0 ? '+' : ''}${num(c.empathy, 0)}`,
              '(вежливых − невежливых) ÷ сумму по репликам оператора; от −100 до +100',
              delta(c.empathy, p.empathy, признак('empathy')))}
        ${kpi('Возражений без отработки',
              c.objections_unhandled_share === null || c.objections_unhandled_share === undefined
                ? '—' : num(c.objections_unhandled),
              c.objections_unhandled_share === null || c.objections_unhandled_share === undefined
                ? (c.objections ? `возражений ${num(c.objections)}; отработку считать нечем — в наборе нет категорий «отработка»`
                                : 'возражений клиента за период нет')
                : `${num(c.objections_unhandled_share, 1)}% из ${num(c.objections)} — за «дорого» и «подумаю» не последовало отработки`)}
      </div>
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

      ${card('Речь и разговор',
             `усреднённые характеристики записей периода; норма — медиана и коридор половины записей за ${
               num(нормы.baseline_days || 28)} дней до периода (${num(нормы.baseline_records || 0)} записей)`,
             `<div class="table-wrap full"><table>
               <thead><tr><th>Показатель</th><th class="num">За период</th>
                 <th class="num">Прошлый период</th><th class="num">Изменение</th>
                 <th class="num" title="медиана и межквартильный размах по своему архиву за четыре недели до периода">Своя норма</th>
                 <th title="медиана периода против коридора нормы">Относительно нормы</th></tr></thead>
               <tbody>${(свод.features || []).map((f) => {
                 const a = c[f.key], b = p[f.key];
                 if (a === null || a === undefined) return '';
                 let d = (b === null || b === undefined) ? null : a - b;
                 // Разница мельче показанной точности — это ноль, а не
                 // изменение: иначе столбец пестрит «−0» и «+0.000».
                 if (d !== null && Math.abs(d) < Math.pow(10, -f.digits) / 2) d = 0;
                 const н = норма(f.key);
                 return `<tr><td${f.hint ? ` title="${esc(f.hint)}"` : ''}>${esc(f.title)}${f.unit ? ` <span class="faint small">${esc(f.unit)}</span>` : ''}${
                   f.hint ? `<div class="faint small">${esc(f.hint)}</div>` : ''}</td>
                   <td class="num mono">${num(a, f.digits)}</td>
                   <td class="num mono faint">${b === null || b === undefined ? '—' : num(b, f.digits)}</td>
                   <td class="num mono ${d && f.good ? (Math.sign(d) * f.good > 0 ? 'ok-text' : 'err-text') : ''}">${
                     d === null ? '—' : d === 0 ? 'без изменений'
                       : (d > 0 ? '+' : '') + num(d, f.digits)}</td>
                   <td class="num mono small">${н.enough ? `${num(н.median, f.digits)} <span class="faint">[${num(н.q1, f.digits)}–${num(н.q3, f.digits)}]</span>`
                     : н.n ? `<span class="faint" title="норма считается от ${num(н.min_sample || 30)} записей">мало данных (${num(н.n)})</span>` : '—'}</td>
                   <td>${НОРМА_ФИШКА(н.status, f.good)}</td></tr>`;
               }).join('')}</tbody></table></div>`)}

      ${(карты.charts || []).length ? card('Контрольные карты',
        `по дням; пределы 2σ и 3σ — по ${num(карты.baseline_days || 28)} дням до периода; серия из семи точек по одну сторону от среднего — сдвиг`,
        `<div class="grid cols-2">${(карты.charts || []).map((к) => `
          <div>
            <div class="row" style="gap:8px;margin-bottom:6px"><b>${esc(к.title)}</b>
              ${к.worst === 'critical' ? '<span class="chip err">за 3σ</span>'
                : к.worst === 'warning' ? '<span class="chip warn">за 2σ или серия</span>'
                : к.limits && к.limits.enough ? '<span class="chip ok">в пределах</span>'
                : '<span class="chip">пределы не посчитаны — мало дней в базе</span>'}</div>
            <div id="spc-${esc(к.key)}"></div>
            ${(к.flags || []).length ? `<div class="small dim" style="margin-top:6px">${(к.flags || []).slice(0, 4).map((ф) =>
              `${fmtTime((к.points[ф.index] || {}).ts).slice(0, 5)}: ${num(ф.value, 2)} — ${esc(ф.why)}`).join('; ')}</div>` : ''}
          </div>`).join('')}</div>`) : ''}`;

    toneBar(qs('#tone-bar'), c);
    // Контрольные карты: значение по дням и пределы 2σ/3σ плоскими линиями.
    (карты.charts || []).forEach((к) => {
      const узел = qs(`#spc-${к.key}`);
      if (!узел) return;
      const точки = к.points || [];
      const метки = точки.map((т) => fmtTime(т.ts).slice(0, 5));
      const пределы = к.limits || {};
      const ряд = (v) => точки.map(() => (v === null || v === undefined ? null : v));
      window.Charts.line(узел, {
        height: 180, labels: метки,
        series: [
          { name: к.title, values: точки.map((т) => т.value) },
          ...(пределы.enough ? [
            { name: 'среднее базы', values: ряд(пределы.mean) },
            { name: '+2σ', values: ряд(пределы.warn_high) },
            { name: '−2σ', values: ряд(пределы.warn_low) },
          ] : []),
        ],
        emptyText: 'нет данных за период',
      });
    });
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
    host.insertAdjacentHTML('beforeend', '<div id="llm-summary"></div>');
    this.drawLlmSummary(qs('#llm-summary', host), период);
  },

  /* Смысловой слой в своде: причины и исходы по ответам языковой модели,
   * доля решённых, действия к исполнению, умные трекеры и скоркарта.
   * Отдельным запросом и после остального: модель — необязательный слой, и
   * свод без неё обязан рисоваться так же быстро, как раньше. */
  async drawLlmSummary(host, период) {
    if (!host) return;
    let д;
    try {
      д = await API.latest('content-llm', `/api/content/llm?period=${период}`);
    } catch (err) {
      if (err && err.silent) return;
      return;
    }
    if (!host.isConnected) return;
    if (!д.enabled && !д.analyzed) return;
    const проц = (v) => (v === null || v === undefined ? '—' : `${num(v, 0)}%`);
    if (!д.analyzed) {
      host.innerHTML = card('По ответам языковой модели',
        `модель ${esc(д.model || '')} подключена, но записей с ответом за период нет`,
        `<div class="empty small">Новые записи разбираются в фоне${(д.worker || {}).queued ? ` — в очереди ${num(д.worker.queued)}` : ''}.
         Разобрать архив: кнопка в «Настройках» → «Языковая модель» или POST /api/llm/backfill.</div>`);
      return;
    }
    const исходы = д.outcomes || [];
    const причины = д.reasons || [];
    host.innerHTML = card('По ответам языковой модели',
      `${esc(д.model || '')} · разобрано ${num(д.analyzed)} из ${num(д.records)} записей периода${д.stale ? ` · ${num(д.stale)} по прежним подсказкам` : ''}${д.errors ? ` · ошибок ${num(д.errors)}` : ''}${д.off_list ? ` · ${num(д.off_list)} ответов мимо списка` : ''}`,
      `<div class="grid cols-4" style="margin-bottom:10px">
        ${kpi('Вопрос решён', проц(д.resolved_share), 'по оценке модели')}
        ${kpi('Действий к исполнению', num(д.actions || 0), `в ${num(д.records_with_actions || 0)} записях`)}
        ${kpi('Покрытие', д.coverage === null ? '—' : pct(д.coverage, 0), 'записей с ответом')}
        ${kpi('Ответ модели', д.avg_latency_ms ? `${num(д.avg_latency_ms / 1000, 1)} с` : '—', 'на запись, в среднем')}
      </div>
      <div class="grid cols-2">
        <div><div class="small dim" style="margin-bottom:6px"><b>Исходы</b></div><div id="llm-outcomes"></div></div>
        <div><div class="small dim" style="margin-bottom:6px"><b>Причины обращений</b></div><div id="llm-reasons"></div></div>
      </div>
      ${(д.trackers || []).length ? `<table style="margin-top:10px"><thead><tr><th>Умный трекер</th><th class="num">Проверено</th><th class="num">Сработал</th><th class="num">Доля</th><th></th></tr></thead><tbody>
        ${д.trackers.map((т) => `<tr><td>${esc(т.label || т.id)}</td><td class="num">${num(т.checked)}</td><td class="num">${num(т.fired)}</td>
          <td class="num mono">${т.checked ? pct(т.fired / т.checked, 0) : '—'}</td><td></td></tr>`).join('')}</tbody></table>` : ''}
      ${(д.scorecard || []).length ? `<table style="margin-top:10px"><thead><tr><th>Вопрос скоркарты</th><th class="num">Да</th><th class="num">Нет</th><th class="num">Н/п</th><th class="num">Доля «да»</th></tr></thead><tbody>
        ${д.scorecard.map((в) => `<tr><td>${esc(в.question || в.id)}</td><td class="num">${num(в['да'])}</td><td class="num">${num(в['нет'])}</td><td class="num">${num(в['н/п'])}</td>
          <td class="num mono">${(в['да'] + в['нет']) ? pct(в['да'] / (в['да'] + в['нет']), 0) : '—'}</td></tr>`).join('')}</tbody></table>` : ''}
      ${(д.action_items || []).length ? `<div class="small dim" style="margin-top:10px"><b>Действия к исполнению</b> — последние</div>
        <div class="analysis-lines">${д.action_items.slice(0, 8).map((а) => `<div class="analysis-line">
          <span class="ts mono">${fmtTime(а.created_at).slice(0, 5)}</span>
          <span class="who">${esc(а.who || '')}</span>
          <span class="what"><a href="#" onclick="window.__asrhub.openJob('${esc(а.job_id)}', {tab: 'analysis'});return false">${esc(а.what)}</a>${а.when ? ` <span class="chip">${esc(а.when)}</span>` : ''}</span></div>`).join('')}</div>` : ''}
      <div class="small faint" style="margin-top:8px">Сгенерировано языковой моделью: причина и исход выбраны из закрытых списков, резюме и действия — пересказ модели, который может ошибаться. Отбор записей по исходу и причине — в «Результатах».</div>`);
    if (исходы.length) {
      Charts.donut(qs('#llm-outcomes', host), {
        size: 150, centerLabel: 'записей', emptyText: 'исходы не спрашивались',
        parts: исходы.map((и) => ({ label: и.key, value: и.records })),
      });
    } else { qs('#llm-outcomes', host).innerHTML = '<div class="empty small">исходы не спрашивались</div>'; }
    if (причины.length) {
      Charts.hbars(qs('#llm-reasons', host), {
        items: причины.map((п) => ({ label: п.key, value: п.records })), labelWidth: 200,
        emptyText: 'причины не спрашивались',
      });
    } else { qs('#llm-reasons', host).innerHTML = '<div class="empty small">причины не спрашивались</div>'; }
  },

  // --- разрезы -------------------------------------------------------------

  async tab_groups(host) {
    const период = state.contentPeriod;
    const разрезы = ['owner', 'speaker', 'tag', 'category', 'model', 'source', 'weekday', 'hour'];
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
          <th class="num">Темп</th><th class="num" title="перебиваний на запись">Перебив.</th>
          <th class="num" title="доля тишины в записи">Тишина</th>
          <th class="num" title="доля времени речи оператора; ориентир 40–60 %">Речь опер.</th>
          <th class="num" title="средний самый долгий монолог оператора, секунд">Монолог</th>
          <th class="num" title="средний балл оператора из 100: скрипт с весами минус штрафы">Балл</th>
          <th class="num" title="индекс эмпатии от −100 до +100">Эмпатия</th>
          <th class="num" title="записей с нарушениями оператора">Наруш.</th></tr></thead>
        <tbody>${items.map((г) => `<tr>
          <td>${esc(г.label)}${г.kind && г.kind !== 'topic'
            ? ` <span class="chip ${КАТЕГОРИЯ_ЦВЕТ[г.kind] || ''}">${esc(КАТЕГОРИЯ_ВИД[г.kind] || г.kind)}</span>` : ''}</td>
          <td class="num mono">${num(г.records)}</td>
          <td class="num">${toneChip(г.sentiment)}</td>
          <td class="num mono">${г.negative_share === null ? '—'
            : num(г.negative_share, 1) + '%'}</td>
          <td class="num mono">${num(г.alert_records)}</td>
          <td class="num mono">${г.compliance === null ? '—' : pct(г.compliance, 0)}</td>
          <td class="num mono">${num(г.wpm, 0)}</td>
          <td class="num mono">${num(г.interruptions, 1)}</td>
          <td class="num mono">${г.silence_share === null ? '—' : pct(г.silence_share, 0)}</td>
          <td class="num mono">${г.talk_share === null || г.talk_share === undefined ? '—' : pct(г.talk_share, 0)}</td>
          <td class="num mono">${г.monologue_s === null || г.monologue_s === undefined ? '—' : num(г.monologue_s, 0) + ' с'}</td>
          <td class="num mono">${г.agent_score === null || г.agent_score === undefined ? '—' : num(г.agent_score, 0)}</td>
          <td class="num mono">${г.empathy === null || г.empathy === undefined ? '—' : num(г.empathy, 0)}</td>
          <td class="num mono">${num(г.violation_records)}</td></tr>`).join('')}</tbody></table>
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
      ${card('По категориям обращений',
             'запись про оплату и доставку входит в обе группы; средние по категории — против средних по всем',
             таблица(по_ключу.category))}
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
    // Метод зовут и по щелчку по кнопке отбора — без await и без catch.
    // Отказ запроса оставлял на экране таблицу ПРЕЖНЕГО отбора под новой
    // подписью: человек читает чужие данные как результат своего выбора.
    let данные;
    try {
      данные = await API.latest('content-records',
        `/api/content/records?kind=${encodeURIComponent(state.contentKind)}` +
        `&period=${state.contentPeriod}&limit=50`);
    } catch (err) {
      if (err && err.silent) return;
      if (host.isConnected) {
        host.innerHTML = `<div class="empty small">Записи не получены: ${
          esc((err && err.message) || 'ошибка запроса')}</div>`;
      }
      return;
    }
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

/* Операторы: список с баллом, карточка против команды, очередь коучинга,
 * эталонные разговоры.
 *
 * Оператор здесь — метка говорящего в разборе («кто заговорил первым») или
 * владелец задания (ключ доступа). Второе точнее там, где у каждого
 * сотрудника свой ключ; первое работает без всякой настройки. Переключатель
 * — в панели вкладки, и выбор запоминается на время сеанса.
 */

const ПРИЧИНА_ЦВЕТ = {
  'нарушение оператора': 'err', 'нецензурная лексика у сотрудника': 'err',
  'невежливых оборотов больше вежливых': 'err', 'балл ниже 60': 'warn',
  'скрипт меньше половины': 'warn', 'возражение без отработки': 'warn',
  'клиент раздражён': 'warn',
};

RENDERERS.content.tab_agents = async function (host) {
  state.contentAgentBy = state.contentAgentBy || 'speaker';
  if (state.contentAgent) return this.drawAgentCard(host);
  const период = state.contentPeriod;
  const [операторы, очередь, эталоны] = await Promise.all([
    API.latest('content-agents',
      `/api/content/agents?period=${период}&by=${state.contentAgentBy}`),
    API.latest('content-coaching', `/api/content/coaching?period=${период}&limit=30`),
    API.latest('content-references', `/api/content/references?period=${период}&limit=10`),
  ]);
  const items = операторы.items || [];
  host.innerHTML = `
    <div class="settings-toolbar">
      <span class="small dim">Оператор — это:</span>
      <div class="group-nav" id="agent-by">
        <button data-by="speaker" class="${state.contentAgentBy === 'speaker' ? 'active' : ''}"
          title="метка говорящего в разборе: кто заговорил первым">говорящий</button>
        <button data-by="owner" class="${state.contentAgentBy === 'owner' ? 'active' : ''}"
          title="владелец задания — ключ доступа, под которым записи загружены">владелец</button>
      </div>
    </div>
    ${card('Операторы за период',
           'щелчок по строке открывает карточку: показатели против команды, ход по неделям, что послушать',
           items.length ? `<div class="table-wrap full"><table id="agents-table">
        <thead><tr><th>Оператор</th><th class="num">Записей</th>
          <th class="num" title="средний балл из 100">Балл</th>
          <th class="num">Тональность</th><th class="num">Отрицательных</th>
          <th class="num" title="индекс эмпатии от −100 до +100">Эмпатия</th>
          <th class="num" title="записей с нарушениями">Наруш.</th>
          <th class="num" title="доля записей, где оператор назвал клиента по имени">По имени</th>
          <th class="num">Скрипт</th><th class="num" title="доля речи оператора">Речь</th><th></th></tr></thead>
        <tbody>${items.map((г) => `<tr class="clickable" data-agent="${esc(г.key)}">
          <td><b>${esc(г.label)}</b></td>
          <td class="num mono">${num(г.records)}</td>
          <td class="num mono">${г.agent_score === null || г.agent_score === undefined ? '—' : num(г.agent_score, 0)}</td>
          <td class="num">${toneChip(г.sentiment)}</td>
          <td class="num mono">${г.negative_share === null ? '—' : num(г.negative_share, 1) + '%'}</td>
          <td class="num mono">${г.empathy === null || г.empathy === undefined ? '—' : num(г.empathy, 0)}</td>
          <td class="num mono">${num(г.violation_records)}</td>
          <td class="num mono">${г.named_share === null || г.named_share === undefined ? '—' : num(г.named_share, 0) + '%'}</td>
          <td class="num mono">${г.compliance === null ? '—' : pct(г.compliance, 0)}</td>
          <td class="num mono">${г.talk_share === null || г.talk_share === undefined ? '—' : pct(г.talk_share, 0)}</td>
          <td><button class="ghost sm" data-agent="${esc(г.key)}">Карточка</button></td></tr>`).join('')}
        </tbody></table></div>${операторы.hidden ? `<p class="small faint" style="margin:8px 12px">
          Скрыто операторов с числом записей меньше пяти: ${операторы.hidden}.</p>` : ''}`
           : '<div class="empty small">За период нет операторов с пятью и более записями</div>')}
    <div class="grid cols-2">
      ${card(`Очередь коучинга (${num(очередь.total)})`,
             'записи, которые стоит разобрать с оператором, — худшие первыми; «разобрано» убирает из очереди',
             this.coachingTable(очередь.items || []))}
      ${card('Эталонные разговоры',
             'лучшие по баллу и тональности без нарушений — и отмеченные руками «показывать новичкам»',
             this.referencesTable(эталоны.items || []))}
    </div>`;
  qsa('#agent-by button').forEach((b) => b.addEventListener('click', () => {
    state.contentAgentBy = b.dataset.by;
    state.contentData = {};
    this.showTab();
  }));
  qsa('[data-agent]', host).forEach((el) => el.addEventListener('click', () => {
    state.contentAgent = el.dataset.agent;
    this.showTab();
  }));
  this.bindMarks(host);
};

/** Таблица очереди коучинга — общая для вкладки и карточки оператора. */
RENDERERS.content.coachingTable = function (items) {
  if (!items.length) return '<div class="empty small">Очередь пуста — разбирать нечего</div>';
  return `<div class="table-wrap"><table>
    <thead><tr><th>Запись</th><th>Когда</th><th>Почему</th>
      <th class="num">Балл</th><th class="num">Тон.</th><th></th></tr></thead>
    <tbody>${items.map((з) => `<tr>
      <td class="truncate" style="max-width:200px"><a href="#" data-open="${esc(з.job_id)}">${esc(з.filename || з.job_id)}</a>
        ${з.mark && з.mark.status === 'done' ? '<span class="chip ok">разобрано</span>' : ''}</td>
      <td class="small faint nowrap">${fmtTime(з.created_at)}</td>
      <td><div class="chips">${(з.reasons || []).map((п) =>
        `<span class="chip ${ПРИЧИНА_ЦВЕТ[п] || ''}">${esc(п)}</span>`).join('')}</div></td>
      <td class="num mono">${з.agent_score === null || з.agent_score === undefined ? '—' : num(з.agent_score, 0)}</td>
      <td class="num">${toneChip(з.sentiment)}</td>
      <td class="nowrap">${з.mark && з.mark.status === 'done'
        ? `<button class="ghost sm" data-mark="coaching" data-status="open" data-job="${esc(з.job_id)}">Вернуть</button>`
        : `<button class="btn sm" data-mark="coaching" data-status="done" data-job="${esc(з.job_id)}"
             title="Отметить разобранным: запись уйдёт из очереди">Разобрано</button>`}</td></tr>`).join('')}
    </tbody></table></div>`;
};

RENDERERS.content.referencesTable = function (items) {
  if (!items.length) return '<div class="empty small">За период нет записей с баллом и без нарушений</div>';
  return `<div class="table-wrap"><table>
    <thead><tr><th>Запись</th><th>Когда</th><th class="num">Балл</th>
      <th class="num">Тон.</th><th></th></tr></thead>
    <tbody>${items.map((з) => `<tr>
      <td class="truncate" style="max-width:200px"><a href="#" data-open="${esc(з.job_id)}">${esc(з.filename || з.job_id)}</a>
        ${з.marked ? '<span class="chip ok" title="показывать новичкам">эталон</span>' : ''}</td>
      <td class="small faint nowrap">${fmtTime(з.created_at)}</td>
      <td class="num mono">${з.agent_score === null || з.agent_score === undefined ? '—' : num(з.agent_score, 0)}</td>
      <td class="num">${toneChip(з.sentiment)}</td>
      <td class="nowrap">${з.marked
        ? `<button class="ghost sm" data-mark="reference" data-status="" data-job="${esc(з.job_id)}">Снять</button>`
        : `<button class="ghost sm" data-mark="reference" data-status="yes" data-job="${esc(з.job_id)}"
             title="Отметить эталоном — показывать новичкам">Эталон</button>`}</td></tr>`).join('')}
    </tbody></table></div>`;
};

/** Кнопки отметок и ссылки на записи — в любой таблице вкладки. */
RENDERERS.content.bindMarks = function (host) {
  qsa('[data-mark]', host).forEach((b) => b.addEventListener('click', async () => {
    try {
      await API.put(`/api/content/marks/${encodeURIComponent(b.dataset.job)}`,
        { kind: b.dataset.mark, status: b.dataset.status });
      toast(b.dataset.status ? 'Отметка поставлена' : 'Отметка снята', 'ok');
    } catch (err) { fail(err); return; }
    state.contentData = {};
    this.showTab();
  }));
  qsa('[data-open]', host).forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    __asrhub.openJob(a.dataset.open);
  }));
};

RENDERERS.content.drawAgentCard = async function (host) {
  const период = state.contentPeriod;
  const к = await API.latest('content-agent',
    `/api/content/agents/${encodeURIComponent(state.contentAgent)}?period=${период}&by=${state.contentAgentBy}`);
  const с = к.summary || {};
  const т = к.team || {};
  const строка = (з, доп) => `<tr>
    <td class="truncate" style="max-width:220px"><a href="#" data-open="${esc(з.job_id)}">${esc(з.filename || з.job_id)}</a></td>
    <td class="small faint nowrap">${fmtTime(з.created_at)}</td>
    <td class="num mono">${з.agent_score === null || з.agent_score === undefined ? '—' : num(з.agent_score, 0)}</td>
    <td class="num">${toneChip(з.sentiment)}</td>
    <td class="num mono">${num(з.violations)}</td></tr>`;
  const список = (items) => items.length ? `<div class="table-wrap"><table>
    <thead><tr><th>Запись</th><th>Когда</th><th class="num">Балл</th>
      <th class="num">Тон.</th><th class="num">Наруш.</th></tr></thead>
    <tbody>${items.map(строка).join('')}</tbody></table></div>`
    : '<div class="empty small">Записей нет</div>';
  host.innerHTML = `
    <div class="settings-toolbar">
      <button class="ghost sm" id="agent-back">← все операторы</button>
      <span class="small dim">${state.contentAgentBy === 'owner' ? 'владелец' : 'говорящий'}:</span>
      <b>${esc(state.contentAgent)}</b>
      <span class="spacer"></span>
      <span class="small dim">записей за период: ${num(с.records)} из ${num(т.records)} у команды</span>
    </div>
    <div class="grid cols-4" style="margin-bottom:16px">
      ${kpi('Балл оператора', с.agent_score === null || с.agent_score === undefined ? '—' : num(с.agent_score, 0),
            т.agent_score === null || т.agent_score === undefined ? 'у команды — нет' : `у команды ${num(т.agent_score, 0)}`,
            delta(с.agent_score, (к.previous || {}).agent_score, { good: 1, digits: 0 }))}
      ${kpi('Тональность', num(с.sentiment, 2), `у команды ${num(т.sentiment, 2)}`,
            delta(с.sentiment, (к.previous || {}).sentiment, { good: 1, digits: 2 }))}
      ${kpi('Индекс эмпатии', с.empathy === null || с.empathy === undefined ? '—' : num(с.empathy, 0),
            т.empathy === null || т.empathy === undefined ? '' : `у команды ${num(т.empathy, 0)}`,
            delta(с.empathy, (к.previous || {}).empathy, { good: 1, digits: 0 }))}
      ${kpi('С нарушениями', num(с.violation_records),
            с.violation_share === null || с.violation_share === undefined ? ''
              : `${num(с.violation_share, 1)}% записей, у команды ${num(т.violation_share, 1)}%`)}
    </div>
    ${card('Против команды', 'разница — оператор минус команда; зелёное — лучше, красное — хуже; прошлый период — тот же оператор',
      `<div class="table-wrap"><table>
        <thead><tr><th>Показатель</th><th class="num">Оператор</th><th class="num">Команда</th>
          <th class="num">Разница</th><th class="num">Прошлый период</th></tr></thead>
        <tbody>${(к.compare || []).map((п) => `<tr>
          <td>${esc(п.title)}</td>
          <td class="num mono">${п.agent === null || п.agent === undefined ? '—' : num(п.agent, п.digits)}</td>
          <td class="num mono">${п.team === null || п.team === undefined ? '—' : num(п.team, п.digits)}</td>
          <td class="num mono ${п.verdict === 'better' ? 'ok' : п.verdict === 'worse' ? 'err' : ''}">${
            п.delta === null || п.delta === undefined ? '—' : (п.delta > 0 ? '+' : '') + num(п.delta, п.digits)}</td>
          <td class="num mono faint">${п.previous === null || п.previous === undefined ? '—' : num(п.previous, п.digits)}</td></tr>`).join('')}
        </tbody></table></div>`)}
    <div class="grid cols-2">
      ${card('Балл по неделям', '', '<div id="agent-score-chart"></div>')}
      ${card('Тональность по неделям', '', '<div id="agent-tone-chart"></div>')}
    </div>
    ${(к.violations || []).length ? card('Нарушения по категориям', 'записей с категорией и совпадений всего',
      `<div class="chips">${(к.violations || []).map((н) =>
        `<span class="chip err">${esc(н.label)} <b>${num(н.records)}</b> <span class="faint">/ ${num(н.mentions)}</span></span>`).join('')}</div>`) : ''}
    <div class="grid cols-2">
      ${card('Лучшие записи', 'по баллу и тональности', список(к.best || []))}
      ${card('Худшие записи', 'по баллу и тональности', список(к.worst || []))}
    </div>
    ${card(`Очередь коучинга оператора (${num(к.coaching_total)})`, 'разобрать с оператором',
           this.coachingTable(к.coaching || []))}`;
  qs('#agent-back').addEventListener('click', () => {
    state.contentAgent = '';
    this.showTab();
  });
  this.bindMarks(host);
  const точки = к.timeline || [];
  const метки = точки.map((т) => new Date(т.ts * 1000).toLocaleDateString('ru-RU',
    { day: 'numeric', month: 'short' }));
  window.Charts.line(qs('#agent-score-chart'), {
    height: 200, labels: метки, yMin: 0, yMax: 100,
    series: [{ name: 'балл', values: точки.map((т) => т.agent_score) }],
    emptyText: 'нет данных за период',
  });
  window.Charts.line(qs('#agent-tone-chart'), {
    height: 200, labels: метки, yMin: -1, yMax: 1,
    series: [{ name: 'тональность', values: точки.map((т) => т.sentiment) }],
    emptyText: 'нет данных за период',
  });
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
  const [свод, перечни, перечень, драйверы] = await Promise.all([
    API.latest('content-categories', `/api/content/categories?period=${период}`),
    API.latest('content-kinds', '/api/content/kinds'),
    API.latest('content-script-jobs',
      '/api/jobs?status=completed&limit=25&light=true&order=created_at DESC'),
    API.latest('content-drivers', `/api/content/drivers?period=${период}`)
      .catch(() => ({ items: [], down: [], up: [] })),
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
        penalty: к.penalty || 0 }))));
    state.contentCategoriesDirty = false;
  }
  state.contentScriptJobs = (перечень.items || []);
  state.contentScriptJob = state.contentScriptJob ||
    (state.contentScriptJobs[0] || {}).id || '';
  this.drawCategories(host, свод, драйверы);
  if (state.contentScriptJob) this.checkCategories();
};

RENDERERS.content.drawCategories = function (host, свод, драйверы) {
  драйверы = драйверы || { items: [], down: [], up: [] };
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

    <div class="grid cols-2">
      ${card('Что тянет вниз',
             `подъём — во сколько раз категория чаще среди отрицательных разговоров, чем вообще; ` +
             `в списке — от ${num(драйверы.lift_threshold || 1.25, 2)} на ${num(драйверы.min_records || 10)} записях и больше`,
             (драйверы.down || []).length
               ? `<div class="table-wrap"><table>
                   <thead><tr><th>Категория</th><th class="num">Записей</th>
                     <th class="num">Отрицательных</th><th class="num" title="доля отрицательных среди записей категории против средней по всем">Против средней</th>
                     <th class="num" title="во сколько раз чаще среди отрицательных, чем вообще">Подъём</th></tr></thead>
                   <tbody>${(драйверы.down || []).map((д) => `<tr>
                     <td>${esc(д.label)}${д.kind !== 'topic'
                       ? ` <span class="chip ${КАТЕГОРИЯ_ЦВЕТ[д.kind] || ''}">${esc(КАТЕГОРИЯ_ВИД[д.kind] || '')}</span>` : ''}</td>
                     <td class="num mono">${num(д.records)}</td>
                     <td class="num mono">${num(д.negative)}</td>
                     <td class="num mono">${num(д.negative_share, 1)}% <span class="faint">/ ${num(драйверы.negative_share, 1)}%</span></td>
                     <td class="num mono"><b>×${num(д.lift, 2)}</b></td></tr>`).join('')}</tbody></table></div>
                  <p class="small faint" style="margin:8px 0 0">${esc(драйверы.note || '')}</p>`
               : `<div class="empty small">${драйверы.scored
                   ? 'Ни одна категория не встречается среди отрицательных разговоров заметно чаще, чем вообще'
                   : 'За период нет оценённых записей'}</div>`)}
      ${card('Что держит наверху',
             'категории, которые среди отрицательных разговоров встречаются заметно реже',
             (драйверы.up || []).length
               ? `<div class="table-wrap"><table>
                   <thead><tr><th>Категория</th><th class="num">Записей</th>
                     <th class="num">Отрицательных</th><th class="num">Подъём</th></tr></thead>
                   <tbody>${(драйверы.up || []).map((д) => `<tr>
                     <td>${esc(д.label)}</td>
                     <td class="num mono">${num(д.records)}</td>
                     <td class="num mono">${num(д.negative_share, 1)}%</td>
                     <td class="num mono">×${num(д.lift, 2)}</td></tr>`).join('')}</tbody></table></div>`
               : '<div class="empty small">Таких категорий за период нет</div>')}
    </div>

    ${(свод.trackers || []).length ? card('Трекеры за период',
      'категории с флагом «сообщать»: срабатывание — событие в журнале и, если задан tracker_url, вызов наружу',
      `<div class="chips">${(свод.trackers || []).map((т) =>
        `<span class="chip warn" title="записей: ${num(т.records)}">${esc(т.label)} <b>${num(т.hits)}</b></span>`).join('')}</div>`) : ''}

    ${card('Набор категорий',
           'правило: слова и фразы с И, ИЛИ, НЕ, РЯДОМ(N) и скобками; без кавычек — по основам ' +
           '(«уточнить» найдёт «уточню»), в кавычках — точно. Операторы — заглавными. ' +
           'Флаг «сообщать» делает категорию трекером',
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
          <input type="number" class="cat-penalty" min="0" step="5" style="width:86px"
                 value="${к.penalty ? esc(String(к.penalty)) : ''}"
                 placeholder="штраф"
                 title="Штраф к баллу оператора в баллах из ста — за категорию, а не за каждое совпадение. Пусто — без штрафа">
          <label class="small nowrap" title="Трекер: срабатывание — событие в журнале и вызов на tracker_url, сразу после распознавания">
            <input type="checkbox" class="cat-notify" ${к.notify ? 'checked' : ''}> сообщать</label>
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
        набор[i].notify = qs('.cat-notify', узел).checked;
        const штраф = Number(qs('.cat-penalty', узел).value);
        набор[i].penalty = штраф > 0 ? штраф : 0;
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
      <button class="ghost sm" id="script-add-name"
        title="Пункт проверяется не по словам, а по факту: назвал ли оператор клиента по имени (словарь имён с формами склонения)">Пункт «Обратился по имени»</button>
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
          <input type="number" class="script-weight" min="0.1" step="0.5" style="width:78px"
                 value="${п.weight !== undefined && п.weight !== null && п.weight !== 1 ? esc(String(п.weight)) : ''}"
                 placeholder="вес 1"
                 title="Вес пункта в балле оператора: балл = сумма весов выполненных ÷ сумма всех × 100 минус штрафы. Пусто — единица">
          <button class="ghost icon script-del" title="Убрать пункт">✕</button>
        </div>
        ${п.check ? `<div class="small dim" style="margin-top:6px">Проверяется по факту:
            <span class="chip info">${esc(п.check === 'customer_name' ? 'обратился к клиенту по имени' : п.check)}</span>
            — по словарю имён с формами склонения; собственное имя оператора после «меня зовут» не считается</div>`
          : `<input type="text" class="script-any" style="margin-top:6px"
               value="${esc((п.any || []).join(', '))}"
               placeholder="слова-приметы через запятую: здравствуйте, добрый день">`}
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
        const вес = Number(qs('.script-weight', узел).value);
        if (вес > 0 && вес !== 1) пункты[i].weight = вес;
        else delete пункты[i].weight;
        const приметы = qs('.script-any', узел);
        if (приметы) {
          пункты[i].any = приметы.value.split(',').map((w) => w.trim()).filter(Boolean);
        }
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
  // Новый пункт дорисовывается на месте, а не через перерисовку вкладки:
  // вкладка заново читает скрипт из настроек и черновик с новым пунктом
  // терялся — «Добавить пункт» не добавлял ничего, и снимок экрана это
  // показал только на третьем заходе.
  const добавить = (пункт) => {
    пункты.push(пункт);
    qs('#script-save').disabled = false;
    рисовать();
    const последний = qs('.script-item:last-child .script-title', список);
    if (последний) последний.focus();
    RENDERERS.content.checkScript();
  };
  qs('#script-add').addEventListener('click', () => добавить({
    id: `п${Date.now().toString(36)}`, label: '', any: [], where: 'any' }));
  qs('#script-add-name').addEventListener('click', () => {
    if (пункты.some((п) => п.check === 'customer_name')) {
      toast('Такой пункт уже есть', 'warn');
      return;
    }
    добавить({ id: 'customer_name', label: 'Обратился по имени',
               check: 'customer_name', where: 'any' });
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
  const плохие = пункты.filter((п) => !п.label || (!п.check && !п.rule && !(п.any || []).length));
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

/* Установка языковой модели одной кнопкой.
 *
 * Раздел настроек «Языковая модель» — шестнадцать параметров о модели,
 * которой на свежем сервере нет. Поставить её значит зайти по ssh,
 * скачать Ollama, поднять службу, выбрать модель под видеокарту, дождаться
 * двадцати гигабайт и вписать три настройки обратно. Здесь то же самое
 * делается отсюда, и главное в этой врезке — не кнопка, а таблица: видно,
 * что поместится в память этой карты, что не поместится и почему.
 *
 * Установка идёт минутами, поэтому её ход спрашивается у сервера, а не
 * держится в странице: обновили вкладку — ход установки на месте.
 */
let llmSetupTimer = null;

async function drawLlmSetup(host) {
  if (!host) return;
  clearTimeout(llmSetupTimer);
  let д;
  try {
    д = await API.get('/api/llm/models');
  } catch (err) {
    // Каталог моделей — за правами администратора: обычному ключу вместо
    // ошибки честнее показать, почему врезки нет.
    host.innerHTML = `<div class="card-head"><h3>Модель на сервере</h3></div>
      <div class="empty small">${esc((err && err.message) || 'нет доступа')}</div>`;
    return;
  }
  if (!host.isConnected) return;
  state.llmModels = д;

  const выбраны = new Set(state.llmChoice || []);
  if (!state.llmChoice) {
    // По умолчанию отмечена рекомендованная — самая крупная из тех, что
    // помещаются. Человек, который просто нажмёт кнопку, получит лучшее
    // из возможного на его железе.
    if (д.recommended) выбраны.add(д.recommended);
    state.llmChoice = [...выбраны];
  }

  const рисовать = () => {
    if (!host.isConnected) return;
    const у = д.setup || {};
    const служба = д.service || {};
    const метка = (м) => (
      м.state === 'да' ? '<span class="chip ok">поместится</span>'
        : м.state === 'впритык' ? '<span class="chip warn">впритык</span>'
        : м.state === 'после освобождения' ? '<span class="chip warn">нужна свободная память</span>'
        : '<span class="chip err">не поместится</span>');
    host.innerHTML = `
      <div class="card-head"><h3>Модель на сервере</h3>
        <span class="hint">каталог, подбор под это оборудование и установка</span>
        <span class="spacer"></span>
        ${служба.running ? `<span class="chip ok" title="${esc(служба.url || '')}">Ollama ${esc(служба.version || '')}</span>`
          : '<span class="chip warn">служба модели не запущена</span>'}
        ${д.active ? `<span class="chip accent">включена: ${esc(д.active)}</span>`
          : '<span class="chip">модель не выбрана</span>'}</div>
      <div class="small dim" style="margin-bottom:8px">${esc((д.hardware || {}).note || '')}
        Свободно на диске ${num(д.disk_free_gb || 0)} ГБ.</div>
      ${у.running || у.error || у.finished_at ? `<div id="llm-setup-run" style="margin-bottom:10px"></div>` : ''}
      <!-- Каталог не растёт со временем: двенадцать строк, и прятать
           половину за внутренней прокруткой значило бы спрятать ровно то,
           ради чего в таблице есть столбец «помещается». -->
      <div class="table-wrap full"><table><thead><tr>
        <th style="width:34px"></th><th>Модель</th><th class="num">Скачать</th>
        <th class="num">Видеопамять</th><th class="num">Контекст</th>
        <th>Помещается</th><th>Зачем она</th><th></th></tr></thead><tbody>
        ${(д.models || []).map((м) => `<tr class="${м.state === 'нет' ? 'faint' : ''}">
          <td><input type="checkbox" data-model="${esc(м.name)}" style="width:auto"
            ${выбраны.has(м.name) ? 'checked' : ''} ${у.running ? 'disabled' : ''}></td>
          <td><b>${esc(м.title)}</b><div class="small faint mono">${esc(м.name)}</div>
            ${м.recommended ? '<span class="chip ok">рекомендуется</span>' : ''}
            ${м.fast_pick ? '<span class="chip info" title="меньше и быстрее рекомендованной">быстрая</span>' : ''}
            ${м.installed ? '<span class="chip">скачана</span>' : ''}</td>
          <td class="num mono">${num(м.size_gb, 1)} ГБ</td>
          <td class="num mono">${num(м.vram_gb, 1)} ГБ</td>
          <td class="num mono">${num(Math.round(м.context / 1000))}K</td>
          <td>${метка(м)}${м.note ? `<div class="small faint">${esc(м.note)}</div>` : ''}</td>
          <td class="small">${esc(м.why)}<div class="small faint">${esc(м.license)}</div></td>
          <td>${м.installed && м.name !== д.active
            ? `<button class="ghost sm" data-drop="${esc(м.name)}" ${у.running ? 'disabled' : ''}>Удалить</button>` : ''}</td>
        </tr>`).join('')}
      </tbody></table></div>
      <div class="row wrap" style="gap:8px;margin-top:10px">
        <button id="llm-go" class="primary" ${у.running ? 'disabled' : ''}>Установить и настроить</button>
        <label class="row small" style="gap:6px;cursor:pointer"><input type="checkbox" id="llm-install-server"
          checked style="width:auto">поставить Ollama, если её нет</label>
        <span class="spacer"></span>
        <button id="llm-sizes" class="ghost sm" ${у.running ? 'disabled' : ''}>Уточнить размеры по реестру</button>
        <button id="llm-check" class="ghost sm">Проверить ответ модели</button>
      </div>
      <div class="small faint" style="margin-top:8px">Модель скачивается с ollama.com на этот сервер и
        работает на нём же: расшифровки никуда не уходят. Выбранная модель делит видеопамять с
        распознаванием — поэтому в таблице считается свободная память, а не общая.</div>`;

    qsa('input[data-model]', host).forEach((кн) => привязать_флажок(кн));
    qsa('button[data-drop]', host).forEach((кн) => {
      кн.onclick = async () => {
        if (!confirm(`Удалить веса модели ${кн.dataset.drop} с диска сервера?`)) return;
        кн.disabled = true;
        try {
          await API.post('/api/llm/models/delete', { model: кн.dataset.drop });
          toast('Модель удалена', 'ok');
          drawLlmSetup(host);
        } catch (err) { fail(err); кн.disabled = false; }
      };
    });
    qs('#llm-go', host).onclick = () => запустить();
    qs('#llm-sizes', host).onclick = async () => {
      toast('Спрашиваем размеры у реестра…');
      try {
        д = await API.get('/api/llm/models?refresh=true');
        рисовать();
      } catch (err) { fail(err); }
    };
    qs('#llm-check', host).onclick = async (e) => {
      e.target.disabled = true;
      try {
        const о = await API.post('/api/llm/test');
        toast(о.ok ? `Модель ответила за ${num(о.ms / 1000, 1)} с` : `Модель не ответила: ${о.error}`,
              о.ok ? 'ok' : 'err');
      } catch (err) { fail(err); } finally { e.target.disabled = false; }
    };
    if (у.running || у.error || у.finished_at) рисоватьХод(qs('#llm-setup-run', host), у);
  };

  const привязать_флажок = (кн) => {
    кн.onchange = () => {
      if (кн.checked) выбраны.add(кн.dataset.model); else выбраны.delete(кн.dataset.model);
      state.llmChoice = [...выбраны];
    };
  };

  const рисоватьХод = (место, у) => {
    if (!место) return;
    const шаги = у.steps || [];
    const значок = { 'готово': '✓', 'идёт': '…', 'сбой': '✕', 'пропущен': '·', 'ждёт': '·' };
    const цвет = { 'готово': 'ok', 'идёт': 'info', 'сбой': 'err', 'пропущен': '', 'ждёт': '' };
    место.innerHTML = `<div class="card tight">
      <div class="row wrap" style="gap:8px;margin-bottom:6px">
        <b class="small">${у.running ? 'Идёт установка' : у.error ? 'Установка не удалась'
          : у.cancelled ? 'Установка отменена' : 'Установка завершена'}</b>
        ${(у.models || []).map((м) => `<span class="chip mono">${esc(м)}</span>`).join('')}
        <span class="spacer"></span>
        ${у.running ? '<button class="ghost sm" id="llm-cancel">Отменить</button>' : ''}</div>
      <div class="progress ${у.error ? 'warn' : 'ok'}"><span style="width:${((у.progress || 0) * 100).toFixed(0)}%"></span></div>
      <div class="small" style="margin-top:6px">${шаги.map((ш) => `<span class="chip ${цвет[ш.state] || ''}"
        title="${esc(ш.note || '')}">${значок[ш.state] || '·'} ${esc(ш.title)}</span>`).join(' ')}</div>
      ${Object.entries(у.model_progress || {}).filter(([, п]) => п.status !== 'ждёт').map(([имя, п]) => `
        <div class="small dim" style="margin-top:6px">${esc(имя)} — ${esc(п.status)} ${num((п.share || 0) * 100, 0)}%
          <div class="progress"><span style="width:${((п.share || 0) * 100).toFixed(0)}%"></span></div></div>`).join('')}
      ${у.error ? `<div class="small" style="color:var(--err);margin-top:6px">${esc(у.error)}</div>` : ''}
      <details style="margin-top:6px"><summary class="small faint">Журнал установки</summary>
        <pre class="small mono" style="white-space:pre-wrap;margin:6px 0 0">${esc((у.log || []).slice(-14).join('\n'))}</pre></details>
    </div>`;
    const отмена = qs('#llm-cancel', место);
    if (отмена) отмена.onclick = async () => {
      отмена.disabled = true;
      try { await API.post('/api/llm/setup/cancel'); } catch (err) { fail(err); }
    };
  };

  const следить = () => {
    clearTimeout(llmSetupTimer);
    llmSetupTimer = setTimeout(async () => {
      if (!host.isConnected) return;
      let у;
      try { у = await API.get('/api/llm/setup/status'); } catch (err) { return; }
      д.setup = у;
      рисоватьХод(qs('#llm-setup-run', host), у);
      if (у.running) { следить(); return; }
      // Установка кончилась: настройки на сервере изменились, и карточки
      // параметров ниже показывают старые значения.
      try {
        const свежие = await API.get('/api/settings');
        state.settings = свежие.values;
      } catch (err) { /* не беда: значения обновятся при следующем открытии */ }
      toast(у.error ? 'Установка не удалась' : у.cancelled ? 'Установка отменена'
        : 'Модель установлена и включена', у.error ? 'err' : 'ok');
      renderView(true);
    }, 2000);
  };

  const запустить = async () => {
    if (!выбраны.size) { toast('Отметьте хотя бы одну модель', 'warn'); return; }
    const тяжёлые = (д.models || []).filter((м) => выбраны.has(м.name) && м.state === 'нет');
    if (тяжёлые.length && !confirm(
      `${тяжёлые.map((м) => м.title).join(', ')} не помещается в память этого сервера — ` +
      'модель пойдёт частично на процессоре и будет отвечать минутами. Всё равно ставить?')) return;
    try {
      д.setup = await API.post('/api/llm/setup', {
        models: [...выбраны], activate: [...выбраны][0],
        install_server: qs('#llm-install-server', host).checked });
      рисовать();
      следить();
    } catch (err) { fail(err); }
  };

  рисовать();
  if ((д.setup || {}).running) следить();
}

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
    if (!host) return;            // раздел успели сменить, пока шёл таймер
    const search = (state.paramSearch || '').toLowerCase();
    const groups = state.catalog.groups;

    // Поиск идёт по параметрам каталога, поэтому раздел «Доступ» показываем
    // только когда он выбран явно и в поиске пусто.
    if (state.paramGroup === ACCESS_GROUP && !search) { renderAccessSection(host); return; }
    // «Языковая модель» — единственная группа, где перед параметрами нужен
    // не параметр, а действие: без установленной модели все шестнадцать
    // настроек ниже описывают то, чего на сервере нет.
    const врезка = (state.paramGroup === 'llm' && !search)
      ? h('<section class="card" id="llm-setup"><div class="empty">Смотрим, что стоит на сервере…</div></section>')
      : null;

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
      if (врезка) { host.prepend(врезка); drawLlmSetup(врезка); }
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
    if (врезка) { host.prepend(врезка); drawLlmSetup(врезка); }
  },
};


/* ========================================================================
 * Раздел «Резервные копии»: снять, вернуть, настроить расписание.
 *
 * Копия — не одна кнопка, а два разных ответа на два разных вопроса.
 * «Только настройки» — килобайты, в которых вся работа по подбору моделей,
 * порогов, словарей и станций АТС: то, что невозможно восстановить по
 * памяти, и что теряется при переустановке первым. «Настройки и данные» —
 * ещё и база: задания, результаты, звонки, показатели.
 *
 * Поэтому и в разделе два действия, а не одно с переключателем: выбор
 * делается до нажатия, а не в диалоге после.
 * ===================================================================== */

const РЕЗЕРВ_ПАРАМЕТРЫ = [
  'backup_enabled', 'backup_time', 'backup_interval_hours', 'backup_kind',
  'backup_keep_days', 'backup_keep', 'backup_include_results', 'backup_dir',
];

RENDERERS.backup = {
  async render(root) {
    root.innerHTML = `
      <div class="settings-toolbar">
        <button class="primary sm" id="bk-settings"
          title="Снять копию одних настроек — несколько килобайт, снимается мгновенно">Копия настроек</button>
        <button class="btn sm" id="bk-full"
          title="Снять копию настроек вместе с базой: задания, результаты, звонки, показатели">Полная копия</button>
        <span class="spacer"></span>
        <label class="ghost sm" style="cursor:pointer;display:inline-flex;align-items:center;gap:6px"
          title="Положить в каталог копию, снятую на другом сервере">
          Загрузить копию<input type="file" id="bk-upload" accept=".gz,.db" hidden></label>
        <button class="ghost sm" id="bk-cleanup"
          title="Убрать копии старше срока хранения прямо сейчас">Подчистить</button>
        <button class="ghost sm" id="bk-refresh" title="Обновить данные раздела">Обновить</button>
      </div>
      <div id="bk-top"></div>
      <div id="bk-list"><div class="empty">Загрузка…</div></div>
      <section class="card" style="margin-top:14px">
        <div class="card-head"><h3>Расписание и хранение</h3>
          <span class="hint">те же параметры, что и в разделе «Настройки» — здесь они под рукой</span></div>
        <div class="params" id="bk-params" style="padding:14px 16px"></div>
      </section>`;

    qs('#bk-settings').addEventListener('click', (е) => this.create('settings', е.currentTarget));
    qs('#bk-full').addEventListener('click', (е) => this.create('full', е.currentTarget));
    qs('#bk-refresh').addEventListener('click', () => this.load());
    qs('#bk-cleanup').addEventListener('click', () => this.cleanup());
    qs('#bk-upload').addEventListener('change', (е) => this.upload(е.currentTarget));

    this.drawParams();
    await this.load();
  },

  async load() {
    try {
      state.backupData = await API.latest('backup-list', '/api/backup');
    } catch (err) {
      if (err.code === 'aborted') return;
      const место = qs('#bk-list');
      if (место) место.innerHTML = `<div class="empty">Раздел недоступен: ${
        esc(err.message || '')}</div>`;
      return;
    }
    this.drawTop();
    this.drawList();
  },

  drawTop() {
    const место = qs('#bk-top');
    const д = state.backupData || {};
    if (!место) return;
    const последняя = д.last;
    const расписание = д.interval_hours > 0
      ? `каждые ${д.interval_hours} ${plural(д.interval_hours, 'час', 'часа', 'часов')}`
      : `ежедневно в ${esc(д.time || '00:01')}`;
    const свежесть = последняя
      ? (Date.now() / 1000 - последняя.created_at) / 3600 : Infinity;
    // Отдельно про давность: «копия есть» и «копия свежая» — разные вещи,
    // и заметить разницу человек должен здесь, а не при восстановлении.
    const тревога = !д.enabled ? 'выключено'
      : свежесть > (д.interval_hours > 0 ? д.interval_hours * 2 : 50) ? 'копия устарела' : '';
    место.innerHTML = `
      <section class="card" style="margin-bottom:14px">
        <div class="card-head">
          <h3>Резервное копирование</h3>
          <span class="chip ${д.enabled ? (тревога ? 'warn' : 'ok') : ''}">${
            д.enabled ? esc(расписание) : 'по расписанию не делается'}</span>
          ${тревога && д.enabled ? `<span class="chip warn">${esc(тревога)}</span>` : ''}
          <span class="spacer"></span>
          <span class="small dim mono" title="каталог, в котором лежат копии">${esc(д.dir || '')}</span>
        </div>
        <div class="grid cols-4" style="padding:14px 16px">
          ${kpi('Копий', num(д.total || 0, 0),
                `занимают ${fmtBytes(д.bytes || 0)}`)}
          ${kpi('Последняя копия', последняя ? esc(fmtAgo(последняя.created_at)) : '—',
                последняя ? `${esc(последняя.kind_title)} · ${fmtBytes(последняя.size)}`
                          : 'копий ещё нет')}
          ${kpi('Хранить', д.keep_days > 0
                  ? `${д.keep_days} ${plural(д.keep_days, 'день', 'дня', 'дней')}`
                  : 'бессрочно',
                д.keep_count > 0 ? `и не больше ${д.keep_count} штук` : 'по числу — без предела')}
          ${kpi('Свободно на диске', fmtBytes(д.free_bytes || 0),
                'в каталоге копий')}
        </div>
        ${!д.enabled ? `<div class="banner warn" style="margin:0 16px 14px">
          <b>Копии по расписанию выключены.</b> Пока это так, единственные копии —
          те, что сняты вручную. Включить можно ниже, в «Расписании и хранении».</div>` : ''}
        ${!д.total ? `<div class="banner" style="margin:0 16px 14px">
          Копий пока нет. Снимите копию настроек прямо сейчас — она весит килобайты,
          а хранит всю работу по подбору параметров.</div>` : ''}
      </section>`;
  },

  drawList() {
    const место = qs('#bk-list');
    const д = state.backupData || {};
    if (!место) return;
    const копии = д.items || [];
    if (!копии.length) {
      место.innerHTML = '<div class="empty">Копий нет</div>';
      return;
    }
    место.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Копии</h3>
          <span class="chip">${num(копии.length, 0)} ${
            plural(копии.length, 'копия', 'копии', 'копий')}</span>
          <span class="hint">свежие сверху</span></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Когда</th><th>Что внутри</th><th class="num">Размер</th>
            <th>Версия</th><th>Примечание</th><th>Действия</th></tr></thead>
          <tbody>${копии.map((к) => this.row(к)).join('')}</tbody>
        </table></div>
      </section>`;
    qsa('#bk-list button[data-act]').forEach((кнопка) => кнопка.addEventListener('click',
      () => this.act(кнопка.dataset.act, кнопка.dataset.name, кнопка)));
  },

  row(к) {
    const внутри = к.kind === 'settings' ? '<span class="chip">только настройки</span>'
      : к.kind === 'database' ? '<span class="chip">база прежнего образца</span>'
      : '<span class="chip ok">настройки и данные</span>';
    const состав = (к.contents || []).includes('results/')
      ? ' <span class="chip">+ файлы результатов</span>' : '';
    return `<tr${к.error ? ' class="row-err"' : ''}>
      <td class="small">${esc(fmtTime(к.created_at))}
        <div class="small dim">${esc(fmtAgo(к.created_at))}</div></td>
      <td>${внутри}${состав}
        ${к.kind === 'full' && (к.calls || к.jobs) ? `<div class="small dim">
          заданий ${num(к.jobs, 0)} · звонков ${num(к.calls, 0)}</div>` : ''}
        ${к.error ? `<div class="small" style="color:var(--err)">${esc(к.error)}</div>` : ''}</td>
      <td class="num">${fmtBytes(к.size || 0)}</td>
      <td class="small dim">${esc(к.version || '—')}${
        к.schema_version ? `<div class="small dim">схема ${к.schema_version}</div>` : ''}</td>
      <td class="small dim">${esc(к.comment || '')}</td>
      <td class="row wrap" style="gap:6px">
        ${к.kind !== 'database' ? `<button class="ghost sm" data-act="settings"
          data-name="${esc(к.name)}"
          title="Применить параметры из копии прямо сейчас, не трогая данные">Вернуть настройки</button>` : ''}
        ${к.kind !== 'settings' ? `<button class="ghost sm danger" data-act="full"
          data-name="${esc(к.name)}"
          title="Подменить базу данными из копии; потребуется перезапуск сервера">Вернуть всё</button>` : ''}
        <button class="ghost sm" data-act="download" data-name="${esc(к.name)}"
          title="Скачать файл копии. Внутри пароли и ключи — храните как пароль">Скачать</button>
        <button class="ghost sm" data-act="delete" data-name="${esc(к.name)}"
          title="Удалить эту копию">Убрать</button>
      </td></tr>`;
  },

  async create(вид, кнопка) {
    const прежний = кнопка.textContent;
    кнопка.disabled = true;
    кнопка.textContent = 'Снимаю…';
    try {
      const итог = await API.post(`/api/backup?kind=${вид}`, { comment: 'вручную' });
      toast(`Копия снята: ${fmtBytes(итог.size || 0)}`, 'ok',
            вид === 'settings' ? 'В ней параметры, словари, скрипт и станции АТС'
                               : 'В ней настройки и база целиком');
      await this.load();
    } catch (err) {
      toast(err.message || 'Копию снять не удалось', 'err', err.hint || '');
    } finally {
      кнопка.disabled = false;
      кнопка.textContent = прежний;
    }
  },

  act(действие, имя, кнопка) {
    if (действие === 'download') {
      // Скачивание идёт обычной ссылкой: файл бывает в гигабайты, и тянуть
      // его в память вкладки ради «сохранить как» незачем.
      const ссылка = document.createElement('a');
      ссылка.href = `/api/backup/${encodeURIComponent(имя)}/file`;
      ссылка.download = имя;
      document.body.appendChild(ссылка);
      ссылка.click();
      ссылка.remove();
      return null;
    }
    if (действие === 'delete') return this.remove(имя);
    return this.restore(имя, действие, кнопка);
  },

  async restore(имя, что, кнопка) {
    const копия = (state.backupData.items || []).find((к) => к.name === имя) || {};
    const когда = fmtTime(копия.created_at);
    const вопрос = что === 'settings'
      ? `Применить настройки из копии от ${когда}?\n\n`
        + 'Текущие значения всех параметров будут заменены на те, что в копии. '
        + 'Данные — задания, результаты, звонки — не изменятся.'
      : `Вернуть данные из копии от ${когда}?\n\n`
        + 'База будет заменена целиком: всё, что появилось после этой копии, '
        + 'из рабочей базы исчезнет. Прежняя база останется рядом под именем '
        + '«asrhub.db.before-restore-…», и вернуть её можно.\n\n'
        + 'После восстановления сервер нужно перезапустить.';
    if (!confirm(вопрос)) return;
    const прежний = кнопка.textContent;
    кнопка.disabled = true;
    кнопка.textContent = 'Восстанавливаю…';
    try {
      const итог = await API.post('/api/backup/restore', { name: имя, what: что });
      if (итог.restart_required) {
        toast('Данные восстановлены — перезапустите сервер', 'warn',
              `Прежняя база сохранена: ${итог.previous || 'рядом с рабочей'}`);
      } else {
        toast(`Настройки восстановлены: параметров ${num(итог.applied || 0, 0)}`, 'ok',
              'Значения применены на ходу, перезапуск не нужен');
      }
      // Настройки применены на сервере — вкладка обязана перечитать их,
      // иначе следующее изменение любого параметра отправит на сервер то,
      // что лежало в памяти вкладки до восстановления, и молча отменит его.
      try {
        const свежие = await API.get('/api/settings');
        state.settings = свежие.values || state.settings;
        state.jobSettings = Object.assign({}, state.settings);
      } catch (e) { /* перечитаем при следующем открытии раздела */ }
      await this.load();
      this.drawParams();
    } catch (err) {
      toast(err.message || 'Восстановить не удалось', 'err', err.hint || '');
    } finally {
      кнопка.disabled = false;
      кнопка.textContent = прежний;
    }
  },

  async remove(имя) {
    if (!confirm(`Убрать копию «${имя}»?\n\nВосстановить её после удаления будет неоткуда.`)) return;
    try {
      const итог = await API.del(`/api/backup/${encodeURIComponent(имя)}`);
      toast('Копия убрана', 'warn', `Освободилось ${fmtBytes(итог.freed || 0)}`);
      await this.load();
    } catch (err) { fail(err); }
  },

  async cleanup() {
    try {
      const итог = await API.post('/api/backup/cleanup');
      toast(итог.count ? `Убрано копий: ${итог.count}` : 'Убирать нечего',
            итог.count ? 'warn' : '', (итог.removed || []).join(', '));
      await this.load();
    } catch (err) { fail(err); }
  },

  async upload(поле) {
    const файл = (поле.files || [])[0];
    if (!файл) return;
    поле.value = '';
    const форма = new FormData();
    форма.append('file', файл);
    try {
      const итог = await API.call('/api/backup/upload', { method: 'POST', body: форма });
      toast(`Копия принята: ${итог.name}`, 'ok',
            'Теперь её можно выбрать для восстановления');
      await this.load();
    } catch (err) {
      toast(err.message || 'Файл не принят', 'err', err.hint || '');
    }
  },

  /* Настройки раздела — теми же карточками, что и в «Настройках»: с
   * описанием, рекомендацией и примерами. Копировать их сюда в сокращённом
   * виде значило бы держать два описания одного параметра, которые рано
   * или поздно разойдутся. */
  drawParams() {
    const место = qs('#bk-params');
    if (!место) return;
    const все = state.params || [];
    место.innerHTML = '';
    РЕЗЕРВ_ПАРАМЕТРЫ.forEach((ключ) => {
      const spec = все.find((п) => п.key === ключ);
      if (!spec) return;
      место.appendChild(paramCard(spec, state.settings[ключ], async (значение) => {
        try {
          await API.put('/api/settings', { [ключ]: значение });
          state.settings[ключ] = значение;
          toast('Настройка применена', 'ok');
          await this.load();
        } catch (err) { fail(err); }
      }));
    });
    if (!место.children.length) {
      место.innerHTML = '<div class="empty small">Каталог параметров ещё не загружен</div>';
    }
  },
};


/* ========================================================================
 * Раздел «Аналитика по сотрудникам».
 *
 * Всё, что сервер знает о человеке, на одном экране: сколько разговоров и
 * сколько наговорено, как звучит его речь, как себя чувствуют его клиенты,
 * что он делает лучше и хуже команды, что стоит разобрать. Разрозненные по
 * разделам те же числа отвечают на вопрос «как дела у отдела»; вопрос «как
 * дела у Петровой» требует, чтобы они лежали рядом.
 *
 * Кто такой сотрудник — выбирается: имя из журнала АТС (самое точное),
 * метка говорящего в записи, ключ доступа, очередь или станция.
 * ===================================================================== */

//: Колонки таблицы сотрудников: ключ, подпись, знаков, куда лучше, подсказка.
const СОТРУДНИК_КОЛОНКИ = [
  ['records', 'Записей', 0, 0, 'разобранных разговоров за период'],
  ['calls', 'Звонков', 0, 0, 'по журналу АТС, включая нераспознанные'],
  ['talk_s', 'Наговорено', 0, 0, 'суммарное время разговоров'],
  ['agent_score', 'Балл', 0, 1, 'скрипт с весами минус штрафы'],
  ['sentiment', 'Тональность', 2, 1, 'средняя окраска разговора'],
  ['mood', 'Настроение клиента', 2, 1, 'по репликам клиента, от −1 до +1'],
  ['stress', 'Напряжение', 0, -1, 'резкие реплики, перебивания, раздражение'],
  ['effort', 'Усилие клиента', 1, 1, 'минус — клиенту пришлось пробиваться'],
  ['fatigue', 'Усталость', 0, -1, 'падение темпа и рост пауз к концу разговора'],
  ['clarity', 'Понятность', 0, 1, 'длина фраз, канцелярит, темп, паразиты'],
  ['accuracy', 'Точность', 0, 1, 'конкретика против «наверное» и «где-то так»'],
  ['politeness', 'Вежливость', 0, 1, 'формулы вежливости против обрывающих оборотов'],
  ['personalization', 'Персонализация', 0, 1, 'имя клиента и отсылки к сказанному'],
  ['rhythm', 'Ритмичность', 0, 1, 'ровность темпа и пауз'],
  ['filler_rate', 'Паразиты', 4, -1, 'доля слов-паразитов в речи'],
  ['diminutive_rate', 'Уменьшительные', 4, -1, '«секундочку», «договорчик»'],
  ['empathy', 'Эмпатия', 0, 1, '(вежливых − невежливых) ÷ сумму'],
  ['compliance', 'Скрипт', 2, 1, 'доля выполненных пунктов'],
  ['nps_index', 'Индекс NPS', 0, 1, 'промоутеры минус критики'],
  ['violation_share', 'Нарушений, %', 1, -1, 'доля записей со стоп-словами'],
];

//: Наборы колонок. Двадцать показателей в одной таблице не читаются: на
//: экране помещается половина, и человек листает вбок вместо того, чтобы
//: сравнивать. Набор выбирается под вопрос, с которым пришли.
const СОТРУДНИК_НАБОРЫ = [
  { key: 'main', title: 'Главное',
    columns: ['records', 'calls', 'talk_s', 'agent_score', 'mood', 'stress',
              'clarity', 'nps_index'] },
  { key: 'speech', title: 'Речь',
    columns: ['records', 'clarity', 'accuracy', 'rhythm', 'filler_rate',
              'diminutive_rate', 'fatigue'] },
  { key: 'clients', title: 'Клиенты',
    columns: ['records', 'sentiment', 'mood', 'stress', 'effort', 'nps_index'] },
  { key: 'script', title: 'Скрипт и вежливость',
    columns: ['records', 'agent_score', 'compliance', 'politeness',
              'personalization', 'empathy', 'violation_share'] },
  { key: 'calls', title: 'Звонки',
    columns: ['records', 'calls', 'talk_s'] },
  { key: 'all', title: 'Все показатели', columns: null },
];

//: Что рисовать на радаре сравнения с командой. Шкала у всех 0–100 и
//: «больше — лучше»: складывать на одну картинку показатели с разными
//: направлениями — способ получить красивую фигуру без смысла.
const СОТРУДНИК_РАДАР = [
  ['agent_score', 'Балл'], ['clarity', 'Понятность'], ['accuracy', 'Точность'],
  ['politeness', 'Вежливость'], ['personalization', 'Персонализация'],
  ['rhythm', 'Ритмичность'],
];

RENDERERS.employees = {
  async render(root) {
    if (!state.employeePeriod) state.employeePeriod = 'month';
    root.innerHTML = `
      <div class="settings-toolbar">
        <span class="small dim">Период:</span>
        <div class="group-nav" id="emp-period">
          ${Object.entries(PERIOD_LABELS).filter(([k]) => k !== 'hour').map(([k, v]) =>
            `<button data-period="${k}" class="${state.employeePeriod === k ? 'active' : ''}">${
              esc(v[0].toUpperCase() + v.slice(1))}</button>`).join('')}
        </div>
        <select id="emp-by" style="width:190px"
          title="Чем считать сотрудника: именем из журнала АТС, меткой говорящего в записи или ключом доступа">
        </select>
        <div class="group-nav" id="emp-cols">
          ${СОТРУДНИК_НАБОРЫ.map((н) => `<button data-cols="${н.key}"
            class="${(state.employeeCols || 'main') === н.key ? 'active' : ''}"
            title="Набор столбцов под вопрос, с которым пришли">${esc(н.title)}</button>`).join('')}
        </div>
        <span class="spacer"></span>
        <input type="search" id="emp-search" placeholder="сотрудник"
          value="${esc(state.employeeSearch || '')}" style="width:190px">
        <button class="ghost sm" id="emp-export"
          title="Выгрузить таблицу в CSV — для сводного отчёта">CSV</button>
        <button class="ghost sm" id="emp-refresh" title="Обновить данные раздела">Обновить</button>
      </div>
      <div id="emp-top"></div>
      <div id="emp-body"><div class="empty">Загрузка…</div></div>
      <div id="emp-card"></div>`;

    qsa('#emp-period button').forEach((b) => b.addEventListener('click', () => {
      state.employeePeriod = b.dataset.period;
      qsa('#emp-period button').forEach((x) => x.classList.toggle('active', x === b));
      this.load();
    }));
    const выбор = qs('#emp-by');
    if (выбор) выбор.addEventListener('change', () => {
      state.employeeBy = выбор.value;
      state.employeeKey = '';
      this.load();
    });
    qsa('#emp-cols button').forEach((b) => b.addEventListener('click', () => {
      state.employeeCols = b.dataset.cols;
      qsa('#emp-cols button').forEach((x) => x.classList.toggle('active', x === b));
      this.drawTable();
    }));
    qs('#emp-refresh').addEventListener('click', () => this.load());
    qs('#emp-export').addEventListener('click', () => this.exportCsv());
    const поиск = qs('#emp-search');
    let таймер = null;
    if (поиск) поиск.addEventListener('input', () => {
      clearTimeout(таймер);
      таймер = setTimeout(() => {
        state.employeeSearch = поиск.value.trim();
        this.drawTable();
      }, 250);
    });
    await this.load();
  },

  async load() {
    const тело = qs('#emp-body');
    if (тело && !state.employeeData) тело.innerHTML = '<div class="empty">Загрузка…</div>';
    try {
      state.employeeData = await API.latest('employees',
        `/api/content/employees?by=${state.employeeBy}&period=${state.employeePeriod}`);
    } catch (err) {
      if (err.code === 'aborted') return;
      if (тело) тело.innerHTML = `<div class="empty">Раздел недоступен: ${
        esc(err.message || '')}</div>`;
      return;
    }
    const выбор = qs('#emp-by');
    if (выбор) выбор.innerHTML = (state.employeeData.dimensions || []).map((р) =>
      `<option value="${esc(р.key)}"${р.key === state.employeeBy ? ' selected' : ''}>${
        esc(р.title)}</option>`).join('');
    this.drawTop();
    this.drawTable();
    if (state.employeeKey) await this.openCard(state.employeeKey);
  },

  drawTop() {
    const место = qs('#emp-top');
    const д = state.employeeData || {};
    const к = д.team || {};
    if (!место) return;
    const люди = д.items || [];
    const сравнимые = люди.filter((ч) => !ч.sparse);
    место.innerHTML = `
      <section class="card" style="margin-bottom:12px">
        <div class="card-head"><h3>Команда</h3>
          <span class="chip">${num(люди.length, 0)} ${
            plural(люди.length, 'сотрудник', 'сотрудника', 'сотрудников')}</span>
          ${люди.length - сравнимые.length ? `<span class="chip warn"
            title="меньше пяти разобранных разговоров за период — средние по ним ещё ни о чём не говорят">${
            люди.length - сравнимые.length} с малыми данными</span>` : ''}
          <span class="spacer"></span>
          <span class="small dim">средние по команде — опора для сравнения</span>
        </div>
        <div class="grid cols-6" style="padding:14px 16px">
          ${kpi('Разговоров', num(к.records || 0, 0), `${num(к.hours || 0, 1)} ч звука`)}
          ${kpi('Балл оператора', к.agent_score === null || к.agent_score === undefined
                  ? '—' : num(к.agent_score, 0), 'среднее по команде')}
          ${kpi('Настроение клиента', к.mood === null || к.mood === undefined
                  ? '—' : num(к.mood, 2), 'от −1 до +1')}
          ${kpi('Напряжение', к.stress === null || к.stress === undefined
                  ? '—' : num(к.stress, 0), 'чем меньше, тем спокойнее')}
          ${kpi('Понятность речи', к.clarity === null || к.clarity === undefined
                  ? '—' : num(к.clarity, 0), 'из 100')}
          ${kpi('Индекс NPS', к.nps_index === null || к.nps_index === undefined
                  ? '—' : num(к.nps_index, 0), 'промоутеры минус критики')}
        </div>
      </section>`;
  },

  drawTable() {
    const место = qs('#emp-body');
    const д = state.employeeData || {};
    if (!место) return;
    let люди = д.items || [];
    const искомое = (state.employeeSearch || '').toLowerCase();
    if (искомое) {
      люди = люди.filter((ч) => String(ч.label || ч.key || '').toLowerCase().includes(искомое));
    }
    if (!люди.length) {
      место.innerHTML = `<div class="empty">${искомое
        ? 'Никто не найден'
        : 'За период разобранных разговоров нет. Сотрудник берётся из журнала АТС — '
          + 'проверьте, что записи приезжают с полем оператора.'}</div>`;
      return;
    }
    const ключ = state.employeeSort || 'records';
    const направление = state.employeeSortDesc === false ? 1 : -1;
    люди = люди.slice().sort((a, b) => {
      const х = a[ключ], у = b[ключ];
      if (х === у) return 0;
      if (х === null || х === undefined) return 1;
      if (у === null || у === undefined) return -1;
      return (х > у ? 1 : -1) * направление;
    });
    const команда = д.team || {};
    const колонки = this.columns();
    место.innerHTML = `
      <section class="card">
        <div class="card-head"><h3>Сотрудники</h3>
          <span class="hint">строка ведёт в карточку · заголовок столбца сортирует</span>
          <span class="spacer"></span>
          <span class="small dim">цветом — отличие от команды</span>
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Сотрудник</th>${колонки.map(([к, имя, , , подсказка]) =>
            `<th class="num sortable" data-sort="${к}" title="${esc(подсказка)}"
               style="cursor:pointer">${esc(имя)}${ключ === к ? (направление < 0 ? ' ↓' : ' ↑') : ''}</th>`
            ).join('')}</tr></thead>
          <tbody>${люди.map((ч) => this.row(ч, команда, колонки)).join('')}</tbody>
        </table></div>
      </section>`;
    qsa('#emp-body th[data-sort]').forEach((з) => з.addEventListener('click', () => {
      if (state.employeeSort === з.dataset.sort) state.employeeSortDesc = state.employeeSortDesc === false;
      else { state.employeeSort = з.dataset.sort; state.employeeSortDesc = true; }
      this.drawTable();
    }));
    qsa('#emp-body tr[data-key]').forEach((строка) => строка.addEventListener('click',
      () => this.openCard(строка.dataset.key)));
  },

  /* Колонки выбранного набора. «Все показатели» — это весь перечень:
   * таблица уедет вбок, и это осознанный выбор человека, а не то, что мы
   * показываем по умолчанию. */
  columns() {
    const набор = СОТРУДНИК_НАБОРЫ.find((н) => н.key === (state.employeeCols || 'main'))
      || СОТРУДНИК_НАБОРЫ[0];
    if (!набор.columns) return СОТРУДНИК_КОЛОНКИ;
    return набор.columns
      .map((к) => СОТРУДНИК_КОЛОНКИ.find((с) => с[0] === к))
      .filter(Boolean);
  },

  row(ч, команда, колонки) {
    const клетка = ([к, , знаков, лучше]) => {
      const значение = ч[к];
      if (значение === null || значение === undefined) return '<td class="num dim">—</td>';
      const показать = к === 'talk_s' ? fmtDur(значение) : num(значение, знаков);
      const общее = команда[к];
      let класс = '';
      if (лучше && общее !== null && общее !== undefined && !ч.sparse) {
        const разница = (значение - общее) * лучше;
        const порог = Math.abs(общее || 1) * 0.12;
        класс = разница > порог ? 'good' : разница < -порог ? 'bad' : '';
      }
      return `<td class="num ${класс}">${показать}</td>`;
    };
    return `<tr data-key="${esc(ч.key)}" style="cursor:pointer">
      <td><b>${esc(ч.key === '—' ? 'без оператора' : (ч.label || ч.key))}</b>
        ${ч.sparse ? '<span class="chip warn" title="меньше пяти разобранных разговоров: средние по ним ещё ни о чём не говорят">мало данных</span>' : ''}
      </td>
      ${(колонки || СОТРУДНИК_КОЛОНКИ).map(клетка).join('')}
    </tr>`;
  },

  /* Карточка: тот же набор, что у вкладки «Операторы», плюс новые
   * показатели и телефония. Открывается под таблицей, а не вместо неё:
   * сравнение с соседями — половина смысла разговора о сотруднике. */
  async openCard(ключ) {
    state.employeeKey = ключ;
    const место = qs('#emp-card');
    if (!место) return;
    место.innerHTML = '<div class="empty">Загрузка карточки…</div>';
    let карточка;
    try {
      карточка = await API.latest('employee-card',
        `/api/content/agents/${encodeURIComponent(ключ)}`
        + `?by=${state.employeeBy}&period=${state.employeePeriod}`);
    } catch (err) {
      if (err.code === 'aborted') return;
      место.innerHTML = `<div class="empty">Карточка недоступна: ${esc(err.message || '')}</div>`;
      return;
    }
    const свой = карточка.summary || {};
    const строка = (state.employeeData.items || []).find((ч) => ч.key === ключ) || {};
    место.innerHTML = `
      <section class="card" style="margin-top:14px">
        <div class="card-head">
          <h3>${esc(строка.label || ключ)}</h3>
          ${строка.sparse ? '<span class="chip warn">мало данных</span>' : ''}
          <span class="chip">${num(свой.records || 0, 0)} ${
            plural(свой.records || 0, 'разговор', 'разговора', 'разговоров')}</span>
          ${строка.calls ? `<span class="chip">${num(строка.calls, 0)} звонков · ${
            fmtDur(строка.talk_s || 0)}</span>` : ''}
          <span class="spacer"></span>
          ${state.employeeBy === 'agent' ? `<button class="ghost sm" id="emp-calls"
            title="Звонки этого сотрудника в журнале">Звонки</button>` : ''}
          <button class="ghost icon" id="emp-close" aria-label="Закрыть" title="Закрыть карточку">✕</button>
        </div>
        <div class="grid cols-2" style="padding:14px 16px">
          <div><h4 style="margin:0 0 8px;font-size:13px">Против команды</h4>
            <div id="emp-radar"></div></div>
          <div><h4 style="margin:0 0 8px;font-size:13px">Ход по неделям</h4>
            <div id="emp-line"></div></div>
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Показатель</th><th class="num">Сотрудник</th>
            <th class="num">Команда</th><th class="num">Прошлый период</th>
            <th class="num">Разница</th></tr></thead>
          <tbody>${(карточка.compare || []).map((с) => `<tr>
            <td>${esc(с.title)}</td>
            <td class="num"><b>${с.agent === null || с.agent === undefined
              ? '—' : num(с.agent, с.digits)}</b></td>
            <td class="num dim">${с.team === null || с.team === undefined
              ? '—' : num(с.team, с.digits)}</td>
            <td class="num dim">${с.previous === null || с.previous === undefined
              ? '—' : num(с.previous, с.digits)}</td>
            <td class="num ${с.verdict === 'better' ? 'good' : с.verdict === 'worse' ? 'bad' : ''}">${
              с.delta === null || с.delta === undefined ? '—'
                : `${с.delta > 0 ? '+' : '−'}${num(Math.abs(с.delta), с.digits)}`}</td>
          </tr>`).join('')}</tbody>
        </table></div>
        ${(карточка.violations || []).length ? `
          <div style="padding:12px 16px"><h4 style="margin:0 0 8px;font-size:13px">Нарушения</h4>
          <div class="row wrap" style="gap:6px">${карточка.violations.map((н) =>
            `<span class="chip err">${esc(н.label || н.category)}: ${num(н.records, 0)}</span>`).join('')}</div></div>` : ''}
        <div class="grid cols-2" style="padding:12px 16px">
          <div><h4 style="margin:0 0 8px;font-size:13px">Разобрать с сотрудником</h4>
            ${(карточка.coaching || []).length ? `<div class="stack">${
              карточка.coaching.slice(0, 8).map((з) => `<div class="row small" style="gap:8px">
                <a href="#" onclick="__asrhub.openJob('${esc(з.job_id || з.id)}');return false"
                   title="${esc(з.filename || з.job_id || '')}">${
                  esc((з.filename || з.job_id || '').slice(0, 34))}</a>
                <span class="spacer"></span>
                ${(з.reasons || []).slice(0, 2).map((п) =>
                  `<span class="chip warn">${esc(п)}</span>`).join('')}
              </div>`).join('')}</div>`
              : '<div class="empty small">Поводов для разбора не нашлось</div>'}</div>
          <div><h4 style="margin:0 0 8px;font-size:13px">Лучшие разговоры</h4>
            ${(карточка.best || []).length ? `<div class="stack">${
              карточка.best.slice(0, 8).map((з) => `<div class="row small" style="gap:8px">
                <a href="#" onclick="__asrhub.openJob('${esc(з.job_id || з.id)}');return false"
                   title="${esc(з.filename || з.job_id || '')}">${
                  esc((з.filename || з.job_id || '').slice(0, 34))}</a>
                <span class="spacer"></span>
                <span class="chip ok">балл ${num(з.agent_score, 0)}</span>
              </div>`).join('')}</div>`
              : '<div class="empty small">Пока не из чего выбрать</div>'}</div>
        </div>
      </section>`;

    const команда = карточка.team || {};
    Charts.hbars(qs('#emp-radar'), {
      items: СОТРУДНИК_РАДАР.filter(([к]) => свой[к] !== null && свой[к] !== undefined)
        .map(([к, имя]) => ({
          label: имя, value: Math.round((свой[к] || 0) - (команда[к] || 0)),
          // Коротко: столбец и так показывает знак и величину, а «против»
          // словами уезжало влево и наползало на подпись строки.
          display: `${num(свой[к], 0)} / ${команда[к] === null
            || команда[к] === undefined ? '—' : num(команда[к], 0)}`,
          note: `${имя}: у сотрудника ${num(свой[к], 0)}, по команде ${
            команда[к] === null || команда[к] === undefined ? '—' : num(команда[к], 0)}`,
        })),
      labelWidth: 170, emptyText: 'Показателей для сравнения пока нет',
    });
    const ход = карточка.timeline || [];
    Charts.line(qs('#emp-line'), {
      labels: ход.map((т) => fmtBucket(т.ts, карточка.step_s || 604800)),
      series: [
        { name: 'балл', values: ход.map((т) => т.agent_score ?? null) },
        { name: 'понятность', values: ход.map((т) => т.clarity ?? null) },
        { name: 'напряжение', values: ход.map((т) => т.stress ?? null) },
      ],
      height: 220, yMin: 0, emptyText: 'Истории пока нет',
    });
    const закрыть = qs('#emp-close');
    if (закрыть) закрыть.addEventListener('click', () => {
      state.employeeKey = '';
      место.innerHTML = '';
    });
    const звонки = qs('#emp-calls');
    if (звонки) звонки.addEventListener('click', () => {
      state.telAgent = ключ;
      go('telephony');
    });
    место.scrollIntoView({ behavior: 'smooth', block: 'start' });
  },

  /* Выгрузка таблицы: то же, что на экране, и ровно теми же числами.
   * Собирается во вкладке, а не на сервере: отчёт по сотрудникам сводят
   * в таблице, и ждать ради этого ответа сервера незачем. */
  exportCsv() {
    const д = state.employeeData || {};
    const строки = [['Сотрудник', ...СОТРУДНИК_КОЛОНКИ.map(([, имя]) => имя)]];
    (д.items || []).forEach((ч) => строки.push([
      ч.label || ч.key,
      ...СОТРУДНИК_КОЛОНКИ.map(([к]) => {
        const з = ч[к];
        return з === null || з === undefined ? '' : String(з).replace('.', ',');
      }),
    ]));
    // Точка с запятой и BOM — чтобы Excel открыл файл как таблицу, а не
    // одной колонкой: с запятой и русской локалью он так и делает.
    const текст = '﻿' + строки.map((с) => с.map((з) =>
      `"${String(з).replace(/"/g, '""')}"`).join(';')).join('\r\n');
    const ссылка = document.createElement('a');
    ссылка.href = URL.createObjectURL(new Blob([текст], { type: 'text/csv;charset=utf-8' }));
    ссылка.download = `сотрудники-${state.employeePeriod}.csv`;
    document.body.appendChild(ссылка);
    ссылка.click();
    setTimeout(() => { URL.revokeObjectURL(ссылка.href); ссылка.remove(); }, 1000);
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
    // Сведения о базе и путях /api/system отдаёт только администратору.
    const база = sys.database;

    root.innerHTML = `
      <div class="grid cols-4" style="margin-bottom:16px">
        ${kpi('Ускоритель', hw.accelerator.toUpperCase(),
              gpu ? esc(gpu.name) : `${hw.cpu_cores_physical} физических ядер`)}
        ${kpi('Оперативная память', `${hw.ram_total_gb} ГБ`,
              `доступно ${hw.ram_available_gb} ГБ`)}
        ${kpi('Свободно на диске', `${hw.disk_free_gb} ГБ`,
              // База и пути приходят только администратору: /api/system прячет
              // их от остальных. Раздел читал их без проверки и падал целиком
              // с «can't access property size_mb» — то есть неадминистратор
              // видел вместо страницы пустоту, а в консоли ошибку.
              база ? `база: ${num(база.size_mb, 1)} МБ` : '')}
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
        ${card('Хранилище и пути', '', !база ? `<div class="empty small">
            Раскладка хранилища и размер базы доступны только администратору.</div>`
          : `<table>
          ${Object.entries(sys.paths || {}).map(([k, v]) =>
            `<tr><td class="dim">${esc(k)}</td><td class="mono small">${esc(v)}</td></tr>`).join('')}
          <tr><td class="dim">Конфигурация</td><td class="mono small">${
            esc(sys.config_file || 'не используется')}</td></tr>
          <tr><td class="dim">Заданий в базе</td><td class="num">${num(база.jobs)}</td></tr>
          <tr><td class="dim">Сегментов</td><td class="num">${num(база.segments)}</td></tr>
          <tr><td class="dim">Метрик</td><td class="num">${num(база.metrics)}</td></tr>
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
            <button class="primary sm" id="key-create">Создать</button></div>
          <label class="small dim" style="display:flex;gap:6px;align-items:center;margin-top:6px">
            <input type="checkbox" id="key-mask"> обезличивать ответы: телефоны, почта, карты, паспорт, СНИЛС, ИНН, даты рождения — пометками</label>`)}

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
          { name, role: qs('#key-role').value, rate_limit: 0,
            mask_pii: !!(qs('#key-mask') && qs('#key-mask').checked) });
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
          <td><span class="chip ${k.role === 'admin' ? 'accent' : ''}">${esc(k.role)}</span>${
            k.mask_pii ? ' <span class="chip" title="персональные данные в ответах заменяются пометками">обезличен</span>' : ''}</td>
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
    } catch (err) {
      // Молчаливый выход оставлял карточку на строке «Загрузка справочника…»
      // навсегда: ни всплывашки, ни следа в консоли. Соседний загрузчик
      // рядов в такой же ситуации честно пишет, что не вышло.
      const box = qs('#mon-catalog');
      if (box) {
        box.innerHTML = `<div class="empty small">Не удалось получить справочник метрик: ${
          esc((err && err.message) || 'ошибка запроса')}</div>`;
      }
      return;
    }
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
