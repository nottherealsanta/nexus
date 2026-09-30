// Presentable tool call details: every parameter and output as labelled rows.
// Port of nexus/ui_support/tool_details.py; keep the two in step (plan section 14.7).
const VALUE_LIMIT = 20000, SECTION_ROWS = 400, MAX_DEPTH = 8;
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const scalar = v => v === null || v === undefined ? 'null' : typeof v === 'boolean' ? (v ? 'true' : 'false') : String(v);

export function toolDetailSections(tool, redact = s => s) {
  const clean = (v, limit = VALUE_LIMIT) => { const t = redact(String(v)); return t.length > limit ? `${t.slice(0, limit)}\n[clipped ${t.length - limit} more characters]` : t; };
  const row = (label, value) => { const text = clean(scalar(value)); return {label, value: text, block: text.includes('\n') || text.length > 120}; };
  const block = (label, value) => ({label, value: clean(value), block: true});
  const flatten = (value, prefix = '', depth = 0) => {
    if (Array.isArray(value)) {
      if (!value.length) return [{label: prefix || 'value', value: '[]'}];
      if (depth >= MAX_DEPTH) return [row(prefix, JSON.stringify(value))];
      if (value.length <= 16 && value.every(i => !i || typeof i !== 'object') && value.reduce((n, i) => n + scalar(i).length + 2, 0) <= 200) return [row(prefix || 'value', value.map(scalar).join(', '))];
      const grouped = value.every(item => item && typeof item === 'object' && !Array.isArray(item) && Object.keys(item).length);
      return value.flatMap((item, i) => {
        const name = prefix ? `${prefix}[${i}]` : `#${i + 1}`;
        // Each object becomes a headed group: `todos[0]` then its fields.
        return grouped ? [{label: name, value: '', header: true}, ...flatten(item, '', depth + 1).map(r => ({...r, indent: (r.indent || 0) + 1}))] : flatten(item, name, depth + 1);
      });
    }
    if (value && typeof value === 'object') {
      const keys = Object.keys(value);
      if (!keys.length) return [{label: prefix || 'value', value: '{}'}];
      if (depth >= MAX_DEPTH) return [row(prefix, JSON.stringify(value))];
      return keys.flatMap(k => flatten(value[k], prefix ? `${prefix}.${k}` : k, depth + 1));
    }
    return [row(prefix || 'value', value)];
  };
  const bounded = rows => rows.length <= SECTION_ROWS ? rows : [...rows.slice(0, SECTION_ROWS), {label: '…', value: `${rows.length - SECTION_ROWS} more rows clipped`}];
  const stamp = t => { if (t == null) return null; const d = new Date(t * 1000); return Number.isNaN(d.getTime()) ? String(t) : d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC'); };
  const status = tool.is_error || tool.status === 'failed' ? 'failed' : ['requested', 'running'].includes(tool.status) ? 'running' : (tool.status || 'running');
  const overview = [row('Status', status)];
  for (const [label, value] of [
    ['Tool', tool.name || null], ['Bundle', tool.bundle], ['Duration', tool.duration_ms == null ? null : `${tool.duration_ms} ms`],
    ['Requested', stamp(tool.requested_ts)], ['Started', stamp(tool.started_ts)], ['Finished', stamp(tool.finished_ts)],
    ['Call ID', tool.call_id || null], ['Model iteration', tool.iteration || null],
    ['Executed', tool.status !== 'requested' ? scalar(!!tool.executed) : null], ['Error result', tool.is_error ? 'yes' : null],
  ]) if (value != null) overview.push(row(label, value));
  const sections = [{title: 'Overview', rows: overview}];
  if (tool.input && Object.keys(tool.input).length) sections.push({title: 'Parameters', rows: bounded(flatten(tool.input))});
  if (tool.code) sections.push({title: 'Code', rows: [block('code', tool.code)]});
  if (tool.progress?.length) sections.push({title: 'Progress', rows: bounded(tool.progress.map((p, i) => ({label: `#${i + 1}`, value: clean(p, 2000), block: String(p).includes('\n')})))});
  if (tool.display) sections.push({title: 'Summary', rows: [block('display', tool.display)]});
  if (tool.result?.length) {
    const rows = [];
    tool.result.forEach((b, i) => {
      const kind = b && typeof b === 'object' && !Array.isArray(b) ? String(b.type ?? 'block') : 'block', label = tool.result.length > 1 ? `#${i + 1} ${kind}` : kind;
      if (b && typeof b.text === 'string') { rows.push(block(label, b.text)); for (const k of Object.keys(b)) if (k !== 'type' && k !== 'text') rows.push(...flatten(b[k], `${label}.${k}`)); }
      else if (b && typeof b === 'object' && !Array.isArray(b)) { const {type, ...rest} = b; const r = flatten(rest, label); rows.push(...(r.length ? r : [{label, value: ''}])); }
      else rows.push(...flatten(b, label));
    });
    sections.push({title: 'Result', rows: bounded(rows)});
  }
  if (tool.error) sections.push({title: 'Error', rows: [block('error', tool.error)]});
  if (tool.context_note) sections.push({title: 'Context', rows: [block('note', tool.context_note)]});
  const todos = tool.metrics?.todos;
  if (Array.isArray(todos) && todos.length && todos.every(t => t && typeof t === 'object')) {
    const glyph = {completed: '[x]', in_progress: '[~]', pending: '[ ]', cancelled: '[-]'};
    sections.push({title: 'Todo list', rows: bounded(todos.map((t, i) => ({label: `${glyph[String(t.status)] || '[?]'} ${t.id ?? i + 1}`, value: [clean(t.content ?? '', 2000), t.priority ? `(${t.priority})` : ''].filter(Boolean).join(' ')})))});
  }
  if (tool.metrics && Object.keys(tool.metrics).length) sections.push({title: 'Metrics', rows: bounded(flatten(tool.metrics))});
  if (tool.diff && typeof tool.diff === 'object') {
    const d = tool.diff, rows = [row('Path', d.path || 'edit'), row('Added lines', d.added_lines ?? 0), row('Removed lines', d.removed_lines ?? 0)];
    if (d.truncated) rows.push(row('Preview', 'clipped'));
    if (typeof d.hunk === 'string' && d.hunk) rows.push(block('Hunk', d.hunk));
    sections.push({title: 'Diff', kind: 'diff', rows});
  }
  return sections;
}

export function renderToolDetails(sections) {
  const root = node('div', 'td-root');
  for (const section of sections) {
    const box = node('section', 'td-section'), list = node('dl', 'td-rows');
    box.append(node('h3', 'td-title', section.title));
    for (const r of section.rows) {
      if (r.header) { const h = node('dt', 'td-group', r.label); h.style.setProperty('--td-indent', r.indent || 0); list.append(h); continue; }
      const term = node('dt', 'td-label', r.label), value = node('dd', r.block ? 'td-value td-block' : 'td-value');
      term.style.setProperty('--td-indent', r.indent || 0); value.style.setProperty('--td-indent', r.indent || 0);
      if (r.block && section.kind === 'diff') for (const line of r.value.split('\n')) value.append(node('span', line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : '', `${line}\n`));
      else value.textContent = r.value;
      list.append(term, value);
    }
    box.append(list); root.append(box);
  }
  return root;
}
