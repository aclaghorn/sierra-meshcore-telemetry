const repeatersElement = document.querySelector('#repeaters');
const statusElement = document.querySelector('#status');
const updatedElement = document.querySelector('#updated');
const chartStatusElement = document.querySelector('#chart-status');
const rangeLabelElement = document.querySelector('#range-label');
const previousButton = document.querySelector('#previous-range');
const nextButton = document.querySelector('#next-range');
const rangeButtons = [...document.querySelectorAll('[data-range]')];

const DAY_SECONDS = 86400;
const SVG_NS = 'http://www.w3.org/2000/svg';
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
];

const state = {
  range: 'day',
  anchor: utcDay(new Date()),
  availableDays: new Set(),
  firstDay: null,
  repeaters: [],
  request: 0,
};

function value(number, suffix = '', digits = 1) {
  return number == null ? '—' : `${Number(number).toFixed(digits)}${suffix}`;
}

function card(repeater) {
  const online = repeater.online ? 'Online' : 'No recent response';
  return `
    <article class="card">
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

function escapeHtml(text) {
  const element = document.createElement('span');
  element.textContent = text;
  return element.innerHTML;
}

function utcDay(date) {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
}

function addUtcDays(date, days) {
  const result = new Date(date);
  result.setUTCDate(result.getUTCDate() + days);
  return result;
}

function rangeFor(type, anchor) {
  let start;
  let end;
  if (type === 'week') {
    const mondayOffset = (anchor.getUTCDay() + 6) % 7;
    start = addUtcDays(anchor, -mondayOffset);
    end = addUtcDays(start, 7);
  } else if (type === 'month') {
    start = new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1));
    end = new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + 1, 1));
  } else {
    start = utcDay(anchor);
    end = addUtcDays(start, 1);
  }
  return { start, end };
}

function shiftAnchor(type, anchor, direction) {
  if (type === 'month') {
    return new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + direction, 1));
  }
  return addUtcDays(anchor, direction * (type === 'week' ? 7 : 1));
}

function isoDay(date) {
  return date.toISOString().slice(0, 10);
}

function daysInRange(start, end) {
  const days = [];
  for (let day = start; day < end; day = addUtcDays(day, 1)) {
    days.push(isoDay(day));
  }
  return days;
}

function formatRange({ start, end }) {
  const last = new Date(end.getTime() - 1);
  const dayFormat = new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
  if (state.range === 'day') return `${dayFormat.format(start)} UTC`;
  if (state.range === 'month') {
    return new Intl.DateTimeFormat(undefined, {
      month: 'long',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(start);
  }
  return `${dayFormat.format(start)} – ${dayFormat.format(last)} UTC`;
}

function setNavigation(range) {
  const today = utcDay(new Date());
  const nextRange = rangeFor(state.range, shiftAnchor(state.range, state.anchor, 1));
  nextButton.disabled = nextRange.start > today;
  previousButton.disabled = state.firstDay
    ? addUtcDays(range.start, -1) < state.firstDay
    : true;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

async function loadHistory() {
  const request = ++state.request;
  const range = rangeFor(state.range, state.anchor);
  rangeLabelElement.textContent = formatRange(range);
  setNavigation(range);
  chartStatusElement.textContent = 'Loading battery history…';

  const days = daysInRange(range.start, range.end).filter((day) =>
    state.availableDays.has(day),
  );
  try {
    const documents = await Promise.all(
      days.map((day) => fetchJson(`/data/days/${day}.json.gz`)),
    );
    if (request !== state.request) return;
    const readings = documents
      .flatMap((document) => document.readings)
      .filter((reading) =>
        reading.ts >= range.start.getTime() / 1000 &&
        reading.ts < range.end.getTime() / 1000
      );

    chartStatusElement.textContent = readings.length
      ? ''
      : 'No battery readings are available in this range.';
    renderChart(
      document.querySelector('#charge-chart'),
      readings,
      'battery_percent',
      range,
      { suffix: '%', digits: 0, min: 0, max: 100 },
    );
  } catch (error) {
    if (request !== state.request) return;
    chartStatusElement.textContent = `Battery history unavailable: ${error.message}`;
    document.querySelector('#charge-chart').replaceChildren();
  }
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, value);
  }
  return element;
}

function renderChart(container, readings, field, range, options) {
  container.replaceChildren();
  const points = readings.filter((reading) => reading[field] != null);
  if (!points.length) {
    const empty = document.createElement('p');
    empty.className = 'chart-empty';
    empty.textContent = 'No data';
    container.append(empty);
    return;
  }

  const width = 960;
  const height = 340;
  const plot = { left: 60, right: 20, top: 20, bottom: 52 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const xMin = range.start.getTime() / 1000;
  const xMax = range.end.getTime() / 1000;
  const values = points.map((point) => Number(point[field]));
  let yMin = options.min ?? Math.min(...values);
  let yMax = options.max ?? Math.max(...values);
  if (options.min == null || options.max == null) {
    const padding = Math.max((yMax - yMin) * 0.12, field === 'battery_voltage' ? 0.03 : 1);
    yMin = Math.max(0, yMin - padding);
    yMax += padding;
  }

  const x = (timestamp) => plot.left + ((timestamp - xMin) / (xMax - xMin)) * plotWidth;
  const y = (number) => plot.top + (1 - (number - yMin) / (yMax - yMin)) * plotHeight;
  const svg = svgElement('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': 'Battery charge chart',
  });

  const grid = svgElement('g', { class: 'chart-grid' });
  for (let index = 0; index <= 4; index += 1) {
    const gridY = plot.top + (plotHeight * index) / 4;
    grid.append(svgElement('line', {
      x1: plot.left,
      x2: width - plot.right,
      y1: gridY,
      y2: gridY,
    }));
    const label = svgElement('text', {
      x: plot.left - 10,
      y: gridY + 4,
      'text-anchor': 'end',
    });
    label.textContent = value(yMax - ((yMax - yMin) * index) / 4, options.suffix, options.digits);
    grid.append(label);
  }
  for (let index = 0; index <= 4; index += 1) {
    const timestamp = xMin + ((xMax - xMin) * index) / 4;
    const gridX = x(timestamp);
    grid.append(svgElement('line', {
      x1: gridX,
      x2: gridX,
      y1: plot.top,
      y2: height - plot.bottom,
    }));
    const label = svgElement('text', {
      x: gridX,
      y: height - 22,
      'text-anchor': index === 0 ? 'start' : index === 4 ? 'end' : 'middle',
    });
    label.textContent = formatAxisTime(new Date(timestamp * 1000), state.range);
    grid.append(label);
  }
  svg.append(grid);

  const grouped = new Map();
  for (const point of points) {
    const series = grouped.get(point.id) ?? [];
    series.push(point);
    grouped.set(point.id, series);
  }
  [...grouped.entries()].forEach(([id, series], index) => {
    series.sort((a, b) => a.ts - b.ts);
    const path = [];
    let previous = null;
    for (const point of series) {
      const command = previous == null || point.ts - previous > 3600 ? 'M' : 'L';
      path.push(`${command}${x(point.ts).toFixed(2)},${y(Number(point[field])).toFixed(2)}`);
      previous = point.ts;
    }
    const color = colorFor(id, index);
    const line = svgElement('path', {
      d: path.join(' '),
      fill: 'none',
      stroke: color,
      'stroke-width': '2.5',
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
    });
    const title = svgElement('title');
    title.textContent = series[0].name;
    line.append(title);
    svg.append(line);
  });
  container.append(svg, buildLegend(grouped, field, options));
}

function colorFor(id, fallback) {
  let hash = 0;
  for (const character of id) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  return COLORS[Math.abs(hash || fallback) % COLORS.length];
}

function buildLegend(grouped, field, options) {
  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  [...grouped.entries()]
    .sort((left, right) => left[1][0].name.localeCompare(right[1][0].name))
    .forEach(([id, series], index) => {
      const item = document.createElement('div');
      const marker = document.createElement('span');
      marker.style.background = colorFor(id, index);
      const name = document.createElement('span');
      name.textContent = series[0].name;
      const latest = document.createElement('strong');
      latest.textContent = value(series.at(-1)[field], options.suffix, options.digits);
      item.append(marker, name, latest);
      legend.append(item);
    });
  return legend;
}

function formatAxisTime(date, range) {
  if (range === 'day') {
    return new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      timeZone: 'UTC',
    }).format(date);
  }
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(date);
}

rangeButtons.forEach((button) => {
  button.addEventListener('click', () => {
    state.range = button.dataset.range;
    state.anchor = utcDay(new Date());
    rangeButtons.forEach((candidate) =>
      candidate.setAttribute('aria-pressed', candidate === button ? 'true' : 'false'),
    );
    loadHistory();
  });
});

previousButton.addEventListener('click', () => {
  state.anchor = shiftAnchor(state.range, state.anchor, -1);
  loadHistory();
});

nextButton.addEventListener('click', () => {
  state.anchor = shiftAnchor(state.range, state.anchor, 1);
  loadHistory();
});

Promise.all([
  fetchJson('/data/current.json'),
  fetchJson('/data/summary.json'),
])
  .then(([current, summary]) => {
    updatedElement.textContent = `Updated ${new Date(current.generated_at).toLocaleString()}`;
    repeatersElement.innerHTML = current.repeaters.map(card).join('');
    if (!current.repeaters.length) {
      statusElement.textContent = 'No repeater data has been published yet.';
    }
    state.repeaters = summary.repeaters;
    state.availableDays = new Set(summary.days.map((day) => day.day));
    state.firstDay = summary.days.length
      ? new Date(`${summary.days[0].day}T00:00:00Z`)
      : null;
    loadHistory();
  })
  .catch((error) => {
    updatedElement.textContent = 'Telemetry unavailable';
    statusElement.textContent = error.message;
    chartStatusElement.textContent = 'Battery history unavailable.';
  });
