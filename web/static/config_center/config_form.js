import { $, titleCase } from './ui.js';

export function createConfigForm({api, setMessage, showConfigErrors, sectionForSaveError, isRunning, refreshStatus}) {
const fields = ['DEVICE_DESCRIPTION', 'DEVICE_ID', 'PROFILE_PATH', 'START_CHANNEL', 'CHANNEL_COUNT', 'CLOCK_RATE', 'SECTION_LENGTH', 'SECTION_COUNT', 'DESTINATION', 'DB_PRODUCTION_TABLE', 'DB_CONNECTION_MODE', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_DSN', 'DB_RETENTION_DAYS', 'SPOOL_MAX_BYTES', 'INFLUX_URL', 'INFLUX_ORG', 'INFLUX_BUCKET', 'INFLUX_TOKEN', 'INFLUX_MEASUREMENT', 'MQTT_BROKER', 'MQTT_PORT', 'MQTT_PRODUCTION_TOPIC_PREFIX', 'MQTT_PRODUCTION_QOS', 'MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_CA_CERTS', 'MQTT_CLIENT_CERT', 'MQTT_CLIENT_KEY'];
const scaleIds = ['scale-low-voltage', 'scale-high-voltage', 'scale-low-value', 'scale-high-value'];
let config = {};
let channels = {};
let selectedChannel = 0;
let dirty = false;
let manualDsn = false;
const defaultChannel = () => ({enabled: false, label: '', unit: '', signal_type: 'SingleEnded', value_range: 'V_0To5', scale: {enabled: false, low_voltage: 0, high_voltage: 5, low_value: 0, high_value: 100}, counter: {enabled: false, direction: 'up', threshold: 0, interval_seconds: 1}});
function setDirty(value = true) {
  dirty = value;
  $('last-saved').textContent = dirty ? 'Unsaved changes' : 'All changes saved';
  $('last-saved').classList.toggle('unsaved', dirty);
  $('save-top').disabled = !dirty;
  if (dirty && $('page-message').classList.contains('success')) setMessage('');
}
function value(id) { return $(id).value.trim(); }

async function loadConfig() {
  $('last-saved').textContent = 'Loading configuration…';
  const response = await api('/api/config');
  if (!response.ok) throw new Error(`DAQ config API returned HTTP ${response.status}`);
  config = await response.json();
  channels = structuredClone(config.CHANNELS || {});
  const secretIds = ['DB_PASSWORD', 'INFLUX_TOKEN', 'MQTT_PASSWORD'];
  fields.forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (secretIds.includes(id)) {
      const hasSecret = Boolean(config[id] && config[id] !== '');
      el.value = '';
      el.dataset.hasSaved = hasSecret ? 'true' : 'false';
      el.placeholder = hasSecret ? 'Saved secret unchanged (leave blank to keep)' : (id === 'MQTT_PASSWORD' ? 'Broker password' : 'Enter secret');
    } else if (config[id] !== undefined) {
      el.value = config[id];
    } else if (id === 'MQTT_PRODUCTION_TOPIC_PREFIX') {
      el.value = 'daq/production/v1';
    } else if (id === 'MQTT_PRODUCTION_QOS') {
      el.value = '1';
    }
  });
  $('AUTO_START_ON_STARTUP').checked = config.AUTO_START_ON_STARTUP === true;
  $('MQTT_TLS_ENABLED').checked = config.MQTT_TLS_ENABLED === true;
  // The server determines the effective mode from the unredacted saved config.
  manualDsn = config.DB_CONNECTION_MODE === 'dsn';
  $('DB_CONNECTION_MODE').value = config.DB_CONNECTION_MODE;
  selectedChannel = Number.isInteger(Number(config.START_CHANNEL)) ? Number(config.START_CHANNEL) : 0;
  renderChannels();
  renderChannelEditor();
  updateSummary();
  updateDestinationFields();
  setDirty(false);
  showConfigErrors([]);
  $('config-state-copy').textContent = `Saved configuration loaded from ${config.DEVICE_DESCRIPTION || 'DAQ service'}.`;
}

function channelSpan() {
  const start = Number($('START_CHANNEL').value);
  const count = Number($('CHANNEL_COUNT').value);
  return {start, end: start + count, valid: $('START_CHANNEL').value !== '' && $('CHANNEL_COUNT').value !== '' && Number.isInteger(start) && Number.isInteger(count) && start >= 0 && count >= 1 && start + count <= 16};
}

function renderChannels() {
  const holder = $('channel-table-body');
  const {start, end, valid} = channelSpan();
  $('CHANNEL_COUNT').max = String(Number.isInteger(start) && start >= 0 && start < 16 ? 16 - start : 16);
  holder.replaceChildren();
  $('channel-span-note').textContent = valid ? `Reading AI${start}–AI${end - 1} (${end - start} hardware inputs). Enable the inputs connected to sensors.` : 'Enter a valid start channel and number of AI channels above (AI0–AI15).';
  const shown = valid ? Array.from({length: end - start}, (_, offset) => start + offset) : [];
  for (let index = 0; index < 16; index += 1) {
    if (channels[String(index)]?.enabled && (!valid || index < start || index >= end)) shown.push(index);
  }
  for (const index of shown) {
    const channel = channels[String(index)] ||= defaultChannel();
    const outside = !valid || index < start || index >= end;
    const row = document.createElement('tr');
    row.className = `${index === selectedChannel ? 'selected-row ' : ''}${outside ? 'outside-span' : ''}`;
    row.innerHTML = `<td><button type="button" class="table-link">AI${index}</button><span class="row-warning"></span></td><td><input class="row-enabled" type="checkbox" aria-label="Enable AI${index}"></td><td><input class="row-label" type="text" maxlength="120" aria-label="AI${index} sensor label" placeholder="Sensor name"></td><td><input class="row-unit" type="text" maxlength="32" aria-label="AI${index} engineering unit" placeholder="e.g. kPa"></td><td><select class="row-signal" aria-label="AI${index} signal type"><option value="SingleEnded">Single ended</option><option value="Differential">Differential</option><option value="PseudoDifferential">Pseudo differential</option></select></td><td><select class="row-range" aria-label="AI${index} input range"><option value="V_0To5">0 to 5 V</option><option value="V_0To10">0 to 10 V</option><option value="V_Neg5To5">−5 to +5 V</option><option value="V_Neg10To10">−10 to +10 V</option><option value="V_Neg12To12">−12 to +12 V</option></select></td><td><div class="row-calibration-control"><label class="toggle"><input class="row-scale-enabled" type="checkbox" aria-label="Apply calibration for AI${index}"><span class="toggle-track"></span><span class="row-scale-state"></span></label><button type="button" class="button button-quiet row-calibrate">Edit</button></div></td>`;
    row.querySelector('.row-warning').textContent = outside ? 'Outside span' : '';
    const enabled = row.querySelector('.row-enabled');
    const label = row.querySelector('.row-label');
    const unit = row.querySelector('.row-unit');
    const signal = row.querySelector('.row-signal');
    const range = row.querySelector('.row-range');
    const scaleEnabled = row.querySelector('.row-scale-enabled');
    const scaleState = row.querySelector('.row-scale-state');
    enabled.checked = channel.enabled === true;
    label.value = channel.label || '';
    unit.value = channel.unit || '';
    signal.value = channel.signal_type || 'SingleEnded';
    range.value = channel.value_range || 'V_0To5';
    scaleEnabled.checked = channel.scale?.enabled === true;
    scaleState.textContent = scaleEnabled.checked ? 'On' : 'Off';
    signal.querySelector('option[value="PseudoDifferential"]').disabled = /^PCI-1716(?:H|L)?(?:,|$)/i.test(value('DEVICE_DESCRIPTION'));
    enabled.addEventListener('change', () => {
      channel.enabled = enabled.checked;
      markChanged();
      updateSignalHint();
      if (outside) {
        if (!channel.enabled && selectedChannel === index && valid) selectedChannel = start;
        renderChannels();
        renderChannelEditor();
      }
    });
    label.addEventListener('input', () => { channel.label = label.value; markChanged(); if (selectedChannel === index) renderChannelEditorTitle(); });
    unit.addEventListener('input', () => { channel.unit = unit.value; markChanged(); });
    signal.addEventListener('change', () => { channel.signal_type = signal.value; markChanged(); if (selectedChannel === index) updateSignalHint(); });
    range.addEventListener('change', () => { channel.value_range = range.value; markChanged(); });
    scaleEnabled.addEventListener('change', () => {
      channel.scale ||= defaultChannel().scale;
      channel.scale.enabled = scaleEnabled.checked;
      scaleState.textContent = scaleEnabled.checked ? 'On' : 'Off';
      if (selectedChannel === index) updateCalibrationStatus();
      markChanged();
    });
    const select = () => {
      pullChannelEditor();
      selectedChannel = index;
      renderChannels();
      renderChannelEditor();
      $('channel-calibration').scrollIntoView({behavior: 'smooth', block: 'center'});
    };
    row.querySelector('.row-calibrate').addEventListener('click', select);
    row.querySelector('.table-link').addEventListener('click', select);
    holder.append(row);
  }
  if (!shown.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td colspan="7" class="empty-table">Enter a valid channel start and count to show the hardware inputs.</td>';
    holder.append(row);
  }
  updateSummary();
}

function renderChannelEditor() {
  const channel = channels[String(selectedChannel)] || defaultChannel();
  const scale = channel.scale || defaultChannel().scale;
  renderChannelEditorTitle();
  $('scale-low-voltage').value = scale.low_voltage ?? '';
  $('scale-high-voltage').value = scale.high_voltage ?? '';
  $('scale-low-value').value = scale.low_value ?? '';
  $('scale-high-value').value = scale.high_value ?? '';
  const counter = channel.counter || defaultChannel().counter;
  $('counter-enabled').checked = counter.enabled === true;
  $('counter-direction').value = counter.direction || 'up';
  $('counter-threshold').value = counter.threshold ?? 0;
  $('counter-interval').value = counter.interval_seconds ?? 1;
  updateCalibrationStatus();
  updateWiringGuide();
  updateSignalHint();
}

function renderChannelEditorTitle() {
  const channel = channels[String(selectedChannel)] || defaultChannel();
  $('channel-editor-title').textContent = `AI${selectedChannel}${channel.label ? ` · ${channel.label}` : ''}`;
}

function updateCalibrationStatus() {
  const enabled = channels[String(selectedChannel)]?.scale?.enabled === true;
  const status = $('calibration-status') || document.querySelector('.calibration-heading p');
  if (status) status.textContent = enabled ? 'Calibration is on for this channel. Edit its conversion values below.' : 'Calibration is off for this channel. Use the switch in the sensor table to apply it.';
}

function updateWiringGuide() {
  const pci1716 = /^PCI-1716(?:H|L)?(?:,|$)/i.test(value('DEVICE_DESCRIPTION'));
  $('wiring-device').textContent = pci1716 ? 'PCI-1716 input reference' : 'Check the selected DAQ device manual';
  $('wiring-single').textContent = pci1716 ? 'Connect signal to AI channel and signal return to AGND.' : 'Connect signal and return to the analog reference specified by the DAQ device.';
  $('wiring-differential').textContent = pci1716 ? 'Use an even input and the next odd input as a pair. The paired input cannot be another sensor.' : 'Confirm the positive and negative input pair in the DAQ device manual.';
}

function pullChannelEditor() {
  const channel = channels[String(selectedChannel)] || defaultChannel();
  channel.scale = {
    ...(channel.scale || defaultChannel().scale),
    low_voltage: optionalNumber('scale-low-voltage'),
    high_voltage: optionalNumber('scale-high-voltage'),
    low_value: optionalNumber('scale-low-value'),
    high_value: optionalNumber('scale-high-value'),
  };
  channel.counter = {
    enabled: $('counter-enabled').checked,
    direction: $('counter-direction').value,
    threshold: $('counter-threshold').value === '' && !$('counter-enabled').checked ? 0 : optionalNumber('counter-threshold'),
    interval_seconds: optionalNumber('counter-interval'),
  };
  channels[String(selectedChannel)] = channel;
  updateSignalHint();
}
function optionalNumber(id) { return $(id).value === '' ? null : Number($(id).value); }

function updateSignalHint() {
  const signal = (channels[String(selectedChannel)] || defaultChannel()).signal_type;
  const isPci = /^PCI-1716(?:H|L)?(?:,|$)/i.test(value('DEVICE_DESCRIPTION'));
  const hint = $('channel-hint');
  hint.className = 'signal-hint';
  if (signal === 'Differential') {
    if (!isPci) {
      hint.textContent = 'Confirm the positive and negative input pair in the selected DAQ device manual.';
    } else if (selectedChannel % 2) {
      hint.textContent = `PCI-1716 differential pairs start on an even input. Configure AI${selectedChannel - 1} to use AI${selectedChannel - 1}/AI${selectedChannel}.`;
      hint.classList.add('warning');
    } else if (channels[String(selectedChannel + 1)]?.enabled) {
      hint.textContent = `AI${selectedChannel + 1} is enabled. Disable it before using AI${selectedChannel}/AI${selectedChannel + 1} as a differential pair.`;
      hint.classList.add('warning');
    } else {
      hint.textContent = `Wire signal + to AI${selectedChannel} and signal − to AI${selectedChannel + 1}. Disable AI${selectedChannel + 1} as an independent sensor.`;
    }
  } else if (signal === 'SingleEnded') {
    hint.textContent = isPci ? `Wire signal to AI${selectedChannel} and signal return to AGND. Single ended is more sensitive to ground noise.` : `Wire signal to AI${selectedChannel} and return to the analog reference specified by the DAQ device.`;
  } else {
    hint.textContent = isPci ? 'Pseudo differential is not supported by PCI-1716.' : 'Confirm pseudo differential support and wiring in the selected device manual.';
    hint.classList.add('warning');
  }
}

function updateSummary() {
  const {start, end, valid} = channelSpan();
  const active = Object.entries(channels).filter(([index, channel]) => channel.enabled && valid && Number(index) >= start && Number(index) < end).length;
  $('summary-device').textContent = value('DEVICE_DESCRIPTION') || config.DEVICE_DESCRIPTION || '—';
  $('summary-device-id').textContent = value('DEVICE_ID') || config.DEVICE_ID || 'Device identifier';
  $('summary-channels').textContent = String(active).padStart(2, '0');
  $('summary-channel-span').textContent = valid ? `AI${start}–AI${end - 1} · ${end - start} read` : 'Set a valid channel span';
  $('summary-rate').textContent = `${value('CLOCK_RATE') || '—'} Hz`;
  const destination = value('DESTINATION') || config.DESTINATION || 'postgresql';
  $('info-mode').textContent = 'Physical DAQ';
  $('info-destination').textContent = destination === 'mqtt' ? 'MQTT' : titleCase(destination);
  $('info-retention').textContent = destination === 'influxdb' ? 'Managed by InfluxDB bucket' : destination === 'mqtt' ? 'Managed by external consumer' : `${$('DB_RETENTION_DAYS').value || config.DB_RETENTION_DAYS || '—'} days`;
}
function postgresDsn() {
  const user = encodeURIComponent(value('DB_USER'));
  const password = encodeURIComponent($('DB_PASSWORD').value);
  const database = encodeURIComponent(value('DB_NAME'));
  const host = value('DB_HOST');
  const port = value('DB_PORT');
  return host && port && database ? `postgresql://${user}:${password}@${host}:${port}/${database}` : '';
}
function syncPostgresDsn() {
  if (!manualDsn) $('DB_DSN').value = postgresDsn();
}

function collectConfig() {
  pullChannelEditor();
  const span = channelSpan();
  if (span.valid) {
    for (let index = span.start; index < span.end; index += 1) {
      channels[String(index)] ||= defaultChannel();
      channels[String(index)].scale ||= defaultChannel().scale;
    }
  }
  const payload = {};
  const secretIds = ['DB_PASSWORD', 'INFLUX_TOKEN', 'MQTT_PASSWORD'];
  fields.forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (secretIds.includes(id)) {
      if (el.value.trim() !== '') {
        payload[id] = el.value.trim();
      } else if (el.dataset.hasSaved === 'true') {
        payload[id] = '********';
      } else {
        payload[id] = '';
      }
      return;
    }
    const integerFields = ['START_CHANNEL', 'CHANNEL_COUNT', 'CLOCK_RATE', 'SECTION_LENGTH', 'SECTION_COUNT', 'DB_PORT', 'DB_RETENTION_DAYS', 'SPOOL_MAX_BYTES', 'MQTT_PORT', 'MQTT_PRODUCTION_QOS'];
    payload[id] = integerFields.includes(id) ? Number(el.value) : el.value.trim();
  });
  if (payload.DB_CONNECTION_MODE === 'fields') {
    delete payload.DB_DSN;
  }
  payload.AUTO_START_ON_STARTUP = $('AUTO_START_ON_STARTUP').checked;
  payload.MQTT_TLS_ENABLED = $('MQTT_TLS_ENABLED').checked;
  payload.CHANNELS = channels;
  return payload;
}

async function saveConfig() {
  const payload = collectConfig();
  payload._REV = config._REV;
  showConfigErrors([]);
  if (isRunning() && !window.confirm('Saving changes while acquisition is running will stop and restart the current run. Continue?')) return false;
  $('save-top').disabled = true;
  $('last-saved').textContent = 'Saving…';
  let result;
  try {
    const response = await api('/api/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    result = await response.json();
    if (!response.ok) throw new Error(result.message || `Save failed (HTTP ${response.status})`);
    config = result.config || {...config, ...payload};
    channels = structuredClone(config.CHANNELS || payload.CHANNELS);
    setDirty(false);
    showConfigErrors([]);
    updateSummary();
    $('config-state-copy').textContent = 'Saved configuration is current.';
    setMessage(isRunning() ? 'Configuration saved. The active acquisition was restarted with the saved values.' : 'Configuration saved to DAQ service.', 'success');
    await refreshStatus();
    return true;
  } catch (error) {
    if (result?.config) {
      config = result.config;
      channels = structuredClone(config.CHANNELS || channels);
      setDirty(false);
      updateSummary();
    } else {
      setDirty(true);
      // Keep the operator's edits after a failed or stale save. Runtime status
      // is refreshed below; reloading the form is an explicit operator action.
    }
    setMessage('');
    showConfigErrors([{section: sectionForSaveError(error.message), message: error.message}], true);
    await refreshStatus();
    return false;
  }
}

async function testDestination() {
  pullChannelEditor();
  const resultBox = $('destination-result');
  const payload = {};
  const secretIds = ['DB_PASSWORD', 'INFLUX_TOKEN', 'MQTT_PASSWORD'];
  ['DESTINATION', 'DB_CONNECTION_MODE', 'DB_DSN', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'INFLUX_URL', 'INFLUX_ORG', 'INFLUX_BUCKET', 'INFLUX_TOKEN', 'MQTT_BROKER', 'MQTT_PORT', 'MQTT_PRODUCTION_TOPIC_PREFIX', 'MQTT_PRODUCTION_QOS', 'MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_TLS_ENABLED', 'MQTT_CA_CERTS', 'MQTT_CLIENT_CERT', 'MQTT_CLIENT_KEY'].forEach((id) => {
    const input = $(id);
    if (!input) return;
    if (secretIds.includes(id)) {
      if (input.value.trim() !== '') {
        payload[id] = input.value.trim();
      } else if (input.dataset.hasSaved === 'true') {
        payload[id] = '********';
      } else {
        payload[id] = '';
      }
    } else {
      payload[id] = input.type === 'checkbox' ? input.checked : input.value.trim();
    }
  });
  if (payload.DB_CONNECTION_MODE === 'fields') {
    delete payload.DB_DSN;
  }
  resultBox.textContent = 'Testing connection…';
  resultBox.className = 'inline-result';
  $('test-destination').disabled = true;
  try {
    const response = await api('/api/test_destination', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.message || `Connection failed (HTTP ${response.status})`);
    resultBox.textContent = result.message;
    resultBox.classList.add('success-text');
  } catch (error) {
    resultBox.textContent = error.message;
    resultBox.classList.add('error-text');
  } finally {
    $('test-destination').disabled = false;
  }
}

async function scanDevices() {
  const result = $('scan-result');
  result.className = 'inline-result';
  result.textContent = 'Scanning host DAQNavi devices…';
  $('scan-button').disabled = true;
  try {
    const response = await api('/api/scan_usb');
    const data = await response.json();
    const daqDevices = (data.devices || []).filter((device) => device.is_daq);
    result.replaceChildren();
    const summary = document.createElement('span');
    summary.textContent = daqDevices.length ? `${daqDevices.length} DAQ device(s) found.` : 'No DAQNavi devices found.';
    result.append(summary);
    if (data.warnings?.length) {
      const warning = document.createElement('span');
      warning.textContent = data.warnings.join(' ');
      result.append(warning);
    }
    daqDevices.forEach((device) => {
      const use = document.createElement('button');
      use.type = 'button'; use.className = 'text-button'; use.textContent = `Use ${device.id}`;
      use.addEventListener('click', () => { $('DEVICE_DESCRIPTION').value = device.id; markChanged(); renderChannelEditor(); });
      result.append(use);
    });
    result.classList.remove('hidden');
    if (data.warnings?.length) result.classList.add('warning');
  } catch (error) {
    result.textContent = `Device scan failed: ${error.message}`;
    result.className = 'inline-result error-text';
  } finally { $('scan-button').disabled = false; }
}

function markChanged() {
  setDirty(true);
  updateSummary();
  showConfigErrors([]);
}
function updateDestinationFields() {
  const destination = value('DESTINATION');
  document.querySelectorAll('.postgres-only').forEach((element) => element.classList.toggle('hidden', destination !== 'postgresql'));
  document.querySelectorAll('.influx-only').forEach((element) => element.classList.toggle('hidden', destination !== 'influxdb'));
  $('influx-fields').classList.toggle('hidden', destination !== 'influxdb');
  $('mqtt-fields').classList.toggle('hidden', destination !== 'mqtt');
  $('mqtt-production-options').classList.toggle('hidden', destination !== 'mqtt');
  document.querySelectorAll('.postgres-fields').forEach((element) => element.classList.toggle('hidden', destination !== 'postgresql' || manualDsn));
  $('db-dsn-fields').classList.toggle('hidden', destination !== 'postgresql' || !manualDsn);
}

document.querySelector('.calibration-heading .toggle')?.remove();
$('save-top').addEventListener('click', saveConfig);
$('test-destination').addEventListener('click', testDestination);
$('scan-button').addEventListener('click', scanDevices);
scaleIds.forEach((id) => $(id).addEventListener('input', () => { pullChannelEditor(); markChanged(); }));
['counter-enabled', 'counter-direction', 'counter-threshold', 'counter-interval'].forEach((id) => $(id).addEventListener('change', () => { pullChannelEditor(); markChanged(); }));
fields.forEach((id) => {
  if (!$(id)) return;
  $(id).addEventListener('input', () => {
    if (['DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD'].includes(id)) syncPostgresDsn();
    markChanged();
    if (id === 'START_CHANNEL' || id === 'CHANNEL_COUNT') {
      pullChannelEditor();
      const span = channelSpan();
      if (span.valid && (selectedChannel < span.start || selectedChannel >= span.end)) selectedChannel = span.start;
      renderChannels();
      renderChannelEditor();
    }
    if (id === 'DEVICE_DESCRIPTION') updateSignalHint();
  });
  $(id).addEventListener('change', () => { markChanged(); updateSummary(); if (id === 'DEVICE_DESCRIPTION') { renderChannels(); renderChannelEditor(); } });
});
$('DB_CONNECTION_MODE').addEventListener('change', () => {
  manualDsn = $('DB_CONNECTION_MODE').value === 'dsn';
  if (!manualDsn) syncPostgresDsn();
  updateDestinationFields();
  markChanged();
});
$('AUTO_START_ON_STARTUP').addEventListener('change', markChanged);
$('DESTINATION').addEventListener('change', updateDestinationFields);
$('MQTT_TLS_ENABLED').addEventListener('change', markChanged);

  return {loadConfig, getConfig: () => config, isDirty: () => dirty};
}
