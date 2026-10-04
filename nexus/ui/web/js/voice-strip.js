// A floating card above the composer with the elapsed time, an audio-reactive
// canvas waveform (newest sample on the right) and the running transcript.
// Words that are new since the previous preview fade in; while the final
// transcript runs, the wave settles into a travelling ripple and a glow
// sweeps across the words. Clipping of long transcripts is announced with a leading "…".
const MAX_LEVELS = 240, SAMPLE_MS = 33, BAR = 3, GAP = 2;

export function createVoiceStrip(root) {
  const canvas = root.querySelector('canvas'), stateEl = root.querySelector('[data-voice-state]');
  const textEl = root.querySelector('[data-voice-text]'), hintEl = root.querySelector('[data-voice-hint]');
  const ellipsis = document.createElement('span');
  ellipsis.className = 'voice-ellipsis'; ellipsis.textContent = '… '; ellipsis.hidden = true;
  textEl.replaceChildren(ellipsis);
  let levels = [], peak = 0, phase = 'hidden', started = 0, words = [], raf = 0, lastSample = 0, display = 0;
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Speech RMS is roughly 0.01–0.2; lift it so quiet talk still moves the wave.
  function pushLevel(level) { peak = Math.max(peak, Math.min(1, Math.pow(Math.max(0, level) * 9, 0.6))); }

  function show(nextPhase, startedAt = performance.now()) {
    phase = nextPhase; started = startedAt; root.hidden = false; root.dataset.phase = nextPhase;
    if (nextPhase === 'recording') { levels = []; peak = 0; setText(''); }
    hintEl.textContent = nextPhase === 'recording' ? 'Listening… any key stops · Esc discards' : 'Finishing the transcript…';
    if (!raf) raf = requestAnimationFrame(frame);
  }

  function hide() { phase = 'hidden'; root.hidden = true; cancelAnimationFrame(raf); raf = 0; setText(''); }

  function setText(text) {
    const next = String(text || '').split(/\s+/).filter(Boolean);
    let same = 0;
    while (same < words.length && same < next.length && words[same] === next[same]) same++;
    for (const span of [...textEl.querySelectorAll('.voice-word')].slice(same)) span.remove();
    for (let index = same; index < next.length; index++) {
      const span = document.createElement('span');
      span.className = 'voice-word is-fresh';
      span.style.setProperty('--delay', `${Math.min(index - same, 8) * 35}ms`);
      span.style.setProperty('--i', String(index % 40));
      span.textContent = `${next[index]} `;
      textEl.append(span);
    }
    words = next;
    root.classList.toggle('has-text', words.length > 0);
    fit();
  }

  // Keep the newest words: hide leading ones until the text fits its rows,
  // and say so with a leading "…" (the TUI strip clips the same way).
  function fit() {
    const spans = [...textEl.querySelectorAll('.voice-word')];
    for (const span of spans) span.hidden = false;
    ellipsis.hidden = true;
    const limit = parseFloat(getComputedStyle(textEl).maxHeight) || Infinity;
    if (textEl.scrollHeight <= limit + 2) return;
    ellipsis.hidden = false;
    for (let index = 0; index < spans.length - 1 && textEl.scrollHeight > limit + 2; index++) spans[index].hidden = true;
  }

  function frame(now) {
    raf = phase === 'hidden' ? 0 : requestAnimationFrame(frame);
    if (now - lastSample >= SAMPLE_MS) {
      lastSample = now;
      levels.push(phase === 'recording' ? peak : 0); peak = 0;
      if (levels.length > MAX_LEVELS) levels.shift();
    }
    const seconds = Math.max(0, Math.floor((now - started) / 1000));
    if (phase === 'recording') stateEl.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    else stateEl.textContent = 'Transcribing';
    draw(now);
  }

  function draw(now) {
    const ratio = devicePixelRatio || 1, width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const context = canvas.getContext('2d'), style = getComputedStyle(root);
    const accent = style.getPropertyValue('--accent').trim() || '#f55b1b', quiet = style.getPropertyValue('--quiet').trim() || '#6b6b6b';
    context.setTransform(ratio, 0, 0, ratio, 0, 0); context.clearRect(0, 0, width, height);
    const count = Math.floor(width / (BAR + GAP)), middle = height / 2, still = reduced();
    display += ((phase === 'recording' ? 1 : 0) - display) * 0.12;
    const gradient = context.createLinearGradient(0, 0, width, 0);
    gradient.addColorStop(0, quiet); gradient.addColorStop(0.55, accent); gradient.addColorStop(1, accent);
    context.fillStyle = gradient;
    for (let index = 0; index < count; index++) {
      const sample = levels[levels.length - count + index] || 0;
      const ripple = still ? 0.06 : 0.06 + 0.05 * Math.sin(now / 260 - index * 0.32) + (1 - display) * 0.12 * (1 + Math.sin(now / 180 - index * 0.25));
      const value = Math.max(ripple, sample * display), bar = Math.max(2, value * (height - 2));
      const x = index * (BAR + GAP), y = middle - bar / 2;
      context.globalAlpha = 0.35 + 0.65 * (index / count);
      if (context.roundRect) { context.beginPath(); context.roundRect(x, y, BAR, bar, BAR / 2); context.fill(); }
      else context.fillRect(x, y, BAR, bar);
    }
    context.globalAlpha = 1;
  }

  return {show, hide, setText, pushLevel, get phase() { return phase; }};
}
