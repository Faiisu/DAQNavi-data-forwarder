import { $, api, setMessage, showConfigErrors, sectionForSaveError } from './ui.js';
import { createConfigForm } from './config_form.js';
import { createRuntime } from './runtime.js';
import { createStreamPreview } from './stream.js';
import { initPasswordModal } from './password.js';

let runtime;
const form = createConfigForm({api, setMessage, showConfigErrors, sectionForSaveError,
  isRunning: () => runtime.isRunning(), refreshStatus: () => runtime.refreshStatus()});
runtime = createRuntime({api, setMessage, showConfigErrors, sectionForSaveError,
  getConfig: form.getConfig, isDirty: form.isDirty});
const streamPreview = createStreamPreview({api});

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
form.loadConfig().then(runtime.refreshStatus).catch((error) => {
  setMessage(`Could not load the saved DAQ configuration: ${error.message}`, 'error');
  $('last-saved').textContent = 'Configuration unavailable';
  $('side-status').textContent = 'DAQ API unavailable';
  $('run-state').textContent = 'API unavailable';
});
setInterval(runtime.refreshStatus, 5000);
setInterval(streamPreview.refresh, 1000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) streamPreview.refresh();
});
initPasswordModal({api, setMessage});
