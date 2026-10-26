// Sessions sidebar wording shared with the desktop panel (rust/desktop/src/panels.rs):
// descriptive titles, the metadata line, the pinned Manager card and the session tree rows.
// Pure functions over host data; the Manager words port ui_support/manager.py:entry.
const MOCK_SUFFIX = /\s*(?:⟦mock scenario=[^⟧]*⟧|\[mock scenario=[^\]]*\])\s*$/;
export const MANAGER_MODEL_REF = 'codex/gpt-6-luna';
export const MANAGER_PRIVACY = "Voice calls also share other sessions' titles, status and reports with OpenAI";

export function sessionTitle(title) {
  const text = String(title || ''), clean = text.replace(MOCK_SUFFIX, '').trim();
  return clean || (text.trim() ? 'Mock session' : 'Untitled conversation');
}

const STATUS_PREFIX = {working: 'Working · ', input: 'Needs input · '};
export function sessionMeta(session, status, age) {
  const count = Number(session.message_count) || 0;
  if (status === 'archived') return age ? `archived · ${age}` : 'archived';
  const messages = `${count} message${count === 1 ? '' : 's'}`;
  return `${STATUS_PREFIX[status] || ''}${messages}${age ? ` · active ${age}` : ''}`;
}

const clean = (value, limit = 200) => {
  const text = String(value || '').replace(/[\u0000-\u001f\u007f]/g, ' ').split(/\s+/).filter(Boolean).join(' ');
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
};
const stateStatus = value => ['running', 'working', 'active'].includes(value) ? 'working'
  : ['awaiting_input', 'awaiting_permission', 'input'].includes(value) ? 'input' : 'idle';

// The pinned Manager card from a ManagerState result; null before the host answered.
export function managerCard(result, current = '') {
  if (!result) return null;
  const enabled = Boolean(result.enabled), session = clean(result.session, 256);
  const model = clean(result.model || MANAGER_MODEL_REF, 120), managed = (result.managed || []).length;
  const status = enabled ? stateStatus(String(result.state || '')) : 'idle';
  let tone = 'off', summary = 'Off · [manager] enabled = false', detail = 'Turn it on with [manager] enabled = true in nexus.toml, then restart the daemon.';
  if (enabled) {
    if (result.signed_in) {
      tone = 'ok';
      summary = {working: 'Working', input: 'Needs you'}[status] || 'Ready';
      if (managed) summary += ` · ${managed} session${managed === 1 ? '' : 's'} launched`;
    } else {
      tone = 'warn';
      summary = `Not signed in · ${clean(result.sign_in || 'sign in to ChatGPT (Codex) under Settings > Providers')}`;
    }
    const live = !result.codex_signed_in ? 'Voice needs a ChatGPT (Codex) sign-in'
      : !result.live_consent || !result.live_manager_consent ? 'Voice asks for its data notice the first time' : 'Voice ready';
    detail = `${model} · ${live}`;
  }
  return {active: Boolean(session) && session === current, session, status, tone, summary, detail, enabled,
    privacy: enabled ? MANAGER_PRIVACY : ''};
}

// Indented rows of a SessionTree result: `{depth, title, status, id}`.
export function treeRows(tree) {
  const rows = [], walk = (nodes, depth) => {
    for (const node of nodes || []) {
      if (rows.length >= 500 || depth > 8) return;
      rows.push({depth, id: node.id, title: sessionTitle(node.title || node.description || node.task), status: node.status || ''});
      walk(node.children, depth + 1);
    }
  };
  walk(tree?.nodes, 0);
  return rows;
}
