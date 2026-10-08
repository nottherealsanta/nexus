// Standalone textarea navigation. The caller owns submission and blocking UI:
// getMessages(session) supplies chronological user messages (text or text blocks),
// isBlocked() covers modal, slash-menu and voice state. Persistence hooks are
// synchronous loadHistory(session) / saveHistory(session, texts); failures are
// non-fatal. editingKeys limits the Ctrl+letter editing keys so the caller can leave
// app shortcuts (Ctrl+E logs, Ctrl+U usage, Ctrl+K commands) alone. Call remember(text) after accepting a submission, before clearing it.
export function installComposerNavigation({input, getSession, getMessages, isBlocked,
  loadHistory, saveHistory, editingKeys = ['a', 'e', 'k', 'u', 'w']}) {
  const histories = new Map();
  let session, position = null, draft = null, composing = false, changing = false;
  let disposed = false;

  function reset() { position = null; draft = null; }
  function currentSession() {
    const next = getSession();
    if (next !== session) { session = next; reset(); }
    return next;
  }
  function clean(rows) {
    return (Array.isArray(rows) ? rows : []).filter(text =>
      typeof text === 'string' && text.trim()).slice(-100);
  }
  function history(id) {
    if (!histories.has(id)) {
      let saved;
      try { saved = loadHistory?.(id); } catch { /* Storage may be unavailable. */ }
      const source = getMessages?.(id);
      const messages = (Array.isArray(source) ? source : []).filter(message => message?.role === 'user')
        .map(message => {
          if (typeof message.content === 'string') return message.content;
          if (typeof message.text === 'string') return message.text;
          const blocks = message.blocks || message.content;
          return (Array.isArray(blocks) ? blocks : []).filter(block =>
            block && (block.kind || block.type) === 'text').map(block => block.text || '').join('');
        });
      histories.set(id, clean(Array.isArray(saved) ? saved : messages));
    }
    return histories.get(id);
  }
  function notifyInput() {
    const EventClass = input.ownerDocument?.defaultView?.Event || globalThis.Event;
    input.dispatchEvent(new EventClass('input', {bubbles: true}));
  }
  function replace(start, end, text) {
    const before = input.value, expected = before.slice(0, start) + text + before.slice(end);
    if (expected === before) return;
    changing = true;
    try {
      input.setSelectionRange(start, end);
      // execCommand remains the broadly available way to participate in the
      // browser's native undo stack for textarea edits. Never execute on another
      // focused element; setRangeText is the safe (non-undoable) fallback.
      const doc = input.ownerDocument;
      try {
        if (doc?.activeElement === input && doc.execCommand) {
          doc.execCommand(text ? 'insertText' : 'delete', false, text);
        }
      } catch { /* Unsupported command; use the textarea API below. */ }
      if (input.value !== expected) {
        if (input.setRangeText) input.setRangeText(text, start, end, 'end');
        else input.value = expected;
        input.setSelectionRange(start + text.length, start + text.length);
        notifyInput();
      }
    } finally { changing = false; }
  }
  function onInput() {
    currentSession();
    if (!changing) reset();
  }
  function onKeyDown(event) {
    if (disposed || event.defaultPrevented || composing || event.isComposing ||
        event.keyCode === 229 || input.disabled || input.readOnly || isBlocked?.()) return;
    const id = currentSession(), value = input.value;
    if (event.metaKey || event.altKey || event.shiftKey) return;
    if (!event.ctrlKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
      // Even at the first/last line, multiline arrows belong to the browser.
      if (/[\r\n]/.test(value) || input.selectionStart !== input.selectionEnd || !id) return;
      const rows = history(id);
      if (!rows.length || (position === null && event.key === 'ArrowDown')) return;
      event.preventDefault();
      event.stopPropagation();
      if (position === null) {
        draft = {text: value, start: input.selectionStart, end: input.selectionEnd};
        position = rows.length;
      }
      position = Math.max(0, Math.min(rows.length,
        position + (event.key === 'ArrowUp' ? -1 : 1)));
      const text = position === rows.length ? draft.text : rows[position];
      replace(0, value.length, text);
      if (position === rows.length) {
        input.setSelectionRange(draft.start, draft.end);
        reset();
      } else input.setSelectionRange(text.length, text.length);
      return;
    }
    if (!event.ctrlKey) return;
    const key = event.key.toLowerCase();
    if (!editingKeys.includes(key)) return;
    // In particular Ctrl+W must not bubble to app shortcuts or close the tab.
    event.preventDefault();
    event.stopPropagation();
    const start = input.selectionStart, end = input.selectionEnd;
    const lineStart = start === 0 ? 0 : value.lastIndexOf('\n', start - 1) + 1;
    const newline = value.indexOf('\n', end);
    const lineEnd = newline < 0 ? value.length : newline;
    if (key === 'a' || key === 'e') {
      const caret = key === 'a' ? lineStart : lineEnd;
      input.setSelectionRange(caret, caret);
      return;
    }
    let from = start, to = end;
    if (start === end) {
      if (key === 'k') to = lineEnd;
      if (key === 'u') from = lineStart;
      if (key === 'w') {
        // Shell-style backward word: consume whitespace, then the prior word.
        while (from > 0 && /\s/.test(value[from - 1])) from--;
        while (from > 0 && !/\s/.test(value[from - 1])) from--;
      }
    }
    reset();
    replace(from, to, '');
  }
  function onCompositionStart() { composing = true; }
  function onCompositionEnd() { composing = false; }
  const listeners = {keydown: onKeyDown, input: onInput,
    compositionstart: onCompositionStart, compositionend: onCompositionEnd};
  for (const [type, listener] of Object.entries(listeners)) input.addEventListener(type, listener);
  return {
    remember(text) {
      if (disposed) return;
      const id = currentSession();
      if (!id || typeof text !== 'string' || !text.trim()) return;
      const rows = history(id);
      if (rows.at(-1) !== text) rows.push(text);
      if (rows.length > 100) rows.splice(0, rows.length - 100);
      reset();
      try { saveHistory?.(id, [...rows]); } catch { /* Keep in-memory history. */ }
    },
    dispose() {
      disposed = true;
      for (const [type, listener] of Object.entries(listeners)) input.removeEventListener(type, listener);
      histories.clear();
      reset();
    },
  };
}
