// Settings → Agents/Tools/MCP servers/Skills/Hooks/Config/Soul: the list + editor
// same SettingsInventory/Read/Write/Delete/Reset host commands. Edits autosave
// 700 ms after typing stops; agent model/fallbacks are a form over the frontmatter
// (port of ui_support/agent_frontmatter.py).
export const FILE_CATEGORIES = {
  agents: ['Agents', 'Build is the default root agent; advisor, task and quick are subagents. Blank model fields inherit the session model. Fallbacks are tried in order when the model fails before replying.'],
  tools: ['Tools', 'Python tools loaded from <scope>/tools.'],
  mcp: ['MCP servers', 'MCP servers from mcp.json (mcpServers).'],
  skills: ['Skills', 'Skills from <scope>/skills/<name>/SKILL.md.'],
  hooks: ['Hooks', 'Lifecycle hooks from hooks.toml.'],
  config: ['Config', 'Nexus configuration: models, fallbacks, permissions, context and more.'],
  soul: ['Soul', 'Instructions added to every conversation (SOUL.md).'],
};
const MAX_FALLBACKS = 8, AGENT_ORDER = {build: 0, orchestrator: 1, advisor: 2, task: 3, quick: 4}, FORM_FIELDS = ['model', 'provider', 'reasoning_effort', 'fallback'];

const bounds = lines => {
  if (!lines.length || lines[0].replace(/[\r\n]+$/, '').replace(/^﻿/, '') !== '---') return null;
  for (let i = 1; i < Math.min(lines.length, 400); i++) if (lines[i].replace(/[\r\n]+$/, '') === '---') return [1, i];
  return null;
};
const splitLines = body => body.match(/[^\n]*\n|[^\n]+$/g) || [];
export function agentFields(body) {
  const lines = splitLines(body), b = bounds(lines), fields = {};
  if (!b) return fields;
  for (const line of lines.slice(b[0], b[1])) {
    const text = line.replace(/[\r\n]+$/, ''), at = text.indexOf(':');
    if (at > 0) { const key = text.slice(0, at); if (key === key.trim() && !(key in fields)) fields[key] = text.slice(at + 1).trim(); }
  }
  return fields;
}
export function fallbackItems(value) {
  let inner = value.trim();
  if (inner.startsWith('[') && inner.endsWith(']')) inner = inner.slice(1, -1);
  return inner.replace(/\n/g, ',').split(',').map(s => s.trim()).filter(Boolean).slice(0, MAX_FALLBACKS);
}
export function setAgentFields(body, updates) {
  const lines = splitLines(body), b = bounds(lines);
  if (!b) return body;
  const nl = lines[0].endsWith('\r\n') ? '\r\n' : '\n', head = lines.slice(b[0], b[1]);
  for (const [key, raw] of Object.entries(updates)) {
    if (!FORM_FIELDS.includes(key)) continue;
    let value = String(raw).split(/\s+/).filter(Boolean).join(' ');
    if (key === 'fallback' && value) { value = `[${fallbackItems(value).join(', ')}]`; if (value === '[]') value = ''; }
    const index = head.findIndex(line => line.split(':')[0] === key);
    if (!value) { if (index >= 0) head.splice(index, 1); continue; }
    const line = `${key}: ${value}${nl}`;
    if (index >= 0) head[index] = line; else head.push(line);
  }
  return [...lines.slice(0, b[0]), ...head, ...lines.slice(b[1])].join('');
}

export function createSettingsFiles({api, el, $, notify, onChange = () => {}}) {
  const s = {category: 'agents', scope: 'global', items: [], id: '', sha: null, saved: '', builtin: false, overrides: false, pendingNew: false, revision: 0, timer: null, models: null, saving: Promise.resolve()};
  const editor = () => $('files-editor'), dirty = () => s.id !== '' && (s.pendingNew || editor().value !== s.saved);
  const status = text => { $('files-status').textContent = text; };
  const whereText = () => s.scope === 'global' ? '~/.nexus' : '<project>/.agents';

  function renderList() {
    const box = $('files-list'), rows = s.items.filter(r => r.category === s.category)
      .sort((a, b) => (AGENT_ORDER[a.id] ?? 9) - (AGENT_ORDER[b.id] ?? 9) || String(a.id).localeCompare(String(b.id)));
    box.replaceChildren(...rows.map(row => {
      const b = el('button', `files-item${row.id === s.id ? ' selected' : ''}`, String(row.label || row.id)); b.type = 'button';
      if (row.builtin) b.append(el('small', '', 'built-in')); else if (row.overrides_builtin) b.append(el('small', '', 'edited'));
      b.onclick = () => openItem(row.id);
      return b;
    }));
    if (!rows.length) box.append(el('p', 'settings-help', 'Nothing here yet.'));
  }
  function syncActions() {
    const del = $('files-delete'); del.textContent = s.overrides ? 'Reset to default' : 'Delete'; del.disabled = !s.id || s.builtin;
    $('files-editor').disabled = !s.id;
  }
  function clearEditor() {
    s.revision++; s.pendingNew = false; s.id = ''; s.sha = null; s.saved = ''; s.builtin = s.overrides = false;
    editor().value = ''; $('files-title').textContent = 'Select an item'; syncForm(); syncActions(); status('');
  }
  async function loadInventory() {
    try {
      const result = await api.command({type: 'SettingsInventory', scope: s.scope});
      s.items = (result.items || []).slice(0, 512);
      $('files-scope-path').textContent = result.root_display || whereText();
      const counts = Object.fromEntries((result.categories || []).map(c => [c.key, c.count]));
      document.querySelectorAll('.settings-nav a[data-category]').forEach(a => { const n = counts[a.dataset.category]; let badge = a.querySelector('.nav-count'); if (n) { badge ||= a.appendChild(el('span', 'nav-count')); badge.textContent = n; } else badge?.remove(); });
      renderList();
    } catch (e) { status(`Inventory unavailable: ${e.message}`); }
  }
  async function leave() { await flush(); return !dirty() || window.confirm('Discard invalid changes?'); }
  async function openItem(id) {
    if (!(await leave())) return;
    const revision = ++s.revision, {scope, category} = s;
    try {
      const r = await api.command({type: 'SettingsRead', scope, category, id});
      if (revision !== s.revision || scope !== s.scope || category !== s.category) return;
      Object.assign(s, {id, sha: r.sha256, builtin: !!r.builtin, overrides: !!r.overrides_builtin, saved: String(r.body ?? ''), pendingNew: false});
      editor().value = s.saved; $('files-title').textContent = r.rel_path || id;
      syncForm(); syncActions(); renderList();
      status(s.builtin ? `Built-in default · saving writes an override to ${whereText()}` : '');
    } catch (e) { status(e.message); }
  }
  async function save() {
    if (!s.id || !(s.category in FILE_CATEGORIES)) return;
    const body = editor().value; status('Saving…');
    try {
      const r = await api.command({type: 'SettingsWrite', scope: s.scope, category: s.category, id: s.id, body, expected_sha256: s.sha});
      if (r.status === 'conflict') { status('Changed on disk since it was opened · reopen to edit'); return; }
      Object.assign(s, {sha: r.sha256, saved: body, pendingNew: false, overrides: s.overrides || s.builtin, builtin: false});
      syncActions(); status(`Saved · ${(r.loaded || []).length} loaded · ${(r.failed || []).length} failed`);
      await loadInventory(); onChange(s.category);
    } catch (e) { status(e.message); }
  }
  function flush() { clearTimeout(s.timer); s.timer = null; s.saving = s.saving.then(() => dirty() ? save() : undefined); return s.saving; }
  function scheduleSave() { clearTimeout(s.timer); if (dirty()) s.timer = setTimeout(flush, 700); if (s.category === 'agents' && s.id) syncForm(); }

  // Agent model form over the frontmatter.
  const modelRef = f => { const m = f.model || '', p = f.provider || ''; return m && p && !m.includes('/') ? `${p}/${m}` : m || (p ? `${p}/…` : ''); };
  const setFields = updates => { const next = setAgentFields(editor().value, updates); if (next !== editor().value) { editor().value = next; scheduleSave(); } syncForm(); };
  const fallbacks = () => fallbackItems(agentFields(editor().value).fallback || '');
  async function loadModels() {
    if (s.models) return s.models;
    try { const r = await api.command({type: 'ModelsList', selectable_only: true}); s.models = (r.models || []).filter(m => m.provider && m.id); } catch { s.models = []; }
    $('agent-model-options').replaceChildren(...s.models.map(m => { const o = el('option'); o.value = `${m.provider}/${m.id}`; return o; }));
    return s.models;
  }
  function syncForm() {
    const form = $('agent-form'); form.hidden = !(s.category === 'agents' && s.id); if (form.hidden) return;
    const f = agentFields(editor().value), model = modelRef(f), input = $('agent-model');
    if (document.activeElement !== input) input.value = model;
    const select = $('agent-effort'), supported = (s.models || []).find(m => `${m.provider}/${m.id}` === model)?.supported_efforts || [], effort = f.reasoning_effort || '';
    const options = ['', ...new Set([...supported, ...(effort ? [effort] : [])])];
    select.replaceChildren(...options.map(v => { const o = el('option', '', v || 'default'); o.value = v; return o; })); select.value = effort;
    const list = fallbacks(), box = $('agent-fallbacks');
    if (![...box.querySelectorAll('input')].some(i => i === document.activeElement)) box.replaceChildren(...list.map((ref, index) => {
      const row = el('div', 'agent-form-row'), inp = el('input', 'files-input'); inp.value = ref; inp.setAttribute('list', 'agent-model-options'); inp.setAttribute('aria-label', `Fallback ${index + 1}`);
      inp.onchange = () => { const cur = fallbacks(); if (inp.value.trim()) cur[index] = inp.value.trim(); else cur.splice(index, 1); setFields({fallback: cur.join(', ')}); };
      const rm = el('button', 'toolbar-button', '×'); rm.type = 'button'; rm.setAttribute('aria-label', `Remove fallback ${index + 1}`); rm.onclick = () => { const cur = fallbacks(); cur.splice(index, 1); setFields({fallback: cur.join(', ')}); };
      row.append(inp, rm); return row;
    }));
    $('agent-fallback-add').hidden = list.length >= MAX_FALLBACKS;
  }

  async function showCategory(category) {
    if (!(await leave())) return false;
    s.category = category;
    if (category === 'agents') s.scope = 'global';
    $('files-scope').hidden = category === 'agents';
    document.querySelectorAll('input[name="files-scope"]').forEach(r => { r.checked = r.value === s.scope; });
    const [label, help] = FILE_CATEGORIES[category];
    $('files-heading').textContent = label; $('files-help').textContent = help;
    $('files-default-agent').hidden = category !== 'agents';
    $('files-new-name').hidden = true; clearEditor(); renderList();
    if (category === 'agents') loadModels().then(syncForm);
    loadInventory(); return true;
  }
  async function setScope(scope) {
    if (s.category === 'agents' || scope === s.scope || !(await leave())) { document.querySelector(`input[name="files-scope"][value="${s.scope}"]`).checked = true; return; }
    s.scope = scope; clearEditor(); await loadInventory(); onChange('scope');
  }
  async function create(name) {
    const id = name.trim(); if (!id) return;
    if (!(await leave())) return;
    clearEditor(); Object.assign(s, {id, pendingNew: true, sha: ''});
    const templates = {
      agents: `---\nname: ${id}\ndescription: Describe when the root agent should use ${id}.\ncontexts: [subagent]\n---\nYou are a subagent. Do the task you are given and finish with a report that lists every file you changed.\n`,
      skills: `---\nname: ${id}\ndescription: Describe this skill.\n---\nInstructions.\n`,
      mcp: '{"mcpServers": {}}\n',
    };
    editor().value = templates[s.category] || ''; $('files-title').textContent = id;
    syncForm(); syncActions(); editor().focus(); await save();
  }
  async function remove() {
    if (!(await leave()) || !s.id || s.builtin) return;
    const reset = s.overrides;
    if (!window.confirm(reset ? `Reset ${s.id} to the built-in default? Your edits move to trash.` : `Delete ${s.id} to trash?`)) return;
    try { await api.command({type: 'SettingsDelete', scope: s.scope, category: s.category, id: s.id}); } catch (e) { status(e.message); return; }
    clearEditor(); status(reset ? 'Reset to built-in default' : 'Deleted to trash'); await loadInventory(); onChange(s.category);
  }

  function install() {
    editor().addEventListener('input', scheduleSave);
    document.querySelectorAll('input[name="files-scope"]').forEach(r => r.addEventListener('change', () => r.checked && setScope(r.value)));
    $('files-new').onclick = () => { const i = $('files-new-name'); i.hidden = !i.hidden; if (!i.hidden) i.focus(); };
    $('files-new-name').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); const v = e.target.value; e.target.value = ''; e.target.hidden = true; create(v); } else if (e.key === 'Escape') { e.stopPropagation(); e.target.hidden = true; } });
    $('files-delete').onclick = remove;
    $('agent-model').onchange = async () => { const ref = $('agent-model').value.trim(), rows = await loadModels(), supported = rows.find(m => `${m.provider}/${m.id}` === ref)?.supported_efforts || [], effort = agentFields(editor().value).reasoning_effort || ''; setFields({model: ref, provider: '', reasoning_effort: supported.includes(effort) ? effort : ''}); };
    $('agent-effort').onchange = () => setFields({reasoning_effort: $('agent-effort').value});
    $('agent-model-clear').onclick = () => setFields({model: '', provider: '', reasoning_effort: ''});
    $('agent-fallback-add').onclick = () => { const cur = fallbacks(); if (cur.length >= MAX_FALLBACKS) return; const box = $('agent-fallbacks'), row = el('div', 'agent-form-row'), inp = el('input', 'files-input'); inp.setAttribute('list', 'agent-model-options'); inp.setAttribute('aria-label', 'New fallback'); inp.onchange = () => { if (inp.value.trim()) setFields({fallback: [...cur, inp.value.trim()].join(', ')}); else row.remove(); }; row.append(inp); box.append(row); inp.focus(); };
  }
  return {install, showCategory, flush, get dirty() { return dirty(); }, get scope() { return s.scope; }, reset: async () => {
    if (!(await leave())) return;
    const names = s.items.filter(r => r.category === s.category && !r.builtin && (s.category !== 'agents' || r.overrides_builtin)).map(r => r.id);
    if (!window.confirm(`Reset ${s.category} to default? Removed files move to trash.${names.length ? `\n${names.join(', ')}` : ''}`)) return;
    try { await api.command({type: 'SettingsReset', scope: s.scope, category: s.category}); } catch (e) { status(e.message); return; }
    clearEditor(); await loadInventory(); status('Reset to default'); onChange(s.category);
  }, leave};
}
