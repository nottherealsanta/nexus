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
      appendPart(item, (message.blocks || []).map(blockText).join('\n\n') || '(empty message)', {preview, openDetails});
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
    appendPart(item, (message.blocks || []).map(blockText).join('\n\n') || '(empty)', {preview, openDetails});
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
