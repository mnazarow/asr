/* ASR Hub — графики на чистом SVG, без внешних библиотек.
 *
 * Палитра проверена валидатором в обеих темах:
 *   тёмная  — все шесть слотов проходят все проверки;
 *   светлая — три слота ниже контраста 3:1, поэтому у всех графиков
 *             обязательны легенда и подписи значений (правило рельефа).
 * Линии 2 px, маркеры от 8 px, столбцы со скруглением 4 px у вершины,
 * зазор 2 px между сегментами, сетка приглушённая.
 */
(function (global) {
  'use strict';

  const PALETTE = {
    dark:  ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#9085e9', '#008300', '#e66767'],
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#4a3aa7', '#008300', '#e34948'],
  };
  const STATUS = {
    dark:  { ok: '#3fb950', warn: '#d29922', err: '#f85149', idle: '#6b7889', info: '#a371f7' },
    light: { ok: '#1a7f37', warn: '#9a6700', err: '#cf222e', idle: '#808da0', info: '#8250df' },
  };

  const NS = 'http://www.w3.org/2000/svg';

  function mode() {
    return document.body.classList.contains('light') ? 'light' : 'dark';
  }
  function palette() { return PALETTE[mode()]; }
  function status() { return STATUS[mode()]; }
  function css(name, fallback) {
    const value = getComputedStyle(document.body).getPropertyValue(name);
    return (value && value.trim()) || fallback;
  }
  function ink() { return css('--text-dim', '#9aa7b6'); }
  function faint() { return css('--text-faint', '#6b7889'); }
  function gridColor() { return css('--border-soft', '#1e2530'); }
  function surface() { return css('--bg-elev', '#151a21'); }

  function el(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    for (const key in attrs) {
      if (attrs[key] === null || attrs[key] === undefined) continue;
      node.setAttribute(key, attrs[key]);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function fmtNum(value, digits) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    const abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(1) + ' млрд';
    if (abs >= 1e6) return (value / 1e6).toFixed(1) + ' млн';
    if (abs >= 1e4) return (value / 1e3).toFixed(1) + ' тыс';
    if (digits !== undefined) return value.toFixed(digits);
    if (Number.isInteger(value)) return String(value);
    return value.toFixed(abs < 1 ? 3 : 2);
  }

  function niceTicks(min, max, count) {
    if (min === max) { max = min + 1; }
    const span = max - min;
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const start = Math.floor(min / step) * step;
    const ticks = [];
    for (let v = start; v <= max + step * 0.5; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }

  // ---- всплывающая подсказка -------------------------------------------

  const tip = {
    node: null,
    show(html, event) {
      if (!this.node) this.node = document.getElementById('chart-tip');
      if (!this.node) return;
      this.node.textContent = html;
      this.node.style.display = 'block';
      const rect = this.node.getBoundingClientRect();
      let x = event.clientX + 14;
      let y = event.clientY - rect.height - 10;
      if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - 14;
      if (y < 8) y = event.clientY + 16;
      this.node.style.left = x + 'px';
      this.node.style.top = y + 'px';
    },
    hide() { if (this.node) this.node.style.display = 'none'; },
  };

  function attachTip(node, text) {
    node.addEventListener('mousemove', (e) => tip.show(text, e));
    node.addEventListener('mouseleave', () => tip.hide());
  }

  // ---- каркас графика ---------------------------------------------------

  function frame(host, opts) {
    host.innerHTML = '';
    const width = opts.width || host.clientWidth || 640;
    const height = opts.height || 220;
    const pad = Object.assign({ top: 14, right: 16, bottom: 26, left: 46 }, opts.pad || {});
    const svg = el('svg', {
      class: 'chart', viewBox: `0 0 ${width} ${height}`,
      width: '100%', height: height, role: 'img',
      'aria-label': opts.title || 'график',
    }, host);
    return { svg, width, height, pad,
             iw: width - pad.left - pad.right, ih: height - pad.top - pad.bottom };
  }

  function axes(ctx, yTicks, xLabels, opts) {
    const { svg, pad, iw, ih } = ctx;
    const grid = gridColor();
    yTicks.forEach((value) => {
      const y = pad.top + ih - ((value - ctx.yMin) / (ctx.yMax - ctx.yMin || 1)) * ih;
      el('line', { x1: pad.left, y1: y, x2: pad.left + iw, y2: y,
                   stroke: grid, 'stroke-width': 1 }, svg);
      el('text', { x: pad.left - 7, y: y + 3.5, 'text-anchor': 'end',
                   fill: faint(), 'font-size': 10.5 }, svg)
        .textContent = (opts && opts.yFormat ? opts.yFormat(value) : fmtNum(value));
    });
    if (xLabels && xLabels.length) {
      const step = Math.max(1, Math.ceil(xLabels.length / (opts && opts.xTicks || 7)));
      xLabels.forEach((label, index) => {
        if (index % step !== 0 && index !== xLabels.length - 1) return;
        const x = pad.left + (xLabels.length === 1 ? iw / 2
          : (index / (xLabels.length - 1)) * iw);
        el('text', { x, y: pad.top + ih + 15, 'text-anchor': 'middle',
                     fill: faint(), 'font-size': 10.5 }, svg).textContent = label;
      });
    }
  }

  // ---- линейный график ---------------------------------------------------

  function line(host, config) {
    const series = (config.series || []).filter((s) => s && s.values);
    if (!series.length) return empty(host, config.emptyText);
    const ctx = frame(host, config);
    const colors = palette();

    let min = config.yMin !== undefined ? config.yMin : Infinity;
    let max = config.yMax !== undefined ? config.yMax : -Infinity;
    series.forEach((s) => s.values.forEach((v) => {
      if (v === null || v === undefined) return;
      if (config.yMin === undefined) min = Math.min(min, v);
      if (config.yMax === undefined) max = Math.max(max, v);
    }));
    if (!isFinite(min)) min = 0;
    if (!isFinite(max)) max = 1;
    if (min > 0) min = 0;
    if (max === min) max = min + 1;
    const ticks = niceTicks(min, max, 4);
    ctx.yMin = Math.min(min, ticks[0]);
    ctx.yMax = Math.max(max, ticks[ticks.length - 1]);
    axes(ctx, ticks, config.labels, config);

    const count = Math.max(1, (config.labels || series[0].values).length - 1);
    const xAt = (i) => ctx.pad.left + (count === 0 ? ctx.iw / 2 : (i / count) * ctx.iw);
    const yAt = (v) => ctx.pad.top + ctx.ih -
      ((v - ctx.yMin) / (ctx.yMax - ctx.yMin)) * ctx.ih;

    series.forEach((s, si) => {
      const color = s.color || colors[si % colors.length];
      const points = [];
      s.values.forEach((v, i) => { if (v !== null && v !== undefined) points.push([xAt(i), yAt(v), i, v]); });
      if (!points.length) return;
      if (config.area) {
        const d = 'M' + points.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('L') +
          `L${points[points.length - 1][0].toFixed(1)},${yAt(ctx.yMin).toFixed(1)}` +
          `L${points[0][0].toFixed(1)},${yAt(ctx.yMin).toFixed(1)}Z`;
        el('path', { d, fill: color, opacity: 0.13 }, ctx.svg);
      }
      el('path', {
        d: 'M' + points.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('L'),
        fill: 'none', stroke: color, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
      }, ctx.svg);
      if (points.length <= 40) {
        points.forEach((p) => {
          const dot = el('circle', { cx: p[0], cy: p[1], r: 4, fill: color,
                                     stroke: surface(), 'stroke-width': 2 }, ctx.svg);
          const label = (config.labels && config.labels[p[2]]) || p[2];
          attachTip(dot, `${s.name}\n${label}: ${fmtNum(p[3])}${config.unit || ''}`);
        });
      }
    });

    // Слой перекрестия: одна подсказка на всю вертикаль
    const overlay = el('rect', { x: ctx.pad.left, y: ctx.pad.top, width: ctx.iw,
                                 height: ctx.ih, fill: 'transparent' }, ctx.svg);
    const cross = el('line', { y1: ctx.pad.top, y2: ctx.pad.top + ctx.ih,
                               stroke: faint(), 'stroke-width': 1,
                               'stroke-dasharray': '3 3', opacity: 0 }, ctx.svg);
    overlay.addEventListener('mousemove', (event) => {
      const box = ctx.svg.getBoundingClientRect();
      const scale = ctx.width / box.width;
      const x = (event.clientX - box.left) * scale;
      const index = Math.round(((x - ctx.pad.left) / ctx.iw) * count);
      const clamped = Math.max(0, Math.min(count, index));
      cross.setAttribute('x1', xAt(clamped));
      cross.setAttribute('x2', xAt(clamped));
      cross.setAttribute('opacity', 0.7);
      const label = (config.labels && config.labels[clamped]) || clamped;
      const lines = [String(label)];
      series.forEach((s) => {
        const v = s.values[clamped];
        lines.push(`  ${s.name}: ${v === null || v === undefined ? '—' : fmtNum(v) + (config.unit || '')}`);
      });
      tip.show(lines.join('\n'), event);
    });
    overlay.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); tip.hide(); });

    if (series.length >= 2) legend(host, series.map((s, i) => ({
      name: s.name, color: s.color || colors[i % colors.length] })));
    return ctx.svg;
  }

  // ---- столбчатый график --------------------------------------------------

  function bars(host, config) {
    const values = config.values || [];
    if (!values.length) return empty(host, config.emptyText);
    const ctx = frame(host, config);
    const colors = palette();
    const max = Math.max(...values.map((v) => v || 0), config.yMin || 0) || 1;
    const ticks = niceTicks(0, max, 4);
    ctx.yMin = 0;
    ctx.yMax = ticks[ticks.length - 1];
    axes(ctx, ticks, null, config);

    const slot = ctx.iw / values.length;
    const gap = Math.min(10, slot * 0.28);
    const barWidth = Math.max(3, slot - gap);
    const radius = Math.min(4, barWidth / 2);

    values.forEach((value, index) => {
      const v = value || 0;
      const height = (v / ctx.yMax) * ctx.ih;
      const x = ctx.pad.left + index * slot + gap / 2;
      const y = ctx.pad.top + ctx.ih - height;
      const color = config.colors ? config.colors[index] : colors[0];
      const path = el('path', {
        d: roundedTop(x, y, barWidth, Math.max(height, v > 0 ? 2 : 0), radius),
        fill: color,
      }, ctx.svg);
      attachTip(path, `${config.labels[index]}: ${fmtNum(v)}${config.unit || ''}`);
      if (config.showValues !== false && values.length <= 14 && v > 0) {
        el('text', { x: x + barWidth / 2, y: y - 5, 'text-anchor': 'middle',
                     fill: ink(), 'font-size': 10.5 }, ctx.svg).textContent = fmtNum(v);
      }
      if (config.labels) {
        const step = Math.max(1, Math.ceil(values.length / 14));
        if (index % step === 0 || values.length <= 14) {
          el('text', { x: x + barWidth / 2, y: ctx.pad.top + ctx.ih + 15,
                       'text-anchor': 'middle', fill: faint(), 'font-size': 10 },
             ctx.svg).textContent = config.labels[index];
        }
      }
    });
    return ctx.svg;
  }

  function roundedTop(x, y, width, height, radius) {
    const r = Math.min(radius, height, width / 2);
    return `M${x},${y + height}L${x},${y + r}Q${x},${y} ${x + r},${y}` +
           `L${x + width - r},${y}Q${x + width},${y} ${x + width},${y + r}` +
           `L${x + width},${y + height}Z`;
  }

  // ---- горизонтальные столбцы ----------------------------------------------

  function hbars(host, config) {
    const items = config.items || [];
    if (!items.length) return empty(host, config.emptyText);
    const rowHeight = config.rowHeight || 26;
    const height = items.length * rowHeight + 16;
    const labelWidth = config.labelWidth || 160;
    const ctx = frame(host, Object.assign({}, config, {
      height, pad: { top: 8, right: 56, bottom: 8, left: labelWidth } }));
    const colors = palette();
    const max = Math.max(...items.map((i) => i.value || 0)) || 1;

    items.forEach((item, index) => {
      const y = ctx.pad.top + index * rowHeight;
      const width = Math.max(2, ((item.value || 0) / max) * ctx.iw);
      el('text', { x: ctx.pad.left - 10, y: y + rowHeight / 2 + 4, 'text-anchor': 'end',
                   fill: ink(), 'font-size': 12 }, ctx.svg).textContent = item.label;
      const rect = el('rect', {
        x: ctx.pad.left, y: y + 4, width, height: rowHeight - 10,
        rx: 4, fill: item.color || colors[index % colors.length],
      }, ctx.svg);
      attachTip(rect, `${item.label}: ${fmtNum(item.value)}${config.unit || ''}` +
        (item.note ? `\n${item.note}` : ''));
      el('text', { x: ctx.pad.left + width + 8, y: y + rowHeight / 2 + 4,
                   fill: ink(), 'font-size': 11.5 }, ctx.svg)
        .textContent = item.display || (fmtNum(item.value) + (config.unit || ''));
    });
    return ctx.svg;
  }

  // ---- составные столбцы ----------------------------------------------------

  function stacked(host, config) {
    const parts = config.parts || [];
    const total = parts.reduce((sum, p) => sum + (p.value || 0), 0);
    if (!total) return empty(host, config.emptyText);
    const height = config.height || 34;
    host.innerHTML = '';
    const svg = el('svg', { class: 'chart', viewBox: `0 0 1000 ${height}`,
                            width: '100%', height, preserveAspectRatio: 'none' }, host);
    const colors = palette();
    let offset = 0;
    parts.forEach((part, index) => {
      const width = ((part.value || 0) / total) * 1000;
      if (width <= 0) return;
      const rect = el('rect', {
        x: offset, y: 0, width: Math.max(0, width - 2), height, rx: 3,
        fill: part.color || colors[index % colors.length],
      }, svg);
      attachTip(rect, `${part.label}: ${fmtNum(part.value)}${config.unit || ''} ` +
        `(${((part.value / total) * 100).toFixed(1)} %)`);
      offset += width;
    });
    legend(host, parts.map((p, i) => ({
      name: `${p.label} — ${fmtNum(p.value)}${config.unit || ''}`,
      color: p.color || colors[i % colors.length] })));
    return svg;
  }

  // ---- кольцевая диаграмма -----------------------------------------------------

  function donut(host, config) {
    const parts = (config.parts || []).filter((p) => (p.value || 0) > 0);
    const total = parts.reduce((sum, p) => sum + p.value, 0);
    if (!total) return empty(host, config.emptyText);
    const size = config.size || 180;
    host.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'row';
    wrap.style.gap = '18px';
    host.appendChild(wrap);
    const holder = document.createElement('div');
    wrap.appendChild(holder);
    const svg = el('svg', { class: 'chart', viewBox: `0 0 ${size} ${size}`,
                            width: size, height: size }, holder);
    const colors = palette();
    const cx = size / 2, cy = size / 2;
    const outer = size / 2 - 4, inner = outer * 0.62;
    let angle = -Math.PI / 2;

    parts.forEach((part, index) => {
      const share = part.value / total;
      const sweep = share * Math.PI * 2;
      const gap = share > 0.02 ? 0.018 : 0;
      const a0 = angle + gap / 2, a1 = angle + sweep - gap / 2;
      const large = sweep > Math.PI ? 1 : 0;
      const path = el('path', {
        d: `M${cx + outer * Math.cos(a0)},${cy + outer * Math.sin(a0)}` +
           `A${outer},${outer} 0 ${large} 1 ${cx + outer * Math.cos(a1)},${cy + outer * Math.sin(a1)}` +
           `L${cx + inner * Math.cos(a1)},${cy + inner * Math.sin(a1)}` +
           `A${inner},${inner} 0 ${large} 0 ${cx + inner * Math.cos(a0)},${cy + inner * Math.sin(a0)}Z`,
        fill: part.color || colors[index % colors.length],
      }, svg);
      attachTip(path, `${part.label}: ${fmtNum(part.value)} (${(share * 100).toFixed(1)} %)`);
      angle += sweep;
    });

    el('text', { x: cx, y: cy - 2, 'text-anchor': 'middle', fill: css('--text', '#e6edf3'),
                 'font-size': 22, 'font-weight': 650 }, svg).textContent = fmtNum(total);
    el('text', { x: cx, y: cy + 16, 'text-anchor': 'middle', fill: faint(),
                 'font-size': 11 }, svg).textContent = config.centerLabel || 'всего';

    const list = document.createElement('div');
    list.className = 'stack';
    list.style.fontSize = '12.5px';
    parts.forEach((part, index) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.style.gap = '7px';
      row.innerHTML = `<i style="width:10px;height:10px;border-radius:2px;background:${
        part.color || colors[index % colors.length]};display:inline-block"></i>` +
        `<span class="dim">${part.label}</span><span class="spacer"></span>` +
        `<b class="mono">${fmtNum(part.value)}</b>` +
        `<span class="faint mono">${((part.value / total) * 100).toFixed(0)} %</span>`;
      list.appendChild(row);
    });
    wrap.appendChild(list);
    return svg;
  }

  // ---- искровая линия -------------------------------------------------------

  function spark(host, values, options) {
    const opts = options || {};
    const data = (values || []).filter((v) => v !== null && v !== undefined);
    if (data.length < 2) { host.innerHTML = '<span class="faint small">нет данных</span>'; return; }
    const width = opts.width || 120, height = opts.height || 28;
    host.innerHTML = '';
    const svg = el('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`,
                            width, height }, host);
    const min = Math.min(...data), max = Math.max(...data);
    const span = (max - min) || 1;
    const points = data.map((v, i) => [
      (i / (data.length - 1)) * (width - 4) + 2,
      height - 3 - ((v - min) / span) * (height - 6),
    ]);
    const color = opts.color || palette()[0];
    el('path', { d: 'M' + points.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('L'),
                 fill: 'none', stroke: color, 'stroke-width': 2,
                 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
    el('circle', { cx: points[points.length - 1][0], cy: points[points.length - 1][1],
                   r: 2.5, fill: color }, svg);
    return svg;
  }

  // ---- тепловая карта по часам --------------------------------------------------

  function heat(host, config) {
    const values = config.values || [];
    if (!values.length) return empty(host, config.emptyText);
    host.innerHTML = '';
    const cell = config.cell || 26;
    const cols = config.cols || values.length;
    const width = cols * cell + 34;
    const height = cell + 26;
    const svg = el('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`,
                            width: '100%', height }, host);
    const max = Math.max(...values) || 1;
    // Последовательная шкала: один тон. На светлой поверхности идёт от светлого
    // к тёмному, на тёмной — наоборот, чтобы «мало» всегда сливалось с фоном,
    // а «много» контрастировало. Иначе шкала читается перевёрнуто.
    const rampLight = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95'];
    const ramp = mode() === 'light' ? rampLight : rampLight.slice().reverse();
    values.forEach((value, index) => {
      const level = max ? Math.min(ramp.length - 1, Math.floor((value / max) * ramp.length)) : 0;
      const x = 30 + index * cell;
      const rect = el('rect', { x, y: 4, width: cell - 2, height: cell - 2, rx: 3,
                                fill: value ? ramp[level] : gridColor() }, svg);
      attachTip(rect, `${config.labels[index]}: ${fmtNum(value)}`);
      if (index % 3 === 0) {
        el('text', { x: x + (cell - 2) / 2, y: cell + 16, 'text-anchor': 'middle',
                     fill: faint(), 'font-size': 9.5 }, svg).textContent = config.labels[index];
      }
    });
    return svg;
  }

  // ---- полоса громкости -----------------------------------------------------

  /* Огибающая записи: по дорожке на канал или говорящего.
   *
   * Дорожками, а не одной картинкой с наложением: наложенные полосы двух
   * собеседников сливаются там, где говорят оба, и именно эти места важнее
   * всего. Каждая дорожка подписана слева — цвет здесь опознавательный
   * знак, а не единственный признак.
   *
   * config: { curves: [{label, audio_waveform, sample_rate}], duration,
   *           interval, onSeek(seconds) }
   */
  function waveform(host, config) {
    const curves = (config.curves || []).filter(
      (c) => c && (c.audio_waveform || []).length);
    if (!curves.length) return empty(host, config.emptyText);

    host.innerHTML = '';
    const colors = palette();
    const lane = config.laneHeight || 46;
    const gap = 8;
    const padLeft = config.labelWidth !== undefined ? config.labelWidth : 92;
    const padRight = 10, padTop = 6, padBottom = 22;
    const width = config.width || host.clientWidth || 640;
    const height = padTop + curves.length * lane + (curves.length - 1) * gap + padBottom;
    const iw = Math.max(40, width - padLeft - padRight);

    // Общий предел по всем дорожкам: если у каждой свой, тихий собеседник
    // выглядит таким же громким, как крикливый, и полоса врёт.
    let peak = 0;
    curves.forEach((c) => c.audio_waveform.forEach((pt) => {
      if (pt.amplitude > peak) peak = pt.amplitude;
    }));
    peak = peak || 1;

    const last = curves[0].audio_waveform[curves[0].audio_waveform.length - 1];
    const step = config.interval || (curves[0].audio_waveform.length > 1
      ? curves[0].audio_waveform[1].time - curves[0].audio_waveform[0].time : 1);
    const duration = config.duration || (last.time + step);
    const xOf = (seconds) => padLeft + Math.min(1, seconds / duration) * iw;

    const svg = el('svg', {
      class: 'chart waveform', viewBox: `0 0 ${width} ${height}`,
      width: '100%', height, role: 'img',
      'aria-label': 'Полоса громкости записи по дорожкам',
    }, host);

    const seekable = typeof config.onSeek === 'function';
    const seekAt = (event) => {
      if (!seekable) return;
      const box = svg.getBoundingClientRect();
      const ratio = (event.clientX - box.left) / box.width;      // viewBox тянется
      const seconds = ((ratio * width) - padLeft) / iw * duration;
      config.onSeek(Math.max(0, Math.min(duration, seconds)));
    };

    curves.forEach((curve, index) => {
      const color = curve.color || colors[index % colors.length];
      const top = padTop + index * (lane + gap);
      const base = top + lane;
      const points = curve.audio_waveform;

      // Дорожка обозначена приглушённой подложкой и линией основания:
      // без них столбики висят в воздухе и тишину не отличить от пропуска.
      el('rect', { x: padLeft, y: top, width: iw, height: lane, rx: 4,
                   fill: gridColor(), opacity: 0.5 }, svg);
      el('line', { x1: padLeft, y1: base, x2: padLeft + iw, y2: base,
                   stroke: css('--border', '#262e3a'), 'stroke-width': 1 }, svg);

      // Столбик на замер, пока они шире двух пикселей; на длинной записи
      // столбики тоньше волоса, и вместо них рисуется заливка.
      const cell = iw / Math.max(1, points.length);
      if (cell >= 2.5) {
        points.forEach((pt) => {
          // Ровный ноль ничем не рисуется: полную тишину показывает пустая
          // дорожка с линией основания. Минимум в пиксель нужен только
          // тихому звуку, иначе он пропадает совсем.
          if (!pt.amplitude) return;
          const h = Math.max(1, (pt.amplitude / peak) * (lane - 4));
          el('rect', { x: xOf(pt.time) + 0.5, y: base - h,
                       width: Math.max(1, cell - 1.5), height: h,
                       rx: Math.min(2, cell / 3), fill: color }, svg);
        });
      } else {
        let d = `M${padLeft.toFixed(1)},${base.toFixed(1)}`;
        points.forEach((pt) => {
          const h = (pt.amplitude / peak) * (lane - 4);
          d += `L${xOf(pt.time).toFixed(1)},${(base - h).toFixed(1)}`;
        });
        d += `L${(padLeft + iw).toFixed(1)},${base.toFixed(1)}Z`;
        el('path', { d, fill: color, opacity: 0.9 }, svg);
      }

      el('text', { x: padLeft - 8, y: top + lane / 2 + 4, 'text-anchor': 'end',
                   fill: ink(), 'font-size': 11.5 }, svg)
        .textContent = curve.label || `Дорожка ${index + 1}`;
      el('rect', { x: padLeft - 5, y: top, width: 3, height: lane, rx: 1.5,
                   fill: color }, svg);
    });

    // Ось времени.
    const marks = niceTicks(0, duration, 6).filter((v) => v >= 0 && v <= duration);
    marks.forEach((value) => {
      el('text', { x: xOf(value), y: height - 7, 'text-anchor': 'middle',
                   fill: faint(), 'font-size': 10.5 }, svg)
        .textContent = config.timeFormat ? config.timeFormat(value) : fmtNum(value);
    });

    // Отслеживающая линия поверх всего: цель наведения — вся картинка,
    // попасть в отдельный столбик мышью нереально.
    const plotTop = padTop, plotH = height - padTop - padBottom;
    const cursor = el('line', { x1: padLeft, y1: plotTop, x2: padLeft,
                                y2: plotTop + plotH, stroke: css('--text', '#e6edf3'),
                                'stroke-width': 1, opacity: 0 }, svg);
    const hit = el('rect', { x: padLeft, y: plotTop, width: iw, height: plotH,
                             fill: 'transparent',
                             style: seekable ? 'cursor:pointer' : '' }, svg);

    const at = (event) => {
      const box = svg.getBoundingClientRect();
      const seconds = Math.max(0, Math.min(duration,
        (((event.clientX - box.left) / box.width) * width - padLeft) / iw * duration));
      const slot = Math.min(curves[0].audio_waveform.length - 1,
                            Math.max(0, Math.round(seconds / step)));
      cursor.setAttribute('x1', xOf(seconds));
      cursor.setAttribute('x2', xOf(seconds));
      cursor.setAttribute('opacity', 0.55);
      const label = config.timeFormat ? config.timeFormat(seconds) : fmtNum(seconds);
      const rows = curves.map((c) => {
        const pt = c.audio_waveform[slot];
        return `${c.label}: ${pt ? pt.amplitude.toFixed(3) : '—'}`;
      }).join(' · ');
      tip.show(`${label} — ${rows}${seekable ? ' · щелчок: перейти к месту' : ''}`, event);
    };
    hit.addEventListener('mousemove', at);
    hit.addEventListener('mouseleave', () => {
      cursor.setAttribute('opacity', 0);
      tip.hide();
    });
    if (seekable) hit.addEventListener('click', seekAt);
    return svg;
  }

  // ---- вспомогательное ------------------------------------------------------------

  function legend(host, items) {
    const box = document.createElement('div');
    box.className = 'chart-legend';
    items.forEach((item) => {
      const span = document.createElement('span');
      span.innerHTML = `<i style="background:${item.color}"></i>${item.name}`;
      box.appendChild(span);
    });
    host.appendChild(box);
  }

  function empty(host, text) {
    host.innerHTML = `<div class="empty small">${text || 'Пока нет данных для графика'}</div>`;
    return null;
  }

  global.Charts = { line, bars, hbars, stacked, donut, spark, heat, waveform,
                    palette, status, fmtNum, legend, empty };
})(window);
