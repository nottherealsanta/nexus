/** Bounded, HTML-free Markdown rendering for transcripts and context previews.
 * Browser port of the native prose presentation contract (docs/surfaces.md).
 * Raw HTML stays text; links allow only HTTP(S) and mailto. No remote images.
 */
const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));

function safeLink(value) {
  try {
    const url = new URL(value, document.baseURI);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function inline(source) {
  // Tokenize before escaping, rather than substituting HTML into user text.
  const pattern = /(`+)([^`\n]+)\1|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_|~~([^~\n]+)~~/g;
  let result = '', end = 0;
  for (const match of String(source).matchAll(pattern)) {
    result += escape(source.slice(end, match.index));
    if (match[1]) result += `<code>${escape(match[2])}</code>`;
    else if (match[3]) {
      const href = safeLink(match[4]);
      result += href ? `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${escape(match[3])}</a>` : escape(match[0]);
    } else if (match[5] || match[6]) result += `<strong>${escape(match[5] || match[6])}</strong>`;
    else if (match[7] || match[8]) result += `<em>${escape(match[7] || match[8])}</em>`;
    else result += `<del>${escape(match[9])}</del>`;
    end = match.index + match[0].length;
  }
  return result + escape(source.slice(end));
}

const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
const separator = line => line?.includes('|') && cells(line).every(cell => /^:?-{3,}:?$/.test(cell));
const fence = line => line.match(/^\s*(`{3,}|~{3,})(.*)$/);
const listItem = line => line.match(/^\s*(?:([-*+])|(\d+)[.)])\s+(.+)$/);

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
      const language = start[2].trim();
      output.push(`<pre>${language ? `<span class="code-language">${escape(language)}</span>` : ''}<code>${escape(code.join('\n'))}</code></pre>`);
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
      output.push(`<div class="markdown-table"><table><thead><tr>${headers.map((cell, column) => `<th style="text-align:${align(column)}">${inline(cell)}</th>`).join('')}</tr></thead><tbody>`);
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        const row = cells(lines[index++]);
        // Keep surplus values visible instead of silently discarding model output.
        output.push(`<tr>${row.map((cell, column) => `<td style="text-align:${align(column)}">${inline(cell)}</td>`).join('')}</tr>`);
      }
      output.push('</tbody></table></div>'); continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) quote.push(lines[index++].replace(/^\s*> ?/, ''));
      // No recursive parsing: pathological quote nesting cannot exhaust the stack.
      output.push(`<blockquote>${quote.map(inline).join('<br>')}</blockquote>`); continue;
    }
    const item = listItem(line);
    if (item) {
      const ordered = Boolean(item[2]), tag = ordered ? 'ol' : 'ul';
      output.push(`<${tag}${ordered ? ` start="${Number(item[2]) || 1}"` : ''}>`);
      while (index < lines.length) {
        const next = listItem(lines[index]);
        if (!next || Boolean(next[2]) !== ordered) break;
        const task = next[3].match(/^\[([ xX])\]\s+(.*)$/);
        output.push(`<li>${task ? `<span class="task-marker" aria-label="${task[1] === ' ' ? 'Unchecked' : 'Checked'}">${task[1] === ' ' ? '☐' : '☑'}</span> ${inline(task[2])}` : inline(next[3])}</li>`); index++;
      }
      output.push(`</${tag}>`); continue;
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
