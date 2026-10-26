"""Sessions sidebar wording shared with the desktop panel, exercised without a browser."""
from pathlib import Path
import shutil
import subprocess
import unittest


MODULE = Path(__file__).resolve().parents[1] / "nexus/ui/web/js/sidebar.js"
FIXTURE = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {sessionTitle, sessionMeta, managerCard, treeRows} = await import(
  'data:text/javascript;base64,' + Buffer.from(readFileSync(process.argv[1])).toString('base64'));
"""


@unittest.skipUnless(shutil.which("node"), "node is required for web sidebar tests")
class WebSidebarTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", FIXTURE + script, str(MODULE)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_titles_and_metadata(self):
        self.run_node(r"""
assert.equal(sessionTitle(''), 'Untitled conversation');
assert.equal(sessionTitle(undefined), 'Untitled conversation');
assert.equal(sessionTitle('Fix login'), 'Fix login');
assert.equal(sessionTitle('Fix login [mock scenario=a]'), 'Fix login');
assert.equal(sessionTitle('[mock scenario=a]'), 'Mock session');
assert.equal(sessionMeta({message_count: 1}, 'idle', '5m ago'), '1 message · active 5m ago');
assert.equal(sessionMeta({message_count: 0}, 'idle', ''), '0 messages');
assert.equal(sessionMeta({message_count: 4}, 'working', 'just now'), 'Working · 4 messages · active just now');
assert.equal(sessionMeta({message_count: 2}, 'input', '1h ago'), 'Needs input · 2 messages · active 1h ago');
assert.equal(sessionMeta({message_count: 2}, 'archived', '2d ago'), 'archived · 2d ago');
""")

    def test_manager_card(self):
        self.run_node(r"""
assert.equal(managerCard(null), null);
const off = managerCard({enabled: false});
assert.equal(off.tone, 'off'); assert.equal(off.summary, 'Off · [manager] enabled = false');
assert.match(off.detail, /nexus\.toml/); assert.equal(off.privacy, ''); assert.equal(off.enabled, false);
const warn = managerCard({enabled: true, signed_in: false, sign_in: 'sign in\nnow'});
assert.equal(warn.tone, 'warn'); assert.equal(warn.summary, 'Not signed in · sign in now');
const ready = managerCard({enabled: true, signed_in: true, state: 'idle', session: 'nexus-manager',
  model: 'codex/x', codex_signed_in: true, live_consent: true, live_manager_consent: true,
  managed: ['a', 'b']}, 'nexus-manager');
assert.equal(ready.summary, 'Ready · 2 sessions launched'); assert.equal(ready.active, true);
assert.equal(ready.detail, 'codex/x · Voice ready'); assert.match(ready.privacy, /OpenAI/);
assert.equal(managerCard({enabled: true, signed_in: true, state: 'awaiting_input'}).summary, 'Needs you');
assert.equal(managerCard({enabled: true, signed_in: true, session: 'nexus-manager'}, 'other').active, false);
""")

    def test_tree_rows(self):
        self.run_node(r"""
const rows = treeRows({nodes: [{id: 'r', title: 'Root', status: 'idle',
  children: [{id: 'c', title: '', description: 'Child task', status: 'running', children: []}]}]});
assert.deepEqual(rows, [
  {depth: 0, id: 'r', title: 'Root', status: 'idle'},
  {depth: 1, id: 'c', title: 'Child task', status: 'running'}]);
assert.deepEqual(treeRows({}), []);
""")
