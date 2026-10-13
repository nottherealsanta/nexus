/** Bounded, HTML-free Markdown rendering for transcripts and context previews.
 * Browser port of the native prose presentation contract (docs/surfaces.md).
 * Raw HTML stays text; links allow only HTTP(S) and mailto. No remote images.
 * No inline style attributes (the CSP forbids them): alignment and state are classes.
 */
import {highlightHTML} from './highlight.js';

const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));

function safeLink(value) {
  try {
    const url = new URL(value, document.baseURI);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

// Emphasis, strikethrough and link text format their contents once more (code, links,
// other emphasis), bounded so hostile nesting cannot recurse deeply.
const INLINE_DEPTH = 3;

function inline(source, depth = 0) {
  // Tokenize before escaping, rather than substituting HTML into user text.
  const inner = text => depth < INLINE_DEPTH ? inline(text, depth + 1) : escape(text);
  const pattern = /(`+)([^`\n]+)\1|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*(?!\s)((?:[^*\n]|\*(?!\*))+?)(?<!\s)\*\*|(?<!\w)__(?!\s)((?:[^_\n]|_(?!_))+?)(?<!\s)__(?!\w)|\*([^\s*](?:[^*\n]*[^\s*])?)\*|(?<!\w)_([^\s_](?:[^_\n]*[^\s_])?)_(?!\w)|~~([^~\n]+)~~/g;
  let result = '', end = 0;
  for (const match of String(source).matchAll(pattern)) {
    result += escape(source.slice(end, match.index));
    if (match[1]) result += `<code>${escape(match[2])}</code>`;
    else if (match[3]) {
      const href = safeLink(match[4]);
      result += href ? `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${inner(match[3])}</a>` : escape(match[0]);
    } else if (match[5] || match[6]) result += `<strong>${inner(match[5] || match[6])}</strong>`;
    else if (match[7] || match[8]) result += `<em>${inner(match[7] || match[8])}</em>`;
    else result += `<del>${inner(match[9])}</del>`;
    end = match.index + match[0].length;
  }
  return result + escape(source.slice(end));
}

const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
const separator = line => line?.includes('|') && cells(line).every(cell => /^:?-{3,}:?$/.test(cell));
const fence = line => line.match(/^\s*(`{3,}|~{3,})(.*)$/);
const listItem = line => line.match(/^(\s*)(?:([-*+])|(\d+)[.)])\s+(.+)$/);
const indentWidth = text => text.replace(/\t/g, '    ').length;
const MAX_LIST_DEPTH = 6;

function codeBlock(code, language) {
  // The head is outside <code>, so selecting or copying the code never includes it.
  const label = language ? `<span class="code-language">${escape(language)}</span>` : '<span class="code-language-none"></span>';
  return `<pre class="code-block"><span class="code-head">${label}<button type="button" class="code-copy" aria-label="Copy code">Copy</button></span><code>${highlightHTML(code, language)}</code></pre>`;
}

function listItemHTML(text) {
  const task = text.match(/^\[([ xX])\]\s+(.*)$/);
  if (!task) return `<li>${inline(text)}`;
  const checked = task[1] !== ' ';
  return `<li class="task${checked ? ' done' : ''}"><span class="task-marker" role="img" aria-label="${checked ? 'Checked' : 'Unchecked'}">${checked ? '☑' : '☐'}</span> ${inline(task[2])}`;
}

/** Consecutive list lines become nested lists by indentation, like the terminal. */
function listHTML(lines, index) {
  const output = [], stack = [];
  const open = (item, indent) => {
    const ordered = Boolean(item[3]), tag = ordered ? 'ol' : 'ul';
    output.push(`<${tag}${ordered ? ` start="${Number(item[3]) || 1}"` : ''}>`);
    stack.push({indent, tag});
  };
  while (index < lines.length) {
    const item = listItem(lines[index]);
    if (!item) {
      // An indented line under an item continues it (lazy continuation).
      const line = lines[index];
      if (!stack.length || !line.trim() || !/^\s+\S/.test(line) || fence(line)) break;
      output.push(`<br>${inline(line.trim())}`); index++; continue;
    }
    const indent = indentWidth(item[1]), tag = item[3] ? 'ol' : 'ul';
    if (!stack.length) open(item, indent);
    else if (indent > stack.at(-1).indent && stack.length < MAX_LIST_DEPTH) open(item, indent);
    else {
      while (stack.length > 1 && indent < stack.at(-1).indent) output.push(`</li></${stack.pop().tag}>`);
      if (stack.at(-1).tag !== tag) {
        const previous = stack.pop();
        output.push(`</li></${previous.tag}>`);
        open(item, previous.indent);
      } else output.push('</li>');
    }
    output.push(listItemHTML(item[4])); index++;
  }
  while (stack.length) output.push(`</li></${stack.pop().tag}>`);
  return {html: output.join(''), index};
}

export function markdownHTML(value, {headingOffset = 0} = {}) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    const start = fence(line);
    if (start) {
      const code = []; index++;
      const close = new RegExp(`^\\s*${start[1][0]}{${start[1].length},}\\s*$`);
      while (index < lines.length && !close.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index++;
      output.push(codeBlock(code.join('\n'), start[2].trim()));
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(6, heading[1].length + headingOffset);
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`); index++; continue;
    }
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { output.push('<hr>'); index++; continue; }
    if (line.includes('|') && separator(lines[index + 1])) {
      const headers = cells(line), alignment = cells(lines[index + 1]);
      const align = column => alignment[column]?.startsWith(':') && alignment[column]?.endsWith(':') ? 'center' : alignment[column]?.endsWith(':') ? 'right' : 'left';
      const cell = (tag, value, column) => `<${tag} class="align-${align(column)}">${inline(value)}</${tag}>`;
      output.push(`<div class="markdown-table"><table><thead><tr>${headers.map((value, column) => cell('th', value, column)).join('')}</tr></thead><tbody>`);
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        const row = cells(lines[index++]);
        // Keep surplus values visible instead of silently discarding model output.
        output.push(`<tr>${row.map((value, column) => cell('td', value, column)).join('')}</tr>`);
      }
      output.push('</tbody></table></div>'); continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) quote.push(lines[index++].replace(/^\s*> ?/, ''));
      // No recursive parsing: pathological quote nesting cannot exhaust the stack.
      output.push(`<blockquote>${quote.map(inline).join('<br>')}</blockquote>`); continue;
    }
    if (listItem(line)) {
      const list = listHTML(lines, index);
      output.push(list.html); index = list.index; continue;
    }
    const paragraph = [line]; index++;
    while (index < lines.length && lines[index].trim() && !fence(lines[index]) && !listItem(lines[index]) && !/^\s*(?:#{1,6}\s|>|(?:-{3,}|\*{3,}|_{3,})\s*$)/.test(lines[index]) && !separator(lines[index + 1])) paragraph.push(lines[index++]);
    output.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
  }
  return output.join('');
}

export function renderMarkdown(value) {
  const element = document.createElement('div');
  element.className = 'context-markdown';
  element.innerHTML = markdownHTML(value);
  return element;
}
