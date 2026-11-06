import { platform, signal } from "nsh";
import { ApiError, newKey } from "./core/transport";
import { terminals, terminalStream, type TerminalResource } from "./core/terminal";
import { confirmDialog, currentProject, model, viewed } from "./store";
export const terminalAvailable = (platform.os === "web" || platform.os === "macos" || platform.os === "windows" || platform.os === "linux") && platform.desktop && platform.idiom !== "terminal";

export const terminalOpen = signal(false);
export const terminalFocused = signal(false);
export const terminalItems = signal<TerminalResource[]>([]);
export const terminalSelected = signal<string | null>(null);
export const terminalControlled = signal(false);
export const terminalError = signal("");
export const terminalNotice = signal("");
export const terminalRename = signal(false);
export const terminalName = signal("");
const client = newKey();
let attachment: string | null = null;
let abort: AbortController | null = null;
let inputAbort: AbortController | null = null;
let generation = 0;
let refreshGeneration = 0;
let seq = 0;
let inputSeq = 0;
let inputs = Promise.resolve();
let queuedInput = 0;
type TerminalMessage = {type: string; [key: string]: unknown};
let sender: ((message: TerminalMessage) => void | Promise<void>) | null = null;
let cols = 80, rows = 24;
const send = (message: TerminalMessage) => sender?.(message);
const failure = (error: unknown) => terminalError.set(error instanceof Error ? error.message : String(error));
const selected = () => terminalItems().find(t => t.id === terminalSelected());
const changeItem = (item: TerminalResource) => { refreshGeneration++; terminalItems.update(items => items.map(t => t.id === item.id ? item : t)); };

export function setTerminalSender(fn: ((message: TerminalMessage) => void | Promise<void>) | null) { sender = fn; }
export async function refreshTerminals() {
  const project = currentProject(), request = ++refreshGeneration;
  if (!project) return;
  try { const items = await terminals.list(project); if (request === refreshGeneration && project === currentProject()) terminalItems.set(items); }
  catch (e) { if (request === refreshGeneration) failure(e); }
}
export async function openTerminal() {
  if (!terminalAvailable || !currentProject()) return;
  terminalOpen.set(true); terminalError.set("");
  await refreshTerminals();
}
export function closeTerminalPanel() {
  terminalOpen.set(false); terminalFocused.set(false); detach(); terminalSelected.set(null);
}
export function disconnectTerminal() { detach(); }
function detach() {
  const id = terminalSelected(), aid = attachment;
  generation++; abort?.abort(); abort = null; attachment = null; inputAbort?.abort(); inputAbort = null;
  terminalControlled.set(false); send({type:"writable", value:false});
  if (id && aid) void terminals.detach(id, aid).catch(() => {});
}
export async function selectTerminal(id: string) {
  detach(); terminalSelected.set(id); terminalNotice.set(""); terminalError.set(""); seq = 0; inputSeq = 0;
  send({type:"reset"});
  const gen = generation;
  try {
    const a = await terminals.attach(id, client);
    if (gen !== generation) { void terminals.detach(id, a.attachment_id).catch(() => {}); return; }
    attachment = a.attachment_id; abort = new AbortController(); inputAbort = new AbortController();
    void stream(id, a.attachment_id, gen, abort.signal);
  } catch (e) { if (gen === generation) failure(e); }
}
async function stream(id: string, first: string, gen: number, signal: AbortSignal) {
  let aid = first;
  let after: number | null = null;
  let uncertainWrite = false;
  while (gen === generation && !signal.aborted) {
    try {
      for await (const frame of terminalStream(id, aid, after, signal)) {
        if (gen !== generation) return;
        if (frame.type === "gap") {
          terminalNotice.set("Terminal history was discarded; screen may be incomplete.");
          send({type:"reset", rows:frame.rows, cols:frame.cols}); seq = frame.seq;
        } else {
          if (seq && frame.seq !== seq + 1) throw new Error("Terminal stream lost frames.");
          if (frame.type === "output" && typeof frame.data === "string") { uncertainWrite = true; await send({type:"write", data:frame.data}); uncertainWrite = false; }
          if (gen !== generation || signal.aborted) return;
          seq = frame.seq;
          if (frame.type === "discard") terminalNotice.set("Terminal history was discarded; screen may be incomplete.");
          if (["control", "exit", "rename"].includes(frame.type)) void refreshTerminals();
        }
      }
    } catch (e) { if (signal.aborted || gen !== generation) return; failure(e); }
    if (signal.aborted || gen !== generation) return;
    terminalControlled.set(false); attachment = null; inputAbort?.abort(); inputAbort = null; send({type:"writable",value:false});
    if (uncertainWrite) { seq = 0; send({type:"reset"}); uncertainWrite = false; }
    terminalNotice.set("Terminal disconnected; resynchronizing. Take control again to type.");
    await new Promise<void>(resolve => setTimeout(resolve, 500));
    if (signal.aborted || gen !== generation) return;
    try {
      const next = await terminals.attach(id, client);
      if (signal.aborted || gen !== generation) { void terminals.detach(id, next.attachment_id).catch(() => {}); return; }
      aid = next.attachment_id; attachment = aid; inputAbort = new AbortController(); inputSeq = 0; after = seq || null;
    } catch (e) { failure(e); return; }
  }
}
export async function newTerminal() {
  const project_id = currentProject(); if (!project_id || !terminalAvailable) return;
  const session = viewed(); const project = model.projects.get(project_id);
  const workspace = session?.project_id === project_id ? session.directory : project?.path;
  if (!workspace) { terminalError.set("No workspace selected."); return; }
  try {
    const t = await terminals.create({project_id, workspace, ...(session?.project_id === project_id ? {session_id:session.id} : {}), client_id:client, rows, cols});
    await refreshTerminals(); await selectTerminal(t.id);
  } catch (e) { failure(e); }
}
export async function renameTerminal() {
  const id = terminalSelected(); if (!id) return;
  try { changeItem(await terminals.rename(id, terminalName())); terminalRename.set(false); }
  catch (e) { failure(e); }
}
export async function terminateTerminal() {
  const id = terminalSelected(); if (!id) return;
  try { changeItem(await terminals.close(id)); detach(); await refreshTerminals(); }
  catch (e) { failure(e); }
}
export async function controlTerminal(action: "acquire" | "release") {
  const id = terminalSelected(), aid = attachment; if (!id || !aid) return;
  try {
    const item = await terminals.control(id, aid, action);
    if (aid !== attachment) return;
    changeItem(item); terminalControlled.set(action === "acquire"); send({type:"writable",value:action === "acquire"});
    if (action === "acquire") await terminals.resize(id, aid, rows, cols);
  } catch (e) { failure(e); }
}
export function resizeTerminal(r: number, c: number) {
  if (!Number.isInteger(r) || !Number.isInteger(c) || r < 1 || c < 1) return;
  rows = r; cols = c; const id = terminalSelected(), aid = attachment;
  if (id && aid && terminalControlled()) void terminals.resize(id, aid, rows, cols).catch(failure);
}
export function inputTerminal(value: string) {
  const id = terminalSelected(), aid = attachment, gen = generation, signal = inputAbort?.signal;
  if (!id || !aid || !terminalControlled()) return;
  const bytes = new TextEncoder().encode(value);
  if (queuedInput + bytes.length > 1024 * 1024) { terminalError.set("Terminal input queue is full; input was not sent."); return; }
  queuedInput += bytes.length;
  inputs = inputs.then(async () => {
    for (let offset = 0; offset < bytes.length; offset += 16384) {
      if (generation !== gen || attachment !== aid || !terminalControlled()) return;
      let binary = ""; for (const b of bytes.subarray(offset, offset + 16384)) binary += String.fromCharCode(b);
      const data = btoa(binary), n = ++inputSeq;
      for (let attempt = 0; ; attempt++) {
        try { await terminals.input(id, aid, n, data, signal); break; }
        catch (e) {
          if (generation !== gen || attachment !== aid) { terminalError.set("Terminal input outcome is indeterminate."); return; }
          if (attempt < 2 && e instanceof ApiError && e.isUnreachable) continue;
          terminalError.set("Terminal input outcome is indeterminate; reconnect before typing again.");
          detach(); return;
        }
      }
    }
  }).finally(() => { queuedInput -= bytes.length; });
}
export function beginRenameTerminal() { terminalName.set(selected()?.name ?? ""); terminalRename.set(true); }
export function currentTerminal() { return selected(); }
export function requestTerminalClose() {
  const t = selected(); if (!t) return;
  if (t.status === "running") confirmDialog.set({title:"Close running terminal?",body:"",confirm:"Close terminal",destructive:true,run:terminateTerminal});
  else void terminateTerminal();
}
let copyAction: (() => void) | null = null;
let focusListAction: (() => void) | null = null;
export function setTerminalActions(copy: (() => void) | null, focusList: (() => void) | null) { copyAction = copy; focusListAction = focusList; }
export function requestTerminalCopy() { copyAction?.(); }
export function focusTerminalList() { focusListAction?.(); }
