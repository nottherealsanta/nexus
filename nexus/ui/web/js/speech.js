// Speech is synthesized and played on the daemon host, never in the browser.
// Wire /speak [download], /voice status and Esc to speak(), voiceStatus(), stop().
export function createSpeech({api, el, notify, showText, getSession, isWorking}) {
  let generation = 0, busy = false, timer = null, wake = null;
  const text = value => typeof value === 'string' ? value : '';
  const report = message => notify(`Speech: ${message}`);
  function display(title, message) {
    // Host messages are untrusted plain text, not Markdown/HTML or URLs.
    showText(title, '', {node: el('pre', 'speech-status', message)});
  }
  async function command(payload, expected) {
    const result = await api.command(payload);
    if (result?.type === 'ErrorResult') throw new Error(text(result.message) || 'Host command failed');
    if (result?.type !== expected) throw new Error('Unexpected host response');
    return result;
  }
  function invalidate() {
    generation++; busy = false;
    clearTimeout(timer); timer = null;
    if (wake) { wake(); wake = null; }
  }
  const pause = () => new Promise(resolve => {
    wake = resolve;
    timer = setTimeout(() => { timer = null; wake = null; resolve(); }, 700);
  });
  function statusText(result) {
    const progress = Math.round(Math.max(0, Math.min(1, Number(result.progress) || 0)) * 100);
    const mb = value => (Math.max(0, Number(value) || 0) / 1000000).toFixed(0);
    return `State: ${text(result.state)}\n${text(result.message)}\nProgress: ${progress}% · ${mb(result.bytes_done)} / ${mb(result.bytes_total)} MB`;
  }
  async function status() {
    try {
      const result = await command({type: 'SpeechStatus'}, 'SpeechStatusResult');
      display('Speech status · daemon host', statusText(result));
      return result;
    } catch (error) { report(`Status failed: ${error.message}`); }
  }
  async function voiceStatus() {
    try {
      const result = await command({type: 'VoiceStatus'}, 'VoiceStatusResult');
      display('Voice status · daemon host', `${statusText(result)}\nEnabled: ${result.enabled === true}\nCached: ${result.cached === true}\nDevice: ${text(result.device)}\nConfigured device: ${text(result.configured_device)}\nAuto-send: ${result.auto_send === true}\nMaximum seconds: ${Number(result.max_seconds) || 0}`);
      return result;
    } catch (error) { notify(`Voice status failed: ${error.message}`); }
  }
  async function run(request, session, downloadOnly, prepared = false) {
    const active = () => request === generation && (downloadOnly || getSession() === session);
    try {
      let result = await command({type: prepared ? 'SpeechPrepare' : 'SpeechStatus'}, 'SpeechStatusResult');
      while (active()) {
        if (result.state === 'ready') {
          if (downloadOnly) { report('The local speech model is available on the daemon host.'); return; }
          if (isWorking()) throw new Error('Wait for the current answer to finish');
          report('Speaking the latest completed answer on the daemon host (not in this browser)…');
          const spoken = await command({type: 'Speak', session_id: session, download: false}, 'SpeakResult');
          if (active()) report(`Host result: ${text(spoken.message) || 'No speech playing'}${text(spoken.backend) ? ` · ${text(spoken.backend)}` : ''}`);
          return;
        }
        if (result.state === 'downloading') {
          display('Speech download · daemon host', statusText(result));
          await pause();
          if (!active()) return;
          result = await command({type: 'SpeechStatus'}, 'SpeechStatusResult');
          continue;
        }
        if (result.state === 'unsupported') throw new Error(text(result.message) || 'Speech packages are not installed on the daemon host');
        if (!['absent', 'error'].includes(result.state)) throw new Error('Unknown speech status');
        if (prepared) throw new Error(text(result.message) || 'Speech model download failed');
        const root = el('div', 'speech-confirm');
        root.append(el('p', '', `${text(result.message)}\nDownload the local Paradee speech model (about 25 MB) and English phonemizer on the daemon host? Answers are not sent to a speech service. Playback uses the host speakers, not this browser.`));
        const accept = el('button', 'toolbar-button', 'Download on host'), cancel = el('button', 'toolbar-button', 'Not now');
        accept.type = cancel.type = 'button';
        let decided = false;
        accept.onclick = async () => {
          if (decided || !active()) return;
          decided = true; accept.disabled = cancel.disabled = true;
          await run(request, session, downloadOnly, true);
        };
        cancel.onclick = () => {
          if (decided || !active()) return;
          decided = true; accept.disabled = cancel.disabled = true; invalidate(); report('Download cancelled.');
        };
        root.append(accept, cancel);
        showText('Speech model · daemon host', '', {node: root});
        return;
      }
    } catch (error) { if (active()) report(`Failed: ${error.message}`); }
    finally { if (request === generation) busy = false; }
  }
  async function speak(args = []) {
    const parts = Array.isArray(args) ? args : String(args).trim().split(/\s+/).filter(Boolean);
    if (parts.length > 1 || (parts.length && parts[0] !== 'download')) { report('Usage: /speak [download]'); return; }
    if (busy) { report('A speech request is already running.'); return; }
    const downloadOnly = parts[0] === 'download', session = getSession();
    if (!downloadOnly && (!session || isWorking())) { report(!session ? 'Select a session first.' : 'Wait for the current answer to finish.'); return; }
    busy = true;
    await run(++generation, session, downloadOnly);
  }
  async function stop() {
    invalidate();
    try {
      const result = await command({type: 'SpeakStop'}, 'SpeakResult');
      report(`Host result: ${text(result.message) || 'No speech playing'}`);
      return result;
    } catch (error) { report(`Stop failed: ${error.message}`); }
  }
  return {speak, stop, status, voiceStatus, invalidate};
}
