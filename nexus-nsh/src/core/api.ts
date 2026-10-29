// Core API shapes the shell needs (docs/api.md). Loose on purpose: Core owns the schema.

export interface Project { id: string; path: string; name: string; created: number; updated: number; revision: number }
export interface Session {
  id: string; project_id: string; directory: string; title: string; agent: string;
  parent_id: string | null; children?: string[]; model?: string | null; model_source?: string;
  effort?: string | null; tools?: string[]; disabled_tools?: string[]; permission?: string;
  status?: string; revision: number; created: number; updated: number; archived?: boolean;
  [k: string]: unknown;
}
export interface Part { type: string; text?: string; summary?: string; id?: string; name?: string; arguments?: string; call_id?: string; output?: string; is_error?: boolean; [k: string]: unknown }
export interface Message { id: string; session_id: string; seq: number; created: number; role: "user" | "assistant" | "tool"; parts: Part[] }
export interface Approval { id: string; session_id: string; tool: string; call_id: string; args: Record<string, unknown> }
export interface QuestionOption { id: string; label: string; preview?: string }
export interface QuestionItem { id: string; question: string; options: QuestionOption[]; multiple: boolean; allow_free_text: boolean }
export interface Question { id: string; session_id: string; call_id: string; created: number; questions: QuestionItem[] }
export interface CoreEvent {
  id: number; cursor?: string; seq?: number; time: number; type: string; session_id: string | null;
  data: any;
}
/** Token counts of one request or attempt (`GET …/turn`); `null` where it appears means unknown, never zeros. */
export interface TokenUsage { input: number; cached: number; output: number; reasoning: number }
export interface AttemptSummary {
  n: number; status: string; delay: number | null; started: number | null; finished: number | null;
  error: string | null; usage: TokenUsage | null;
}
export interface RequestSummary {
  id: string; step: number; status: string; model: string | null; effort: string | null;
  started: number | null; finished: number | null; stop: string | null; error: any;
  message_id: string | null; usage: TokenUsage | null; reasoning_seconds: number | null;
  attempts: AttemptSummary[];
}
export interface ToolSummary { call_id: string; name: string; state: string; started: number | null; finished: number | null; is_error: boolean }
/** One turn as Core records it (also the data of the durable `turn.summary` event). */
export interface TurnSummary {
  id: string; status: "running" | "completed" | "failed" | "interrupted" | string;
  started: number; finished: number | null;
  /** The last message seq before the turn: its messages have a higher seq. */
  start_seq: number; steps: number; stop: string | null;
  ended: { stop: string; steps: number } | null;
  error: { kind?: string; message?: string; status?: number | null; [k: string]: unknown } | null;
  recovery: string; requests: RequestSummary[]; tools: ToolSummary[];
}
/** The session's spend ledger per model; it outlives pruned turns. */
export interface UsageLedger { since: number | null; models: Record<string, TokenUsage & { requests: number }> }
export interface TurnPage {
  turns: TurnSummary[]; more_before: boolean; more_after: boolean;
  /** Messages with a lower seq have no summary; `null` when every message is covered. */
  floor: { seq: number } | null; usage: UsageLedger;
}
export interface Watermark { generation: string; head: number; floor: number; head_cursor: string }
export interface Snapshot {
  watermark: Watermark; projects: Project[]; sessions: Session[];
  running: { id: string; session_id: string }[]; approvals: Approval[]; questions: Question[];
  selected?: { session_id: string; messages: Message[]; messages_more: boolean } | null;
}
export interface Info {
  name?: string; version?: string; pid?: number; home?: string; url?: string;
  api_version?: unknown; log?: { generation: string; head: number };
}

export const CLIENT_VERSION = "0.10.0";
/** The API version this client was written against (same convention as nexus-tui). */
export const CLIENT_API = { major: 1, minor: 2 };

export interface ApiVersion { major: number; minor: number }

/** Accepts "1.2", "1", 1.2 or {major, minor} (the TUI's tolerance: Core reports none yet). */
export function parseApiVersion(v: unknown): ApiVersion | null {
  if (v == null) return null;
  if (typeof v === "string" || typeof v === "number") {
    const [a, b] = String(v).split(".");
    const major = Number(a), minor = b === undefined ? 0 : Number(b);
    return Number.isInteger(major) && Number.isInteger(minor) ? { major, minor } : null;
  }
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (typeof o.major === "number") return { major: o.major, minor: typeof o.minor === "number" ? o.minor : 0 };
  }
  return null;
}

export type Compat =
  | { kind: "compatible" }
  | { kind: "unreported" }
  | { kind: "newer_minor"; core: ApiVersion }
  | { kind: "incompatible"; core: ApiVersion; update: "client" | "core" };

export function checkCompat(core: ApiVersion | null, client: ApiVersion = CLIENT_API): Compat {
  if (!core) return { kind: "unreported" };
  if (core.major === client.major) return core.minor > client.minor ? { kind: "newer_minor", core } : { kind: "compatible" };
  return { kind: "incompatible", core, update: core.major > client.major ? "client" : "core" };
}

export function parseCursor(c: string | undefined | null): { generation: string; id: number } | null {
  if (!c) return null;
  const i = c.lastIndexOf(":");
  if (i <= 0) return null;
  const id = Number(c.slice(i + 1));
  return Number.isInteger(id) ? { generation: c.slice(0, i), id } : null;
}
export const formatCursor = (generation: string, id: number) => `${generation}:${id}`;
