"""Browser-local context visibility, exercised without browser dependencies."""
from pathlib import Path
import shutil
import subprocess
import unittest


MODULE = Path(__file__).resolve().parents[1] / "nexus/ui/web/js/context-preferences.js"
FIXTURE = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {loadContextPreferences, saveContextPreferences, createContextPreferences,
  applyContextPreferences, CONTEXT_PREFERENCES_KEY} = await import(
  'data:text/javascript;base64,' + Buffer.from(readFileSync(process.argv[1])).toString('base64'));
const defaults = {system:true, tools:true, agents_md:true, skills:true, mcp:true};
class Element {
  constructor(tag, cls = '', text = '') {
    this.tag = tag; this.className = cls; this.textContent = text;
    this.children = []; this.dataset = {}; this.style = {display:''};
    this.attributes = {}; this.ownerDocument = {createElement:tag => el(tag)};
  }
  append(...nodes) { for (const n of nodes) {n.parent = this; this.children.push(n);} }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  remove() { this.parent.children = this.parent.children.filter(n => n !== this); }
  querySelectorAll(selector) {
    const matches = node => selector === '.context-block, [data-context-key]'
      ? node.className === 'context-block' || 'contextKey' in node.dataset
      : selector === '.context-chip' ? node.className === 'context-chip'
      : 'contextPreferencesNotice' in node.dataset;
    return this.children.flatMap(n => [...(matches(n) ? [n] : []), ...n.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const el = (...args) => new Element(...args);
const storage = {value:null, writes:0, getItem(key) {this.key = key; return this.value;},
  setItem(key, value) {this.key = key; this.value = value; this.writes++;}};
function header() {
  const h = el('div');
  for (const [key, label] of [['system','System prompt'], ['tools','Tools'],
    ['agents_md','AGENTS.md'], ['skills','Skills'], ['mcp','MCP'], ['task','Task']]) {
    const card = el('div', 'context-block');
    card.dataset.contextKey = key;
    card.append(el('span', 'context-chip', label), el('pre', '', 'Full source content'));
    h.append(card);
  }
  return h;
}
"""


@unittest.skipUnless(shutil.which("node"), "node is required for web context tests")
class WebContextPreferencesTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", FIXTURE + script,
             str(MODULE)], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bounded_safe_storage(self):
        self.run_node(r"""
assert.deepEqual(loadContextPreferences(), defaults);
for (const raw of [null, 'broken', 'null', '[]', 'false', 'x'.repeat(1025)]) {
  storage.value = raw; assert.deepEqual(loadContextPreferences(storage), defaults);
}
storage.value = '{"tools":false,"system":0,"mcp":"false","unknown":false}';
assert.deepEqual(loadContextPreferences(storage), {...defaults, tools:false});
assert.equal(storage.key, CONTEXT_PREFERENCES_KEY);
const clean = saveContextPreferences(storage, {skills:false, unknown:false});
assert.deepEqual(clean, {...defaults, skills:false});
assert.ok(storage.value.length < 1024);
assert.deepEqual(JSON.parse(storage.value), clean);
const blocked = {getItem(){throw Error('blocked');}, setItem(){throw Error('full');}};
assert.deepEqual(loadContextPreferences(blocked), defaults);
assert.deepEqual(saveContextPreferences(blocked, {mcp:false}), {...defaults,mcp:false});
""")

    def test_visibility_disclosure_and_restore_preserve_data(self):
        self.run_node(r"""
const h = header(), cards = [...h.children], contents = cards.map(c => c.children[1]);
cards[0].style.display = 'grid';
assert.equal(applyContextPreferences(h, {system:false, tools:false}), 2);
assert.equal(cards[0].hidden, true); assert.equal(cards[0].style.display, 'none');
assert.equal(cards[5].hidden, undefined);
assert.deepEqual(cards.map(c => c.children[1]), contents);
assert.equal(h.children.length, 7);
const notice = h.children.at(-1);
assert.match(notice.children[0].textContent, /2 context cards hidden/);
assert.match(notice.children[0].textContent, /context is unchanged/);
assert.equal(notice.children[1].type, 'button');
applyContextPreferences(h, {system:false});
assert.equal(h.children.length, 7); assert.match(notice.children[0].textContent, /1 context card hidden/);
notice.children[1].onclick();
assert.equal(h.children.length, 6); assert.equal(cards[0].hidden, false);
assert.equal(cards[0].style.display, 'grid');
assert.deepEqual(cards.map(c => c.children[1]), contents);
// Existing label-only context blocks work too; unrelated blocks are untouched.
delete cards[2].dataset.contextKey;
assert.equal(applyContextPreferences(h, {agents_md:false}), 1);
assert.equal(cards[2].hidden, true);
assert.equal(applyContextPreferences(null, {}), 0);
""")

    def test_controller_render_change_load_and_multi_header_restore(self):
        self.run_node(r"""
const root = el('div'), a = header(), b = header();
let calls = 0, controller;
controller = createContextPreferences({el, root, storage, onChange(values) {
  calls++; assert.deepEqual(values, controller.values); controller.apply(a); controller.apply(b);
}});
assert.deepEqual(controller.values, defaults);
const group = root.children[0]; assert.equal(group.tag, 'fieldset');
const labels = group.children.filter(n => n.tag === 'label'); assert.equal(labels.length, 5);
assert.ok(labels.every(n => n.children[0].type === 'checkbox' && n.children[0].checked));
const checkbox = labels[1].children[0]; checkbox.checked = false; checkbox.onchange();
assert.equal(calls, 1); assert.equal(controller.values.tools, false);
assert.equal(a.children[1].hidden, true); assert.equal(b.children[1].hidden, true);
const copy = controller.values; copy.tools = true; assert.equal(controller.values.tools, false);
controller.set('task', false); controller.set('tools', 'false'); assert.equal(calls, 1);
a.children.at(-1).children[1].onclick();
assert.equal(calls, 2); assert.deepEqual(controller.values, defaults);
assert.equal(b.children[1].hidden, false); assert.equal(b.children.length, 6);
storage.value = '{"mcp":false}'; controller.load(); assert.equal(controller.values.mcp, false);
controller.reset(); assert.deepEqual(loadContextPreferences(storage), defaults);
const blocked = createContextPreferences({storage:{setItem(){throw Error();}}});
blocked.set('system', false); assert.equal(blocked.values.system, false);
""")
