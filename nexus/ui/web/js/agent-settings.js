// Standalone guided agent editor. No YAML serializer: only provably simple
// routing fields are changed; complex documents remain editable explicitly.
export const MAX_AGENT_FALLBACKS = 8;
const KEYS = ['model', 'provider', 'model_tier', 'fallback'];
const linesOf = body => body.match(/[^\n]*\n|[^\n]+$/g) || [];
const scalar = text => {
  text = text.trim();
  if (!text) return '';
  if (/^[A-Za-z0-9_./:@+-]+$/.test(text)) return text;
  if (/^"(?:[^"\\]|\\.)*"$/.test(text)) return JSON.parse(text);
  if (/^'(?:[^']|'')*'$/.test(text)) return text.slice(1, -1).replace(/''/g, "'");
  throw new Error('Complex routing YAML requires explicit editing.');
};
export function inspectAgentRouting(body) {
  const lines = linesOf(body), fields = {}, positions = {};
  const fail = reason => ({safe: false, reason, fields, lines});
  if (!lines.length || lines[0].replace(/^\uFEFF/, '').trimEnd() !== '---') return fail('No simple frontmatter block.');
  const end = lines.findIndex((line, i) => i > 0 && i < 400 && line.trimEnd() === '---');
  if (end < 0) return fail('No bounded frontmatter block.');
  try {
    for (let i = 1; i < end; i++) {
      const line = lines[i].replace(/[\r\n]+$/, ''), match = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(line);
      // YAML aliases/merge keys can supply hidden routing values. Do not guess.
      if (/^\s*<<:|^[^#\n]*[&*][\w-]+/.test(line)) return fail('YAML aliases require explicit editing.');
      if (!match && line.trim() && !/^\s|^#/.test(line)) return fail('Non-simple frontmatter keys require explicit editing.');
      if (!match || !KEYS.includes(match[1])) continue;
      const [, key, value] = match;
      if (key in positions) return fail('Duplicate routing keys require explicit editing.');
      if (i + 1 < end && /^\s+\S|^-\s/.test(lines[i + 1])) return fail('Multiline routing fields require explicit editing.');
      positions[key] = i;
      if (key === 'fallback') {
        if (!value.trim()) fields[key] = [];
        else {
          if (!/^\[.*\]$/.test(value.trim())) return fail('Complex fallbacks require explicit editing.');
          const inner = value.trim().slice(1, -1);
          // Splitting only unquoted simple refs is intentional. Quoted commas
          // and nested YAML must go through the explicit, host-validated editor.
          fields[key] = inner.trim() ? inner.split(',').map(scalar) : [];
          if (fields[key].length > MAX_AGENT_FALLBACKS || fields[key].some(ref => !ref)) return fail('Fallback list exceeds guided limits or contains empty entries.');
        }
      } else fields[key] = scalar(value);
    }
  } catch { return fail('Complex routing values require explicit editing.'); }
  const mode = fields.model_tier ? 'tier' : fields.model || fields.provider ? 'model' : 'session';
  return {safe: true, fields, positions, lines, end, mode};
}
export function transformAgentRouting(body, {mode, model = '', tier = '', fallbacks = []}) {
  const parsed = inspectAgentRouting(body);
  if (!parsed.safe) throw new Error(parsed.reason);
  if (!['session', 'model', 'tier'].includes(mode)) throw new Error('Unknown routing mode.');
  if (mode === 'model' && !model.trim()) throw new Error('Choose a model first.');
  if (mode === 'tier' && !tier.trim()) throw new Error('Choose a tier first.');
  if (!Array.isArray(fallbacks) || fallbacks.length > MAX_AGENT_FALLBACKS || fallbacks.some(ref => typeof ref !== 'string' || !ref.trim())) throw new Error('Use at most eight nonempty fallbacks.');
  const values = {};
  if (mode === 'model') { values.model = JSON.stringify(model.trim()); if (fallbacks.length) values.fallback = `[${fallbacks.map(ref => JSON.stringify(ref.trim())).join(', ')}]`; }
  if (mode === 'tier') values.model_tier = JSON.stringify(tier.trim());
  const newline = parsed.lines[0].endsWith('\r\n') ? '\r\n' : '\n';
  const head = parsed.lines.slice(1, parsed.end).filter((_, i) => !Object.values(parsed.positions).includes(i + 1));
  for (const [key, value] of Object.entries(values)) head.push(`${key}: ${value}${newline}`);
  return [parsed.lines[0], ...head, ...parsed.lines.slice(parsed.end)].join('');
}

// load({id, scope: 'global'|'project'}) is the entire integration contract.
// pickModel receives a callback accepting a provider/model reference string.
export function createAgentSettings({api, el, root, notify, isOpen, pickModel, onChange = () => {}, runSave = action => action()}) {
  let generation = 0, descriptor = null, data = null, tiers = null, busy = false, blocked = false;
  let draft = '', explicit = false, acknowledged = false;
  const active = request => request === generation && isOpen();
  const note = text => el('p', 'settings-help', text);
  const button = (label, action, disabled = false) => {
    const node = el('button', 'toolbar-button', label);
    node.type = 'button'; node.disabled = busy || blocked || disabled; node.onclick = action; return node;
  };
  function invalidate() {
    generation++; descriptor = data = tiers = null; busy = blocked = false;
    draft = ''; explicit = acknowledged = false; root.removeAttribute('aria-busy');
  }
  async function load(next) {
    if (!next || typeof next.id !== 'string' || !next.id || !['global', 'project'].includes(next.scope)) throw new Error('Agent descriptor requires id and global/project scope.');
    const request = ++generation;
    descriptor = {id: next.id, scope: next.scope}; data = null; tiers = null;
    busy = blocked = explicit = acknowledged = false; draft = '';
    root.setAttribute('aria-busy', 'true'); root.replaceChildren(note('Loading agent settings…'));
    try {
      const read = await api.command({type: 'SettingsRead', category: 'agents', ...descriptor});
      if (!active(request)) return;
      data = read; draft = String(read.body ?? ''); render();
      try { const result = await api.command({type: 'ModelTiers'}); if (active(request)) {tiers = result; render();} }
      catch (error) {if (active(request)) notify(`Tier choices unavailable: ${error.message}`);}
    } catch (error) {
      if (active(request)) root.replaceChildren(note(`Could not load agent: ${error.message}`), button('Retry', () => load(next)));
    } finally {if (request === generation) root.removeAttribute('aria-busy');}
  }
  async function write(body) {
    if (!data || busy || blocked || !isOpen() || body === data.body) return;
    const request = generation, target = {...descriptor}, sha = data.sha256;
    if (typeof sha !== 'string' || !sha) {notify('Missing file SHA; reload before saving.'); return;}
    busy = true; render();
    try {
      const result = await runSave(async () => {
        if (!active(request)) throw new Error('Agent settings changed; reload before saving.');
        const result = await api.command({type: 'SettingsWrite', category: 'agents', ...target, body, expected_sha256: sha});
        // Refresh the containing editor under the same write lock, even if this
        // panel was invalidated while the host was writing.
        if (result.status === 'written') await onChange(target, result);
        return result;
      });
      if (!active(request)) return;
      if (result.status === 'conflict') {
        blocked = true; notify('Changed on disk. Reload before saving; your draft has not been written.'); return;
      }
      if (result.status !== 'written') throw new Error(result.message || `Write rejected: ${result.status}`);
      // Only the host validates YAML/agent schema. Never mark a rejection saved.
      data = {...data, body, sha256: result.sha256, builtin: false}; draft = body;
      notify(`Saved agent in ${target.scope} scope${result.restart_required ? '. Restart required.' : '.'}`);
    } catch (error) {if (active(request)) notify(`Could not save agent: ${error.message}`);}
    finally {if (active(request)) {busy = false; render();}}
  }
  function update(change) {
    const parsed = inspectAgentRouting(draft);
    const f = parsed.fields;
    const model = f.model && f.provider && !f.model.includes('/') ? `${f.provider}/${f.model}` : f.model || '';
    try {draft = transformAgentRouting(draft, {mode: parsed.mode, model, tier: f.model_tier || '', fallbacks: f.fallback || [], ...change}); render();}
    catch (error) {notify(error.message);}
  }
  function pick(change) {
    const request = generation;
    pickModel(ref => {if (active(request) && !busy && !blocked && typeof ref === 'string' && ref.trim()) change(ref);});
  }
  function select(label, options, value, change) {
    const row = el('label', 'settings-row'), node = el('select');
    node.setAttribute('aria-label', label); node.disabled = busy || blocked;
    for (const [text, ref] of options) {const option = el('option', '', text); option.value = ref; node.append(option);}
    node.value = value; node.onchange = () => change(node.value); row.append(el('span', '', label), node); return row;
  }
  function render() {
    if (!data) return;
    const parsed = inspectAgentRouting(draft), f = parsed.fields;
    root.replaceChildren(el('h3', '', `Agent: ${descriptor.id}`), note(`${descriptor.scope} scope${data.builtin ? ' · built-in; saving creates an override' : ''}`));
    if (blocked) {
      root.append(note('Conflict: reload to read the current file. Copy your draft before reloading.'));
      const reload = el('button', 'toolbar-button', 'Reload from disk'); reload.type = 'button'; reload.onclick = () => load(descriptor); root.append(reload);
    }
    if (!parsed.safe) root.append(note(`${parsed.reason} Use explicit editing below; guided changes are disabled.`));
    if (parsed.safe && !explicit) {
      root.append(select('Routing', [['Follow session', 'session'], ['Model and fallbacks', 'model'], ['Tier', 'tier']], parsed.mode, mode => {
        if (mode === 'model') pick(ref => update({mode, model: ref, fallbacks: []}));
        else if (mode === 'tier') {
          const tier = tiers?.order?.[0]; if (tier) update({mode, tier}); else notify('No tiers available.');
        } else update({mode, fallbacks: []});
      }));
      if (parsed.mode === 'session') root.append(note('Follows the parent/session model. Explicit model, provider, tier and fallbacks are removed when selecting this mode.'));
      if (parsed.mode === 'tier') {
        const order = [...(tiers?.order || [])]; if (f.model_tier && !order.includes(f.model_tier)) order.push(f.model_tier);
        root.append(select('Model tier', order.map(name => [name, name]), f.model_tier, tier => update({mode: 'tier', tier})), note('Tier choices follow global tier settings; explicit model and fallbacks are cleared.'));
      }
      if (parsed.mode === 'model') {
        const ref = f.provider && f.model && !f.model.includes('/') ? `${f.provider}/${f.model}` : f.model || `${f.provider}/…`;
        root.append(note(`Model: ${ref}`), button('Choose model…', () => pick(model => update({mode: 'model', model}))));
        const list = el('ol', 'model-chain'), refs = f.fallback || [];
        refs.forEach((ref, index) => {
          const row = el('li', 'model-chain-row'); row.append(el('span', '', ref));
          const move = delta => {const next = [...refs]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; update({fallbacks: next});};
          row.append(button('↑', () => move(-1), index === 0), button('↓', () => move(1), index === refs.length - 1), button('Remove', () => update({fallbacks: refs.filter((_, i) => i !== index)})));
          row.children[1].setAttribute('aria-label', `Move up ${ref}`); row.children[2].setAttribute('aria-label', `Move down ${ref}`); row.children[3].setAttribute('aria-label', `Remove ${ref}`); list.append(row);
        });
        root.append(el('h4', '', 'Ordered fallbacks'), list, button('Add fallback…', () => pick(ref => update({fallbacks: [...refs, ref]})), refs.length >= MAX_AGENT_FALLBACKS), note('At most eight fallbacks, tried in order before the model replies.'));
      }
    }
    root.append(button(explicit ? 'Close explicit editor' : 'Explicit frontmatter / prompt edit', () => {explicit = !explicit; acknowledged = false; render();}));
    if (explicit || !parsed.safe || blocked) {
      const editor = el('textarea', 'files-input'); editor.value = draft; editor.disabled = busy || blocked; editor.setAttribute('aria-label', 'Agent source');
      editor.oninput = () => {draft = editor.value; acknowledged = false; guard.checked = false; save.disabled = true;};
      const label = el('label', 'settings-row'), guard = el('input'); guard.type = 'checkbox'; guard.checked = acknowledged; guard.disabled = busy || blocked;
      guard.onchange = () => {acknowledged = guard.checked; save.disabled = busy || blocked || !acknowledged || draft === data.body;};
      label.append(guard, el('span', '', 'I reviewed the complete source; validate and save this explicit edit.'));
      const save = button('Validate and save explicit edit', () => write(draft), !acknowledged || draft === data.body);
      root.append(editor, label, save);
    } else root.append(button('Validate and save', () => write(draft), draft === data.body));
    root.append(note('All saves are validated by the host and guarded by the original file SHA. Prompt and unrelated frontmatter are retained by guided edits.'));
  }
  return {load, invalidate};
}
