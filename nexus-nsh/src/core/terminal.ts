// Core-owned terminal resources. This transport does not connect them to the UI.
import { request, sseStream } from "./transport";

export interface TerminalResource {
  id: string; name: string; project_id: string; workspace: string; session_id: string | null;
  creator: string; status: "running" | "exited" | "closed" | "ended";
  created: number; ended: number | null; rows: number; cols: number;
  exit_code: number | null; signal: number | null; controller: string | null;
  background_job_id?: string | null;
}
export interface TerminalFrame {
  version: number; seq: number; type: string;
  [key: string]: unknown;
}
const resource = (id: string) => `/api/terminal/${encodeURIComponent(id)}`;
export const terminals = {
  list: (project_id: string) => request<TerminalResource[]>("GET", "/api/terminal", { query: { project_id } }),
  create: (body: { project_id: string; workspace: string; session_id?: string; client_id: string; rows: number; cols: number }) =>
    request<TerminalResource>("POST", "/api/terminal", { body }),
  get: (id: string) => request<TerminalResource>("GET", resource(id)),
  rename: (id: string, name: string) => request<TerminalResource>("PATCH", resource(id), { body: { name } }),
  close: (id: string) => request<TerminalResource>("DELETE", resource(id)),
  attach: (id: string, client_id: string) => request<{ attachment_id: string }>("POST", `${resource(id)}/attach`, { body: { client_id } }),
  control: (id: string, attachment_id: string, action: "acquire" | "release") =>
    request<TerminalResource>("POST", `${resource(id)}/control`, { body: { attachment_id, action } }),
  detach: (id: string, attachment_id: string) => request("POST", `${resource(id)}/detach`, { body: { attachment_id } }),
  resize: (id: string, attachment_id: string, rows: number, cols: number) =>
    request<TerminalResource>("POST", `${resource(id)}/resize`, { body: { attachment_id, rows, cols } }),
  input: (id: string, attachment_id: string, input_seq: number, data: string, signal?: AbortSignal) =>
    request<{ input_seq: number }>("POST", `${resource(id)}/input`, { body: { attachment_id, input_seq, data }, signal }),
  signal: (id: string, attachment_id: string, signal: "INT" | "TERM" | "HUP") =>
    request("POST", `${resource(id)}/signal`, { body: { attachment_id, signal } }),
};

export async function* terminalStream(id: string, attachmentId: string, after: number | null, signal: AbortSignal): AsyncGenerator<TerminalFrame> {
  const q = new URLSearchParams({ attachment_id: attachmentId });
  if (after !== null) q.set("after", String(after));
  for await (const event of sseStream(`${resource(id)}/stream?${q}`, signal)) {
    const frame = JSON.parse(event.data) as TerminalFrame;
    if (frame.version !== 1 || !Number.isSafeInteger(frame.seq) || frame.seq < 0 || typeof frame.type !== "string") {
      throw new Error("Invalid terminal stream frame");
    }
    yield frame;
  }
}
