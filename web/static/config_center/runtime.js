import { $, titleCase } from './ui.js';

export function createRuntime({api, setMessage, showConfigErrors, sectionForSaveError, getConfig, isDirty}) {
  let running = false;
  let clearing = false;
  let pendingSamples = [];
  const pendingWindowMs = 60000;
async function refreshStatus() {
  try {
    const response = await api('/api/status');
    const status = await response.json();
    running = status.is_running === true;
    const state = status.status || (running ? 'running' : 'stopped');
    $('side-status').textContent = titleCase(state);
    const sideDetail = $('side-detail');
    if (sideDetail) sideDetail.textContent = status.writer_error || status.fault || 'DAQ service :8081';
    $('run-state').textContent = titleCase(state);
    $('mode-pill').textContent = titleCase(status.mode || 'stopped');
    $('run-dot').className = `dot ${state === 'running' ? 'online' : state === 'buffering' ? 'warning' : state === 'faulted' ? 'fault' : ''}`;
    $('side-dot').className = `dot ${state === 'running' ? 'online' : state === 'buffering' ? 'warning' : state === 'faulted' ? 'fault' : ''}`;
    $('start-acquisition').disabled = running;
    $('stop-acquisition').disabled = !running;
    const jobResponse = await api('/api/buffer/clear');
    const job = jobResponse.ok ? await jobResponse.json() : {state: 'idle'};
    clearing = job.state === 'starting' || job.state === 'running';
    $('clear-buffer').disabled = running || clearing || Number(status.pending_batches || 0) === 0;
    $('runtime-description').textContent = state === 'faulted' ? 'Acquisition faulted.' :
      running ? `Acquisition is ${state}.` : 'Acquisition is stopped.';
    $('runtime-message').textContent = status.writer_error || status.fault ||
      (state === 'starting' ? 'Waiting for the acquisition process to report ready.' :
        state === 'faulted' ? 'The acquisition process reported a fault.' :
        state === 'running' || state === 'buffering' ? 'Samples are being acquired by the DAQ service.' :
          'Start only after verifying the device, wiring, and destination.');
    const pending = Number(status.pending_batches);
    $('summary-pending').textContent = String(status.pending_batches ?? 0);
    $('buffer-batches').textContent = String(status.pending_batches ?? 0);
    updatePendingTrend(pending);
    const bytes = Number(status.spool_bytes || status.pending_bytes || 0);
    const capacity = Number(getConfig().SPOOL_MAX_BYTES || 0);
    $('summary-spool').textContent = `${formatBytes(bytes)} local spool used`;
    $('buffer-used').textContent = `${formatBytes(bytes)} used`;
    $('buffer-capacity').textContent = `${formatBytes(capacity)} capacity`;
    $('buffer-meter').style.width = `${capacity ? Math.min(100, bytes / capacity * 100) : 0}%`;
    $('info-last-sample').textContent = status.last_sample_ns ? new Date(Number(status.last_sample_ns) / 1e6).toLocaleTimeString() : 'No recent sample';
    const mqttSelected = getConfig().DESTINATION === 'mqtt';
    $('mqtt-runtime-card').classList.toggle('hidden', !mqttSelected);
    if (mqttSelected) {
      const broker = getConfig().MQTT_BROKER ? `${getConfig().MQTT_BROKER}:${getConfig().MQTT_PORT || 8883}` : 'Not configured';
      $('mqtt-runtime-broker').textContent = broker;
      $('mqtt-runtime-qos').textContent = `QoS ${getConfig().MQTT_PRODUCTION_QOS ?? 1}`;
      const delivery = status.mqtt_delivery || {};
      const completedAt = delivery.last_completed_ns ? new Date(Number(delivery.last_completed_ns) / 1e6).toLocaleString() : null;
      $('mqtt-runtime-state').textContent = delivery.state === 'error' ? `Delivery error: ${status.writer_error || 'unknown error'}` :
        delivery.state === 'completed' ? `Last send completed ${completedAt}` :
        delivery.state === 'pending' ? 'Pending first delivery' :
        delivery.state === 'idle' ? 'No completed delivery this run' :
        delivery.state === 'stopped' ? (completedAt ? `Stopped; last send completed ${completedAt}` : 'Stopped; no completed delivery this run') :
        'Delivery status unavailable';
      const gaps = Array.isArray(status.gaps) ? status.gaps.slice(0, 5) : [];
      $('mqtt-runtime-gaps').textContent = String(gaps.length);
      const gapList = $('mqtt-gap-list');
      gapList.replaceChildren();
      if (!gaps.length) {
        const empty = document.createElement('li');
        empty.textContent = 'No recent local gaps.';
        gapList.append(empty);
      } else {
        gaps.forEach((gap) => {
          const item = document.createElement('li');
          const start = Number(gap.start_ns);
          const end = gap.end_ns === null || gap.end_ns === undefined ? null : Number(gap.end_ns);
          const interval = document.createElement('strong');
          interval.textContent = `${Number.isFinite(start) ? new Date(start / 1e6).toLocaleString() : 'Unknown start'} – ${end === null ? 'open' : Number.isFinite(end) ? new Date(end / 1e6).toLocaleString() : 'Unknown end'}`;
          const cause = document.createElement('small');
          cause.textContent = String(gap.cause || 'Unspecified');
          item.append(interval, cause);
          gapList.append(item);
        });
      }
      const cutovers = Array.isArray(status.destination_cutovers) ? status.destination_cutovers.slice(0, 5) : [];
      const cutoverList = $('mqtt-cutover-list');
      cutoverList.replaceChildren();
      if (!cutovers.length) {
        const empty = document.createElement('li');
        empty.textContent = 'No destination changes recorded.';
        cutoverList.append(empty);
      } else {
        cutovers.forEach((cutover) => {
          const item = document.createElement('li');
          const time = document.createElement('time');
          const timestamp = Number(cutover.time_ns);
          time.textContent = Number.isFinite(timestamp) ? new Date(timestamp / 1e6).toLocaleString() : String(cutover.time || '');
          const route = document.createElement('strong');
          route.textContent = `${cutover.from || 'Unknown'} → ${cutover.to || 'Unknown'}`;
          const pendingAtCutover = document.createElement('small');
          pendingAtCutover.textContent = `${Number(cutover.pending_records || 0)} record(s) pending at cutover`;
          item.append(time, route, pendingAtCutover);
          cutoverList.append(item);
        });
      }
    }
    if (!response.ok && status.fault) throw new Error(status.fault);
  } catch (error) {
    pendingSamples = [];
    setPendingTrend('↑ —', '↓ —', 'Rate unavailable');
    running = false;
    $('side-status').textContent = 'Service unavailable';
    const sideDetail = $('side-detail');
    if (sideDetail) sideDetail.textContent = 'Could not reach DAQ API';
    $('run-state').textContent = 'API unavailable';
    $('run-dot').className = 'dot fault';
    $('side-dot').className = 'dot fault';
    $('start-acquisition').disabled = true;
    $('stop-acquisition').disabled = true;
    $('clear-buffer').disabled = true;
    clearing = false;
    $('runtime-description').textContent = 'DAQ service unavailable.';
    $('runtime-message').textContent = 'Check the DAQ service before controlling acquisition.';
    $('mqtt-runtime-state').textContent = 'Delivery status unavailable';
  }
}
function pendingTrendElements() {
  const card = $('summary-pending')?.closest('.metric-card');
  if (!card) return null;
  card.classList.add('pending-card');
  const rise = $('pending-rise');
  const fall = $('pending-fall');
  const net = $('pending-net');
  if (rise && fall && net) return {rise, fall, net};

  // The server may still serve a cached template after the static script updates.
  const trend = card.querySelector('.pending-trend') || document.createElement('div');
  trend.className = 'pending-trend';
  trend.setAttribute('aria-live', 'polite');
  const rates = document.createElement('div');
  rates.className = 'pending-rates';
  const increase = document.createElement('span');
  increase.id = 'pending-rise';
  increase.className = 'pending-rise';
  increase.title = 'Increase rate';
  const decrease = document.createElement('span');
  decrease.id = 'pending-fall';
  decrease.className = 'pending-fall';
  decrease.title = 'Decrease rate';
  const summary = document.createElement('small');
  summary.id = 'pending-net';
  rates.append(increase, decrease);
  trend.replaceChildren(rates, summary);
  card.append(trend);
  return {rise: increase, fall: decrease, net: summary};
}
function setPendingTrend(rise, fall, net) {
  const elements = pendingTrendElements();
  if (!elements) return;
  elements.rise.textContent = rise;
  elements.fall.textContent = fall;
  elements.net.textContent = net;
}
function formatBatchRate(rate) {
  return new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(rate);
}
function updatePendingTrend(pending) {
  if (!Number.isSafeInteger(pending) || pending < 0) {
    pendingSamples = [];
    setPendingTrend('↑ —', '↓ —', 'Rate unavailable');
    return;
  }
  const now = performance.now();
  if (pendingSamples.length && now - pendingSamples[pendingSamples.length - 1].time > pendingWindowMs) pendingSamples = [];
  pendingSamples.push({time: now, count: pending});
  const cutoff = now - pendingWindowMs;
  while (pendingSamples.length > 1 && pendingSamples[1].time <= cutoff) pendingSamples.shift();
  if (pendingSamples.length < 2 || now === pendingSamples[0].time) {
    setPendingTrend('↑ —', '↓ —', 'Waiting for next reading');
    return;
  }
  let rise = 0;
  let fall = 0;
  for (let i = 1; i < pendingSamples.length; i++) {
    const previous = pendingSamples[i - 1];
    const current = pendingSamples[i];
    const duration = current.time - previous.time;
    if (duration <= 0) continue;
    const fraction = (current.time - Math.max(previous.time, cutoff)) / duration;
    const change = (current.count - previous.count) * fraction;
    if (change > 0) rise += change;
    else fall -= change;
  }
  const seconds = Math.min(pendingWindowMs, now - pendingSamples[0].time) / 1000;
  const factor = 60 / seconds;
  const net = (rise - fall) * factor;
  setPendingTrend(`↑ ${formatBatchRate(rise * factor)}/min`, `↓ ${formatBatchRate(fall * factor)}/min`, `Net ${net > 0 ? '+' : ''}${formatBatchRate(net)}/min · ${Math.round(seconds)}s observed`);
}
function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** power)).toFixed(power ? 1 : 0)} ${units[power]}`;
}
async function runAcquisition() {
  if (isDirty()) {
    setMessage('');
    showConfigErrors([{section: 'runtime', message: 'Save the current configuration before starting acquisition.'}], true);
    return;
  }
  const warning = 'Start physical production acquisition with the saved hardware configuration? Verify signal wiring and the destination first.';
  if (!window.confirm(warning)) return;
  $('start-acquisition').disabled = true;
  try {
    const response = await api('/api/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: 'production'})});
    const result = await response.json();
    if (!response.ok || !result.started) throw new Error(result.message || 'Acquisition did not start.');
    showConfigErrors([]);
    setMessage('Acquisition process launched. Waiting for the device to report running status.', 'success');
    await refreshStatus();
  } catch (error) {
    setMessage('');
    showConfigErrors([{section: sectionForSaveError(error.message), message: error.message}], true);
    await refreshStatus();
  }
}
async function stopAcquisition() {
  if (!window.confirm('Stop the active DAQ acquisition?')) return;
  $('stop-acquisition').disabled = true;
  try {
    const response = await api('/api/stop', {method: 'POST'});
    const result = await response.json();
    if (!response.ok || !result.stopped) throw new Error(result.message || 'DAQ did not stop cleanly.');
    setMessage(result.pending_replay ? `Acquisition stopped; ${result.pending_batches} batch(es) remain queued for delivery.` : 'Acquisition stopped and the local queue is drained.', result.pending_replay ? 'warning' : 'success');
    await refreshStatus();
  } catch (error) { setMessage(error.message, 'error'); await refreshStatus(); }
}
async function clearBuffer() {
  try {
    const statusResponse = await api('/api/status');
    const status = await statusResponse.json();
    if (status.is_running) throw new Error('Stop acquisition before clearing the buffer.');
    const batches = Number(status.pending_batches || 0);
    if (!batches) { setMessage('The local buffer is already empty.', 'success'); await refreshStatus(); return; }
    const size = formatBytes(Number(status.pending_bytes || 0));
    if (!window.confirm(`Permanently discard ${batches} pending production batches (${size})? They will not be delivered to TimescaleDB. Discarded sample intervals will be recorded as gaps.`)) return;
    $('clear-buffer').disabled = true;
    const response = await api('/api/buffer/clear', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: 'CLEAR BUFFER'})});
    let result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Could not clear pending data.');
    clearing = result.state === 'starting' || result.state === 'running';
    while (clearing) {
      setMessage('Clearing pending data. The DAQ service remains available during this operation.', 'warning');
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const jobResponse = await api('/api/buffer/clear');
      result = await jobResponse.json();
      if (!jobResponse.ok || result.state === 'failed') throw new Error(result.message || 'Could not clear pending data.');
      clearing = result.state === 'starting' || result.state === 'running';
    }
    setMessage(`Cleared ${result.cleared_batches} pending batch(es) and recorded ${result.recorded_gaps} gap(s).`, 'success');
    await refreshStatus();
  } catch (error) { setMessage(error.message, 'error'); await refreshStatus(); }
}

$('start-acquisition').addEventListener('click', runAcquisition);
$('stop-acquisition').addEventListener('click', stopAcquisition);
$('clear-buffer').addEventListener('click', clearBuffer);
  setPendingTrend('↑ —', '↓ —', 'Waiting for next reading');
  return {refreshStatus, isRunning: () => running};
}
