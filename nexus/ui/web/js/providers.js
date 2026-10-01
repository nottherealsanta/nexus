// Settings → Providers: mirrors the TUI pane (nexus/ui_support/tui_providers.py).
// Every action is a host command; the daemon keeps credentials in its keychain.
// A browser or device sign-in shows a link and code here, then this page polls
// ProviderLoginPoll until the daemon reports the outcome. First-run setup
// renders a second instance into its own list (listId), like the TUI. A
// code_entry sign-in (Claude) shows a field for the code its page displays,
// sent inward with ProviderLoginCode.

export const PROVIDERS = [
  {id: 'codex', label: 'ChatGPT (Codex)', actions: [['browser', 'Sign in with browser'], ['device', 'Use a device code']]},
  {id: 'github-copilot', label: 'GitHub Copilot', actions: [['device', 'Use a device code']]},
  {id: 'opencode-go', label: 'OpenCode Go', actions: [['api_key', 'Save key']], key: true},
  {id: 'claude-agent', label: 'Claude (Pro/Max)', actions: [['browser', 'Sign in with browser']], code: true},
];
const POLL_MS = 1500, POLL_LIMIT = 600;

export function createProviders({api, el, $, listId = 'provider-list', isOpen = () => !$('settings-overlay').hidden}) {
  const polling = new Set(), logins = {}, codeEntry = {};
  let request = 0;

  function card(id) { return $(listId).querySelector(`[data-provider="${id}"]`); }
  function input(id) { return card(id)?.querySelector('.provider-input'); }
  function flow(id, text, link = '') {
    const box = card(id)?.querySelector('.provider-flow');
    if (!box) return;
    box.replaceChildren();
    if (text) box.append(el('span', '', text));
    if (link) {
      const anchor = el('a', 'provider-link', 'Open sign-in page');
      anchor.href = link; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
      box.append(anchor);
    }
  }
  function pending(id, on) {
    const node = card(id);
    if (!node) return;
    node.classList.toggle('pending', on);
    for (const button of node.querySelectorAll('.provider-action')) button.hidden = on;
    node.querySelector('.provider-cancel').hidden = !on;
    for (const item of node.querySelectorAll('.provider-code')) item.hidden = !(on && codeEntry[id]);
  }

  function build() {
    const list = $(listId);
    list.replaceChildren(...PROVIDERS.map(provider => {
      const node = el('article', 'provider-card');
      node.dataset.provider = provider.id;
      const head = el('div', 'provider-head');
      head.append(el('h4', 'provider-name', provider.label), el('span', 'provider-state', ''));
      const actions = el('div', 'provider-actions');
      if (provider.key) {
        const input = el('input', 'provider-input');
        input.type = 'password'; input.placeholder = 'OpenCode Go API key';
        input.autocomplete = 'off'; input.setAttribute('aria-label', 'OpenCode Go API key');
        input.addEventListener('keydown', e => { if (e.key === 'Enter') saveKey(provider.id); });
        actions.append(input);
      }
      if (provider.code) {
        const field = el('input', 'provider-input provider-code');
        field.type = 'password'; field.placeholder = 'Paste the code from the sign-in page';
        field.autocomplete = 'off'; field.setAttribute('aria-label', 'Claude sign-in code'); field.hidden = true;
        field.addEventListener('keydown', e => { if (e.key === 'Enter') sendCode(provider.id); });
        const submit = el('button', 'toolbar-button provider-code', 'Submit code');
        submit.type = 'button'; submit.hidden = true; submit.onclick = () => sendCode(provider.id);
        actions.append(field, submit);
      }
      for (const [method, text] of provider.actions) {
        const button = el('button', 'toolbar-button provider-action', text);
        button.type = 'button';
        button.onclick = () => method === 'api_key' ? saveKey(provider.id) : signIn(provider.id, method);
        actions.append(button);
      }
      const cancel = el('button', 'toolbar-button provider-cancel', 'Cancel');
      cancel.type = 'button'; cancel.hidden = true; cancel.onclick = () => cancelLogin(provider.id);
      const logout = el('button', 'text-button provider-logout', 'Disconnect');
      logout.type = 'button'; logout.hidden = true; logout.onclick = () => disconnect(provider.id);
      actions.append(cancel, logout);
      const flowBox = el('p', 'provider-flow');
      flowBox.setAttribute('role', 'status'); flowBox.setAttribute('aria-live', 'polite');
      node.append(head, el('p', 'settings-help provider-help', ''), actions, flowBox);
      return node;
    }));
  }

  async function load() {
    if (!$(listId).querySelector('.provider-card')) build();
    const current = ++request;
    let result;
    try { result = await api.command({type: 'ProvidersStatus'}); }
    catch (error) { if (current === request) for (const {id} of PROVIDERS) flow(id, `Status unavailable · ${error.message}`); return; }
    if (current !== request) return;
    for (const row of (result.providers || []).slice(0, 8)) {
      const node = card(row.id);
      if (!node) continue;
      const connected = row.connected === true;
      node.classList.toggle('connected', connected);
      node.querySelector('.provider-state').textContent = connected ? `Connected${row.detail ? ` · ${row.detail}` : ''}` : 'Not connected';
      node.querySelector('.provider-help').textContent = row.help || '';
      node.querySelector('.provider-logout').hidden = !connected || row.can_logout === false;
      if (row.login?.status === 'pending') { show(row.id, row.login); watch(row.id, row.login.login_id); }
    }
  }

  function show(id, login) {
    logins[id] = login.login_id;
    codeEntry[id] = login.code_entry === true;
    const text = login.user_code ? `Enter code ${login.user_code}, then approve access. Waiting…`
      : codeEntry[id] ? 'Sign in, then paste the code the page shows below. Waiting…' : 'Finish signing in, then return here. Waiting…';
    flow(id, text, String(login.url || '').startsWith('https://') ? login.url : '');
    pending(id, true);
  }

  async function signIn(id, method) {
    const domain = '';
    flow(id, 'Starting sign-in…');
    try {
      const login = await api.command({type: 'ProviderLogin', provider: id, method, domain});
      show(id, login);
      watch(id, login.login_id);
    } catch (error) { flow(id, error.message); }
  }

  function watch(id, loginId) {
    if (!loginId || polling.has(loginId)) return;
    polling.add(loginId);
    (async () => {
      try {
        for (let i = 0; i < POLL_LIMIT; i++) {
          await new Promise(resolve => setTimeout(resolve, POLL_MS));
          if (!isOpen()) return; // resumes from ProvidersStatus on reopen
          let login;
          try { login = await api.command({type: 'ProviderLoginPoll', login_id: loginId}); }
          catch (error) { flow(id, error.message); break; }
          if (login.status !== 'pending') { flow(id, login.message || ''); break; }
        }
      } finally { polling.delete(loginId); pending(id, false); await load(); }
    })();
  }

  async function cancelLogin(id) {
    if (!logins[id]) return;
    try { await api.command({type: 'ProviderLoginCancel', login_id: logins[id]}); }
    catch (error) { flow(id, error.message); }
  }

  async function sendCode(id) {
    const field = card(id)?.querySelector('input.provider-code');
    if (!field) return;
    const code = field.value;
    field.value = '';
    if (!code.trim() || !logins[id]) { flow(id, 'Paste the code shown after signing in first.'); return; }
    try { const login = await api.command({type: 'ProviderLoginCode', login_id: logins[id], code}); flow(id, login.message || ''); }
    catch (error) { flow(id, error.message); }
  }

  async function saveKey(id) {
    const field = input(id), key = field.value;
    field.value = '';
    if (!key.trim()) { flow(id, 'Paste your OpenCode Go API key first.'); return; }
    try { const result = await api.command({type: 'ProviderKeySet', provider: id, key}); flow(id, result.message || ''); }
    catch (error) { flow(id, error.message); }
    await load();
  }

  async function disconnect(id) {
    try { const result = await api.command({type: 'ProviderLogout', provider: id}); flow(id, result.message || ''); }
    catch (error) { flow(id, error.message); }
    await load();
  }

  return {load};
}
