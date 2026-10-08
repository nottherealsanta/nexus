// Persistent MCP controls. ContextInspect supplies redacted effective state;
// SettingsRead is used only for the optimistic-concurrency hash, never its body.
export function createMcpSettings({api, el, root, notify, isOpen}) {
  let generation = 0, session = '', scope = 'global', rows = null, busy = false, loading = false, message = '';
  const note = text => el('p', 'settings-help', text);
  const active = request => request === generation && isOpen();
  const button = (text, run) => {
    const node = el('button', 'toolbar-button', text);
    node.type = 'button'; node.disabled = busy || loading; node.onclick = run;
    return node;
  };
  function report(text) { message = text; notify(text); }
  async function load(nextSession = session, nextScope = scope) {
    if (!['global', 'project'].includes(nextScope)) throw new Error('Unknown MCP settings scope');
    if (nextSession !== session || nextScope !== scope) rows = null;
    session = nextSession; scope = nextScope;
    const request = ++generation;
    loading = true; render();
    try {
      if (!session) throw new Error('Select a session to inspect MCP server state');
      const result = await api.command({type: 'ContextInspect', session});
      if (!active(request)) return;
      rows = (result.mcp_servers || []).filter(row => row.scope === scope);
      message = '';
    } catch (error) {
      if (!active(request)) return;
      rows = null; message = `MCP server state is unavailable: ${error.message}`;
    } finally {
      if (active(request)) { loading = false; render(); }
    }
  }
  async function save(row, field, value) {
    if (busy || loading || !isOpen()) return;
    const request = generation, savedScope = scope;
    busy = true; message = ''; render();
    try {
      const file = await api.command({type: 'SettingsRead', scope: savedScope, category: 'mcp', id: 'mcp.json'});
      if (!active(request)) return;
      if (typeof file.sha256 !== 'string' || !file.sha256) throw new Error('mcp.json hash is unavailable');
      const command = field === 'enabled'
        ? {type: 'SettingsMcpEnabledSet', enabled: value}
        : {type: 'SettingsMcpLoadingSet', mode: value};
      const result = await api.command({...command, scope: savedScope, server: row.name, expected_sha256: file.sha256});
      if (!active(request)) return;
      const text = result.status === 'conflict'
        ? 'mcp.json changed on disk; settings were not saved. Reload and try again.'
        : result.status === 'written'
          ? `${row.name} saved in ${savedScope} scope · applies to new sessions.`
          : `Could not save ${row.name}: ${result.status || 'unexpected host response'}`;
      // Refresh the persisted controls; never retry a conflicted write automatically.
      await load();
      if (isOpen() && generation === request + 1) report(text);
    } catch (error) {
      if (active(request)) report(`Could not save MCP settings: ${error.message}`);
    } finally { busy = false; if (isOpen()) render(); }
  }
  function render() {
    root.setAttribute('aria-busy', String(busy || loading));
    root.replaceChildren(el('h3', '', 'MCP servers'), note('Persistent settings in mcp.json apply to new sessions. Current session overrides and frozen loading choices are shown separately.'));
    const scopeRow = el('label', 'settings-row'), select = el('select');
    select.setAttribute('aria-label', 'MCP settings scope'); select.disabled = busy || loading;
    for (const [value, label] of [['global', 'Global · ~/.nexus'], ['project', 'Project · <workspace>/.agents']]) {
      const option = el('option', '', label); option.value = value; select.append(option);
    }
    select.value = scope; select.onchange = () => load(session, select.value);
    scopeRow.append(el('span', 'settings-label', 'Persistent scope'), select);
    root.append(scopeRow, note('Project definitions override global servers with the same name. Only effective definitions in the selected scope are listed.'));
    if (loading) root.append(note('Loading MCP server state…'));
    if (message) { const status = note(message); status.setAttribute('role', 'status'); root.append(status); }
    if (rows && !rows.length) root.append(note(`No effective MCP servers in ${scope} scope. Add or edit mcp.json in the file editor below.`));
    for (const row of rows || []) {
      const section = el('section', 'model-settings-section');
      section.append(el('h4', '', String(row.name)), note(`${row.status || 'unknown'} · ${row.tool_count ?? 0} tools · ${row.transport || 'unknown transport'}`));
      const enabledRow = el('label', 'settings-row'), enabled = el('input');
      enabled.type = 'checkbox'; enabled.checked = row.config_enabled !== false; enabled.disabled = busy || loading;
      enabled.setAttribute('role', 'switch'); enabled.setAttribute('aria-label', `Persistently enable ${row.name}`);
      enabled.onchange = () => save(row, 'enabled', enabled.checked);
      enabledRow.append(el('span', 'settings-label', 'Enabled for new sessions'), enabled);
      const loadingRow = el('label', 'settings-row'), mode = el('select');
      mode.setAttribute('aria-label', `Persistent tool loading for ${row.name}`); mode.disabled = busy || loading;
      for (const [value, label] of [['search', 'Search'], ['all', 'All']]) {
        const option = el('option', '', label); option.value = value; mode.append(option);
      }
      mode.value = row.config_tool_loading || 'search';
      mode.onchange = () => { if (['search', 'all'].includes(mode.value)) return save(row, 'loading', mode.value); };
      loadingRow.append(el('span', 'settings-label', 'Tool loading for new sessions'), mode);
      section.append(enabledRow, loadingRow, note(`Search finds tools on demand. All includes every schema (~${row.schema_tokens ?? 0} tokens).`),
        note(`Current session: ${row.enabled ? 'enabled' : 'disabled'} · loading ${row.tool_loading || 'search'} · source ${row.tool_loading_source || 'default'}.`));
      root.append(section);
    }
    root.append(button('Reload MCP settings', () => load()));
  }
  return {load, invalidate: () => { generation++; rows = null; loading = false; message = ''; root.removeAttribute('aria-busy'); }};
}
