"""Run the standalone speech controller against serialized host protocol payloads."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import msgspec

from nexus.host import protocol as p


MODULE = Path(__file__).resolve().parents[1] / "nexus/ui/web/js/speech.js"


@unittest.skipUnless(shutil.which("node"), "node is required for web speech tests")
class WebSpeechTests(unittest.TestCase):
    def test_host_protocol_and_confirmation(self):
        payloads = {
            state: msgspec.to_builtins(p.SpeechStatusResult(
                state=state, message="<img src=x onerror=alert(1)>",
                progress=0.5, bytes_done=12_000_000, bytes_total=25_000_000,
            ))
            for state in ("unsupported", "absent", "downloading", "ready", "error")
        }
        payloads["spoken"] = msgspec.to_builtins(p.SpeakResult(
            message="Finished speaking the latest answer", backend="paradee-cpu"))
        payloads["stopped"] = msgspec.to_builtins(p.SpeakResult(message="", backend=""))
        payloads["voice"] = msgspec.to_builtins(p.VoiceStatusResult(
            state="disabled", enabled=False, cached=True, device="cpu",
            configured_device="auto", auto_send=False, max_seconds=120))
        payloads["failure"] = msgspec.to_builtins(p.ErrorResult(
            kind="speech_no_answer", message="There is no completed assistant answer to speak"))
        script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const {createSpeech} = await import('data:text/javascript;base64,' + Buffer.from(readFileSync(process.argv[1])).toString('base64'));
const fixtures = JSON.parse(process.argv[2]);
class Element {
  constructor(tag, cls, text = '') { this.tag = tag; this.textContent = text; this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
}
let calls = [], notices = [], views = [], session = 's1', working = false, phase = 'ready', fail = false;
let prepared = 'ready', deferred = null;
const api = {command: async command => {
  calls.push(command);
  if (command.type === 'SpeechStatus') return fixtures[phase];
  if (command.type === 'SpeechPrepare') { phase = prepared; return fixtures[phase]; }
  if (command.type === 'Speak') return deferred || (fail ? fixtures.failure : fixtures.spoken);
  if (command.type === 'SpeakStop') return fixtures.stopped;
  if (command.type === 'VoiceStatus') return fixtures.voice;
  throw Error('Unexpected command');
}};
const speech = createSpeech({api, el: (...args) => new Element(...args), notify: text => notices.push(text),
 showText: (...args) => views.push(args), getSession: () => session, isWorking: () => working});
const descendants = node => [node, ...node.children.flatMap(descendants)];
const button = label => descendants(views.at(-1)[2].node).find(node => node.textContent === label);
await speech.speak([]);
assert.deepEqual(calls, [{type:'SpeechStatus'}, {type:'Speak', session_id:'s1', download:false}]);
assert(notices.some(text => text.includes('not in this browser')));
assert(notices.at(-1).includes('Host result: Finished speaking'));
calls = []; await speech.speak(['nonsense']); assert.equal(calls.length, 0);
working = true; await speech.speak(); assert.equal(calls.length, 0); working = false;
session = ''; await speech.speak(); assert.equal(calls.length, 0); session = 's1';
for (const state of ['absent', 'error']) {
 phase = state; calls = []; await speech.speak('download');
 assert.equal(calls.length, 1); assert.equal(calls[0].type, 'SpeechStatus');
 assert.equal(views.at(-1)[1], '');
 assert(descendants(views.at(-1)[2].node).some(node => node.textContent.includes('<img')));
 assert(descendants(views.at(-1)[2].node).some(node => node.textContent.includes('25 MB')));
 await button('Download on host').onclick();
 assert.deepEqual(calls[1], {type:'SpeechPrepare'});
 assert(!calls.some(call => call.type === 'Speak'));
 assert(notices.at(-1).includes('available on the daemon host'));
}
phase = 'absent'; calls = []; await speech.speak(); await button('Not now').onclick();
assert.equal(calls.length, 1);
phase = 'absent'; await speech.speak(); const stale = button('Download on host');
session = 's2'; calls = []; await stale.onclick(); assert.equal(calls.length, 0);
phase = 'unsupported'; await speech.speak(); assert(notices.at(-1).includes('Failed:'));
phase = 'absent'; prepared = 'error'; await speech.speak(); await button('Download on host').onclick();
assert(notices.at(-1).includes('Failed:'));
phase = 'ready'; fail = true; await speech.speak(); assert(notices.at(-1).includes('no completed assistant answer')); fail = false;
let resolve; deferred = new Promise(r => resolve = r);
const speaking = speech.speak(); await new Promise(r => setTimeout(r, 0));
await speech.stop(); assert.deepEqual(calls.at(-1), {type:'SpeakStop'});
const count = notices.length; resolve(fixtures.spoken); await speaking;
assert.equal(notices.length, count); assert(notices.at(-1).includes('No speech playing')); deferred = null;
phase = 'downloading'; const pending = speech.speak('download');
await new Promise(r => setTimeout(r, 0));
assert(descendants(views.at(-1)[2].node).some(node => node.textContent.includes('50%')));
phase = 'ready'; await pending; assert(notices.at(-1).includes('available'));
for (const state of ['unsupported','absent','downloading','ready','error']) {
 phase = state; assert.equal((await speech.status()).state, state);
 assert.equal(views.at(-1)[2].node.tag, 'pre');
}
assert.equal((await speech.voiceStatus()).state, 'disabled');
assert.deepEqual(calls.at(-1), {type:'VoiceStatus'});
assert(views.at(-1)[2].node.textContent.includes('Enabled: false'));
assert(views.at(-1)[2].node.textContent.includes('Configured device: auto'));
"""
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script,
             str(MODULE), json.dumps(payloads)], capture_output=True, text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
