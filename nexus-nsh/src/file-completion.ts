// The TUI's completion ranking and per-prefix fallback, in UTF-16-independent characters.
const LIMIT = 10;

function distance(a: string, b: string): number {
  const left = Array.from(a), right = Array.from(b);
  const rows: number[][] = [Array.from({ length: right.length + 1 }, (_, i) => i)];
  for (let i = 1; i <= left.length; i++) {
    const row = [i];
    for (let j = 1; j <= right.length; j++) {
      let cost = Math.min(rows[i - 1][j] + 1, row[j - 1] + 1, rows[i - 1][j - 1] + Number(left[i - 1] !== right[j - 1]));
      if (i > 1 && j > 1 && left[i - 1] === right[j - 2] && left[i - 2] === right[j - 1]) cost = Math.min(cost, rows[i - 2][j - 2] + 1);
      row.push(cost);
    }
    rows.push(row);
  }
  return rows[left.length][right.length];
}

function tier(query: string, path: string): number {
  const needle = query.toLowerCase();
  const name = (path.replace(/\/$/, "").split("/").at(-1) ?? "").toLowerCase();
  if (name.startsWith(needle)) return 0;
  let at = 0;
  for (const c of name) if (c === Array.from(needle)[at]) at++;
  if (name.includes(needle) || at === Array.from(needle).length) return 1;
  const count = Array.from(needle).length;
  const tolerance = count < 5 ? 1 : 2;
  if (count && (distance(needle, Array.from(name).slice(0, count + 1).join("")) <= tolerance || distance(needle, name) <= tolerance)) return 2;
  return 3;
}

export function fileMenu(query: string, hits: string[], pool: string[] | null): string[] {
  if (!pool || hits.length >= LIMIT) return hits;
  const rest = pool.filter((path) => !hits.includes(path)).map((path, index) => ({ path, index, tier: tier(query, path) }));
  rest.sort((a, b) => a.tier - b.tier || a.index - b.index);
  return [...hits, ...rest.map((item) => item.path)].slice(0, LIMIT);
}

export function fileCandidates(cache: { prefix: string; paths: string[] }[], prefix: string): string[] | null {
  const token = prefix.split(/\s/).at(-1) ?? "";
  const context = prefix.slice(0, prefix.length - token.length);
  const entry = [...cache].reverse().filter((item) => {
    const old = item.prefix.split(/\s/).at(-1) ?? "";
    return item.prefix.slice(0, item.prefix.length - old.length) === context && token.startsWith(old);
  }).sort((a, b) => b.prefix.length - a.prefix.length)[0];
  if (!entry) return null;
  if (entry.prefix === prefix) return entry.paths;
  const query = token.slice(1).toLowerCase();
  const paths = entry.paths.filter((path) => !(path.endsWith("/") && path.toLowerCase() === query));
  return [...paths.filter((path) => path.toLowerCase().startsWith(query)), ...paths.filter((path) => !path.toLowerCase().startsWith(query))].slice(0, LIMIT);
}
