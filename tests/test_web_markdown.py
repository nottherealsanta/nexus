"""Exercise the browser Markdown renderer and fenced-code colouring in Node (docs/web.md)."""
from pathlib import Path
import shutil
import subprocess
import unittest


JS = Path(__file__).resolve().parents[1] / "nexus/ui/web/js"

FIXTURE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
globalThis.document = {baseURI: 'http://127.0.0.1/'};
const dir = process.argv[1];
const {markdownHTML} = await import(pathToFileURL(`${dir}/markdown.js`));
const {highlightHTML} = await import(pathToFileURL(`${dir}/highlight.js`));
const text = html => html.replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
"""


@unittest.skipUnless(shutil.which("node"), "node is required for web markdown tests")
class WebMarkdownTest(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", FIXTURE + script, str(JS)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_tables_use_alignment_classes_not_style_attributes(self):
        self.run_node(r"""
const html = markdownHTML('| A | B | C |\n| --- | :---: | ---: |\n| 1 | 2 | 3 |');
assert.ok(!html.includes('style='), html);
assert.match(html, /<th class="align-left">A<\/th><th class="align-center">B<\/th><th class="align-right">C<\/th>/);
assert.match(html, /<td class="align-right">3<\/td>/);
""")

    def test_nested_inline_and_flanking(self):
        self.run_node(r"""
assert.equal(markdownHTML('**from `seq`**'), '<p><strong>from <code>seq</code></strong></p>');
assert.equal(markdownHTML('**a *b* a**'), '<p><strong>a <em>b</em> a</strong></p>');
assert.match(markdownHTML('[**l**](https://x.y)'), /<a href="https:\/\/x.y\/"[^>]*><strong>l<\/strong><\/a>/);
assert.equal(markdownHTML('a * b * c'), '<p>a * b * c</p>');
assert.equal(markdownHTML('snake_case_name'), '<p>snake_case_name</p>');
assert.equal(markdownHTML('`**x**`'), '<p><code>**x**</code></p>');
assert.equal(markdownHTML('[bad](javascript:alert(1))'), '<p>[bad](javascript:alert(1))</p>');
assert.equal(markdownHTML('**<img src=x>**'), '<p><strong>&lt;img src=x&gt;</strong></p>');
""")

    def test_lists_nest_and_tasks_keep_their_glyph(self):
        self.run_node(r"""
assert.equal(markdownHTML('- a\n  - b\n    - c\n  - d\n- e'),
  '<ul><li>a<ul><li>b<ul><li>c</li></ul></li><li>d</li></ul></li><li>e</li></ul>');
assert.equal(markdownHTML('- a\n  1. one\n  2. two\n- b'),
  '<ul><li>a<ol start="1"><li>one</li><li>two</li></ol></li><li>b</li></ul>');
assert.equal(markdownHTML('- a\n1. b'), '<ul><li>a</li></ul><ol start="1"><li>b</li></ol>');
assert.equal(markdownHTML('3. x\n4. y'), '<ol start="3"><li>x</li><li>y</li></ol>');
assert.equal(markdownHTML('- item\n  more\n- next'), '<ul><li>item<br>more</li><li>next</li></ul>');
const tasks = markdownHTML('- [x] done\n- [ ] todo');
assert.match(tasks, /<li class="task done"><span class="task-marker" role="img" aria-label="Checked">☑<\/span> done/);
assert.match(tasks, /<li class="task"><span class="task-marker" role="img" aria-label="Unchecked">☐<\/span> todo/);
// Deep nesting is bounded and stays balanced.
const deep = markdownHTML(Array.from({length: 40}, (_, i) => `${' '.repeat(i * 2)}- level ${i}`).join('\n'));
assert.equal((deep.match(/<ul>/g) || []).length, (deep.match(/<\/ul>/g) || []).length);
assert.ok((deep.match(/<ul>/g) || []).length <= 6);
""")

    def test_code_fence_head_and_text_fidelity(self):
        self.run_node(r"""
const source = 'def f(x):\n    \"\"\"doc\n    <more>\"\"\"\n    return x + 1  # note\n';
const html = markdownHTML('```python\n' + source + '```');
assert.match(html, /^<pre class="code-block"><span class="code-head"><span class="code-language">python<\/span><button type="button" class="code-copy" aria-label="Copy code">Copy<\/button><\/span><code>/);
const code = html.slice(html.indexOf('<code>') + 6, html.lastIndexOf('</code>'));
assert.equal(text(code), source.replace(/\n$/, ''));
assert.match(code, /<span class="tok-kw">def<\/span> <span class="tok-fn">f<\/span>/);
assert.match(code, /<span class="tok-str">&quot;&quot;&quot;doc\n    &lt;more&gt;&quot;&quot;&quot;<\/span>/);
assert.match(code, /<span class="tok-com"># note<\/span>/);
// No language: a head with Copy, plain escaped code.
assert.equal(markdownHTML('```\n<b>\n```'), '<pre class="code-block"><span class="code-head"><span class="code-language-none"></span><button type="button" class="code-copy" aria-label="Copy code">Copy</button></span><code>&lt;b&gt;</code></pre>');
// Incomplete fences stay readable while streaming.
assert.match(markdownHTML('```js\nconst x = "<safe>"'), /&lt;safe&gt;/);
""")

    def test_highlighter_is_bounded_and_lossless(self):
        self.run_node(r"""
for (const [code, language] of [
  ['const s = `a${b}`; /* c */ fn("x\\"y")', 'ts'],
  ['{"a": 1, "b": [true, null]}', 'json'],
  ['echo "$HOME" # comment\nls -la', 'bash'],
  ['SELECT * FROM t -- c', 'sql'],
  ['key: "v" # c', 'yaml'],
  ['<script>alert(1)</script>', 'unknown-language'],
  ['"unterminated', 'python'],
]) assert.equal(text(highlightHTML(code, language)), code, language);
assert.equal(highlightHTML('<x>', 'brainfuck'), '&lt;x&gt;');
assert.match(highlightHTML('{"a": 1}', 'json'), /<span class="tok-key">&quot;a&quot;<\/span>/);
const diff = highlightHTML('@@ -1 +1 @@\n-old\n+new\n same', 'diff');
assert.equal(diff, '<span class="tok-hunk">@@ -1 +1 @@</span>\n<span class="tok-del">-old</span>\n<span class="tok-add">+new</span>\n same');
const big = 'x = 1\n'.repeat(10000);
assert.equal(highlightHTML(big, 'python'), big);
const started = Date.now();
highlightHTML('a = "b\\"c" '.repeat(3000), 'python');
markdownHTML('**' + '*a'.repeat(20000) + '**');
assert.ok(Date.now() - started < 2000);
""")


if __name__ == "__main__":
    unittest.main()
