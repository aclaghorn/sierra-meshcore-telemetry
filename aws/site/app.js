const repeatersElement = document.querySelector('#repeaters');
const statusElement = document.querySelector('#status');
const updatedElement = document.querySelector('#updated');
const chartStatusElement = document.querySelector('#chart-status');
const chartElement = document.querySelector('#chart');
const legendElement = document.querySelector('#chart-legend');
const historyTitleElement = document.querySelector('#history-title');
const chartTitleElement = document.querySelector('#chart-title');
const chartDescriptionElement = document.querySelector('#chart-description');
const metricSelectorElement = document.querySelector('.metric-selector');
const focusElement = document.querySelector('#focus');
const focusNameElement = document.querySelector('#focus-name');
const rangeLabelElement = document.querySelector('#range-label');
const pickerToggle = document.querySelector('#picker-toggle');
const pickerPanel = document.querySelector('#picker-panel');
const absoluteForm = document.querySelector('#absolute-form');
const fromInput = document.querySelector('#range-from');
const toInput = document.querySelector('#range-to');
const pickerError = document.querySelector('#picker-error');
const presetSearch = document.querySelector('#preset-search');
const presetList = document.querySelector('#preset-list');
const shiftBackButton = document.querySelector('#shift-back');
const shiftForwardButton = document.querySelector('#shift-forward');
const timezoneButtons = [...document.querySelectorAll('[data-timezone]')];

const DAY_SECONDS = 86400;
const FETCH_CONCURRENCY = 6;
// Ranges longer than this are averaged into buckets so long views stay fast.
const RAW_RANGE_SECONDS = 3 * DAY_SECONDS;
const TARGET_BUCKETS = 1200;
const BUCKET_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
const COLORS = [
  '#70d6ff',
  '#ff9770',
  '#ffd670',
  '#8ce99a',
  '#c77dff',
  '#f783ac',
  '#63e6be',
  '#ffa94d',
  '#91a7ff',
  '#e9fac8',
  '#ff6b6b',
  '#4dabf7',
  '#d8f5a2',
  '#eebefa',
];

const METRICS = {
  charge: {
    label: 'Charge',
    title: 'Battery charge',
    description: 'Reported or voltage-estimated percentage',
    source: 'readings',
    field: 'battery_percent',
    suffix: '%',
    digits: 0,
    range: [0, 100],
  },
  voltage: {
    label: 'Voltage',
    title: 'Battery voltage',
    description: 'Measured battery voltage',
    source: 'readings',
    field: 'battery_voltage',
    suffix: ' V',
    digits: 2,
  },
  temperature: {
    label: 'Temperature',
    title: 'Temperature',
    description: 'Onboard sensor temperature',
    source: 'readings',
    field: 'temperature_c',
    suffix: ' °C',
    digits: 1,
  },
  noise: {
    label: 'Noise floor',
    title: 'Noise floor',
    description: 'Radio noise floor reported by the repeater',
    source: 'stats',
    field: 'noise_floor_dbm',
    suffix: ' dBm',
    digits: 0,
  },
  snr: {
    label: 'SNR',
    title: 'Last SNR',
    description: 'Signal-to-noise ratio of the last packet received',
    source: 'stats',
    field: 'last_snr_db',
    suffix: ' dB',
    digits: 1,
  },
};

// Grafana-style quick ranges. `from`/`to` use the same syntax as the inputs.
const PRESETS = [
  { from: 'now-3h', to: 'now', label: 'Last 3 hours' },
  { from: 'now-6h', to: 'now', label: 'Last 6 hours' },
  { from: 'now-12h', to: 'now', label: 'Last 12 hours' },
  { from: 'now-24h', to: 'now', label: 'Last 24 hours' },
  { from: 'now-2d', to: 'now', label: 'Last 2 days' },
  { from: 'now-7d', to: 'now', label: 'Last 7 days' },
  { from: 'now-30d', to: 'now', label: 'Last 30 days' },
  { from: 'now-90d', to: 'now', label: 'Last 90 days' },
  { from: 'now-6M', to: 'now', label: 'Last 6 months' },
  { from: 'now-1y', to: 'now', label: 'Last 1 year' },
  { from: 'now/d', to: 'now/d', label: 'Today' },
  { from: 'now/d', to: 'now', label: 'Today so far' },
  { from: 'now-1d/d', to: 'now-1d/d', label: 'Yesterday' },
  { from: 'now-2d/d', to: 'now-2d/d', label: 'Day before yesterday' },
  { from: 'now/w', to: 'now/w', label: 'This week' },
  { from: 'now/w', to: 'now', label: 'This week so far' },
  { from: 'now-1w/w', to: 'now-1w/w', label: 'Previous week' },
  { from: 'now/M', to: 'now/M', label: 'This month' },
  { from: 'now/M', to: 'now', label: 'This month so far' },
  { from: 'now-1M/M', to: 'now-1M/M', label: 'Previous month' },
  { from: 'now/y', to: 'now', label: 'This year so far' },
];

const DEFAULT_RANGE = { from: 'now-24h', to: 'now' };

const state = {
  range: { ...DEFAULT_RANGE },
  timezone: 'local',
  metric: 'charge',
  // Repeater ids to draw; empty means every repeater.
  selected: new Set(),
  availableDays: new Set(),
  repeaters: [],
  request: 0,
  series: [],
  resolved: null,
  chart: null,
};

const dayCache = new Map();

// ---------------------------------------------------------------- formatting

function value(number, suffix = '', digits = 1) {
  return number == null ? '—' : `${Number(number).toFixed(digits)}${suffix}`;
}

function escapeHtml(text) {
  const element = document.createElement('span');
  element.textContent = text;
  return element.innerHTML;
}

function card(repeater) {
  const online = repeater.online ? 'Online' : 'No recent response';
  return `
    <article class="card" data-id="${escapeHtml(repeater.id)}" tabindex="0" role="button"
      aria-pressed="false" title="Show only this repeater in the chart">
      <div class="card-heading">
        <h2>${escapeHtml(repeater.name)}</h2>
        <span class="${repeater.online ? 'online' : 'offline'}">${online}</span>
      </div>
      <dl>
        <div><dt>Battery</dt><dd>${value(repeater.battery_percent, '%', 0)}</dd></div>
        <div><dt>Voltage</dt><dd>${value(repeater.battery_voltage, ' V', 2)}</dd></div>
        <div><dt>Temperature</dt><dd>${value(repeater.temperature_c, ' °C')}</dd></div>
      </dl>
      <p class="reading-time">Last reading: ${
        repeater.last_success
          ? new Date(repeater.last_success * 1000).toLocaleString()
          : 'never'
      }</p>
    </article>
  `;
}

function pad(number) {
  return String(number).padStart(2, '0');
}

// ----------------------------------------------------------------- date math

// Calendar helpers for the chosen time zone (browser local or UTC).
function calendar(utc) {
  return utc
    ? {
        parts: (d) => [d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(),
          d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCDay()],
        make: (y, mo, d, h = 0, mi = 0, s = 0) => new Date(Date.UTC(y, mo, d, h, mi, s)),
      }
    : {
        parts: (d) => [d.getFullYear(), d.getMonth(), d.getDate(),
          d.getHours(), d.getMinutes(), d.getSeconds(), d.getDay()],
        make: (y, mo, d, h = 0, mi = 0, s = 0) => new Date(y, mo, d, h, mi, s),
      };
}

function addUnits(date, amount, unit, cal) {
  const ms = { s: 1e3, m: 6e4, h: 36e5 }[unit];
  if (ms) return new Date(date.getTime() + amount * ms);
  const [y, mo, d, h, mi, s] = cal.parts(date);
  if (unit === 'd') return cal.make(y, mo, d + amount, h, mi, s);
  if (unit === 'w') return cal.make(y, mo, d + amount * 7, h, mi, s);
  if (unit === 'M') return cal.make(y, mo + amount, d, h, mi, s);
  return cal.make(y + amount, mo, d, h, mi, s);
}

function startOf(date, unit, cal) {
  const [y, mo, d, h, mi, s, weekday] = cal.parts(date);
  switch (unit) {
    case 's': return cal.make(y, mo, d, h, mi, s);
    case 'm': return cal.make(y, mo, d, h, mi);
    case 'h': return cal.make(y, mo, d, h);
    case 'd': return cal.make(y, mo, d);
    case 'w': return cal.make(y, mo, d - ((weekday + 6) % 7)); // weeks start Monday
    case 'M': return cal.make(y, mo, 1);
    default: return cal.make(y, 0, 1);
  }
}

const RELATIVE = /^now((?:\s*[+-]\s*\d+\s*[smhdwMy])*)(?:\s*\/\s*([smhdwMy]))?$/;
const ABSOLUTE = /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/;

// Parse a Grafana-like time expression. `isEnd` rounds `/unit` and date-only
// values to the end of the period, so "now-1d/d" to "now-1d/d" is yesterday.
function parseTime(text, isEnd, now = new Date()) {
  const cal = calendar(state.timezone === 'utc');
  const input = text.trim();
  const relative = input.match(RELATIVE);
  if (relative) {
    let date = new Date(now);
    for (const [, sign, amount, unit] of relative[1].matchAll(/([+-])\s*(\d+)\s*([smhdwMy])/g)) {
      date = addUnits(date, Number(amount) * (sign === '-' ? -1 : 1), unit, cal);
    }
    if (relative[2]) {
      date = startOf(date, relative[2], cal);
      if (isEnd) date = addUnits(date, 1, relative[2], cal);
    }
    return date;
  }
  const absolute = input.match(ABSOLUTE);
  if (absolute) {
    const [, y, mo, d, h, mi, s] = absolute;
    let date = cal.make(Number(y), Number(mo) - 1, Number(d),
      Number(h ?? 0), Number(mi ?? 0), Number(s ?? 0));
    if (Number.isNaN(date.getTime())) return null;
    if (isEnd && h == null) date = addUnits(date, 1, 'd', cal);
    return date;
  }
  return null;
}

function resolveRange(range) {
  const now = new Date();
  const start = parseTime(range.from, false, now);
  const end = parseTime(range.to, true, now);
  if (!start || !end) return null;
  return { start, end };
}

function isRelative(range) {
  return range.from.startsWith('now') || range.to.startsWith('now');
}

function formatAbsolute(date) {
  const [y, mo, d, h, mi, s] = calendar(state.timezone === 'utc').parts(date);
  const time = s ? `${pad(h)}:${pad(mi)}:${pad(s)}` : `${pad(h)}:${pad(mi)}`;
  return `${y}-${pad(mo + 1)}-${pad(d)} ${time}`;
}

function formatDisplay(date) {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: state.timezone === 'utc' ? 'UTC' : undefined,
  }).format(date);
}

function rangeLabel(range, resolved) {
  const preset = PRESETS.find((item) => item.from === range.from && item.to === range.to);
  const zone = state.timezone === 'utc' ? ' UTC' : '';
  if (preset) return `${preset.label}${zone}`;
  if (!resolved) return `${range.from} to ${range.to}`;
  return `${formatDisplay(resolved.start)} – ${formatDisplay(resolved.end)}${zone}`;
}

function formatSeconds(seconds) {
  if (seconds % DAY_SECONDS === 0) return `${seconds / DAY_SECONDS} d`;
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  return `${seconds / 60} min`;
}

// ------------------------------------------------------------------ URL state

function readUrl() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('from') && params.get('to')) {
    state.range = { from: params.get('from'), to: params.get('to') };
    if (!resolveRange(state.range)) state.range = { ...DEFAULT_RANGE };
  }
  if (params.get('tz') === 'utc') state.timezone = 'utc';
  if (METRICS[params.get('metric')]) state.metric = params.get('metric');
  const repeaters = params.get('repeater');
  state.selected = new Set(repeaters ? repeaters.split(',').filter(Boolean) : []);
}

function writeUrl() {
  const params = new URLSearchParams();
  if (state.range.from !== DEFAULT_RANGE.from || state.range.to !== DEFAULT_RANGE.to) {
    params.set('from', state.range.from);
    params.set('to', state.range.to);
  }
  if (state.timezone === 'utc') params.set('tz', 'utc');
  if (state.metric !== 'charge') params.set('metric', state.metric);
  if (state.selected.size) params.set('repeater', [...state.selected].join(','));
  const query = params.toString();
  window.history.replaceState(null, '', query ? `?${query}` : window.location.pathname);
}

// --------------------------------------------------------------- data loading

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function utcDaysBetween(start, end) {
  const days = [];
  let day = Math.floor(start.getTime() / 1000 / DAY_SECONDS) * DAY_SECONDS;
  for (; day < end.getTime() / 1000; day += DAY_SECONDS) {
    days.push(new Date(day * 1000).toISOString().slice(0, 10));
  }
  return days;
}

function todayUtc() {
  return new Date().toISOString().slice(0, 10);
}

function loadDay(day) {
  if (!dayCache.has(day)) {
    // Past days are immutable; today's file keeps changing, so revalidate it.
    const options = day >= todayUtc() ? { cache: 'no-cache' } : undefined;
    const promise = fetchJson(`/data/days/${day}.json.gz`, options).catch((error) => {
      dayCache.delete(day);
      throw error;
    });
    dayCache.set(day, promise);
  }
  return dayCache.get(day);
}

async function loadDays(days, onProgress) {
  const results = new Array(days.length);
  let next = 0;
  let done = 0;
  async function worker() {
    while (next < days.length) {
      const index = next++;
      results[index] = await loadDay(days[index]);
      onProgress(++done, days.length);
    }
  }
  await Promise.all(Array.from({ length: Math.min(FETCH_CONCURRENCY, days.length) }, worker));
  return results;
}

async function loadHistory() {
  const request = ++state.request;
  const resolved = resolveRange(state.range);
  state.resolved = resolved;
  updateRangeControls();
  writeUrl();
  if (!resolved || resolved.end <= resolved.start) {
    chartStatusElement.textContent = 'The selected time range is not valid.';
    return;
  }

  const days = utcDaysBetween(resolved.start, resolved.end).filter((day) =>
    state.availableDays.has(day),
  );
  chartStatusElement.textContent = days.length ? 'Loading history…' : '';
  try {
    const documents = await loadDays(days, (done, total) => {
      if (request === state.request && total > 1) {
        chartStatusElement.textContent = `Loading history… ${done} of ${total} days`;
      }
    });
    if (request !== state.request) return;
    state.documents = documents;
    buildSeries();
    chartStatusElement.textContent = state.series.length
      ? ''
      : `No ${METRICS[state.metric].title.toLowerCase()} readings are available in this range.`;
    render();
  } catch (error) {
    if (request !== state.request) return;
    chartStatusElement.textContent = `History unavailable: ${error.message}`;
  }
}

// ---------------------------------------------------------- series building

function bucketSize(spanSeconds) {
  if (spanSeconds <= RAW_RANGE_SECONDS) return 0;
  const target = spanSeconds / TARGET_BUCKETS;
  return BUCKET_STEPS.find((step) => step >= target) ?? BUCKET_STEPS.at(-1);
}

function buildSeries() {
  const metric = METRICS[state.metric];
  const startSec = state.resolved.start.getTime() / 1000;
  const endSec = state.resolved.end.getTime() / 1000;
  const bucket = bucketSize(endSec - startSec);
  state.bucket = bucket;

  const grouped = new Map();
  for (const document of state.documents ?? []) {
    for (const row of document[metric.source] ?? []) {
      const reading = row[metric.field];
      if (reading == null || row.ts < startSec || row.ts >= endSec) continue;
      let entry = grouped.get(row.id);
      if (!entry) {
        entry = { id: row.id, name: row.name, points: [] };
        grouped.set(row.id, entry);
      }
      entry.name = row.name;
      entry.points.push([row.ts, Number(reading)]);
    }
  }

  const order = new Map(state.repeaters.map((repeater, index) => [repeater.id, index]));
  const names = new Map(state.repeaters.map((repeater) => [repeater.id, repeater.name]));
  state.series = [...grouped.values()]
    .map((entry) => {
      entry.points.sort((a, b) => a[0] - b[0]);
      if (bucket) entry.points = averageBuckets(entry.points, bucket);
      entry.name = names.get(entry.id) ?? entry.name;
      return entry;
    })
    .sort((a, b) =>
      (order.get(a.id) ?? Infinity) - (order.get(b.id) ?? Infinity) ||
      a.name.localeCompare(b.name),
    );
}

function averageBuckets(points, bucket) {
  const result = [];
  let current = null;
  let sum = 0;
  let count = 0;
  for (const [ts, reading] of points) {
    const slot = Math.floor(ts / bucket) * bucket;
    if (slot !== current) {
      if (count) result.push([current + bucket / 2, sum / count]);
      current = slot;
      sum = 0;
      count = 0;
    }
    sum += reading;
    count += 1;
  }
  if (count) result.push([current + bucket / 2, sum / count]);
  return result;
}

function colorFor(id) {
  let index = state.repeaters.findIndex((repeater) => repeater.id === id);
  if (index < 0) {
    let hash = 0;
    for (const character of id) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
    index = Math.abs(hash);
  }
  return COLORS[index % COLORS.length];
}

function isVisible(id) {
  return !state.selected.size || state.selected.has(id);
}

// Build uPlot's aligned columns. Values at other series' timestamps are left
// `undefined`, which uPlot draws straight across, so missed polls join the
// last good sample to the next one instead of leaving a break in the line.
function alignedData(series) {
  const timestamps = [...new Set(series.flatMap((entry) => entry.points.map(([ts]) => ts)))]
    .sort((a, b) => a - b);
  const position = new Map(timestamps.map((ts, index) => [ts, index]));
  const data = [timestamps];
  for (const entry of series) {
    const values = new Array(timestamps.length).fill(undefined);
    for (const [ts, reading] of entry.points) values[position.get(ts)] = reading;
    data.push(values);
  }
  return data;
}

// ------------------------------------------------------------------ rendering

function chartSize() {
  return {
    width: Math.max(chartElement.clientWidth, 200),
    height: window.matchMedia('(max-width: 600px)').matches ? 260 : 340,
  };
}

function tooltipPlugin(metric) {
  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  tooltip.hidden = true;
  return {
    hooks: {
      init: (u) => u.over.append(tooltip),
      setCursor: (u) => {
        const { left, top, idx } = u.cursor;
        if (idx == null || left < 0) {
          tooltip.hidden = true;
          return;
        }
        const rows = [];
        u.series.forEach((series, index) => {
          if (index === 0 || !series.show) return;
          const dataIndex = u.cursor.idxs?.[index];
          const reading = dataIndex == null ? null : u.data[index][dataIndex];
          if (reading == null) return;
          rows.push({ label: series.label, color: series._color, reading });
        });
        if (!rows.length) {
          tooltip.hidden = true;
          return;
        }
        rows.sort((a, b) => b.reading - a.reading);
        const time = new Intl.DateTimeFormat(undefined, {
          month: 'short',
          day: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
          timeZone: state.timezone === 'utc' ? 'UTC' : undefined,
        }).format(new Date(u.data[0][idx] * 1000));
        tooltip.innerHTML = `<p>${escapeHtml(time)}${state.bucket ? ` · ${formatSeconds(state.bucket)} avg` : ''}</p>${rows
          .map((row) => `<div><span style="background:${row.color}"></span><span>${escapeHtml(row.label)}</span><strong>${value(row.reading, metric.suffix, metric.digits)}</strong></div>`)
          .join('')}`;
        tooltip.hidden = false;
        const overWidth = u.over.clientWidth;
        const overHeight = u.over.clientHeight;
        const box = tooltip.getBoundingClientRect();
        const x = left + 16 + box.width > overWidth ? left - box.width - 16 : left + 16;
        const y = Math.min(Math.max(top - box.height / 2, 0), Math.max(overHeight - box.height, 0));
        tooltip.style.transform = `translate(${Math.max(x, 0)}px, ${y}px)`;
      },
    },
  };
}

function render() {
  const metric = METRICS[state.metric];
  historyTitleElement.textContent = metric.title;
  chartTitleElement.textContent = metric.title;
  chartDescriptionElement.textContent = state.bucket
    ? `${metric.description} · ${formatSeconds(state.bucket)} averages`
    : metric.description;
  renderFocus();
  renderLegend();
  renderCards();

  state.chart?.destroy();
  state.chart = null;
  chartElement.replaceChildren();
  if (!state.resolved) return;

  const visible = state.series.filter((entry) => isVisible(entry.id));
  if (!visible.length) {
    const empty = document.createElement('p');
    empty.className = 'chart-empty';
    empty.textContent = state.series.length ? 'No data for the selected repeaters' : 'No data';
    chartElement.append(empty);
    return;
  }

  const startSec = state.resolved.start.getTime() / 1000;
  const endSec = state.resolved.end.getTime() / 1000;
  const utc = state.timezone === 'utc';
  const axis = {
    stroke: '#91a0b8',
    grid: { stroke: '#263650', width: 1 },
    ticks: { stroke: '#263650', width: 1 },
    font: '12px Inter, ui-sans-serif, system-ui, sans-serif',
  };
  const options = {
    ...chartSize(),
    tzDate: utc ? (ts) => uPlot.tzDate(new Date(ts * 1000), 'Etc/UTC') : undefined,
    legend: { show: false },
    focus: { alpha: 0.35 },
    cursor: {
      drag: { x: true, y: false, setScale: false },
      focus: { prox: 24 },
      points: { size: 7 },
    },
    scales: {
      x: { time: true, range: () => [startSec, endSec] },
      y: metric.range
        ? { range: metric.range }
        : { range: (u, min, max) => uPlot.rangeNum(min, max, 0.1, true) },
    },
    axes: [
      { ...axis, space: 70 },
      {
        ...axis,
        size: 64,
        values: (u, splits) => splits.map((split) => value(split, metric.suffix, metric.digits)),
      },
    ],
    series: [
      {},
      ...visible.map((entry) => ({
        label: entry.name,
        stroke: colorFor(entry.id),
        _color: colorFor(entry.id),
        width: 2,
        points: { show: false },
      })),
    ],
    hooks: {
      setSelect: [
        (u) => {
          if (u.select.width < 4) return;
          const from = u.posToVal(u.select.left, 'x');
          const to = u.posToVal(u.select.left + u.select.width, 'x');
          u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
          setRange({
            from: formatAbsolute(new Date(from * 1000)),
            to: formatAbsolute(new Date(to * 1000)),
          });
        },
      ],
    },
    plugins: [tooltipPlugin(metric)],
  };
  state.chart = new uPlot(options, alignedData(visible), chartElement);
}

function renderLegend() {
  const metric = METRICS[state.metric];
  legendElement.replaceChildren(
    ...state.series.map((entry) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.dataset.id = entry.id;
      item.className = isVisible(entry.id) ? '' : 'muted';
      item.setAttribute('aria-pressed', state.selected.has(entry.id) ? 'true' : 'false');
      const marker = document.createElement('span');
      marker.style.background = colorFor(entry.id);
      const name = document.createElement('span');
      name.textContent = entry.name;
      const latest = document.createElement('strong');
      latest.textContent = value(entry.points.at(-1)?.[1], metric.suffix, metric.digits);
      item.append(marker, name, latest);
      return item;
    }),
  );
}

function renderFocus() {
  focusElement.hidden = !state.selected.size;
  if (!state.selected.size) return;
  if (state.selected.size === 1) {
    const [id] = state.selected;
    focusNameElement.textContent =
      state.repeaters.find((repeater) => repeater.id === id)?.name ??
      state.series.find((entry) => entry.id === id)?.name ??
      'one repeater';
  } else {
    focusNameElement.textContent = `${state.selected.size} repeaters`;
  }
}

function renderCards() {
  for (const element of repeatersElement.querySelectorAll('.card')) {
    const selected = state.selected.has(element.dataset.id);
    element.classList.toggle('selected', selected);
    element.setAttribute('aria-pressed', selected ? 'true' : 'false');
  }
}

// ----------------------------------------------------------------- controls

function updateRangeControls() {
  rangeLabelElement.textContent = rangeLabel(state.range, state.resolved);
  const now = Date.now();
  shiftForwardButton.disabled = !state.resolved || state.resolved.end.getTime() >= now - 1000;
  timezoneButtons.forEach((button) =>
    button.setAttribute('aria-pressed', button.dataset.timezone === state.timezone ? 'true' : 'false'),
  );
  for (const item of presetList.querySelectorAll('button')) {
    item.setAttribute(
      'aria-pressed',
      item.dataset.from === state.range.from && item.dataset.to === state.range.to ? 'true' : 'false',
    );
  }
}

function setRange(range) {
  state.range = range;
  closePicker();
  loadHistory();
}

function selectRepeater(id, additive) {
  if (additive) {
    if (!state.selected.size) {
      state.series.forEach((entry) => entry.id !== id && state.selected.add(entry.id));
    } else if (state.selected.has(id)) {
      state.selected.delete(id);
    } else {
      state.selected.add(id);
    }
    if (state.selected.size === state.series.length) state.selected.clear();
  } else if (state.selected.size === 1 && state.selected.has(id)) {
    state.selected.clear();
  } else {
    state.selected = new Set([id]);
  }
  writeUrl();
  render();
}

function shiftRange(direction) {
  if (!state.resolved) return;
  const span = state.resolved.end - state.resolved.start;
  let start = state.resolved.start.getTime() + direction * span;
  let end = state.resolved.end.getTime() + direction * span;
  if (end > Date.now()) {
    start -= end - Date.now();
    end = Date.now();
  }
  setRange({ from: formatAbsolute(new Date(start)), to: formatAbsolute(new Date(end)) });
}

function zoomOut() {
  if (!state.resolved) return;
  const span = state.resolved.end - state.resolved.start;
  let start = state.resolved.start.getTime() - span / 2;
  let end = state.resolved.end.getTime() + span / 2;
  if (end > Date.now()) {
    start -= end - Date.now();
    end = Date.now();
  }
  setRange({ from: formatAbsolute(new Date(start)), to: formatAbsolute(new Date(end)) });
}

function openPicker() {
  pickerPanel.hidden = false;
  pickerToggle.setAttribute('aria-expanded', 'true');
  fromInput.value = state.range.from;
  toInput.value = state.range.to;
  pickerError.textContent = '';
  presetSearch.value = '';
  renderPresets();
  presetSearch.focus();
}

function closePicker() {
  pickerPanel.hidden = true;
  pickerToggle.setAttribute('aria-expanded', 'false');
}

function renderPresets() {
  const query = presetSearch.value.trim().toLowerCase();
  presetList.replaceChildren(
    ...PRESETS.filter((preset) => preset.label.toLowerCase().includes(query)).map((preset) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.from = preset.from;
      button.dataset.to = preset.to;
      button.textContent = preset.label;
      button.setAttribute(
        'aria-pressed',
        preset.from === state.range.from && preset.to === state.range.to ? 'true' : 'false',
      );
      item.append(button);
      return item;
    }),
  );
}

function renderMetricSelector() {
  metricSelectorElement.replaceChildren(
    ...Object.entries(METRICS).map(([key, metric]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.metric = key;
      button.textContent = metric.label;
      button.setAttribute('aria-pressed', key === state.metric ? 'true' : 'false');
      return button;
    }),
  );
}

pickerToggle.addEventListener('click', () => {
  if (pickerPanel.hidden) openPicker();
  else closePicker();
});

document.addEventListener('click', (event) => {
  if (!pickerPanel.hidden && !event.target.closest('.time-picker')) closePicker();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !pickerPanel.hidden) {
    closePicker();
    pickerToggle.focus();
  }
});

presetSearch.addEventListener('input', renderPresets);

presetList.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (button) setRange({ from: button.dataset.from, to: button.dataset.to });
});

absoluteForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const range = { from: fromInput.value.trim(), to: toInput.value.trim() };
  const resolved = resolveRange(range);
  if (!resolved) {
    pickerError.textContent = !parseTime(range.from, false)
      ? 'Could not read the From time.'
      : 'Could not read the To time.';
    return;
  }
  if (resolved.end <= resolved.start) {
    pickerError.textContent = 'From must be before To.';
    return;
  }
  setRange(range);
});

timezoneButtons.forEach((button) => {
  button.addEventListener('click', () => {
    if (state.timezone === button.dataset.timezone) return;
    // Keep absolute ranges pointing at the same instants in the new zone.
    const resolved = state.resolved;
    state.timezone = button.dataset.timezone;
    if (resolved && !isRelative(state.range)) {
      state.range = { from: formatAbsolute(resolved.start), to: formatAbsolute(resolved.end) };
      fromInput.value = state.range.from;
      toInput.value = state.range.to;
    }
    loadHistory();
  });
});

shiftBackButton.addEventListener('click', () => shiftRange(-1));
shiftForwardButton.addEventListener('click', () => shiftRange(1));
document.querySelector('#zoom-out').addEventListener('click', zoomOut);
document.querySelector('#refresh').addEventListener('click', () => {
  for (const day of dayCache.keys()) if (day >= todayUtc()) dayCache.delete(day);
  loadCurrent();
});

metricSelectorElement.addEventListener('click', (event) => {
  const button = event.target.closest('[data-metric]');
  if (!button || button.dataset.metric === state.metric) return;
  state.metric = button.dataset.metric;
  for (const item of metricSelectorElement.children) {
    item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
  }
  writeUrl();
  buildSeries();
  chartStatusElement.textContent = state.series.length
    ? ''
    : `No ${METRICS[state.metric].title.toLowerCase()} readings are available in this range.`;
  render();
});

legendElement.addEventListener('click', (event) => {
  const item = event.target.closest('[data-id]');
  if (item) selectRepeater(item.dataset.id, event.ctrlKey || event.metaKey || event.shiftKey);
});

document.querySelector('#clear-focus').addEventListener('click', () => {
  state.selected.clear();
  writeUrl();
  render();
});

function cardActivated(event) {
  const element = event.target.closest('.card');
  if (!element) return;
  selectRepeater(element.dataset.id, event.ctrlKey || event.metaKey || event.shiftKey);
  if (state.selected.size) {
    document.querySelector('.history').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

repeatersElement.addEventListener('click', cardActivated);
repeatersElement.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    cardActivated(event);
  }
});

let resizeFrame = 0;
new ResizeObserver(() => {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => state.chart?.setSize(chartSize()));
}).observe(chartElement);

// ------------------------------------------------------------------- startup

function loadCurrent() {
  return Promise.all([
    fetchJson('/data/current.json', { cache: 'no-cache' }),
    fetchJson('/data/summary.json', { cache: 'no-cache' }),
  ])
    .then(([current, summary]) => {
      updatedElement.textContent = `Updated ${new Date(current.generated_at).toLocaleString()}`;
      repeatersElement.innerHTML = current.repeaters.map(card).join('');
      statusElement.textContent = current.repeaters.length
        ? ''
        : 'No repeater data has been published yet.';
      state.repeaters = summary.repeaters;
      state.availableDays = new Set(summary.days.map((day) => day.day));
      return loadHistory();
    })
    .catch((error) => {
      updatedElement.textContent = 'Telemetry unavailable';
      statusElement.textContent = error.message;
      chartStatusElement.textContent = 'History unavailable.';
    });
}

readUrl();
renderMetricSelector();
renderPresets();
updateRangeControls();
loadCurrent();
