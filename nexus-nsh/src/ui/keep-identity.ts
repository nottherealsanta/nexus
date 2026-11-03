function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || !a || !b || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((k) => sameValue((a as any)[k], (b as any)[k]));
}

/**
 * `next` with each unchanged row replaced by its object in `prev`. The list diffs `each` by identity and forgets the
 * measured height of every row between the first and last that differ, so rebuilt objects collapse the content to
 * estimates and the viewport jumps with it.
 */
export function keepIdentity<T extends { key: string }>(prev: T[], next: T[]): T[] {
  const byKey = new Map(prev.map((row) => [row.key, row]));
  return next.map((row) => {
    const old = byKey.get(row.key);
    return old && sameValue(old, row) ? old : row;
  });
}
