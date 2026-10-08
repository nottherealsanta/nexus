"""Exercise the standalone composer controller with a minimal Node textarea."""
from pathlib import Path
import shutil
import subprocess
import unittest


MODULE = Path(__file__).resolve().parents[1] / "nexus/ui/web/js/composer-navigation.js"

FIXTURE = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {installComposerNavigation} = await import('data:text/javascript;base64,' +
  Buffer.from(readFileSync(process.argv[1])).toString('base64'));
class Input extends EventTarget {
  constructor(native = false) {
    super(); this.value = ''; this.selectionStart = this.selectionEnd = 0;
    this.disabled = this.readOnly = false; this.edits = []; this.inputs = 0;
    this.ownerDocument = {activeElement: this, defaultView: {Event}};
    if (native) this.ownerDocument.execCommand = (command, _, text) => {
      this.edits.push([command, text]); this.setRangeText(text,
        this.selectionStart, this.selectionEnd, 'end');
      this.dispatchEvent(new Event('input')); return true;
    };
    this.addEventListener('input', () => this.inputs++);
  }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  setRangeText(text, start, end) {
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    this.setSelectionRange(start + text.length, start + text.length);
  }
  set(text, start = text.length, end = start) {
    this.value = text; this.setSelectionRange(start, end);
    this.dispatchEvent(new Event('input'));
  }
  key(key, props = {}) {
    const event = new Event('keydown', {cancelable: true, bubbles: true});
    Object.assign(event, {key, ctrlKey: false, metaKey: false, altKey: false,
      shiftKey: false, isComposing: false}, props);
    this.dispatchEvent(event); return event.defaultPrevented;
  }
}
let session = 'one', blocked = false, messages = [], saved = [];
const input = new Input();
const nav = installComposerNavigation({input, getSession: () => session,
  getMessages: () => messages, isBlocked: () => blocked,
  saveHistory: (id, rows) => saved.push([id, rows])});
"""


@unittest.skipUnless(shutil.which("node"), "node is required for web composer tests")
class WebComposerNavigationTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", FIXTURE + script,
             str(MODULE)], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_draft_sessions_and_limit(self):
        self.run_node(r"""
messages = [{role:'assistant', text:'ignore'}, {role:'user', content:'first'},
  {role:'user', blocks:[{kind:'text',text:'sec'}, {kind:'image'}, {type:'text',text:'ond'}]}];
input.set('live draft', 3);
assert.equal(input.key('ArrowDown'), false);
assert(input.key('ArrowUp')); assert.equal(input.value, 'second');
assert(input.key('ArrowUp')); assert.equal(input.value, 'first');
assert(input.key('ArrowUp')); assert.equal(input.value, 'first');
input.key('ArrowDown'); assert.equal(input.value, 'second');
input.key('ArrowDown'); assert.equal(input.value, 'live draft');
assert.equal(input.selectionStart, 3);
input.key('ArrowUp'); input.set('edited'); input.key('ArrowUp');
assert.equal(input.value, 'second'); input.key('ArrowDown'); assert.equal(input.value, 'edited');
nav.remember('third'); nav.remember('third'); nav.remember('   ');
assert.deepEqual(saved.at(-1), ['one', ['first','second','third']]);
input.set(''); input.key('ArrowUp'); assert.equal(input.value, 'third');
session = 'two'; messages = []; input.set('other draft');
assert.equal(input.key('ArrowUp'), false);
for (let i = 0; i < 105; i++) nav.remember(`entry ${i}`);
assert.equal(saved.at(-1)[1].length, 100); assert.equal(saved.at(-1)[1][0], 'entry 5');
input.set(''); for (let i = 0; i < 110; i++) input.key('ArrowUp');
assert.equal(input.value, 'entry 5');
session = 'one'; input.set('back'); input.key('ArrowUp'); assert.equal(input.value, 'third');
nav.dispose(); nav.dispose(); const count = saved.length; nav.remember('ignored');
assert.equal(saved.length, count); assert.equal(input.key('ArrowUp'), false);
""")

    def test_editing_shortcuts_and_native_undo_path(self):
        self.run_node(r"""
const ctrl = key => input.key(key, {ctrlKey:true});
input.set('one two three', 7); assert(ctrl('a')); assert.equal(input.selectionStart, 0);
assert(ctrl('e')); assert.equal(input.selectionStart, 13);
input.set('one two three', 7); ctrl('k'); assert.equal(input.value, 'one two');
ctrl('u'); assert.equal(input.value, '');
input.set('one two   '); assert(ctrl('w')); assert.equal(input.value, 'one ');
input.set('one two', 1, 5); ctrl('w'); assert.equal(input.value, 'owo');
input.set('first\nsecond\nthird', 9); ctrl('a'); assert.equal(input.selectionStart, 6);
ctrl('e'); assert.equal(input.selectionStart, 12);
input.set('first\nsecond\nthird', 9); ctrl('k'); assert.equal(input.value, 'first\nsec\nthird');
ctrl('u'); assert.equal(input.value, 'first\n\nthird');
input.set(''); assert(ctrl('w')); assert.equal(input.value, '');
const native = new Input(true);
const nativeNav = installComposerNavigation({input:native, getSession:()=>'native',
  getMessages:()=>[], isBlocked:()=>false});
native.set('keep remove'); native.key('w', {ctrlKey:true});
assert.equal(native.value, 'keep '); assert.deepEqual(native.edits, [['delete','']]);
nativeNav.remember('history'); native.set('draft'); native.key('ArrowUp');
assert.equal(native.value, 'history'); assert.deepEqual(native.edits.at(-1), ['insertText','history']);
native.key('ArrowDown'); assert.equal(native.value, 'draft');
// Both a thrown command and one returning false must fall back safely.
for (const command of [() => {throw Error('unsupported');}, () => false]) {
 native.ownerDocument.execCommand = command; native.set('remove');
 native.key('u', {ctrlKey:true}); assert.equal(native.value, '');
}
// Never send an editing command to a different focused element.
native.ownerDocument.activeElement = {}; native.ownerDocument.execCommand = () => {throw Error('wrong target');};
native.set('text'); native.key('u', {ctrlKey:true}); assert.equal(native.value, '');
""")

    def test_blocking_and_native_multiline_arrows(self):
        self.run_node(r"""
nav.remember('history');
for (const text of ['first\nsecond', 'first\rsecond']) {
 input.set(text, 0); assert.equal(input.key('ArrowUp'), false);
 input.set(text); assert.equal(input.key('ArrowDown'), false); assert.equal(input.value, text);
}
input.set('draft', 0, 2); assert.equal(input.key('ArrowUp'), false);
for (const modifier of ['ctrlKey','metaKey','altKey','shiftKey']) {
 input.set('draft'); assert.equal(input.key('ArrowUp', {[modifier]:true}), false);
}
for (const props of [{isComposing:true}, {keyCode:229}]) {
 input.set('draft'); assert.equal(input.key('ArrowUp', props), false);
 assert.equal(input.key('w', {ctrlKey:true, ...props}), false);
}
input.dispatchEvent(new Event('compositionstart'));
assert.equal(input.key('u', {ctrlKey:true}), false);
input.dispatchEvent(new Event('compositionend'));
for (const reason of ['modal','slash','voice']) {
 blocked = reason; input.set('draft'); assert.equal(input.key('ArrowUp'), false);
 assert.equal(input.key('w', {ctrlKey:true}), false); assert.equal(input.value, 'draft');
}
blocked = false;
for (const flag of ['disabled','readOnly']) {
 input[flag] = true; assert.equal(input.key('w', {ctrlKey:true}), false); input[flag] = false;
}
for (const modifier of ['metaKey','altKey','shiftKey'])
 assert.equal(input.key('w', {ctrlKey:true, [modifier]:true}), false);
assert(input.key('w', {ctrlKey:true})); assert.equal(input.value, '');
input.addEventListener('keydown', event => event.preventDefault(), {capture:true});
nav.dispose();
""")

    def test_persistence_hooks_and_storage_failures(self):
        self.run_node(r"""
nav.dispose(); let loads = 0, writes = [];
const restored = installComposerNavigation({input, getSession:()=>session,
 getMessages:()=>[{role:'user', text:'fallback'}], isBlocked:()=>false,
 loadHistory:id => {loads++; return [null, '', ' ', ...Array.from({length:102}, (_,i)=>`saved ${i}`)];},
 saveHistory:(id, rows) => {writes.push(rows); rows.length = 0;}});
input.set(''); input.key('ArrowUp'); assert.equal(input.value, 'saved 101');
restored.remember('new'); input.set(''); input.key('ArrowUp'); assert.equal(input.value, 'new');
assert.equal(loads, 1); assert.equal(writes.length, 1); restored.dispose();
const broken = installComposerNavigation({input, getSession:()=>session,
 getMessages:()=>[{role:'user', text:'fallback'}],
 loadHistory:()=>{throw Error('denied');}, saveHistory:()=>{throw Error('full');}});
input.set(''); input.key('ArrowUp'); assert.equal(input.value, 'fallback');
broken.remember('memory'); input.set(''); input.key('ArrowUp'); assert.equal(input.value, 'memory');
session = null; input.set('draft'); broken.remember('no session');
assert.equal(input.key('ArrowUp'), false); broken.dispose();
""")


if __name__ == "__main__":
    unittest.main()
