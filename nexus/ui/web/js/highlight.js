/** Light, bounded syntax colouring for fenced code in Markdown (js/markdown.js).
 * Presentation only: the code text is unchanged (textContent equals the source),
 * every token is escaped, and blocks over MAX_CHARS or in unknown languages stay
 * plain. Covers comments, strings, numbers, keywords, literals and call names for
 * common families, plus +/- lines in diffs. Not a parser; nesting is not tracked.
 */
const MAX_CHARS = 40000;
const escape = value => value.replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
const words = text => new Set(text.split(' '));

const LITERALS = words('true false null None True False nil undefined NaN Infinity self this super');
const FAMILIES = {
  python: {comment: '#[^\\n]*', strings: ['"""[\\s\\S]*?(?:"""|(?![\\s\\S]))', "'''[\\s\\S]*?(?:'''|(?![\\s\\S]))"], keywords: words('and as assert async await break case class continue def del elif else except finally for from global if import in is lambda match nonlocal not or pass raise return try type while with yield print')},
  shell: {comment: '(?:^|(?<=\\s))#[^\\n]*', strings: [], keywords: words('if then else elif fi for in do done while until case esac function return local export source alias echo cd set unset exit sudo')},
  c: {comment: '//[^\\n]*|/\\*[\\s\\S]*?(?:\\*/|(?![\\s\\S]))', strings: ['`(?:[^`\\\\]|\\\\.)*`?'], keywords: words('abstract as async await break case catch class const continue default defer delete do else enum export extends final finally fn for from func function go if impl implements import in instanceof interface let loop match mod move mut new package private protected pub public readonly return static struct switch throw trait try type typeof use var void where while yield int float double char bool string long short unsigned signed auto namespace template typename using')},
  sql: {comment: '--[^\\n]*', strings: [], keywords: words('select from where and or not insert into values update set delete create table index view drop alter add join left right inner outer on group by order having limit offset as distinct union all null primary key foreign references default begin commit rollback SELECT FROM WHERE AND OR NOT INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE INDEX VIEW DROP ALTER ADD JOIN LEFT RIGHT INNER OUTER ON GROUP BY ORDER HAVING LIMIT OFFSET AS DISTINCT UNION ALL NULL PRIMARY KEY FOREIGN REFERENCES DEFAULT BEGIN COMMIT ROLLBACK')},
  data: {comment: '#[^\\n]*', strings: [], keywords: new Set()},
  json: {comment: '', strings: [], keywords: new Set()},
  css: {comment: '/\\*[\\s\\S]*?(?:\\*/|(?![\\s\\S]))', strings: [], keywords: words('important media supports import keyframes root')},
};
const ALIASES = {
  python: 'python', py: 'python', python3: 'python', pyi: 'python',
  sh: 'shell', bash: 'shell', zsh: 'shell', shell: 'shell', console: 'shell', fish: 'shell', dockerfile: 'shell', makefile: 'shell', make: 'shell',
  js: 'c', javascript: 'c', mjs: 'c', cjs: 'c', jsx: 'c', ts: 'c', typescript: 'c', tsx: 'c', rust: 'c', rs: 'c', go: 'c', golang: 'c', java: 'c', kotlin: 'c', kt: 'c', swift: 'c', c: 'c', h: 'c', cpp: 'c', 'c++': 'c', cc: 'c', hpp: 'c', cs: 'c', csharp: 'c', scala: 'c', php: 'c', dart: 'c', zig: 'c',
  sql: 'sql', sqlite: 'sql', postgres: 'sql', postgresql: 'sql', lua: 'sql',
  yaml: 'data', yml: 'data', toml: 'data', ini: 'data', conf: 'data', env: 'data',
  json: 'json', jsonc: 'c', json5: 'c', jsonl: 'json',
  css: 'css', scss: 'c', less: 'c',
};
const STRINGS = ['"(?:[^"\\\\\\n]|\\\\.)*"?', "'(?:[^'\\\\\\n]|\\\\.)*'?"];
const NUMBER = '\\b(?:0[xX][\\da-fA-F_]+|0[bB][01_]+|\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?)\\b';
const WORD = '[A-Za-z_$][\\w$]*';
const patterns = new Map();

function pattern(family) {
  if (!patterns.has(family)) {
    const spec = FAMILIES[family];
    // Strings that may span lines (Python triple quotes) come before the one-line forms.
    const parts = [spec.comment && `(${spec.comment})`, `(${[...spec.strings, ...STRINGS].join('|')})`, `(${NUMBER})`, `(${WORD})(\\s*\\()?`].filter(Boolean);
    patterns.set(family, {regex: new RegExp(parts.join('|'), 'gm'), hasComment: Boolean(spec.comment)});
  }
  return patterns.get(family);
}

const span = (kind, text) => `<span class="tok-${kind}">${escape(text)}</span>`;

function diffHTML(code) {
  return code.split('\n').map(line => {
    if (/^(?:\+\+\+|---)(?:\s|$)/.test(line) || /^(?:diff |index )/.test(line)) return span('meta', line);
    if (line.startsWith('@@')) return span('hunk', line);
    if (line.startsWith('+')) return span('add', line);
    if (line.startsWith('-')) return span('del', line);
    return escape(line);
  }).join('\n');
}

/** HTML for `code` in `language`; unknown languages and oversized blocks are escaped only. */
export function highlightHTML(code, language = '') {
  const source = String(code ?? '');
  const name = String(language).trim().split(/\s+/)[0].toLowerCase();
  if (source.length > MAX_CHARS) return escape(source);
  if (name === 'diff' || name === 'patch') return diffHTML(source);
  const family = ALIASES[name];
  if (!family) return escape(source);
  const {regex, hasComment} = pattern(family), keywords = FAMILIES[family].keywords;
  let result = '', end = 0;
  for (const match of source.matchAll(regex)) {
    result += escape(source.slice(end, match.index));
    const [comment, string, number, word, call] = hasComment ? match.slice(1) : [undefined, ...match.slice(1)];
    if (comment) result += span('com', comment);
    else if (string) result += span(family === 'json' && /^\s*:/.test(source.slice(match.index + string.length)) ? 'key' : 'str', string);
    else if (number) result += span('num', number);
    else if (keywords.has(word)) result += span('kw', word) + escape(call || '');
    else if (LITERALS.has(word)) result += span('lit', word) + escape(call || '');
    else if (call) result += span('fn', word) + escape(call);
    else result += escape(word);
    end = match.index + match[0].length;
  }
  return result + escape(source.slice(end));
}
