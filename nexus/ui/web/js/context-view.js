function node(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function json(value) {
  try { return JSON.stringify(value, null, 2); }
  catch { return String(value ?? ''); }
}

function section(title, content) {
  const element = node('section', 'context-section context-request-section');
  element.append(node('h3', '', title), content);
  return element;
}

function previewSection(title, count) {
  const element = node('section', 'context-preview-card');
  const heading = node('h3', 'context-preview-card-title', title);
  if (count !== undefined) heading.append(node('span', 'context-preview-count', count));
  element.append(heading);
  const content = node('div', 'context-preview-card-content');
  element.append(content);
  return {element, content};
}

function previewEmpty(parent, message) {
  parent.append(node('p', 'context-preview-empty', message));
}

function appendPart(parent, value, {preview, openDetails}) {
  const text = typeof value === 'string' ? value : `\`\`\`json\n${json(value)}\n\`\`\``;
  if (!preview) {
    parent.append(markdown(text));
    return;
  }
  const content = markdown(text);
  content.classList.add('context-inline-part-preview');
  content.setAttribute('role', 'button');
  content.setAttribute('tabindex', '0');
  content.setAttribute('aria-label', 'Open full content in context modal');
  content.setAttribute('aria-haspopup', 'dialog');
  content.onclick = openDetails;
  content.onkeydown = event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openDetails();
    }
  };
  parent.append(content);
  const reveal = node('button', 'context-link context-inline-part-more', '...');
  reveal.type = 'button';
  reveal.hidden = true;
  reveal.setAttribute('aria-label', 'Show full content in modal');
  reveal.title = 'Show full content in modal';
  reveal.onclick = openDetails;
  parent.append(reveal);
  const updateTruncation = () => {
    if (!content.isConnected) return;
    const clipped = content.scrollHeight > content.clientHeight + 1;
    reveal.hidden = !clipped;
  };
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(updateTruncation);
    observer.observe(content);
  }
  requestAnimationFrame(updateTruncation);
}

function safeLink(value) {
  try {
    const url = new URL(value, document.baseURI);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function inlineMarkdown(parent, source) {
  const pattern = /(`[^`\n]+`|\[[^\]]+\]\([^\s)]+(?:\s+"[^"]*")?\)|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_)/g;
  let offset = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > offset) parent.append(document.createTextNode(source.slice(offset, match.index)));
    const token = match[0];
    let element;
    if (token.startsWith('`')) {
      element = node('code', '', token.slice(1, -1));
    } else if (token.startsWith('[')) {
      const link = token.match(/^\[([^\]]+)\]\(([^\s)]+)(?:\s+"([^"]*)")?\)$/);
      const href = link && safeLink(link[2]);
      if (href) {
        element = node('a', '', link[1]);
        element.href = href;
        element.rel = 'noopener noreferrer';
        element.target = '_blank';
        if (link[3]) element.title = link[3];
      } else {
        parent.append(document.createTextNode(token));
      }
    } else if (token.startsWith('**') || token.startsWith('__')) {
      element = node('strong', '', token.slice(2, -2));
    } else {
      element = node('em', '', token.slice(1, -1));
    }
    if (element) parent.append(element);
    offset = match.index + token.length;
  }
  if (offset < source.length) parent.append(document.createTextNode(source.slice(offset)));
}

// A deliberately small Markdown renderer. Every node and text fragment is
// created with DOM APIs; raw HTML is always text and links are protocol-filtered.
function markdown(value) {
  const root = node('div', 'context-markdown');
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  let list = null;
  let code = null;
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const block = node('p');
    paragraph.forEach((line, index) => {
      if (index) block.append(document.createElement('br'));
      inlineMarkdown(block, line);
    });
    root.append(block);
    paragraph = [];
  };
  const flushList = () => { list = null; };
  for (const line of lines) {
    const fence = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)/);
    if (code) {
      if (fence && fence[1][0] === code.marker) {
        code.code.textContent = code.text.join('\n');
        root.append(code.pre);
        code = null;
      } else code.text.push(line);
      continue;
    }
    if (fence) {
      flushParagraph(); flushList();
      const pre = node('pre', 'context-code');
      code = {marker: fence[1][0], pre, text: []};
      code.code = node('code');
      code.pre.append(code.code);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    const item = line.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
    if (!line.trim()) { flushParagraph(); flushList(); continue; }
    if (heading) {
      flushParagraph(); flushList();
      const block = node(`h${heading[1].length}`);
      inlineMarkdown(block, heading[2]);
      root.append(block);
    } else if (item) {
      flushParagraph();
      const ordered = /^\d/.test(item[1]);
      if (!list || list.tagName !== (ordered ? 'OL' : 'UL')) {
        flushList();
        list = node(ordered ? 'ol' : 'ul');
        root.append(list);
      }
      const li = node('li');
      inlineMarkdown(li, item[2]);
      list.append(li);
    } else {
      flushList();
      paragraph.push(line);
    }
  }
  if (code) { code.code.textContent = code.text.join('\n'); root.append(code.pre); }
  flushParagraph();
  return root;
}

function blockText(block) {
  if (block?.type === 'text' || block?.type === 'thinking') return block.text || '';
  if (block?.type === 'tool_use') return `**Tool call · ${block.name || 'tool'} · id ${block.id || 'not reported'}**\n\n\`\`\`json\n${json(block.input || {})}\n\`\`\``;
  if (block?.type === 'tool_result') {
    return `**Tool result · for call ${block.tool_use_id || 'not reported'}${block.is_error ? ' · error' : ''}**\n\n${(block.content || []).map(row => row.text || '[image omitted]').join('\n')}`;
  }
  return block?.text || `[${block?.type || 'content'} omitted]`;
}

function messageEstimate(message) {
  const size = String(message?.role || '').length + (message?.blocks || []).reduce(
    (total, block) => total + blockText(block).length, 0,
  );
  return Math.max(1, Math.ceil(size / 4));
}

export function renderCurrentContext({result, loading, error, chooseAgent, chooseModel, openDetails, preview = false}) {
  const root = node('div', 'context-request-tree');
  if (loading) {
    root.append(node('p', 'context-muted', 'Loading the assembled request…'));
    return root;
  }
  if (error) {
    root.append(node('p', 'context-muted', `Context unavailable · ${error}`));
    return root;
  }
  if (!result) {
    root.append(node('p', 'context-muted', 'Current request has not been inspected.'));
    return root;
  }

  const agent = result.agent || {};
  if (preview) {
    const controls = node('div', 'context-inline-summary');
    for (const [label, text, action] of [
      ['Choose agent', `Agent · ${agent.name || 'Default'}`, chooseAgent],
      ['Choose model', `Model · ${[result.provider, result.model].filter(Boolean).join('/') || 'Not reported'}`, chooseModel],
    ]) {
      const button = node('button', 'context-pill context-choice', text);
      button.type = 'button';
      button.setAttribute('aria-label', label);
      button.onclick = action;
      controls.append(button);
    }
    root.append(controls);

    const accounting = result.request_context || {};
    root.append(node(
      'p', 'context-muted context-request-accounting',
      `${accounting.used_tokens == null ? 'Token use not reported' : `${Number(accounting.used_tokens).toLocaleString()} / ${Number(accounting.input_budget || 0).toLocaleString()} input tokens`} · ${result.tools?.length || 0} tools · ${result.messages?.length || 0} messages · history ${result.history_included ? 'included' : 'empty'}`,
    ));

    const cards = node('div', 'context-preview-grid');
    const system = previewSection('SYSTEM PROMPT · request.system');
    if (typeof result.system_text === 'string' && result.system_text.trim()) {
      appendPart(system.content, result.system_text, {preview, openDetails});
    } else {
      previewEmpty(system.content, 'No system prompt text reported.');
    }
    cards.append(system.element);

    const toolRows = Array.isArray(result.tools) ? result.tools : [];
    const tools = previewSection(`TOOLS · structured request.tools · ${toolRows.length}`);
    if (result.tools_supported === false) {
      previewEmpty(tools.content, 'Tools are not supported by the selected model.');
    } else if (!toolRows.length) {
      previewEmpty(tools.content, 'No tool definitions reported.');
    }
    for (const [index, tool] of toolRows.entries()) {
      const item = node('article', 'context-preview-item');
      item.append(node('h4', '', tool.name || `Tool ${index + 1}`));
      appendPart(item, [
        `**Description** · ${tool.description || '(no description reported)'}`,
        '**Input schema · request.tools**',
        '```json',
        json(tool.input_schema || {}),
        '```',
      ].join('\n'), {preview, openDetails});
      tools.content.append(item);
    }
    cards.append(tools.element);

    const skillRows = Array.isArray(result.skills_index) ? result.skills_index : [];
    const skills = previewSection('Skills', skillRows.length);
    if (!skillRows.length) previewEmpty(skills.content, 'No skills reported.');
    for (const skill of skillRows) {
      const item = node('article', 'context-preview-item');
      const name = skill?.name || 'Unnamed skill';
      item.append(node('h4', '', name), node(
        'span', `context-preview-state ${skill?.included ? 'is-included' : ''}`,
        skill?.included ? 'Included in prompt' : 'Available, not included',
      ));
      if (skill?.description) item.append(node('p', 'context-preview-description', skill.description));
      skills.content.append(item);
    }
    cards.append(skills.element);

    const mcpText = typeof result.mcp_index === 'string' ? result.mcp_index.trim() : '';
    const mcp = previewSection('MCP');
    if (mcpText) appendPart(mcp.content, result.mcp_index, {preview, openDetails});
    else previewEmpty(mcp.content, 'No MCP index reported.');
    cards.append(mcp.element);

    const partsRows = Array.isArray(result.included_parts) ? result.included_parts : [];
    const parts = previewSection('Included prompt parts', partsRows.length);
    if (!partsRows.length) previewEmpty(parts.content, 'No additional prompt parts reported.');
    for (const part of partsRows) {
      const item = node('article', 'context-preview-item');
      item.append(node('h4', '', part?.name || 'System part'));
      if (part?.text) appendPart(item, part.text, {preview, openDetails});
      else previewEmpty(item, 'No content reported.');
      parts.content.append(item);
    }
    cards.append(parts.element);

    const messageRows = Array.isArray(result.messages) ? result.messages : [];
    const messages = previewSection(`MESSAGES · ordered request.messages · ${messageRows.length}`);
    if (!messageRows.length) previewEmpty(messages.content, 'No conversation messages yet.');
    for (const [index, message] of messageRows.entries()) {
      const item = node('article', 'context-preview-item context-request-message');
      item.append(node('h4', '', `[${index + 1}] ${message.role || 'unknown'} · ~${messageEstimate(message).toLocaleString()} tokens`));
      appendMessageParts(item, message, {preview, openDetails});
      messages.content.append(item);
    }
    cards.append(messages.element);

    const accountingCard = previewSection('Request accounting');
    if (Object.keys(accounting).length) {
      appendPart(accountingCard.content, accounting, {preview, openDetails});
    } else if (result.budget && Object.keys(result.budget).length) {
      appendPart(accountingCard.content, result.budget, {preview, openDetails});
    } else {
      previewEmpty(accountingCard.content, 'Request accounting not reported.');
    }
    cards.append(accountingCard.element);

    root.append(cards);
    if (result.omitted?.length) root.append(node('p', 'context-muted', `Display limitations · ${result.omitted.join('; ')}`));
    const full = node('button', 'context-link', 'Open complete context details');
    full.type = 'button';
    full.onclick = openDetails;
    root.append(full);
    return root;
  }

  const accounting = result.request_context || {};
  root.append(node(
    'p', 'context-muted context-request-accounting',
    `${accounting.used_tokens == null ? 'Token use not reported' : `${Number(accounting.used_tokens).toLocaleString()} / ${Number(accounting.input_budget || 0).toLocaleString()} input tokens`} · ${result.tools?.length || 0} tools · ${result.messages?.length || 0} messages · history ${result.history_included ? 'included' : 'empty'}`,
  ));

  const system = node('div', 'context-request-items');
  appendPart(system, result.system_text || '(empty)', {preview, openDetails});
  root.append(section('SYSTEM PROMPT · request.system', system));

  const tools = node('div', 'context-request-items');
  if (result.tools_supported === false) tools.append(node('p', 'context-muted', 'Tools are unsupported by the selected model.'));
  else if (!result.tools?.length) tools.append(node('p', 'context-muted', '(none)'));
  for (const [index, tool] of (result.tools || []).entries()) {
    const item = node('article', 'context-item');
    item.append(node('h4', '', `[${index + 1}] ${tool.name || 'tool'}`));
    const definition = [
      `**Description** · ${tool.description || '(no description)'}`,
      '**Input schema · request.tools**',
      '```json',
      json(tool.input_schema || {}),
      '```',
    ].join('\n');
    appendPart(item, definition, {preview, openDetails});
    tools.append(item);
  }
  root.append(section(`TOOLS · structured request.tools · ${result.tools?.length || 0}`, tools));

  const messages = node('div', 'context-request-items');
  if (!result.messages?.length) messages.append(node('p', 'context-muted', '(none)'));
  for (const [index, message] of (result.messages || []).entries()) {
    const item = node('article', 'context-item context-request-message');
    item.append(node('h4', '', `[${index + 1}] ${message.role || 'unknown'} · ~${messageEstimate(message).toLocaleString()} tokens`));
    appendMessageParts(item, message, {preview, openDetails});
    messages.append(item);
  }
  root.append(section(`MESSAGES · ordered request.messages · ${result.messages?.length || 0}`, messages));

  const parts = node('div', 'context-request-items');
  for (const part of result.included_parts || []) {
    const item = node('article', 'context-item');
    item.append(node('h4', '', part.name || 'system part'));
    appendPart(item, part.text || '', {preview, openDetails});
    parts.append(item);
  }
  if (preview) {
    root.append(section('INCLUDED SYSTEM PARTS · request.system contributions', parts));
    const indexes = node('div', 'context-request-items');
    const skills = node('article', 'context-item');
    skills.append(node('h4', '', 'Skills index'));
    appendPart(skills, result.skills_index || [], {preview, openDetails});
    indexes.append(skills);
    const mcp = node('article', 'context-item');
    mcp.append(node('h4', '', 'MCP index · part of system text'));
    appendPart(mcp, result.mcp_index || '(none)', {preview, openDetails});
    indexes.append(mcp);
    root.append(section('INDEXES', indexes));
    const accountingPreview = node('div', 'context-request-items');
    const accountingItem = node('article', 'context-item');
    accountingItem.append(node('h4', '', 'Assembler accounting'));
    appendPart(accountingItem, Object.keys(accounting).length ? accounting : result.budget || '(not reported)', {preview, openDetails});
    accountingPreview.append(accountingItem);
    if (result.budget && Object.keys(result.budget).length) {
      const budgetItem = node('article', 'context-item');
      budgetItem.append(node('h4', '', 'Assembly budget'));
      appendPart(budgetItem, result.budget, {preview, openDetails});
      accountingPreview.append(budgetItem);
    }
    if (result.params && Object.values(result.params).some(value => value != null)) {
      const parameterItem = node('article', 'context-item');
      parameterItem.append(node('h4', '', 'Model parameters'));
      appendPart(parameterItem, result.params, {preview, openDetails});
      accountingPreview.append(parameterItem);
    }
    root.append(section('ACCOUNTING', accountingPreview));
  } else {
    root.append(section('INCLUDED SYSTEM PARTS · request.system contributions', parts));
    const indexes = node('div', 'context-request-items');
    const skills = node('article', 'context-item');
    skills.append(node('h4', '', 'Skills index'));
    appendPart(skills, result.skills_index || [], {preview, openDetails});
    indexes.append(skills);
    const mcp = node('article', 'context-item');
    mcp.append(node('h4', '', 'MCP index · part of system text'));
    appendPart(mcp, result.mcp_index || '(none)', {preview, openDetails});
    indexes.append(mcp);
    root.append(section('INDEXES', indexes));
    const accountingDetails = node('div', 'context-request-items');
    const accountingItem = node('article', 'context-item');
    accountingItem.append(node('h4', '', 'Assembler accounting'));
    appendPart(accountingItem, Object.keys(accounting).length ? accounting : result.budget || '(not reported)', {preview, openDetails});
    accountingDetails.append(accountingItem);
    if (result.budget && Object.keys(result.budget).length) {
      const budgetItem = node('article', 'context-item');
      budgetItem.append(node('h4', '', 'Assembly budget'));
      appendPart(budgetItem, result.budget, {preview, openDetails});
      accountingDetails.append(budgetItem);
    }
    if (result.params && Object.values(result.params).some(value => value != null)) {
      const parameterItem = node('article', 'context-item');
      parameterItem.append(node('h4', '', 'Model parameters'));
      appendPart(parameterItem, result.params, {preview, openDetails});
      accountingDetails.append(parameterItem);
    }
    root.append(section('ACCOUNTING', accountingDetails));
  }
  if (result.omitted?.length) {
    root.append(node('p', 'context-muted', `Display limitations · ${result.omitted.join('; ')}`));
  }
  const full = node('button', 'context-link', 'Open complete context details');
  full.type = 'button';
  full.onclick = openDetails;
  if (!preview) root.append(full);
  return root;
}

// ---------------------------------------------------------------------------
// Grouped context and tools dialogs: a port of ui_support/context.py
// (context_groups, tool_groups, context_summary). Keep the two in step.
// ---------------------------------------------------------------------------

const MAX_ROWS = 512;
export const estimateTokens = text => Math.ceil(String(text || '').length / 4);
export const compactTokens = value => value >= 1e6 ? `${(value / 1e6).toFixed(1).replace(/\.0$/, '')}M` : value >= 1e3 ? `${(value / 1e3).toFixed(1).replace(/\.0$/, '')}K` : String(value);
const entry = (title, body, tokens = 0, detail = '', error = false) => ({title, body, tokens, detail, error});
const group = (key, title, entries, detail = '', total = null) => ({key, title, entries, detail, tokens: total ?? entries.reduce((sum, row) => sum + row.tokens, 0)});

function schemaRows(schema) {
  const properties = schema && typeof schema === 'object' ? schema.properties : null;
  if (!properties || typeof properties !== 'object') return [];
  const required = new Set(Array.isArray(schema.required) ? schema.required : []);
  return Object.entries(properties).slice(0, 64).map(([name, prop]) => {
    prop = prop && typeof prop === 'object' ? prop : {};
    let kind = Array.isArray(prop.type) ? prop.type.join(' | ') : String(prop.type ?? 'any');
    if (kind === 'array' && prop.items && typeof prop.items === 'object') kind = `array<${prop.items.type ?? 'any'}>`;
    if (Array.isArray(prop.enum)) kind = prop.enum.slice(0, 8).join(' | ');
    const description = String(prop.description || '').slice(0, 300);
    return `- \`${name}\` ${kind} · ${required.has(name) ? 'required' : 'optional'}${description ? ` — ${description}` : ''}`;
  });
}

export function toolEntry(tool) {
  const description = String(tool.description || '');
  const schema = tool.input_schema || {};
  const rows = schemaRows(schema);
  const body = [
    description || '(no description)',
    `**Parameters**\n${rows.length ? rows.join('\n') : '(none)'}`,
    `**Schema**\n\`\`\`json\n${json(schema)}\n\`\`\``,
  ].join('\n\n');
  const first = description.trim().split('\n')[0] || '';
  const detail = `${rows.length} param${rows.length === 1 ? '' : 's'}${first ? ` · ${first.slice(0, 90)}` : ''}`;
  return entry(String(tool.name || 'tool'), body, estimateTokens(json({name: tool.name, description: tool.description, input_schema: schema})), detail);
}

export function toolGroups(tools) {
  const families = new Map(), servers = new Map();
  const add = (map, key, tool) => { if (!map.has(key)) map.set(key, []); map.get(key).push(toolEntry(tool)); };
  for (const tool of (Array.isArray(tools) ? tools : []).slice(0, MAX_ROWS)) {
    if (!tool || typeof tool !== 'object') continue;
    const name = String(tool.name || ''), family = String(tool.group || '');
    if (name.startsWith('mcp__')) add(servers, name.split('__')[1], tool);
    else if (family.startsWith('mcp:')) add(servers, family.slice(4), tool);
    else add(families, family || 'other', tool);
  }
  const sorted = map => [...map].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  return [
    ...sorted(families).map(([key, rows]) => group(`tools:${key}`, key, rows)),
    ...sorted(servers).map(([key, rows]) => group(`mcp:${key}`, `MCP · ${key}`, rows)),
  ];
}

function messageTurns(messages) {
  const turns = [], calls = new Map();
  for (const message of (Array.isArray(messages) ? messages : []).slice(0, MAX_ROWS)) {
    if (!message || typeof message !== 'object') continue;
    const role = String(message.role || 'unknown');
    const blocks = (message.blocks || []).filter(block => block && typeof block === 'object');
    if ((role === 'user' && blocks.some(block => block.type !== 'tool_result')) || !turns.length) turns.push([]);
    const entries = turns.at(-1);
    const text = blocks.filter(block => block.type === 'text').map(block => block.text || '').join('\n\n');
    if (text) entries.push(entry(role === 'user' ? 'User message' : role === 'assistant' ? 'Assistant' : role[0].toUpperCase() + role.slice(1), text, estimateTokens(text)));
    for (const block of blocks) {
      if (block.type === 'thinking' && block.text) entries.push(entry('Thinking', block.text, estimateTokens(block.text)));
      else if (block.type === 'tool_use') {
        const input = json(block.input || {});
        calls.set(String(block.id || ''), [turns.length - 1, entries.length]);
        entries.push(entry(`Tool · ${block.name || 'tool'}`, `**Input**\n\`\`\`json\n${input}\n\`\`\``, estimateTokens(input), input.split(/\s+/).join(' ').slice(0, 90)));
      } else if (block.type === 'tool_result') {
        const output = (block.content || []).map(row => row?.text || '[image omitted]').join('\n') || '(empty)';
        const key = String(block.tool_use_id || ''), where = calls.get(key);
        calls.delete(key);
        if (where) {
          const call = turns[where[0]][where[1]];
          turns[where[0]][where[1]] = entry(call.title, `${call.body}\n\n${block.is_error ? '**Error**' : '**Result**'}\n\`\`\`\n${output}\n\`\`\``, call.tokens + estimateTokens(output), call.detail, Boolean(block.is_error));
        } else entries.push(entry('Tool result', output, estimateTokens(output), '', Boolean(block.is_error)));
      } else if (block.type !== 'text' && block.type !== 'thinking') {
        const kind = String(block.type || 'content');
        entries.push(entry(kind[0].toUpperCase() + kind.slice(1), block.text || `[${kind} omitted]`));
      }
    }
  }
  return turns;
}

export function contextGroups(result) {
  const system = result.system_text || '';
  const parts = (result.included_parts || []).filter(part => part && typeof part === 'object')
    .map(part => entry(String(part.name || 'part'), String(part.text || ''), estimateTokens(part.text)));
  const names = new Set(parts.map(part => part.title.toLowerCase()));
  const skills = (result.skills_index || []).filter(row => row && typeof row === 'object');
  if (skills.length && !['skills', 'skills_index', 'skills index'].some(name => names.has(name))) {
    parts.push(entry('Skills index', skills.slice(0, MAX_ROWS).map(row => `- **${row.name || '?'}** · ${row.included === false ? 'available, not included' : 'included'}${row.description ? ` — ${row.description}` : ''}`).join('\n'), 0, `${skills.length} skill(s)`));
  }
  if (result.mcp_index && !['mcp', 'mcp_index', 'mcp index'].some(name => names.has(name))) parts.push(entry('MCP index', String(result.mcp_index).slice(0, 4000)));
  parts.push(entry('Full system prompt', system || '(empty)', estimateTokens(system), 'as sent'));
  const groups = [group('system', 'System prompt', parts, parts.length > 1 ? `${parts.length - 1} part(s)` : '', estimateTokens(system))];
  const tools = (result.tools || []).slice(0, MAX_ROWS).filter(tool => tool && typeof tool === 'object').map(toolEntry);
  groups.push(group('tools', 'Tools', tools, result.tools_supported === false ? 'not supported by this model' : `${tools.length} definition(s)`));
  messageTurns(result.messages).forEach((entries, index) => {
    const calls = entries.filter(row => row.title.startsWith('Tool · ')).length;
    groups.push(group(`turn:${index + 1}`, `Turn ${index + 1}`, entries, calls ? `${calls} tool call(s)` : ''));
  });
  const accounting = result.request_context && Object.keys(result.request_context).length ? result.request_context : result.budget;
  const request = [entry('Accounting', `\`\`\`json\n${json(accounting && Object.keys(accounting).length ? accounting : '(not reported)')}\n\`\`\``)];
  if (result.params && Object.values(result.params).some(value => value != null)) request.push(entry('Model parameters', `\`\`\`json\n${json(result.params)}\n\`\`\``));
  const files = [['soul', 'SOUL.md'], ['agents', 'AGENTS.md'], ['memory', 'MEMORY.md']].map(([key, label]) => {
    const item = result.system_files?.[key] || {};
    return `${label}: ${item.included_nonempty ? 'included' : item.loaded ? 'loaded, empty' : 'not loaded'}${item.source ? ` · ${item.source}` : ''}`;
  });
  request.push(entry('System files', files.join('\n')));
  if (result.omitted?.length) request.push(entry('Display limitations', result.omitted.map(row => `- ${row}`).join('\n')));
  groups.push(group('request', 'Request details', request, 'not counted'));
  return groups;
}

function summaryLine(parent, title, tokens, detail) {
  parent.append(node('span', 'ctx-title', title));
  if (tokens) parent.append(node('span', 'ctx-tokens', `~${compactTokens(tokens)} tokens`));
  if (detail) parent.append(node('span', 'ctx-detail', detail));
}

function renderEntry(row) {
  const details = node('details', `ctx-entry${row.error ? ' is-error' : ''}`);
  const summary = node('summary');
  summaryLine(summary, row.title, row.tokens, row.detail);
  const body = node('div', 'ctx-body');
  body.append(markdown(row.body || '(empty)'));
  details.append(summary, body);
  return details;
}

// flattenSingle shows a one-entry group as just its entry (a tool family of one).
function renderGroups(groups, open = () => false, flattenSingle = false) {
  const root = node('div', 'ctx-groups');
  for (const item of groups) {
    if (flattenSingle && item.entries.length === 1) {
      root.append(renderEntry(item.entries[0]));
      continue;
    }
    const details = node('details', 'ctx-group');
    details.dataset.key = item.key;
    details.open = open(item);
    const summary = node('summary');
    summaryLine(summary, item.title, item.tokens, item.detail);
    details.append(summary);
    if (!item.entries.length) details.append(node('p', 'context-muted ctx-empty', '(none)'));
    for (const row of item.entries) details.append(renderEntry(row));
    root.append(details);
  }
  return root;
}

function expandAll(root) {
  const button = node('button', 'toolbar-button ctx-expand', 'Expand all');
  button.type = 'button';
  button.onclick = () => {
    const expand = button.textContent === 'Expand all';
    root.querySelectorAll('details').forEach(details => { details.open = expand; });
    button.textContent = expand ? 'Collapse all' : 'Expand all';
  };
  return button;
}

export function renderContextGroups({result, usage = ''}) {
  const root = node('div', 'context-request-tree ctx-report');
  const groups = contextGroups(result);
  const agent = result.agent || {};
  const header = node('div', 'ctx-summary');
  header.append(node('p', 'ctx-route', `${agent.name || 'default'} · ${[result.provider, result.model].filter(Boolean).join('/') || 'model not reported'}`));
  if (usage) header.append(node('p', 'context-muted', usage));
  const parts = [['system', groups[0].tokens], ['tools', groups[1].tokens], ['conversation', groups.filter(row => row.key.startsWith('turn:')).reduce((sum, row) => sum + row.tokens, 0)]];
  const total = parts.reduce((sum, [, tokens]) => sum + tokens, 0) || 1;
  const bar = node('div', 'ctx-bar');
  bar.setAttribute('aria-hidden', 'true');
  const legend = node('p', 'context-muted ctx-legend');
  legend.append(`Next request (estimated) · ${groups.filter(row => row.key.startsWith('turn:')).length} turn(s) · `);
  for (const [name, tokens] of parts) {
    const segment = node('span', `ctx-bar-${name}`);
    segment.style.setProperty('--share', `${(tokens / total) * 100}%`);
    bar.append(segment);
    legend.append(node('span', `ctx-key ctx-key-${name}`, `${name} ~${compactTokens(tokens)}`));
  }
  const tools = node('div', 'ctx-toolbar');
  const list = renderGroups(groups);
  tools.append(legend, expandAll(list));
  header.append(bar, tools);
  root.append(header, list);
  return root;
}

export function renderSystemPrompt({result}) {
  const root = node('div', 'context-request-tree ctx-report');
  const text = headerSystemPrompt(result);
  root.append(node('p', 'context-muted', `~${compactTokens(estimateTokens(text))} tokens`));
  const body = node('div', 'ctx-body ctx-system');
  // Prompt XML is literal model input, not HTML for Markdown to hide.
  body.style.whiteSpace = 'pre-wrap';
  body.textContent = text || '(empty)';
  root.append(body);
  return root;
}

// Tools dialog (twin of tui_context_header.ToolsModal): built-in tools then MCP servers, one
// row per family with every tool name and its tokens; a row expands to its tools, a tool to
// everything the model is given for it.
export function renderToolsReport({result}) {
  const root = node('div', 'context-request-tree ctx-report tools-table');
  const groups = toolGroups(result.tools);
  const count = groups.reduce((sum, row) => sum + row.entries.length, 0);
  root.append(node('p', 'context-muted', `${count} definition${count === 1 ? '' : 's'} · ~${compactTokens(groups.reduce((sum, row) => sum + row.tokens, 0))} tokens · click a row for its tools`));
  if (result.tools_supported === false) root.append(node('p', 'context-muted', 'The selected model does not support tools; none are sent.'));
  if (!groups.length) root.append(node('p', 'context-muted', '(none)'));
  let swatch = 0;
  for (const [label, rows] of [['Built-in tools', groups.filter(row => !row.key.startsWith('mcp:'))], ['MCP', groups.filter(row => row.key.startsWith('mcp:'))]]) {
    if (!rows.length) continue;
    const n = rows.reduce((sum, row) => sum + row.entries.length, 0);
    const heading = node('h3', 'tools-section');
    heading.append(node('span', '', label), node('span', 'context-muted', ` ${n} tool${n === 1 ? '' : 's'} · ~${compactTokens(rows.reduce((sum, row) => sum + row.tokens, 0))} tokens`));
    const head = node('div', 'tools-row tools-head');
    head.append(node('span', '', 'Group'), node('span', '', 'Tools'), node('span', 'tools-tokens', 'Tokens'));
    root.append(heading, head);
    for (const item of rows) {
      const details = node('details', 'tools-group');
      details.dataset.key = item.key;
      const summary = node('summary', 'tools-row');
      const name = node('span', 'tools-name');
      const mark = node('span', 'tools-swatch');
      mark.dataset.swatch = String(swatch++ % 6);
      name.append(mark, node('strong', '', item.title.replace(/^MCP · /, '')));
      summary.append(name, node('span', 'tools-names', item.entries.map(row => row.title).join('  ')), node('span', 'tools-tokens', `~${compactTokens(item.tokens)}`));
      details.append(summary);
      for (const row of item.entries) {
        const tool = node('details', 'tools-tool');
        const line = node('summary', 'tools-tool-row');
        line.append(node('strong', '', row.title), node('span', 'context-muted', row.detail), node('span', 'tools-tokens', `~${compactTokens(row.tokens)}`));
        const body = node('div', 'ctx-body');
        body.append(markdown(row.body || '(empty)'));
        tool.append(line, body);
        details.append(tool);
      }
      root.append(details);
    }
  }
  return root;
}

// Twin of tui_context_header.one_line_preview: the first non-empty line, the rest counted.
export function oneLinePreview(text, limit = 100) {
  const lines = String(text || '').split('\n').filter(line => line.trim());
  if (!lines.length) return '';
  let first = lines[0].trim();
  if (first.length > limit) first = `${first.slice(0, limit - 1).trimEnd()}…`;
  const rest = lines.length - 1;
  return rest ? `${first}  … +${rest} more line${rest === 1 ? '' : 's'}` : first;
}

export function headerSystemPrompt(result) {
  const system = result.system_text || '';
  const parts = (result.included_parts || []).filter(part => part && typeof part === 'object');
  if (parts.some(part => part.name === 'agents_md') && parts.map(part => String(part.text || '')).join('\n\n') === system) {
    return parts.filter(part => part.name !== 'agents_md').map(part => String(part.text || '')).join('\n\n');
  }
  return system;
}


const byteSize = count => count < 1024 ? `${count} B` : count < 1048576 ? `${(count / 1024).toFixed(1)} KB` : `${(count / 1048576).toFixed(1)} MB`;

// Submitted attachment metadata and payloads remain distinct from the prompt.
export function attachmentMessage(blocks = []) {
  const attachments = [], prose = [];
  for (const block of blocks) {
    const kind = block.kind || block.type;
    const match = kind === 'text' && String(block.text || '').match(/^\n\nAttachment: ((?:image|document) [1-9][0-9]*) · ([^\n]+)\n([\s\S]*)$/);
    if (match) {
      // Twin of timeline.submitted_attachment_summary: images drop their byte size and
      // media type; documents show the size of the text the model receives.
      const text = match[3].replace(/^\n/, '');
      const metadata = match[1].startsWith('image ') ? match[2].replace(/ · \d+ bytes$/, '').replace(/ · image\/[\w.+-]+$/, '') : `${match[2]} · ${byteSize(new TextEncoder().encode(text).length)}`;
      attachments.push({label:match[1], metadata, text});
    }
    else if (kind === 'image') {
      if (attachments.at(-1)?.label.startsWith('image ') && !attachments.at(-1).url) attachments.at(-1).url = block.image_url || '';
      else attachments.push({label:`image ${attachments.filter(item=>item.label.startsWith('image ')).length+1}`, metadata:block.media_type || 'Attached image', url:block.image_url || ''});
    }
    else if (kind === 'text') prose.push(block.text || '');
  }
  return {text:prose.join(''), attachments};
}

export function renderAttachmentCards(attachments) {
  const root = node('div', 'submitted-attachments');
  for (const attachment of attachments) {
    const card = node('details', 'submitted-attachment');
    const summary = node('summary');
    summary.append(node('span', 'attachment-reference', attachment.label), node('span', 'attachment-filename', attachment.metadata));
    card.append(summary);
    if (attachment.label.startsWith('image ')) {
      if (/^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(attachment.url || '')) {
        const img = node('img', 'attachment-image');
        img.src = attachment.url; img.alt = `${attachment.label} · ${attachment.metadata}`;
        card.append(img);
        // Show the thumbnail immediately; opening the card shows its details.
        card.open = true;
      } else card.append(node('p', 'context-muted', 'Image payload included in model context; preview unavailable here.'));
    } else {
      const content = node('pre', 'submitted-document');
      content.textContent = attachment.text || '(empty document)';
      card.append(content);
    }
    root.append(card);
  }
  return root;
}


function appendMessageParts(parent, message, options) {
  const parsed = attachmentMessage(message.blocks);
  if (message.role === 'user' && parsed.attachments.length) {
    if (parsed.text) appendPart(parent, parsed.text, options);
    parent.append(renderAttachmentCards(parsed.attachments));
  } else appendPart(parent, (message.blocks || []).map(blockText).join('\n\n') || '(empty)', options);
}
