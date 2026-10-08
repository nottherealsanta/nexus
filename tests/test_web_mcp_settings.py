"""Exercise the structured web MCP controller without a browser dependency."""
from pathlib import Path
import shutil
import subprocess
import unittest


MODULE = Path(__file__).resolve().parents[1] / "nexus/ui/web/js/mcp-settings.js"


@unittest.skipUnless(shutil.which("node"), "node is required for web controller tests")
class WebMcpSettingsTests(unittest.TestCase):
    def test_host_contract_and_lifecycle(self):
        script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync(process.argv[1], 'utf8');
const {createMcpSettings} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
class Element {
  constructor(tag, cls, text = '') { this.tag = tag; this.textContent = text; this.children = []; this.attrs = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
}
const el = (...args) => new Element(...args), root = el('div');
const all = (node = root) => [node, ...node.children.flatMap(child => all(child))];
const text = () => all().map(node => node.textContent).join('\n');
const control = label => all().find(node => node.attrs['aria-label'] === label);
const server = {name: 'global-server', scope: 'global', config_enabled: true, enabled: false,
  config_tool_loading: 'search', tool_loading: 'all', tool_loading_source: 'session', status: 'failed', tool_count: 3, schema_tokens: 120};
const project = {...server, name: 'project-server', scope: 'project'};
let calls = [], notices = [], open = true, status = 'written', failRead = false, failInspect = false;
let inspectDeferred = null, readDeferred = null;
const api = {command: async command => {
  calls.push(command);
  if (command.type === 'ContextInspect') {
    if (inspectDeferred) return inspectDeferred;
    if (failInspect) throw Error('offline');
    return {mcp_servers: [server, project]};
  }
  if (command.type === 'SettingsRead') {
    if (readDeferred) return readDeferred;
    if (failRead) throw Error('unreadable');
    // Accessing raw configuration would throw: controller must only use sha256.
    return {sha256: 'hash', get body() {throw Error('raw config access');}};
  }
  if (command.type === 'SettingsMcpEnabledSet') { if (status === 'written') server.config_enabled = command.enabled; return {status}; }
  if (command.type === 'SettingsMcpLoadingSet') { if (status === 'written') server.config_tool_loading = command.mode; return {status}; }
  throw Error('Unexpected command ' + command.type);
}};
const controller = createMcpSettings({api, el, root, notify: value => notices.push(value), isOpen: () => open});
await controller.load('session-1');
assert.deepEqual(calls[0], {type: 'ContextInspect', session: 'session-1'});
assert(text().includes('global-server'));
assert(!text().includes('project-server'));
assert(text().includes('Current session: disabled · loading all · source session'));
assert(text().includes('failed · 3 tools'));
assert(text().includes('~120 tokens'));
let toggle = control('Persistently enable global-server');
assert.equal(toggle.checked, true);
toggle.checked = false; await toggle.onchange();
assert.deepEqual(calls[1], {type: 'SettingsRead', scope: 'global', category: 'mcp', id: 'mcp.json'});
assert.deepEqual(calls[2], {type: 'SettingsMcpEnabledSet', enabled: false, scope: 'global', server: 'global-server', expected_sha256: 'hash'});
assert(notices.at(-1).includes('applies to new sessions'));
let mode = control('Persistent tool loading for global-server');
mode.value = 'all'; await mode.onchange();
assert.deepEqual(calls.find(call => call.type === 'SettingsMcpLoadingSet'), {type: 'SettingsMcpLoadingSet', mode: 'all', scope: 'global', server: 'global-server', expected_sha256: 'hash'});
status = 'conflict';
mode = control('Persistent tool loading for global-server'); mode.value = 'search';
const before = calls.filter(call => call.type === 'SettingsMcpLoadingSet').length;
await mode.onchange();
assert.equal(calls.filter(call => call.type === 'SettingsMcpLoadingSet').length, before + 1);
assert.equal(control('Persistent tool loading for global-server').value, 'all');
assert(text().includes('settings were not saved'));
failRead = true;
toggle = control('Persistently enable global-server'); toggle.checked = true; await toggle.onchange();
assert(text().includes('unreadable')); assert.equal(control('Persistently enable global-server').checked, false);
failRead = false;
let scope = control('MCP settings scope'); scope.value = 'project'; await scope.onchange();
assert(text().includes('project-server')); assert(!text().includes('global-server'));
failInspect = true; await controller.load(); assert(text().includes('offline')); assert(!control('Persistently enable project-server'));
failInspect = false;
let resolveInspect;
inspectDeferred = new Promise(resolve => {resolveInspect = resolve;});
const pending = controller.load('session-old', 'global');
controller.invalidate(); open = false;
const closedText = text(); resolveInspect({mcp_servers: [server]}); await pending;
assert.equal(text(), closedText);
open = true; inspectDeferred = null;
await controller.load('session-new', 'global');
let resolveRead;
readDeferred = new Promise(resolve => {resolveRead = resolve;});
toggle = control('Persistently enable global-server'); toggle.checked = true;
const pendingSave = toggle.onchange();
assert(control('MCP settings scope').disabled);
const writes = calls.filter(call => call.type.startsWith('SettingsMcp')).length;
controller.invalidate(); open = false; resolveRead({sha256: 'new-hash'}); await pendingSave;
assert.equal(calls.filter(call => call.type.startsWith('SettingsMcp')).length, writes);
readDeferred = null; open = true;
await controller.load('session-new', 'project');
assert.equal(control('MCP settings scope').disabled, false);
await controller.load('session-new', 'global');
server.scope = 'project'; await controller.load(); assert(text().includes('No effective MCP servers in global scope'));
await assert.rejects(controller.load('session-new', 'bogus'), /Unknown MCP settings scope/);
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, str(MODULE)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
