// Browser-local presentation only: never filter the request/context source data.
// Cards should have data-context-key="system|tools|agents_md|skills|mcp".
// Legacy context-chip labels are also recognised; Task cards are never hidden.
export const CONTEXT_PREFERENCES_KEY = 'nexus-web-context-preferences';
export const CONTEXT_CARDS = Object.freeze([
  ['system', 'System prompt'], ['tools', 'Tools'], ['agents_md', 'AGENTS.md'],
  ['skills', 'Skills'], ['mcp', 'MCP'],
].map(row => Object.freeze(row)));
const MAX_STORAGE_LENGTH = 1024;
const displays = new WeakMap();

function normalize(values) {
  return Object.fromEntries(CONTEXT_CARDS.map(([key]) =>
    [key, values && typeof values[key] === 'boolean' ? values[key] : true]));
}

export function loadContextPreferences(storage) {
  try {
    const raw = storage?.getItem(CONTEXT_PREFERENCES_KEY);
    if (typeof raw !== 'string' || raw.length > MAX_STORAGE_LENGTH) return normalize();
    const saved = JSON.parse(raw);
    return normalize(saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : null);
  } catch { return normalize(); }
}

// Return the sanitised values even when storage is blocked or full.
export function saveContextPreferences(storage, values) {
  const clean = normalize(values);
  try { storage?.setItem(CONTEXT_PREFERENCES_KEY, JSON.stringify(clean)); }
  catch { /* Presentation preferences remain usable in memory. */ }
  return clean;
}

function cardKey(card) {
  const explicit = card.dataset?.contextKey;
  if (explicit) return explicit;
  const label = card.querySelector('.context-chip')?.textContent;
  return CONTEXT_CARDS.find(([, name]) => name === label)?.[0];
}

// Optional onRestore lets the parent persist restoration and rerender all headers.
// Without it, the action restores this header only. Returns the hidden card count.
export function applyContextPreferences(header, values, {onRestore} = {}) {
  if (!header) return 0;
  const clean = normalize(values);
  let count = 0;
  for (const card of header.querySelectorAll('.context-block, [data-context-key]')) {
    const key = cardKey(card);
    if (!Object.hasOwn(clean, key)) continue;
    const hide = !clean[key];
    card.hidden = hide;
    // The existing .context-block display rule can override the UA [hidden] rule.
    if (hide) {
      if (!displays.has(card)) displays.set(card, card.style.display);
      card.style.display = 'none';
      count++;
    } else if (displays.has(card)) {
      card.style.display = displays.get(card);
      displays.delete(card);
    }
  }
  let notice = header.querySelector('[data-context-preferences-notice]');
  if (!count) { notice?.remove(); return 0; }
  const doc = header.ownerDocument;
  if (!notice) {
    notice = doc.createElement('div');
    notice.className = 'context-preferences-notice';
    notice.dataset.contextPreferencesNotice = '';
    notice.setAttribute('role', 'status');
    header.append(notice);
  }
  const text = doc.createElement('span');
  text.textContent = `${count} context card${count === 1 ? '' : 's'} hidden. Full request context is unchanged. `;
  const restore = doc.createElement('button');
  restore.type = 'button';
  restore.textContent = 'Show all context cards';
  restore.onclick = () => {
    applyContextPreferences(header, normalize());
    onRestore?.(normalize());
  };
  notice.replaceChildren(text, restore);
  return count;
}

// el follows app.js's el(tag, className, text) helper. The controller owns only
// root's children. onChange receives a fresh snapshot; no host commands are sent.
export function createContextPreferences({el, root, storage, onChange} = {}) {
  let values = loadContextPreferences(storage);
  const snapshot = () => ({...values});
  function render() {
    if (!root || !el) return;
    const group = el('fieldset', 'context-preferences');
    group.append(el('legend', '', 'Context header cards'));
    group.append(el('p', '', 'Choose which cards appear in conversation headers. Hidden cards remain in the full request context. Saved in this browser only.'));
    for (const [key, name] of CONTEXT_CARDS) {
      const label = el('label', 'context-preference');
      const input = el('input');
      input.type = 'checkbox';
      input.checked = values[key];
      input.dataset.contextPreference = key;
      input.onchange = () => set(key, input.checked);
      label.append(input, el('span', '', name));
      group.append(label);
    }
    const resetButton = el('button', '', 'Show all context cards');
    resetButton.type = 'button';
    resetButton.onclick = reset;
    group.append(resetButton);
    root.replaceChildren(group);
  }
  function commit(next) {
    values = saveContextPreferences(storage, next);
    render();
    onChange?.(snapshot());
    return snapshot();
  }
  function set(key, visible) {
    if (!CONTEXT_CARDS.some(([name]) => name === key) || typeof visible !== 'boolean') return snapshot();
    return commit({...values, [key]: visible});
  }
  function reset() { return commit(normalize()); }
  function load() { values = loadContextPreferences(storage); render(); return snapshot(); }
  function apply(header) { return applyContextPreferences(header, values, {onRestore: reset}); }
  render();
  return {load, render, set, reset, apply, get values() { return snapshot(); }};
}
