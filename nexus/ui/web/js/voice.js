// Browser microphone capture. Audio is held only in bounded memory and returned
// as mono, 16 kHz, signed 16-bit PCM WAV for the local voice route.
const TARGET_RATE = 16000;

function wavBlob(samples) {
  const buffer = new ArrayBuffer(44 + samples.length * 2), view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, TARGET_RATE, true);
  view.setUint32(28, TARGET_RATE * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) view.setInt16(44 + i * 2, samples[i], true);
  return new Blob([buffer], {type: 'audio/wav'});
}

export function createRecorder({maxSeconds = 120, onLevel = () => {}, onLimit = () => {}} = {}) {
  let stream = null, context = null, source = null, processor = null, mute = null, chunks = [];
  let samples = 0, started = 0, stopped = false, limitTimer = 0, flushResolve = null;
  const maxSamples = Math.max(1, Math.min(120, Number(maxSeconds) || 120)) * TARGET_RATE;

  async function start() {
    if (!navigator.mediaDevices?.getUserMedia || !globalThis.AudioWorkletNode) throw new Error('Voice capture is not supported by this browser');
    stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}});
    try {
      if (stopped) throw new DOMException('Voice capture cancelled.', 'AbortError');
      context = new AudioContext();
      await context.audioWorklet.addModule('/js/voice-worklet.js');
      source = context.createMediaStreamSource(stream);
      processor = new AudioWorkletNode(context, 'nexus-voice-resampler');
      mute = context.createGain(); mute.gain.value = 0;
      processor.port.onmessage = event => {
        if (event.data?.samples) {
          const incoming = event.data.samples, remaining = maxSamples - samples;
          if (remaining <= 0) return;
          const kept = incoming.length > remaining ? incoming.slice(0, remaining) : incoming;
          chunks.push(kept); samples += kept.length;
        } else if (event.data?.flushed) flushResolve?.();
        if (Number.isFinite(event.data?.level)) onLevel(event.data.level);
      };
      source.connect(processor); processor.connect(mute); mute.connect(context.destination);
      started = performance.now();
      limitTimer = setTimeout(() => { if (!stopped) { onLimit(); stop(); } }, maxSamples / TARGET_RATE * 1000);
      return this;
    } catch (error) { await release(); throw error; }
  }

  async function release() {
    clearTimeout(limitTimer); limitTimer = 0;
    try { processor?.port.close(); processor?.disconnect(); mute?.disconnect(); source?.disconnect(); } catch {}
    for (const track of stream?.getTracks?.() || []) track.stop();
    stream = null;
    if (context && context.state !== 'closed') await context.close().catch(() => {});
    context = null; source = null; processor = null; mute = null;
  }

  async function stop() {
    if (stopped) return null;
    stopped = true;
    const node = processor;
    if (node) {
      const flushed = new Promise(resolve => { flushResolve = resolve; setTimeout(resolve, 200); });
      node.port.postMessage({type: 'flush'});
      await flushed;
    }
    await release();
    const merged = mergeChunks();
    chunks = [];
    return {wav: wavBlob(merged), duration: merged.length / TARGET_RATE, elapsed: (performance.now() - started) / 1000};
  }

  function mergeChunks() {
    const merged = new Int16Array(Math.min(samples, maxSamples));
    let offset = 0;
    for (const chunk of chunks) { const count = Math.min(chunk.length, merged.length - offset); if (count <= 0) break; merged.set(chunk.subarray(0, count), offset); offset += count; }
    return merged;
  }

  // Everything captured so far, leaving the recording running (live previews).
  function snapshot() {
    const merged = mergeChunks();
    return {wav: wavBlob(merged), duration: merged.length / TARGET_RATE};
  }

  async function cancel() { stopped = true; chunks = []; samples = 0; await release(); }
  return {start, stop, cancel, snapshot, get duration() { return Math.min(samples, maxSamples) / TARGET_RATE; }, get active() { return Boolean(stream) && !stopped; }};
}
