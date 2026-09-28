export const $ = (id) => document.getElementById(id);

export function api(path, options = {}) {
  return fetch(path, {cache: 'no-store', ...options, headers: {'Accept': 'application/json', ...(options.headers || {})}});
}
export function setMessage(message, type = '') {
  const el = $('page-message');
  el.textContent = message;
  el.className = `page-message ${type}`;
  if (type === 'error' && message) el.scrollIntoView({behavior: 'smooth', block: 'center'});
}
export function showConfigErrors(errors, scrollToFirst = false) {
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
export function sectionForSaveError(message) {
  if (/channel span|device|clock|sample rate|section|START_CHANNEL|CHANNEL_COUNT|PROFILE_PATH/i.test(message)) return 'device';
  if (/channel|sensor|calibrat|signal.?type|value.?range|input range|differential|scale/i.test(message)) return 'channels';
  if (/destination|database|postgres|timescale|influx|mqtt|retention|spool|DB_/i.test(message)) return 'destination';
  if (/AUTO_START|acquisition mode/i.test(message)) return 'runtime';
  return 'runtime';
}

export function titleCase(input) { return String(input).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
