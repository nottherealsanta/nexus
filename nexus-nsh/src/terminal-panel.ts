import { Storage, platform, signal } from "nsh";
import { ApiError, newKey } from "./core/transport";
import { terminals, terminalStream, type TerminalResource } from "./core/terminal";
import { confirmDialog, currentProject, model, toast, viewed } from "./store";
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
let inputInFlight = false;
let controlBusy = false;
let resizeBusy = false;
let restored = false;
type TerminalMessage = {type: string; [key: string]: unknown};
let sender: ((message: TerminalMessage) => void | Promise<void>) | null = null;
let cols = 80, rows = 24;
const send = (message: TerminalMessage) => sender?.(message);
const failure = (error: unknown) => terminalError.set(error instanceof Error ? error.message : String(error));
const selected = () => terminalItems().find(t => t.id === terminalSelected());
const validDimensions = (r: unknown, c: unknown) => Number.isInteger(r) && Number.isInteger(c) && Number(r) >= 1 && Number(r) <= 200 && Number(c) >= 1 && Number(c) <= 500;
const changeItem = (item: TerminalResource) => { refreshGeneration++; terminalItems.update(items => items.map(t => t.id === item.id ? item : t)); };

export function setTerminalSender(fn: ((message: TerminalMessage) => void | Promise<void>) | null) { sender = fn; }
export async function refreshTerminals() {
  const project = currentProject(), request = ++refreshGeneration;
  if (!project) return;
  try { const items = await terminals.list(project); if (request === refreshGeneration && project === currentProject()) terminalItems.set(items.filter(item => item.status !== "closed")); }
  catch (e) { if (request === refreshGeneration) failure(e); }
}
export async function restoreTerminalDock() {
  if (restored || !terminalAvailable || !currentProject()) return;
  restored = true;
  try { if (await Storage.getItem("nexus.terminal.open") === "true" && !terminalOpen()) await openTerminal(); } catch {}
}
export async function openTerminal() {
  if (!terminalAvailable || !currentProject()) return;
  if (terminalOpen()) { closeTerminalPanel(); return; }
  terminalOpen.set(true); terminalError.set("");
  void Storage.setItem("nexus.terminal.open", "true").catch(() => {});
  await refreshTerminals();
  const item = terminalItems().find(t => t.status === "running") ?? terminalItems()[0];
  if (item && terminalOpen()) await selectTerminal(item.id);
}
export function closeTerminalPanel() {
  if (inputInFlight) toast("warning", "Terminal input outcome may be indeterminate after detaching");
  terminalOpen.set(false); terminalFocused.set(false); terminalRename.set(false); terminalNotice.set(""); detach(); terminalSelected.set(null);
  void Storage.setItem("nexus.terminal.open", "false").catch(() => {});
}
export function disconnectTerminal() { detach(); }
function detach() {
  const id = terminalSelected(), aid = attachment;
  generation++; controlBusy = false; resizeBusy = false; abort?.abort(); abort = null; attachment = null; inputAbort?.abort(); inputAbort = null;
  terminalControlled.set(false); send({type:"writable", value:false});
  if (id && aid) void terminals.detach(id, aid).catch(() => {});
}
export async function selectTerminal(id: string) {
  if (queuedInput) { terminalError.set("Wait for terminal input to be acknowledged"); return; }
  detach(); terminalRename.set(false); terminalSelected.set(id); terminalNotice.set(""); terminalError.set(""); seq = 0; inputSeq = 0;
  const item = terminalItems().find(t => t.id === id);
  send({type:"reset", rows:item?.rows, cols:item?.cols});
  const gen = generation;
  try {
    const a = await terminals.attach(id, client);
    if (gen !== generation) { void terminals.detach(id, a.attachment_id).catch(() => {}); return; }
    attachment = a.attachment_id; abort = new AbortController(); inputAbort = new AbortController();
    void stream(id, a.attachment_id, gen, abort.signal);
    if (item?.status === "running") await controlTerminal("acquire");
    if (gen === generation) send({type:"focus"});
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
          if (!validDimensions(frame.rows, frame.cols)) throw new Error("Invalid terminal dimensions.");
          terminalNotice.set("Terminal history was discarded; screen may be incomplete.");
          send({type:"reset", rows:frame.rows, cols:frame.cols}); seq = frame.seq;
        } else {
          if (seq && frame.seq <= seq) continue;
          if (seq && frame.seq !== seq + 1) throw new Error("Terminal stream lost frames.");
          if (frame.type === "output") {
            if (typeof frame.data !== "string") throw new Error("Missing terminal output data.");
            uncertainWrite = true; await send({type:"write", data:frame.data}); uncertainWrite = false;
          }
          if (frame.type === "resize") {
            if (!validDimensions(frame.rows, frame.cols)) throw new Error("Invalid terminal dimensions.");
            send({type:"dimensions", rows:frame.rows, cols:frame.cols});
          }
          if (gen !== generation || signal.aborted) return;
          seq = frame.seq;
          if (frame.type === "discard") terminalNotice.set("Terminal history was discarded; screen may be incomplete.");
          if (["control", "exit", "rename"].includes(frame.type)) void refreshTerminals();
        }
      }
    } catch (e) { if (signal.aborted || gen !== generation) return; failure(e); }
    if (signal.aborted || gen !== generation) return;
    terminalControlled.set(false); attachment = null; controlBusy = false; resizeBusy = false; inputAbort?.abort(); inputAbort = null; send({type:"writable",value:false});
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
  if (queuedInput) { terminalError.set("Wait for terminal input to be acknowledged"); return; }
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
  try { changeItem(await terminals.rename(id, terminalName())); if (terminalSelected() === id) terminalRename.set(false); }
  catch (e) { failure(e); }
}
export async function terminateTerminal(id = terminalSelected()) {
  if (!id) return;
  try { changeItem(await terminals.close(id)); if (terminalSelected() === id) detach(); await refreshTerminals(); }
  catch (e) { failure(e); }
}
export async function controlTerminal(action: "acquire" | "release") {
  const id = terminalSelected(), aid = attachment; if (!id || !aid || controlBusy) return;
  if (queuedInput) { terminalError.set("Wait for terminal input to be acknowledged"); return; }
  controlBusy = true;
  try {
    const item = await terminals.control(id, aid, action);
    if (aid !== attachment) return;
    changeItem(item); terminalControlled.set(action === "acquire"); send({type:"writable",value:action === "acquire"});
    if (action === "acquire") { send({type:"fit"}); await flushResize(); }
  } catch (e) { if (aid === attachment) failure(e); }
  finally { if (aid === attachment) controlBusy = false; }
}
async function flushResize() {
  const id = terminalSelected(), aid = attachment;
  if (!id || !aid || !terminalControlled() || resizeBusy) return;
  resizeBusy = true;
  try {
    for (;;) {
      const sentRows = rows, sentCols = cols;
      await terminals.resize(id, aid, sentRows, sentCols);
      if (aid !== attachment || !terminalControlled() || (sentRows === rows && sentCols === cols)) return;
    }
  } catch (e) { if (aid === attachment && terminalControlled()) failure(e); }
  finally { if (aid === attachment) resizeBusy = false; }
}
export function resizeTerminal(r: number, c: number) {
  if (!validDimensions(r, c)) return;
  rows = r; cols = c;
  void flushResize();
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
        inputInFlight = true;
        try { await terminals.input(id, aid, n, data, signal); break; }
        catch (e) {
          if (generation !== gen || attachment !== aid) { terminalError.set("Terminal input outcome is indeterminate."); return; }
          if (attempt < 2 && e instanceof ApiError && e.isUnreachable) continue;
          terminalError.set("Terminal input outcome is indeterminate; reconnect before typing again.");
          detach(); return;
        } finally { inputInFlight = false; }
      }
    }
  }).finally(() => {
    queuedInput -= bytes.length;
    if (!queuedInput && terminalError() === "Wait for terminal input to be acknowledged") terminalError.set("");
  });
}
export function beginRenameTerminal() { terminalName.set(selected()?.name ?? ""); terminalRename.set(true); }
export function currentTerminal() { return selected(); }
export function requestTerminalClose(id = terminalSelected()) {
  const t = terminalItems().find(item => item.id === id); if (!t) return;
  if (t.status === "running") confirmDialog.set({title:"Close running terminal?",body:"",confirm:"Close terminal",destructive:true,run:() => terminateTerminal(t.id)});
  else void terminateTerminal(t.id);
}
let copyAction: (() => void) | null = null;
let focusListAction: (() => void) | null = null;
export function setTerminalActions(copy: (() => void) | null, focusList: (() => void) | null) { copyAction = copy; focusListAction = focusList; }
export function requestTerminalCopy() { copyAction?.(); }
export function focusTerminalList() { focusListAction?.(); }
