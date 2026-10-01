// Shared fuzzy matcher for the command palette and model picker.
// Port of nexus/ui_support/fuzzy.py; keep the constants and scoring in step.
const MAX_TEXT = 512, MAX_QUERY = 128, BASE = 16, CONSECUTIVE = 15, BOUNDARY = 30, GAP = 3, LEAD = 2, SUBSTRING = 20, SEPARATORS = ' /-_.:';
const isLower = c => c.toLowerCase() === c && c.toUpperCase() !== c;
const isUpper = c => c.toUpperCase() === c && c.toLowerCase() !== c;

export function fuzzyMatch(query, text) {
  const q = Array.from(query.trim()).slice(0, MAX_QUERY).map(c => c.toLowerCase());
  if (!q.length) return {score: 0, positions: []};
  const chars = Array.from(text).slice(0, MAX_TEXT), low = chars.map(c => c.toLowerCase()), n = q.length, m = chars.length;
  if (n > m) return null;
  const bonus = chars.map((c, j) => j === 0 || SEPARATORS.includes(chars[j - 1]) || (isLower(chars[j - 1]) && isUpper(c)) ? BOUNDARY : 0);
  const decay = (run, runK, prev, j) => {
    run = run > -Infinity ? run - GAP : -Infinity;
    const cand = prev[j - 2] > -Infinity ? prev[j - 2] - GAP : -Infinity;
    return cand > run ? [cand, j - 2] : [run, runK];
  };
  let prev = [];
  const back = [];
  for (let i = 0; i < n; i++) {
    const row = new Array(m).fill(-Infinity), ptr = new Array(m).fill(-1);
    let run = -Infinity, runK = -1;
    for (let j = 0; j < m; j++) {
      if (low[j] !== q[i]) { if (i && j >= 2) [run, runK] = decay(run, runK, prev, j); continue; }
      const here = BASE + bonus[j];
      if (i === 0) row[j] = here - LEAD * j;
      else {
        if (j >= 2) [run, runK] = decay(run, runK, prev, j);
        let best = -Infinity, bk = -1;
        if (j >= 1 && prev[j - 1] > -Infinity) { best = prev[j - 1] + CONSECUTIVE; bk = j - 1; }
        if (run > best) { best = run; bk = runK; }
        if (best > -Infinity) { row[j] = here + best; ptr[j] = bk; }
      }
    }
    back.push(ptr);
    prev = row;
  }
  let end = 0;
  for (let j = 1; j < m; j++) if (prev[j] > prev[end]) end = j;
  if (prev[end] === -Infinity) return null;
  const pos = [end];
  for (let i = n - 1; i > 0; i--) pos.push(back[i][pos[pos.length - 1]]);
  pos.reverse();
  let win = null;
  for (let s = 0; s + n <= m; s++) {
    if (!q.every((c, i) => low[s + i] === c)) continue;
    const w = q.map((_, i) => s + i);
    let score = -LEAD * s;
    w.forEach((p, i) => { score += BASE + bonus[p] + (i ? CONSECUTIVE : 0); });
    if (!win || score > win.score) win = {score, positions: w};
  }
  if (win) return {score: win.score + SUBSTRING, positions: win.positions};
  return {score: prev[end], positions: pos};
}

export function fuzzyFilter(query, items, key = x => x) {
  const hits = [];
  items.forEach((item, index) => { const f = fuzzyMatch(query, key(item)); if (f) hits.push({item, positions: f.positions, score: f.score, index}); });
  hits.sort((a, b) => b.score - a.score || a.index - b.index);
  return hits.map(({item, positions}) => ({item, positions}));
}

export function highlightSpans(positions) {
  const spans = [];
  for (const p of positions) {
    if (spans.length && spans[spans.length - 1][1] === p) spans[spans.length - 1][1] = p + 1;
    else spans.push([p, p + 1]);
  }
  return spans;
}
