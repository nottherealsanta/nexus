"""Standalone agent editor tests; run the actual ES module with Node's built-ins."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nexus/ui/web/js/agent-settings.js"


def run_js(script, settings_files=False):
    if not shutil.which("node"):
        pytest.skip("Node.js is required for web module tests")
    source = MODULE.read_text()
    if settings_files:
        import base64

        agent_url = "data:text/javascript;base64," + base64.b64encode(source.encode()).decode()
        source = (MODULE.parent / "settings-files.js").read_text().replace("'./agent-settings.js'", json.dumps(agent_url))
    prefix = "import assert from 'node:assert/strict';\n"
    prefix += "const mod = await import('data:text/javascript;base64,' + Buffer.from(" + json.dumps(source) + ").toString('base64'));\n"
    result = subprocess.run(["node", "--input-type=module", "-e", prefix + script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_routing_preserves_prompt_and_unrelated_frontmatter():
    run_js(r'''
const {inspectAgentRouting: inspect, transformAgentRouting: transform} = mod;
const body = '---\r\nname: my-agent # retained\r\nmodel: old\r\nprovider: p\r\nfallback: [p/a, p/b]\r\ntools:\r\n  - read\r\n---\r\nPrompt\n---\nverbatim';
const tier = transform(body, {mode: 'tier', tier: 'fast'});
assert.equal(tier, '---\r\nname: my-agent # retained\r\ntools:\r\n  - read\r\nmodel_tier: "fast"\r\n---\r\nPrompt\n---\nverbatim');
const session = transform(tier, {mode: 'session'});
assert.equal(inspect(session).mode, 'session');
const model = transform(session, {mode: 'model', model: 'p/m', fallbacks: ['p/b', 'p/a']});
assert.deepEqual(inspect(model).fields.fallback, ['p/b', 'p/a']);
assert.throws(() => transform(model, {mode: 'model', model: 'p/m', fallbacks: Array(9).fill('p/x')}));
assert.throws(() => transform(model, {mode: 'tier'}));
for (const source of ['no frontmatter', '---\nmodel: x\nmodel: y\n---\nP', '---\n"model": x\n---\nP', '---\nmodel: |\n  x\n---\nP', '---\nfallback:\n- x\n---\nP', '---\nbase: &alias {}\n---\nP']) {
  assert.equal(inspect(source).safe, false, source);
  assert.throws(() => transform(source, {mode: 'session'}));
}
''')


DOM = r'''
class Node {
  constructor(tag, cls, text) {this.tag = tag; this.text = text; this.children = []; this.attrs = {}; this.disabled = false;}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.children = nodes;}
  setAttribute(k,v) {this.attrs[k] = v;}
  removeAttribute(k) {delete this.attrs[k];}
}
const el = (...args) => new Node(...args), root = el('div');
function find(text, node = root) {if (node.attrs['aria-label'] === text) return node; for (const child of node.children) {const found = find(text, child); if (found && found.attrs['aria-label'] === text) return found;} if (node.text === text) return node; for (const child of node.children) {const found = find(text, child); if (found) return found;}}
const commands = [], notices = [];
let writes = {status: 'written', sha256: 'new'}, read = {body: '---\nname: agent\n---\nPrompt', sha256: 'old', builtin: true};
const api = {command: async command => {commands.push(command); if (command.type === 'SettingsRead') return read; if (command.type === 'ModelTiers') return {order: ['fast', 'slow']}; if (writes instanceof Error) throw writes; return writes;}};
let picked;
const controller = mod.createAgentSettings({api, el, root, notify: text => notices.push(text), isOpen: () => true, pickModel: cb => {picked = cb;}});
'''


def test_controller_scope_conflicts_validation_and_guarded_edit():
    run_js(DOM + r'''
await controller.load({id: 'agent', scope: 'project'});
const routing = find('Routing'); routing.value = 'model'; routing.onchange(); picked('p/m');
find('Add fallback…').onclick(); picked('p/b');
find('Add fallback…').onclick(); picked('p/a');
find('Move up p/a').onclick();
writes = new Error('Invalid agent');
await find('Validate and save').onclick();
assert.ok(notices.at(-1).includes('Invalid agent'));
let command = commands.at(-1);
assert.equal(command.scope, 'project'); assert.equal(command.expected_sha256, 'old');
assert.ok(command.body.includes('fallback: ["p/a", "p/b"]'));
writes = {status: 'conflict', sha256: 'different'};
await find('Validate and save').onclick();
assert.ok(find('Reload from disk'));
assert.equal(find('Validate and save explicit edit').disabled, true);
read = {body: '---\nmodel: |\n  complex\n---\nPrompt', sha256: 'fresh'};
await find('Reload from disk').onclick();
const editor = find('Agent source'); editor.value = '---\nname: fixed\n---\nPrompt retained'; editor.oninput();
assert.equal(find('Validate and save explicit edit').disabled, true);
const guard = find('I reviewed the complete source; validate and save this explicit edit.').__unused;
const checkbox = root.children.find(n => n.tag === 'label').children[0];
checkbox.checked = true; checkbox.onchange();
writes = {status: 'written', sha256: 'latest'};
await find('Validate and save explicit edit').onclick();
command = commands.at(-1);
assert.equal(command.expected_sha256, 'fresh'); assert.equal(command.body, editor.value);
assert.ok(notices.at(-1).includes('Saved agent in project scope'));
controller.invalidate();
''')


def test_stale_load_and_model_picker_are_ignored():
    run_js(DOM + r'''
await controller.load({id: 'first', scope: 'global'});
const routing = find('Routing'); routing.value = 'model'; routing.onchange();
await controller.load({id: 'second', scope: 'global'});
picked('p/stale');
assert.equal(find('Validate and save').disabled, true);
let resolve;
api.command = command => new Promise(done => {resolve = done;});
const pending = controller.load({id: 'late', scope: 'project'});
controller.invalidate();
resolve(read); await pending;
assert.equal(commands.filter(c => c.type === 'SettingsWrite').length, 0);
await assert.rejects(controller.load({id: 'agent', scope: 'invalid'}));
''')


def test_settings_files_guided_save_serializes_and_preserves_raw_draft():
    run_js(r'''
class Node {
  constructor(tag, cls, text) {this.tag = tag; this.text = text; this.children = []; this.attrs = {}; this.value = ''; this.hidden = false; this.events = {};}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.children = nodes;}
  setAttribute(k,v) {this.attrs[k] = v;}
  removeAttribute(k) {delete this.attrs[k];}
  addEventListener(k,v) {this.events[k] = v;}
  querySelectorAll() {return [];}
  after(node) {this.next = node;}
}
const el = (...args) => new Node(...args), nodes = {}, $ = id => nodes[id] ||= el('div');
globalThis.document = {activeElement: null, querySelectorAll: () => []};
let observer;
globalThis.MutationObserver = class {constructor(cb) {observer = cb;} observe() {}};
globalThis.window = {confirm: () => true};
const tick = () => new Promise(resolve => setImmediate(resolve));
let body = '---\nname: build\nreasoning_effort: high\n---\nPrompt', sha = 'old', release;
const writes = [];
const api = {command: async c => {
  if (c.type === 'SettingsInventory') return {items: [{id: 'build', category: 'agents'}]};
  if (c.type === 'SettingsRead') return {body, sha256: sha};
  if (c.type === 'ModelsList') return {models: []};
  if (c.type === 'ModelTiers') return {order: ['fast']};
  if (c.type === 'SettingsWrite') {
    writes.push(c); assert.equal(c.expected_sha256, sha);
    if (writes.length === 1) await new Promise(resolve => {release = resolve;});
    body = c.body; sha = 'sha' + writes.length;
    return {status: 'written', sha256: sha};
  }
}};
const files = mod.createSettingsFiles({api, el, $, notify: () => {}, pickModel: cb => cb('p/m')});
files.install(); await files.showCategory('agents'); await tick();
await $('files-list').children[0].onclick(); await tick();
const root = $('agent-form').next;
function find(text, node = root) {if (node.attrs['aria-label'] === text || (node.tag === 'button' && node.text === text)) return node; for (const c of node.children) {const r = find(text, c); if (r) return r;}}
const routing = find('Routing'); routing.value = 'model'; routing.onchange();
const saving = find('Validate and save').onclick(); await tick();
assert.equal(writes.length, 1);
$('files-editor').value += '\nUnsaved draft'; $('files-editor').events.input();
const flushing = files.flush(); await tick(); assert.equal(writes.length, 1);
release(); await saving; await flushing; await tick();
assert.equal(writes.length, 2);
assert.ok(writes[1].body.endsWith('Unsaved draft'));
assert.equal(writes[1].expected_sha256, 'sha1');
assert.ok($('files-editor').value.endsWith('Unsaved draft'));
assert.ok($('files-editor').value.includes('reasoning_effort: high'));
assert.equal(root.hidden, false);
$('settings-overlay').hidden = true; observer(); assert.equal(root.hidden, true);
''', settings_files=True)
