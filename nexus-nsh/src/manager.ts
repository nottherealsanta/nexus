// The Manager's pure logic (docs/manager-design.md 10.1 to 10.7): wording, card helpers, level smoothing,
// the orb's state priority, the speech queue and `/manager` parsing. No signals and no store here,
// so tests and the controller (core/manager.ts) can both import it.

/** The exact wording shared with the TUI. */
export const WORDS = {
  listening: "Listening",
  thinking: "Thinking",
  muted: "Muted",
  reconnecting: "Reconnecting",
  placeholder: "Tell the Manager…",
  liveUnsupported: "This device cannot carry a GPT-Live voice call yet.",
  liveUnsupportedFix: "Type to the Manager here, or set Settings → Manager → Mode to pipeline to talk.",
  disabled: "Manager is off. Turn it on in Settings › Manager.",
  noVoice: "Voice isn't set up. Type instead, or set it up in Settings › Dictation.",
  gone: "That session no longer exists.",
  via: "via Manager",
  needsYou: "Needs you",
  running: "Running",
  finished: "Finished",
  cut: "… cut",
} as const;

/** A holder is the label a client gave itself; name it for a person, never show the id. */
export function holderName(holder: string): string {
  if (/^nsh/i.test(holder)) return "NSH";
  if (/^tui/i.test(holder)) return "the TUI";
  return "another client";
}
export const moved = (holder: string) => `Moved to ${holderName(holder)}`;

/** A spoken "that's all" ends the call, like the End button and the stop key. */
export function spokenEnd(text: string): boolean {
  const words = text
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .split(/\s+/)
    .join(" ");
  return [
    "thats all", "that is all", "thats all for now", "that is all for now",
    "end the call", "hang up", "goodbye", "bye",
  ].includes(words);
}

/**
 * One quiet hint line under the orb: the current state's keys, including how to
 * end the call. `key` resolves a command id to this client's key label.
 */
export interface HintState {
  consent?: boolean;
  minimised: boolean;
  field: boolean;
  listening: boolean;
  card: boolean;
  cardMax: number;
  speaking: boolean;
  thinking: boolean;
}
export function managerHint(s: HintState, key: (id: string) => string): string {
  if (s.consent) return "enter accept · esc cancel";
  const talk = key("manager.talk");
  const stopSpeech = key("manager.speech_stop");
  const cancel = key("manager.turn_cancel");
  const dismiss = key("manager.card_dismiss");
  const parts: string[] = [];
  if (s.minimised) {
    parts.push(s.card ? `${talk} open` : `${talk} talk`);
    if (s.speaking) parts.push(`${stopSpeech} stop`);
    parts.push("esc end");
    return parts.join(" · ");
  }
  if (s.field) return "enter send · esc end";
  if (s.listening) return "space send · type to type · esc end";
  if (s.card) {
    if (s.cardMax === 1) parts.push("1 open");
    else if (s.cardMax > 1) parts.push(`1-${Math.min(9, s.cardMax)} open`);
    parts.push("space talk", `${dismiss} dismiss`, "esc end");
    return parts.join(" · ");
  }
  if (s.speaking) return `space talk · ${stopSpeech} stop · esc end`;
  if (s.thinking) return `space talk · ${cancel} cancel · esc end`;
  return "space talk · esc end";
}

const squash = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "");
/** A title is worth showing only when it says more than the handle does. */
export const titleAdds = (handle: string, title?: string) => !!title && squash(title) !== squash(handle);
/** An approval's summary that only repeats its detail (`run: ls` over `ls`) is hidden. */
export function summaryRepeats(summary: string, detail: string): boolean {
  const bare = summary.replace(/^[A-Za-z ]{1,12}:\s*/, "").trim();
  const d = detail.trim();
  return !!bare && (d === bare || d.includes(bare) || bare.includes(d));
}
/** The row's second line: the `why` or else the state's words, then the title if it adds to the handle. */
export function rowLine(row: Row): string {
  return [row.why || stateWord(row.state), titleAdds(row.handle, row.title) ? row.title : ""].filter(Boolean).join(" · ");
}
export const more = (n: number) => `+${n} more`;

/** The scrim over the app and the panel's opacity (the theme cannot hold alpha). */
export const SCRIM_ALPHA = 0.25;
/** A focused call dims the whole app to about 30%, like the TUI's call scrim. */
export const CALL_SCRIM_ALPHA = 0.70;
export const PANEL_ALPHA = 0.8;
/** Orb entry and exit, caption fade, and the modal's rise. */
export const ORB_MS = 240;
export const CAPTION_QUIET_MS = 5000;
export const CARD_COLLAPSE_MS = 12000;
/** Orb size: 44 to 72 px, with a 44 px hit target. */
export const ORB_SIZE = 64;
export const ORB_HIT = 44;
/** The focused call's orb: large and centred. */
export const ORB_LARGE = 184;
export const ORB_LARGE_CANVAS = 260;
/** Levels move like a voice: fast attack, slow release. */
export const ATTACK_MS = 60;
export const RELEASE_MS = 240;

export type Phase = "idle" | "thinking" | "replying";
export type OrbMode = "listening" | "speaking" | "thinking" | "settling";

export interface Row { n: number; session_id: string; handle: string; title?: string; project?: string; state: string; why?: string }
interface CardBase { id: string; focused: boolean; title?: string }
export type Card = CardBase & (
  | { type: "board"; project?: string; columns: { needs_you: Row[]; running: Row[]; finished: Row[] }; more?: { needs_you?: number; running?: number; finished?: number } }
  | { type: "sessions"; rows: Row[]; more?: number }
  | { type: "approval"; session_id: string; handle: string; approval_id: string; summary: string; detail: string; clipped?: boolean }
  | { type: "question"; session_id: string; handle: string; question: string; options: string[] }
  | { type: "diagram"; source: string; clipped?: boolean }
  | { type: "excerpt"; handle: string; session_id: string; markdown: string; clipped?: boolean }
  | { type: "note"; markdown: string; clipped?: boolean }
);
export type CardType = Card["type"];

export interface ManagerError { code: string; message: string; fix?: string }

/** Priority when several hold: listening, speaking, thinking, settling. `null` means nothing to draw. */
export function orbMode(s: { listening: boolean; speaking: boolean; phase: Phase; settling: boolean }): OrbMode | null {
  if (s.listening) return "listening";
  if (s.speaking) return "speaking";
  if (s.phase !== "idle") return "thinking";
  return s.settling ? "settling" : null;
}

/** One smoothing step: a fast attack when the target is above, a slow release when below. */
export function followLevel(current: number, target: number, dtMs: number): number {
  const tau = target > current ? ATTACK_MS : RELEASE_MS;
  const k = 1 - Math.exp(-Math.max(0, dtMs) / tau);
  return current + (target - current) * k;
}

/** An ease that never overshoots; `t` runs 0 to 1. */
export const easeOut = (t: number) => 1 - (1 - Math.min(1, Math.max(0, t))) ** 3;

/** About how long a sentence takes to read: a floor, then 55 ms a character, capped. */
export function readingMs(text: string): number {
  return Math.min(9000, Math.max(2200, 900 + text.length * 55));
}

// --- speech queue ------------------------------------------------------------------------------------

export interface SpeechItem { turn_id: string; seq: number; text: string; urgent?: boolean; proactive?: boolean }

/**
 * Sentences in order: a turn plays seq 0, 1, 2, ... and turns play in the order they first
 * arrived. A repeated (turn, seq) is dropped. Nothing waits for a missing seq: the queue plays
 * what it has, so a lost sentence never blocks the rest.
 */
export class SpeechQueue {
  private turns: string[] = [];
  private items: SpeechItem[] = [];
  private seen = new Set<string>();

  push(item: SpeechItem) {
    const key = `${item.turn_id}\n${item.seq}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    if (!this.turns.includes(item.turn_id)) this.turns.push(item.turn_id);
    this.items.push(item);
    this.items.sort((a, b) => this.turns.indexOf(a.turn_id) - this.turns.indexOf(b.turn_id) || a.seq - b.seq);
  }
  next(): SpeechItem | undefined { return this.items.shift(); }
  get length() { return this.items.length; }
  clear() { this.items = []; }
}

// --- cards -----------------------------------------------------------------------------------------

export type Column = "needs_you" | "running" | "finished";
export const COLUMNS: { key: Column; label: string }[] = [
  { key: "needs_you", label: WORDS.needsYou },
  { key: "running", label: WORDS.running },
  { key: "finished", label: WORDS.finished },
];

/** Which column a state word belongs to (Core's unified states, plus the plain words a fixture may use). */
export function columnOf(state: string): Column {
  switch (state) {
    case "needs_approval": case "needs_answer": case "blocked": case "failed": case "waiting": return "needs_you";
    case "working": case "running": return "running";
    default: return "finished";
  }
}

const STATE_WORDS: Record<string, string> = {
  needs_approval: "needs approval", needs_answer: "needs an answer", blocked: "blocked", failed: "failed",
  working: "running", archived: "archived", idle: "idle", finished_unread: "finished", waiting: "waiting",
};
export const stateWord = (state: string) => STATE_WORDS[state] ?? state.replace(/_/g, " ");

/** Every numbered row of a card, in the order it is drawn. */
export function cardRows(card: Card | null): Row[] {
  if (!card) return [];
  if (card.type === "sessions") return card.rows;
  if (card.type === "board") return [...card.columns.needs_you, ...card.columns.running, ...card.columns.finished];
  return [];
}

/** The highest row number (rows are numbered across the whole card), 0 when the card has none. */
export const rowCount = (card: Card | null) => cardRows(card).reduce((top, row) => Math.max(top, row.n), 0);

/** The live facts a client knows about a session: its title and unified state. */
export interface LiveSession { title?: string; state: string }

/**
 * A row drawn from the client's live store when it has the session, from the payload otherwise.
 * `n` and `handle` are the Manager's, so "2" and the spoken name stay the row the user saw.
 */
export function liveRow(row: Row, live: LiveSession | null): Row {
  return live ? { ...row, title: live.title || row.title, state: live.state } : row;
}

/** A board's columns from live rows, so a session that finishes moves column while the user watches. */
export function boardColumns(card: Extract<Card, { type: "board" }>, live: (row: Row) => LiveSession | null): Record<Column, Row[]> {
  const out: Record<Column, Row[]> = { needs_you: [], running: [], finished: [] };
  for (const original of [...card.columns.needs_you, ...card.columns.running, ...card.columns.finished]) {
    const row = liveRow(original, live(original));
    out[columnOf(row.state)].push(row);
  }
  for (const key of Object.keys(out) as Column[]) out[key].sort((a, b) => a.n - b.n);
  return out;
}

/** Footer hints, muted, in this order and only those that apply. */
export function footerHints(card: Card | null, opts: { focused: boolean; dismiss?: string }): string {
  if (!opts.focused) return "";
  const parts: string[] = [];
  const top = rowCount(card);
  if (top === 1) parts.push("1 open");
  else if (top > 1) parts.push(`1-${Math.min(9, top)} open`);
  parts.push("space talk", `${opts.dismiss ?? "alt+shift+d"} dismiss`);
  return parts.join(" · ");
}

/** A short sentence for an unfocused card that collapsed or has not been spoken about. */
export function cardCaption(card: Card): string {
  switch (card.type) {
    case "approval": return `${card.handle}: ${card.summary}`;
    case "question": return `${card.handle} asks: ${card.question}`;
    case "board": return "Sessions";
    case "sessions": return "Matching sessions";
    case "diagram": return card.title ?? "A diagram";
    case "excerpt": return `From ${card.handle}`;
    case "note": return card.title ?? "A note";
  }
}

/** The card's quiet title. */
export function cardTitle(card: Card): string {
  if (card.title) return card.title;
  switch (card.type) {
    case "board": return card.project ? `Sessions in ${card.project}` : "Sessions";
    case "sessions": return "Matching sessions";
    case "approval": return "Approval";
    case "question": return "Question";
    case "diagram": return "Diagram";
    case "excerpt": return "Excerpt";
    case "note": return "Note";
  }
}

/** A well-formed card, or null: the client never draws what it cannot read. */
export function parseCard(value: unknown, forceUnfocused = false): Card | null {
  if (!value || typeof value !== "object") return null;
  const c = value as Record<string, unknown>;
  if (typeof c.id !== "string" || typeof c.type !== "string") return null;
  if (!["board", "sessions", "approval", "question", "diagram", "excerpt", "note"].includes(c.type)) return null;
  const card = { ...c, focused: forceUnfocused ? false : c.focused !== false } as Record<string, unknown>;
  if (card.type === "board") {
    const columns = (c.columns ?? {}) as Record<string, unknown>;
    card.columns = {
      needs_you: Array.isArray(columns.needs_you) ? columns.needs_you : [],
      running: Array.isArray(columns.running) ? columns.running : [],
      finished: Array.isArray(columns.finished) ? columns.finished : [],
    };
  }
  if (card.type === "sessions" && !Array.isArray(card.rows)) card.rows = [];
  if (card.type === "question" && !Array.isArray(card.options)) card.options = [];
  return card as unknown as Card;
}

// --- `/manager` ------------------------------------------------------------------------------------------

export type ManagerSlash = { kind: "start" } | { kind: "stop" } | { kind: "say"; text: string };

/** `/manager` alone starts; `/manager stop` stops; anything else is one typed line. */
export function parseManagerSlash(arg: string): ManagerSlash {
  const text = arg.trim();
  if (!text) return { kind: "start" };
  if (text.toLowerCase() === "stop") return { kind: "stop" };
  return { kind: "say", text };
}

// --- keys ------------------------------------------------------------------------------------------------

/** The character a keystroke types, or null for a key that is not printable text. */
export function printableOf(keystroke: string): string | null {
  const parts = keystroke.toLowerCase().split("+");
  const key = parts.pop() ?? "";
  if (parts.some((p) => p !== "shift")) return null;
  const shift = parts.includes("shift");
  if (key.length === 1) {
    if (shift && /[a-z]/.test(key)) return key.toUpperCase();
    if (shift) return SHIFTED[key] ?? null;
    return key;
  }
  return null;
}
const SHIFTED: Record<string, string> = { "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*", "9": "(", "0": ")", "/": "?", "-": "_", "=": "+", ";": ":", "'": "\"", ",": "<", ".": ">", "[": "{", "]": "}" };

/** Every key the field opens on while the Manager is engaged. */
export const PRINTABLE_KEYS: string[] = [
  ..."abcdefghijklmnopqrstuvwxyz0123456789".split(""),
  ..."abcdefghijklmnopqrstuvwxyz".split("").map((c) => `shift+${c}`),
  ..."/-=;',.[]".split(""),
  "shift+/", "shift+1", "shift+2", "shift+;", "shift+'", "shift+,", "shift+.", "shift+-",
];

// --- dev-only fixed levels (inert unless a client pref names them) --------------------------------------------

/** `nexus.manager.dev` in client storage: `{ "level": 0.7, "voice": "simulate", "live": true, "cards": "unfocused" }`. Nothing writes it. */
export interface DevOrb { level: number; simulate: boolean; live: boolean; say?: string; unfocused: boolean; card?: unknown }
export function parseDevOrb(raw: string | null | undefined): DevOrb | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    if (!v || typeof v !== "object") return null;
    const level = typeof v.level === "number" ? Math.min(1, Math.max(0, v.level)) : v.card ? 0.5 : NaN;
    if (!Number.isFinite(level)) return null;
    return { level, simulate: v.voice === "simulate", live: v.live === true, say: typeof v.say === "string" ? v.say : undefined, unfocused: v.cards === "unfocused", card: v.card };
  } catch { return null; }
}

/** A soft breathing envelope, for a playback that cannot be measured: 0.15 to 0.55, no random jumps. */
export function syntheticLevel(ms: number): number {
  return 0.35 + 0.2 * Math.sin(ms / 190) * Math.sin(ms / 530 + 1.3);
}
