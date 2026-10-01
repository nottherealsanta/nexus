// Tips shown in the middle of an empty session; a copy of ui_support/hints.py:EMPTY_HINTS.
// The pick is seeded by the session id (stable per session, varied across sessions); the
// browser's sequence differs from Python's, which only changes which tips appear.
export const EMPTY_HINTS = [
  ['/', 'chat commands'],
  ['@', 'attach a workspace file to the message'],
  ['ctrl+p', 'command palette'],
  ['shift+tab', 'cycle the root agent'],
  ['ctrl+t', 'cycle reasoning effort'],
  ['ctrl+x m', 'choose a model'],
  ['ctrl+i', 'inspect exactly what the model will receive'],
  ['ctrl+space', 'dictate instead of typing'],
  ['ctrl+b', 'show or hide sessions'],
  ['ctrl+l', 'show or hide details'],
  ['ctrl+e', 'open the logs drawer'],
  ['ctrl+u', 'provider usage and limits'],
  ['shift+enter', 'new line in the message'],
  ['ctrl+enter', 'steer a running turn at its next step'],
  ['alt+enter', 'interrupt the running turn and send'],
  ['esc esc', 'stop the running turn'],
  ['ctrl+f', 'fork this session'],
  ['/attach <path>', 'attach a local file or image'],
  ['ctrl+v', 'paste a clipboard image'],
  ['ctrl+x ?', 'every keyboard shortcut'],
];
export const HINT_COUNT = 4;

export function pickHints(seed = '', count = HINT_COUNT) {
  let h = 2166136261;
  for (const ch of String(seed)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
  const rows = [...EMPTY_HINTS];
  for (let i = rows.length - 1; i > 0; i--) {
    h = (Math.imul(h, 1664525) + 1013904223) >>> 0;
    const j = h % (i + 1);
    [rows[i], rows[j]] = [rows[j], rows[i]];
  }
  return rows.slice(0, Math.min(count, rows.length));
}
