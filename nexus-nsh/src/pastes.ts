import { signal } from "nsh";

/** A collapsed paste: the marker in the draft and the text it stands for. */
export type DraftPaste = { id: string; marker: string; text: string; tokens: number; lines: number };

export const pasteDrafts = signal<Record<string, DraftPaste[]>>({});

/** Pastes at or above this size collapse into a `[Pasted ~N tokens]` marker, as in the TUI. */
const COLLAPSE_CHARS = 200;
const COLLAPSE_LINES = 3;

let nextPaste = 0;

/** The composer's editor cleans a paste as the TUI does: line endings, tabs, controls. */
export function normalisePaste(text: string): string {
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/\t/g, "    ")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "");
}

/** Lines as Rust's `str::lines` counts them: a trailing newline adds no line. */
export function pasteLines(text: string): number {
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return Math.max(1, lines.length);
}

/** The marker's estimate, matching the TUI: characters / 4, at least one. */
export function pasteTokens(text: string): number {
  return Math.max(1, Math.floor([...text].length / 4));
}

/** The marker shown where a pasted block sits in a user message. */
export function pasteLabel(text: string): string {
  return `[Pasted ~${pasteTokens(text)} tokens]`;
}

export function isBigPaste(text: string): boolean {
  return [...text].length >= COLLAPSE_CHARS || pasteLines(text) >= COLLAPSE_LINES;
}

/**
 * A collapsed paste for `text`, with a marker not already used by the draft or
 * another paste (`[Pasted ~12 tokens]`, then ` #2]`, ` #3]`).
 */
export function makePaste(text: string, entries: DraftPaste[], draft: string): DraftPaste {
  const base = pasteLabel(text);
  let marker = base;
  for (let n = 2; entries.some((p) => p.marker === marker) || draft.includes(marker); n++) {
    marker = `${base.slice(0, -1)} #${n}]`;
  }
  return { id: String(++nextPaste), marker, text, tokens: pasteTokens(text), lines: pasteLines(text) };
}

/** Drop pastes whose marker is no longer in the draft (backspace, selection, a sent draft). */
export function prunePastes(key: string, text: string) {
  const entries = pasteDrafts()[key];
  if (!entries?.length) return;
  const kept = entries.filter((p) => text.includes(p.marker));
  if (kept.length !== entries.length) pasteDrafts.update((d) => ({ ...d, [key]: kept }));
}

export function removePaste(key: string, id: string) {
  pasteDrafts.update((d) => ({ ...d, [key]: (d[key] ?? []).filter((p) => p.id !== id) }));
}
