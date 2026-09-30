import { $ } from './ui.js';

export function createStreamPreview({api}) {
  let paused = false;
  let refreshing = false;

  for (let channel = 0; channel < 16; channel++) {
    const option = document.createElement('option');
    option.value = String(channel);
    option.textContent = `AI${channel}`;
    $('stream-channel').append(option);
    const graphOption = document.createElement('option');
    graphOption.value = String(channel);
    graphOption.textContent = `AI${channel}`;
    $('stream-graph-channel').append(graphOption);
  }

  function setState(text, active = false) {
    const state = $('stream-state');
    state.textContent = text;
    state.classList.toggle('active', active);
  }

  function formatNumber(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    return new Intl.NumberFormat(undefined, {maximumFractionDigits: 6}).format(Number(value));
  }

  function formatTime(timeNs) {
    const value = Number(timeNs);
    return Number.isFinite(value) ? new Date(value / 1e6).toLocaleString() : '—';
  }

  function svgNode(name, attributes = {}, text = '') {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text) node.textContent = text;
    return node;
  }

  function clearChart(message) {
    $('stream-chart').replaceChildren();
    $('stream-chart-empty').textContent = message;
    $('stream-chart-empty').classList.remove('hidden');
    $('stream-chart-caption').textContent = 'Waiting for channel data';
    $('stream-chart-range').textContent = '—';
    $('stream-chart-latest').textContent = '—';
  }

  function renderChart(samples, range, checkedAtNs) {
    const key = $('stream-measure').value;
    const label = key === 'raw_voltage' ? 'Raw voltage' : 'Calibrated output';
    const averageKey = `${key}_avg`;
    const minKey = `${key}_min`;
    const maxKey = `${key}_max`;
    const usable = samples
      .filter((sample) => Number.isFinite(Number(sample.time_ns))
        && Number.isFinite(Number(sample[averageKey]))
        && Number.isFinite(Number(sample[minKey]))
        && Number.isFinite(Number(sample[maxKey])))
      .slice()
      .sort((a, b) => Number(a.time_ns) - Number(b.time_ns));
    const svg = $('stream-chart');
    if (!usable.length) {
      clearChart('No sample history is available for this channel yet.');
      return;
    }

    $('stream-chart-empty').classList.add('hidden');
    svg.replaceChildren();
    const width = 960;
    const height = 300;
    const plot = {left: 74, right: 22, top: 18, bottom: 42};
    const plotWidth = width - plot.left - plot.right;
    const plotHeight = height - plot.top - plot.bottom;
    let min = Math.min(...usable.map((sample) => Number(sample[minKey])));
    let max = Math.max(...usable.map((sample) => Number(sample[maxKey])));
    if (min === max) {
      const padding = Math.max(Math.abs(min) * 0.05, 1);
      min -= padding;
      max += padding;
    } else {
      const padding = (max - min) * 0.08;
      min -= padding;
      max += padding;
    }
    const lastNs = Number(checkedAtNs || usable[usable.length - 1].time_ns);
    const timeSpan = (range === '5m' ? 300 : 60) * 1_000_000_000;
    const firstNs = lastNs - timeSpan;
    const x = (timestamp) => plot.left + Math.max(0, Math.min(1, (Number(timestamp) - firstNs) / timeSpan)) * plotWidth;
    const y = (value) => plot.top + ((max - value) / (max - min)) * plotHeight;
    const unit = key === 'raw_voltage' ? 'V' : (usable.at(-1).unit || '');

    for (let tick = 0; tick <= 4; tick++) {
      const ratio = tick / 4;
      const tickY = plot.top + ratio * plotHeight;
      const tickValue = max - ratio * (max - min);
      svg.append(
        svgNode('line', {x1: plot.left, y1: tickY, x2: width - plot.right, y2: tickY, class: 'chart-grid-line'}),
        svgNode('text', {x: plot.left - 10, y: tickY + 4, class: 'chart-axis-label', 'text-anchor': 'end'}, formatNumber(tickValue)),
      );
    }
    svg.append(
      svgNode('line', {x1: plot.left, y1: plot.top, x2: plot.left, y2: height - plot.bottom, class: 'chart-axis-line'}),
      svgNode('line', {x1: plot.left, y1: height - plot.bottom, x2: width - plot.right, y2: height - plot.bottom, class: 'chart-axis-line'}),
    );
    usable.forEach((sample) => {
      const sampleX = x(sample.time_ns);
      svg.append(svgNode('line', {
        x1: sampleX, y1: y(Number(sample[minKey])),
        x2: sampleX, y2: y(Number(sample[maxKey])), class: 'chart-envelope',
      }));
    });
    let previousNs = null;
    const path = usable.map((sample) => {
      const timestamp = Number(sample.time_ns);
      const command = previousNs === null || timestamp - previousNs > 2_000_000_000 ? 'M' : 'L';
      previousNs = timestamp;
      return `${command}${x(timestamp).toFixed(2)},${y(Number(sample[averageKey])).toFixed(2)}`;
    }).join(' ');
    svg.append(svgNode('path', {d: path, class: 'chart-series'}));
    [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
      const tickX = plot.left + ratio * plotWidth;
      const elapsedSeconds = Math.round((ratio - 1) * timeSpan / 1e9);
      const caption = elapsedSeconds === 0 ? 'now' : `${elapsedSeconds}s`;
      svg.append(svgNode('text', {x: tickX, y: height - 14, class: 'chart-axis-label', 'text-anchor': 'middle'}, caption));
    });
    const latest = usable.at(-1);
    const sensor = latest.sensor_name ? ` · ${latest.sensor_name}` : '';
    $('stream-chart-title').textContent = `${label} · AI${latest.channel}`;
    const sampleCount = usable.reduce((total, sample) => total + Number(sample.sample_count || 0), 0);
    $('stream-chart-caption').textContent = `${usable.length} one-second bins · ${formatNumber(sampleCount)} samples${sensor}${unit ? ` · ${unit}` : ''}`;
    $('stream-chart-range').textContent = `${range === '5m' ? '5 minutes' : '1 minute'} ending ${formatTime(lastNs)}`;
    $('stream-chart-latest').textContent = `Latest avg ${formatNumber(latest[averageKey])}${unit ? ` ${unit}` : ''}`;
  }

  function render(samples) {
    const body = $('stream-table-body');
    body.replaceChildren();
    if (!samples.length) {
      const row = document.createElement('tr');
      const empty = document.createElement('td');
      empty.colSpan = 7;
      empty.className = 'empty-table';
      empty.textContent = 'No sample preview for this channel yet.';
      row.append(empty);
      body.append(row);
      return;
    }

    samples.forEach((sample) => {
      const row = document.createElement('tr');
      const values = [
        formatTime(sample.time_ns),
        `AI${sample.channel}`,
        sample.sensor_name || '—',
        formatNumber(sample.raw_voltage),
        formatNumber(sample.calibrated_value),
        sample.unit || '—',
        sample.sample_id || '—',
      ];
      values.forEach((value, index) => {
        const cell = document.createElement('td');
        cell.textContent = String(value);
        if (index === 6) cell.className = 'sample-id-cell';
        row.append(cell);
      });
      body.append(row);
    });
  }

  async function refresh() {
    if (paused || refreshing || document.hidden) return;
    refreshing = true;
    try {
      const channel = $('stream-channel').value;
      const response = await api(`/api/preview?channel=${encodeURIComponent(channel)}&limit=500`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'Could not read sample preview.');
      render(Array.isArray(result.samples) ? result.samples : []);
      const graphChoice = $('stream-graph-channel').value;
      const graphChannel = graphChoice === 'auto'
        ? result.samples?.[0]?.channel
        : graphChoice;
      if (graphChannel === undefined || graphChannel === null) {
        clearChart('Waiting for the first committed sample.');
      } else {
        const range = $('stream-range').value;
        const graphResponse = await api(`/api/preview?channel=${encodeURIComponent(graphChannel)}&range=${range}`);
        const graphResult = await graphResponse.json();
        if (!graphResponse.ok) throw new Error(graphResult.message || 'Could not read graph history.');
        renderChart(Array.isArray(graphResult.history) ? graphResult.history : [], range,
          graphResult.history_checked_at_ns || graphResult.checked_at_ns);
      }
      $('stream-destination').textContent = String(result.destination || '—').toUpperCase();
      $('stream-updated').textContent = result.checked_at_ns
        ? `Snapshot ${formatTime(result.checked_at_ns)}`
        : 'Waiting for acquisition';
      if (result.acquisition_running) setState('Live', true);
      else if (result.samples?.length) setState('Last session');
      else setState('Waiting for samples');
    } catch (error) {
      setState('Preview unavailable');
      $('stream-updated').textContent = error.message;
    } finally {
      refreshing = false;
    }
  }

  $('stream-channel').addEventListener('change', refresh);
  $('stream-graph-channel').addEventListener('change', refresh);
  $('stream-measure').addEventListener('change', refresh);
  $('stream-range').addEventListener('change', refresh);
  $('stream-pause').addEventListener('click', () => {
    paused = !paused;
    const button = $('stream-pause');
    button.textContent = paused ? 'Resume view' : 'Pause view';
    button.setAttribute('aria-pressed', String(paused));
    if (!paused) refresh();
  });
  refresh();
  return {refresh};
}
