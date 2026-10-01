// Provider usage modal body: port of nexus/ui_support/usage.py + ui/tui/usage.py.
// One ProvidersUsage result renders as a section per connected provider with a
// labelled bar per limit window (5-hour, weekly, monthly…), its reset time,
// notes and the endpoint the numbers came from. Wording matches the TUI.

export const WARN_AT = 70, CRITICAL_AT = 90;

export function usedPercent(window) {
  const value = window?.used_percent;
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(value, 100)) : null;
}

export function tone(window) {
  const used = usedPercent(window);
  if (used === null) return 'unknown';
  return used >= CRITICAL_AT ? 'critical' : used >= WARN_AT ? 'warn' : 'ok';
}

export function percent(value) {
  // Whole percents, except near the ends: 0.2% used must not read as 100% left.
  return Number.isInteger(value) || (value >= 10 && value <= 90) ? `${Math.round(value)}%` : `${value.toFixed(1)}%`;
}

export function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return 'now';
  const days = Math.floor(seconds / 86400), hours = Math.floor((seconds % 86400) / 3600), minutes = Math.floor((seconds % 3600) / 60);
  if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
  if (hours) return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  return `${minutes}m`;
}

const pad = value => String(value).padStart(2, '0');
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function resetPhrase(window, now = Date.now() / 1000) {
  const at = window?.resets_at;
  if (typeof at === 'number' && at > 0) {
    const when = new Date(at * 1000), today = new Date(now * 1000);
    const time = `${pad(when.getHours())}:${pad(when.getMinutes())}`;
    const stamp = when.toDateString() === today.toDateString() ? time : `${DAYS[when.getDay()]} ${MONTHS[when.getMonth()]} ${when.getDate()} ${time}`;
    return at <= now ? `reset due (${stamp})` : `resets in ${duration(at - now)} (${stamp})`;
  }
  return window?.reset_text ? `resets ${window.reset_text}` : '';
}

export function summary(window, now) {
  const used = usedPercent(window);
  const parts = used === null ? ['usage unknown'] : [`${percent(used)} used`, `${percent(100 - used)} left`];
  const phrase = resetPhrase(window, now);
  if (phrase) parts.push(phrase);
  if (window?.detail) parts.push(String(window.detail));
  return parts.join(' · ');
}

export function heading(row) {
  const label = String(row?.label || row?.id || '?');
  return row?.plan ? `${label} · ${row.plan}` : label;
}

export function fetchedText(at) {
  if (typeof at !== 'number' || at <= 0) return '';
  const when = new Date(at * 1000);
  return `Fetched ${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
}

export function renderUsage(result, el, now) {
  const root = el('div', 'usage-report');
  const rows = (result?.providers || []).slice(0, 8);
  if (!rows.length) {
    root.append(el('p', 'usage-empty', 'No connected provider reports usage.'),
      el('p', 'usage-note', 'Connect ChatGPT, Claude, GitHub Copilot or OpenCode Go in Settings → Providers (Ctrl+S).'));
  }
  for (const row of rows) {
    const section = el('section', 'usage-provider');
    section.dataset.provider = row.id || '';
    section.append(el('h3', 'usage-provider-name', heading(row)));
    if (row.error) {
      const error = el('p', 'usage-error');
      error.append(el('strong', '', 'Unavailable: '), document.createTextNode(String(row.error)));
      section.append(error);
    }
    const windows = (row.windows || []).slice(0, 16);
    if (!windows.length && !row.error) section.append(el('p', 'usage-note', 'No limit windows reported.'));
    for (const window of windows) {
      const item = el('div', `usage-window ${tone(window)}`);
      const used = usedPercent(window);
      const meter = el('div', 'usage-meter');
      meter.setAttribute('role', 'meter');
      meter.setAttribute('aria-label', `${window.label || 'Limit'} used`);
      meter.setAttribute('aria-valuemin', '0'); meter.setAttribute('aria-valuemax', '100');
      if (used !== null) meter.setAttribute('aria-valuenow', String(used));
      const fill = el('span', 'usage-fill');
      fill.style.width = `${used ?? 0}%`;
      meter.append(fill);
      item.append(el('span', 'usage-label', String(window.label || 'Limit')), meter, el('span', 'usage-summary', summary(window, now)));
      section.append(item);
    }
    for (const note of (row.notes || []).slice(0, 8)) section.append(el('p', 'usage-note', `· ${note}`));
    if (row.source) section.append(el('p', 'usage-source', `Source: ${row.source}`));
    root.append(section);
  }
  const missing = (result?.not_connected || []).slice(0, 8);
  const footer = [rows.length && missing.length ? `Not connected: ${missing.join(', ')}` : '', fetchedText(result?.fetched_at)].filter(Boolean);
  if (footer.length) root.append(el('p', 'usage-footer', footer.join(' · ')));
  return root;
}
