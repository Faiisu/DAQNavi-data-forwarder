const $ = (id) => document.getElementById(id);
const fields = ['DEVICE_DESCRIPTION', 'DEVICE_ID', 'PROFILE_PATH', 'START_CHANNEL', 'CHANNEL_COUNT', 'CLOCK_RATE', 'SECTION_LENGTH', 'SECTION_COUNT', 'DESTINATION', 'DB_PRODUCTION_TABLE', 'DB_CONNECTION_MODE', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_DSN', 'DB_RETENTION_DAYS', 'SPOOL_MAX_BYTES', 'DB_MOCKUP_TABLE', 'INFLUX_URL', 'INFLUX_ORG', 'INFLUX_BUCKET', 'INFLUX_TOKEN', 'INFLUX_MEASUREMENT', 'MQTT_BROKER', 'MQTT_PORT', 'MQTT_PRODUCTION_TOPIC_PREFIX', 'MQTT_PRODUCTION_QOS', 'MQTT_USERNAME', 'MQTT_PASSWORD', 'MQTT_CA_CERTS', 'MQTT_CLIENT_CERT', 'MQTT_CLIENT_KEY', 'AUTO_START_MODE'];
const scaleIds = ['scale-low-voltage', 'scale-high-voltage', 'scale-low-value', 'scale-high-value'];
let config = {};
let channels = {};
let selectedChannel = 0;
let dirty = false;
let running = false;
let clearing = false;
let manualDsn = false;
let validationVisible = false;
let pendingSamples = [];
const pendingWindowMs = 60000;

const defaultChannel = () => ({enabled: false, label: '', unit: '', signal_type: 'SingleEnded', value_range: 'V_0To5', scale: {enabled: false, low_voltage: 0, high_voltage: 5, low_value: 0, high_value: 100}});

function api(path, options = {}) {
  return fetch(path, {cache: 'no-store', ...options, headers: {'Accept': 'application/json', ...(options.headers || {})}});
}
function setMessage(message, type = '') {
  const el = $('page-message');
  el.textContent = message;
  el.className = `page-message ${type}`;
  if (type === 'error' && message) el.scrollIntoView({behavior: 'smooth', block: 'center'});
}
function showConfigErrors(errors, scrollToFirst = false) {
  const sections = ['device', 'channels', 'destination', 'runtime'];
  sections.forEach((id) => {
    const section = $(id);
    section.classList.remove('has-config-errors');
    section.querySelector('.section-error-badge')?.remove();
    section.querySelector('.section-errors')?.remove();
    const messages = errors.filter((error) => error.section === id).map((error) => error.message);
    if (!messages.length) return;
    section.classList.add('has-config-errors');
    const badge = document.createElement('span');
    badge.className = 'section-error-badge';
    badge.textContent = `${messages.length} ${messages.length === 1 ? 'error' : 'errors'}`;
    section.querySelector('.panel-heading').append(badge);
    const list = document.createElement('ul');
    list.className = 'section-errors';
    messages.forEach((message) => {
      const item = document.createElement('li');
      item.textContent = message;
      list.append(item);
    });
    section.querySelector('.panel-heading').after(list);
  });
  if (scrollToFirst && errors.length) $(sections.find((id) => errors.some((error) => error.section === id))).scrollIntoView({behavior: 'smooth', block: 'start'});
}
function sectionForSaveError(message) {
  if (/channel span|device|clock|sample rate|section|START_CHANNEL|CHANNEL_COUNT|PROFILE_PATH/i.test(message)) return 'device';
  if (/channel|sensor|calibrat|signal.?type|value.?range|input range|differential|scale/i.test(message)) return 'channels';
  if (/destination|database|postgres|timescale|influx|mqtt|retention|spool|DB_/i.test(message)) return 'destination';
  if (/AUTO_START|MOCKUP_MODE|acquisition mode/i.test(message)) return 'runtime';
  return 'runtime';
}
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
  const explicitMode = config.DB_CONNECTION_MODE || (config.DB_DSN && config.DB_DSN !== postgresDsn() ? 'dsn' : 'fields');
  manualDsn = (explicitMode === 'dsn');
  $('DB_CONNECTION_MODE').value = explicitMode;
  selectedChannel = Number.isInteger(Number(config.START_CHANNEL)) ? Number(config.START_CHANNEL) : 0;
  renderChannels();
  renderChannelEditor();
  updateSummary();
  updateDestinationFields();
  setDirty(false);
  validationVisible = false;
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
  const mode = value('AUTO_START_MODE') || config.AUTO_START_MODE || 'production';
  $('info-mode').textContent = titleCase(mode);
  $('info-destination').textContent = destination === 'mqtt' ? 'MQTT' : titleCase(destination);
  $('info-retention').textContent = destination === 'influxdb' ? 'Managed by InfluxDB bucket' : destination === 'mqtt' ? 'Managed by external consumer' : `${$('DB_RETENTION_DAYS').value || config.DB_RETENTION_DAYS || '—'} days`;
}
function titleCase(input) { return String(input).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
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

function validateConfig(payload) {
  const errors = [];
  const add = (section, message) => errors.push({section, message});
  const start = payload.START_CHANNEL;
  const end = start + payload.CHANNEL_COUNT;
  if (!Number.isInteger(start) || !Number.isInteger(payload.CHANNEL_COUNT) || start < 0 || payload.CHANNEL_COUNT < 1 || end > 16) add('device', 'Channel span must fit within AI0–AI15.');
  if (!Number.isInteger(payload.SECTION_LENGTH) || payload.SECTION_LENGTH < 1) add('device', 'Section length must be a positive whole number.');
  if (!Number.isInteger(payload.SECTION_COUNT) || payload.SECTION_COUNT < 0) add('device', 'Section count must be zero or a positive whole number.');
  if (!Number.isInteger(payload.CLOCK_RATE) || payload.CLOCK_RATE < 1000 || payload.CLOCK_RATE > 2000) add('device', 'Sample rate must be between 1000 and 2000 Hz per channel.');
  if (payload.DESTINATION === 'postgresql' && (!Number.isInteger(payload.DB_RETENTION_DAYS) || payload.DB_RETENTION_DAYS < 1)) add('destination', 'Retention must be at least one day.');
  Object.entries(payload.CHANNELS).forEach(([index, channel]) => {
    for (const key of ['low_voltage', 'high_voltage', 'low_value', 'high_value']) {
      if (!Number.isFinite(channel.scale?.[key])) add('channels', `AI${index} calibration ${key.replaceAll('_', ' ')} must be a number.`);
    }
  });
  const active = Object.entries(payload.CHANNELS).filter(([, channel]) => channel.enabled);
  const pci1716 = /^PCI-1716(?:H|L)?(?:,|$)/i.test(payload.DEVICE_DESCRIPTION);
  if (pci1716 && payload.AUTO_START_MODE === 'production') {
    for (let index = start; index < end; index += 1) {
      if (payload.CHANNELS[String(index)]?.signal_type === 'PseudoDifferential') add('channels', `AI${index} cannot use pseudo differential on PCI-1716.`);
    }
  }
  active.forEach(([numberText, channel]) => {
    const index = Number(numberText);
    const scale = channel.scale || {};
    if (payload.AUTO_START_MODE === 'production' && (index < start || index >= end)) add('channels', `AI${index} is enabled outside the configured channel span.`);
    if (payload.AUTO_START_MODE === 'production' && pci1716 && channel.signal_type === 'Differential') {
      if (index % 2) add('channels', `AI${index} cannot start a PCI-1716 differential pair; select its even channel.`);
      if (payload.CHANNELS[String(index + 1)]?.enabled) add('channels', `Disable AI${index + 1}; it is the negative leg paired with AI${index}.`);
    }
    if (payload.AUTO_START_MODE === 'production') {
      if (!channel.label || !channel.unit) add('channels', `AI${index} needs a sensor label and engineering unit for production.`);
      if (!scale.enabled) add('channels', `Enable calibration for AI${index} before production acquisition.`);
      if (scale.high_voltage === scale.low_voltage) add('channels', `AI${index} calibration input endpoints must differ.`);
    }
  });
  if (payload.AUTO_START_MODE === 'production') {
    if (!payload.DEVICE_ID) add('device', 'Device ID is required.');
    if (!Number.isInteger(payload.SPOOL_MAX_BYTES) || payload.SPOOL_MAX_BYTES < 1) add('destination', 'Spool capacity must be a positive whole number.');
    if (!['postgresql', 'influxdb', 'mqtt'].includes(payload.DESTINATION)) add('destination', 'Choose PostgreSQL / TimescaleDB, InfluxDB, or MQTT as the production destination.');
    if (payload.DESTINATION === 'influxdb') {
      try { const url = new URL(payload.INFLUX_URL); if (!['http:', 'https:'].includes(url.protocol)) throw new Error(); }
      catch (_) { add('destination', 'Enter a valid HTTP(S) InfluxDB URL.'); }
      if (!payload.INFLUX_ORG || !payload.INFLUX_BUCKET || !payload.INFLUX_TOKEN) add('destination', 'InfluxDB organization, bucket, and token are required.');
    }
    if (payload.DESTINATION === 'mqtt') {
      if (!payload.MQTT_BROKER) add('destination', 'MQTT broker host is required.');
      if (!Number.isInteger(payload.MQTT_PORT) || payload.MQTT_PORT < 1 || payload.MQTT_PORT > 65535) add('destination', 'MQTT broker port must be between 1 and 65535.');
      if (!payload.MQTT_USERNAME) add('destination', 'Production MQTT requires a broker username.');
      if (!payload.MQTT_PASSWORD) add('destination', 'Production MQTT requires a broker password. Enter it or keep the saved password.');
      if (payload.MQTT_TLS_ENABLED !== true) add('destination', 'Production MQTT requires verified TLS.');
      if (![0, 1].includes(payload.MQTT_PRODUCTION_QOS)) add('destination', 'Production MQTT QoS must be 0 or 1.');
      if (!payload.MQTT_PRODUCTION_TOPIC_PREFIX || /[+#]/.test(payload.MQTT_PRODUCTION_TOPIC_PREFIX)) add('destination', 'Enter a production topic prefix without MQTT wildcards.');
    }
    if (Number.isInteger(payload.SECTION_COUNT) && payload.SECTION_COUNT > 0) add('device', 'Production acquisition requires section count 0 (continuous).');
    if (!active.length) add('channels', 'Enable at least one sensor channel.');
    if (payload.DESTINATION === 'postgresql') {
      if (!/^[a-z][a-z0-9_]*$/.test(payload.DB_PRODUCTION_TABLE)) add('destination', 'Production table must use lowercase letters, numbers, and underscores, starting with a letter.');
      if (payload.DB_PRODUCTION_TABLE === payload.DB_MOCKUP_TABLE || payload.DB_PRODUCTION_TABLE === config.DB_TABLE) add('destination', 'Production table must differ from mockup and legacy tables.');
    }
  }
  if (payload.AUTO_START_MODE === 'mockup' && payload.DESTINATION === 'mqtt') add('destination', 'MQTT is a production destination. Choose PostgreSQL / TimescaleDB or InfluxDB for mockup acquisition.');
  return errors;
}

async function saveConfig() {
  const payload = collectConfig();
  payload._REV = config._REV;
  const errors = validateConfig(payload);
  if (errors.length) {
    validationVisible = true;
    setMessage('');
    showConfigErrors(errors, true);
    return false;
  }
  validationVisible = false;
  showConfigErrors([]);
  if (running && !window.confirm('Saving changes while acquisition is running will stop and restart the current run. Continue?')) return false;
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
    setMessage(running ? 'Configuration saved. The active acquisition was restarted with the saved values.' : 'Configuration saved to DAQ service.', 'success');
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
    validationVisible = true;
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
    $('runtime-description').textContent = running ? `Acquisition is ${state}.` : 'Acquisition is stopped.';
    $('runtime-message').textContent = status.writer_error || status.fault || (running ? 'Samples are being acquired by the DAQ service.' : 'Start only after verifying the device, wiring, and destination.');
    const pending = Number(status.pending_batches);
    $('summary-pending').textContent = String(status.pending_batches ?? 0);
    $('buffer-batches').textContent = String(status.pending_batches ?? 0);
    updatePendingTrend(pending);
    const bytes = Number(status.spool_bytes || status.pending_bytes || 0);
    const capacity = Number(config.SPOOL_MAX_BYTES || 0);
    $('summary-spool').textContent = `${formatBytes(bytes)} local spool used`;
    $('buffer-used').textContent = `${formatBytes(bytes)} used`;
    $('buffer-capacity').textContent = `${formatBytes(capacity)} capacity`;
    $('buffer-meter').style.width = `${capacity ? Math.min(100, bytes / capacity * 100) : 0}%`;
    $('info-last-sample').textContent = status.last_sample_ns ? new Date(Number(status.last_sample_ns) / 1e6).toLocaleTimeString() : 'No recent sample';
    const mqttSelected = config.DESTINATION === 'mqtt' && config.AUTO_START_MODE !== 'mockup';
    $('mqtt-runtime-card').classList.toggle('hidden', !mqttSelected);
    if (mqttSelected) {
      const broker = config.MQTT_BROKER ? `${config.MQTT_BROKER}:${config.MQTT_PORT || 8883}` : 'Not configured';
      $('mqtt-runtime-broker').textContent = broker;
      $('mqtt-runtime-qos').textContent = `QoS ${config.MQTT_PRODUCTION_QOS ?? 1}`;
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
  if (dirty) {
    const errors = validateConfig(collectConfig());
    validationVisible = errors.length > 0;
    setMessage('');
    showConfigErrors(errors.length ? errors : [{section: 'runtime', message: 'Save the current configuration before starting acquisition.'}], true);
    return;
  }
  const mode = value('AUTO_START_MODE');
  const warning = mode === 'production'
    ? 'Start physical production acquisition with the saved hardware configuration? Verify signal wiring and the destination first.'
    : 'Start mockup acquisition? Synthetic data will be sent to the configured destination.';
  if (!window.confirm(warning)) return;
  $('start-acquisition').disabled = true;
  try {
    const response = await api('/api/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode})});
    const result = await response.json();
    if (!response.ok || !result.started) throw new Error(result.message || 'Acquisition did not start.');
    validationVisible = false;
    showConfigErrors([]);
    setMessage(`${titleCase(mode)} acquisition started.`, 'success');
    await refreshStatus();
  } catch (error) {
    validationVisible = true;
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
function markChanged() {
  setDirty(true);
  updateSummary();
  if (validationVisible) showConfigErrors(validateConfig(collectConfig()));
}
function updateDestinationFields() {
  const destination = value('DESTINATION');
  const mode = value('AUTO_START_MODE');
  document.querySelectorAll('.postgres-only').forEach((element) => element.classList.toggle('hidden', destination !== 'postgresql'));
  document.querySelectorAll('.influx-only').forEach((element) => element.classList.toggle('hidden', destination !== 'influxdb'));
  $('influx-fields').classList.toggle('hidden', destination !== 'influxdb');
  $('mqtt-fields').classList.toggle('hidden', destination !== 'mqtt');
  $('mqtt-production-options').classList.toggle('hidden', destination !== 'mqtt' || mode !== 'production');
  $('DESTINATION').querySelector('option[value="mqtt"]').disabled = mode === 'mockup';
  document.querySelectorAll('.postgres-fields').forEach((element) => element.classList.toggle('hidden', destination !== 'postgresql' || manualDsn));
  $('db-dsn-fields').classList.toggle('hidden', destination !== 'postgresql' || !manualDsn);
}

document.querySelector('.calibration-heading .toggle')?.remove();
$('save-top').addEventListener('click', saveConfig);
$('test-destination').addEventListener('click', testDestination);
$('scan-button').addEventListener('click', scanDevices);
$('start-acquisition').addEventListener('click', runAcquisition);
$('stop-acquisition').addEventListener('click', stopAcquisition);
$('clear-buffer').addEventListener('click', clearBuffer);
scaleIds.forEach((id) => $(id).addEventListener('input', () => { pullChannelEditor(); markChanged(); }));
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
$('AUTO_START_MODE').addEventListener('change', updateDestinationFields);
$('MQTT_TLS_ENABLED').addEventListener('change', markChanged);
document.querySelectorAll('.side-nav a').forEach((link) => link.addEventListener('click', () => {
  document.querySelectorAll('.side-nav a').forEach((item) => item.classList.remove('active'));
  link.classList.add('active');
}));

if (!document.querySelector('link[rel="icon"]')) {
  const icon = document.createElement('link');
  icon.rel = 'icon';
  icon.type = 'image/svg+xml';
  icon.href = '/static/config_center/favicon.svg';
  document.head.append(icon);
}
setPendingTrend('↑ —', '↓ —', 'Waiting for next reading');
loadConfig().then(refreshStatus).catch((error) => {
  setMessage(`Could not load the saved DAQ configuration: ${error.message}`, 'error');
  $('last-saved').textContent = 'Configuration unavailable';
  $('side-status').textContent = 'DAQ API unavailable';
  $('run-state').textContent = 'API unavailable';
});
setInterval(refreshStatus, 5000);

// Change Password Modal handling
const operatorProfile = $('operator-profile');
const pwdModal = $('password-modal');
const btnOpenPwd = $('btn-open-change-password');
const btnClosePwd = $('btn-close-pwd-modal');
const btnCancelPwd = $('btn-cancel-pwd');
const formChangePwd = $('change-password-form');
const pwdAlert = $('pwd-modal-alert');
const btnSubmitPwd = $('btn-submit-pwd');

function showPwdAlert(msg, type) {
  if (!pwdAlert) return;
  pwdAlert.className = `inline-result ${type}`;
  pwdAlert.textContent = msg;
  pwdAlert.classList.remove('hidden');
}

function openPasswordModal() {
  if (!pwdModal) return;
  if (operatorProfile) operatorProfile.open = false;
  pwdModal.classList.remove('hidden');
  if (pwdAlert) {
    pwdAlert.className = 'inline-result hidden';
    pwdAlert.textContent = '';
  }
  if (formChangePwd) formChangePwd.reset();
  setTimeout(() => {
    const cur = $('pwd-current');
    if (cur) cur.focus();
  }, 50);
}

function closePasswordModal() {
  if (!pwdModal) return;
  pwdModal.classList.add('hidden');
  if (formChangePwd) formChangePwd.reset();
}

if (btnOpenPwd) btnOpenPwd.addEventListener('click', openPasswordModal);
if (btnClosePwd) btnClosePwd.addEventListener('click', closePasswordModal);
if (btnCancelPwd) btnCancelPwd.addEventListener('click', closePasswordModal);

if (operatorProfile) {
  document.addEventListener('click', (e) => {
    if (!operatorProfile.contains(e.target)) operatorProfile.open = false;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && operatorProfile.open) {
      operatorProfile.open = false;
      operatorProfile.querySelector('summary').focus();
    }
  });
}

if (pwdModal) {
  pwdModal.addEventListener('click', (e) => {
    if (e.target === pwdModal) closePasswordModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pwdModal.classList.contains('hidden')) {
      closePasswordModal();
    }
  });
}

if (formChangePwd) {
  formChangePwd.addEventListener('submit', async (e) => {
    e.preventDefault();
    const current_password = $('pwd-current').value;
    const new_password = $('pwd-new').value;
    const confirm_password = $('pwd-confirm').value;

    if (!current_password) {
      showPwdAlert('Please enter your current password.', 'error');
      return;
    }
    if (!new_password || new_password.length < 8) {
      showPwdAlert('New password must be at least 8 characters.', 'error');
      return;
    }
    if (new_password !== confirm_password) {
      showPwdAlert('New password and confirmation do not match.', 'error');
      return;
    }
    if (new_password === current_password) {
      showPwdAlert('New password must be different from current password.', 'error');
      return;
    }

    btnSubmitPwd.disabled = true;
    btnSubmitPwd.textContent = 'Updating...';
    try {
      const response = await api('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password, new_password, confirm_password })
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'ok') {
        throw new Error(data.message || data.error || 'Failed to update password');
      }
      showPwdAlert('Password updated successfully!', 'success');
      setTimeout(() => {
        closePasswordModal();
        setMessage('Operator password changed successfully.', 'success');
      }, 1200);
    } catch (err) {
      showPwdAlert(err.message, 'error');
    } finally {
      btnSubmitPwd.disabled = false;
      btnSubmitPwd.textContent = 'Update Password';
    }
  });
}
