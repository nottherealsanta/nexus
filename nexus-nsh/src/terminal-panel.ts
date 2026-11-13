import { platform, signal } from "nsh";
import { ApiError, newKey } from "./core/transport";
import { terminals, terminalStream, type TerminalResource } from "./core/terminal";
import { confirmDialog, currentProject, model, toast, viewed } from "./store";
export const terminalAvailable = (platform.os === "web" || platform.os === "macos" || platform.os === "windows" || platform.os === "linux") && platform.desktop && platform.idiom !== "terminal";

// Terminals are tabs in the layout (plan 45, docs/layout.md). Each terminal tab has its own controller: its
// attachment, stream, control state and input queue. The module-level signals below describe the *selected*
// terminal (the focused group's terminal tab): the commands' `when` terms, the evidence flow and the tests read
// those; a pane reads its own controller.
/** At least one terminal tab is in the layout (set by the layout; a terminal.new needs one to sit beside). */
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
type TerminalMessage = {type: string; [key: string]: unknown};
type Sender = (message: TerminalMessage) => void | Promise<void>;
let legacySender: Sender | null = null;
let lastRows = 24, lastCols = 80;
let refreshGeneration = 0;
let terminalsLoaded = false;
/** True once the first terminal list arrived: until then a missing terminal proves nothing. */
export const terminalsKnown = () => terminalsLoaded;
const validDimensions = (r: unknown, c: unknown) => Number.isInteger(r) && Number.isInteger(c) && Number(r) >= 1 && Number(r) <= 200 && Number(c) >= 1 && Number(c) <= 500;
const changeItem = (item: TerminalResource) => { refreshGeneration++; terminalItems.update(items => items.map(t => t.id === item.id ? item : t)); };
const message = (e: unknown) => e instanceof Error ? e.message : String(e);

type Field = "controlled" | "notice" | "error" | "focused" | "renaming" | "name";
const globals = {controlled: terminalControlled, notice: terminalNotice, error: terminalError, focused: terminalFocused, renaming: terminalRename, name: terminalName};

/** One terminal's attachment and screen link. */
class Ctl {
  attachment: string | null = null;
  abort: AbortController | null = null;
  inputAbort: AbortController | null = null;
  generation = 0; seq = 0; inputSeq = 0;
  inputs: Promise<void> = Promise.resolve();
  queuedInput = 0; inputInFlight = false; controlBusy = false; resizeBusy = false;
  rows = lastRows; cols = lastCols;
  sender: Sender | null = null;
  copy: (() => void) | null = null;
  focusList: (() => void) | null = null;
  signals = {controlled: signal(false), notice: signal(""), error: signal(""), focused: signal(false), renaming: signal(false), name: signal("")};
  constructor(readonly id: string) {}
  put(field: Field, value: string | boolean) {
    (this.signals[field] as {set(v: string | boolean): void}).set(value);
    if (terminalSelected() === this.id) (globals[field] as {set(v: string | boolean): void}).set(value);
  }
  send(m: TerminalMessage) { return (this.sender ?? legacySender)?.(m); }
  fail(e: unknown) { this.put("error", message(e)); }
}
const ctls = new Map<string, Ctl>();
const ctl = (id: string) => { let c = ctls.get(id); if (!c) { c = new Ctl(id); ctls.set(id, c); } return c; };
const selectedCtl = () => { const id = terminalSelected(); return id ? ctl(id) : null; };
const itemOf = (id: string) => terminalItems().find(t => t.id === id);

/** A pane's view of a terminal's state: the selected terminal's mirror (also fed by outside writers), else its own. */
export function terminalView(id: string) {
  const c = ctl(id);
  const read = <T,>(field: Field, g: () => T, own: () => T) => terminalSelected() === id ? g() : own();
  return {
    controlled: () => read("controlled", terminalControlled, c.signals.controlled),
    notice: () => read("notice", terminalNotice, c.signals.notice),
    error: () => read("error", terminalError, c.signals.error),
    renaming: () => read("renaming", terminalRename, c.signals.renaming),
    name: () => read("name", terminalName, c.signals.name),
  };
}
/** The terminal tab the keys and commands act on. */
export function focusTerminal(id: string | null) {
  if (terminalSelected() === id) return;
  terminalSelected.set(id);
  const c = id ? ctl(id) : null;
  for (const f of Object.keys(globals) as Field[]) (globals[f] as {set(v: string | boolean): void}).set(c ? c.signals[f]() : (f === "name" || f === "notice" || f === "error" ? "" : false));
}

export function terminalEvidenceContext() { const c = selectedCtl(); return {generation: c?.generation ?? 0, stream_seq: c?.seq || null}; }
export function setTerminalSender(fn: Sender | null) { legacySender = fn; }
/** The screen of one terminal pane; null when it goes. */
export function setTerminalScreen(id: string, sender: Sender | null, actions?: {copy: () => void; focusList: () => void}) {
  const c = ctl(id); c.sender = sender; c.copy = actions?.copy ?? null; c.focusList = actions?.focusList ?? null;
}
export function setTerminalScreenFocus(id: string, focused: boolean) { ctl(id).put("focused", focused); }

export async function refreshTerminals() {
  const project = currentProject(), request = ++refreshGeneration;
  if (!project) return;
  try { const items = await terminals.list(project); if (request === refreshGeneration && project === currentProject()) { terminalItems.set(items.filter(item => item.status !== "closed")); terminalsLoaded = true; } }
  catch (e) { if (request === refreshGeneration) terminalError.set(message(e)); }
}
/** Detach every terminal and clear the selection (a client going away, and the tests' cleanup). */
export function closeTerminalPanel() {
  const c = selectedCtl();
  if (c?.inputInFlight) toast("warning", "Terminal input outcome may be indeterminate after detaching");
  for (const t of ctls.values()) { t.put("renaming", false); t.put("notice", ""); detach(t); }
  focusTerminal(null);
  terminalFocused.set(false); terminalRename.set(false); terminalNotice.set("");
}
export function disconnectTerminal(id = terminalSelected()) { const c = id ? ctls.get(id) : null; if (c) detach(c); }
function detach(c: Ctl) {
  const aid = c.attachment;
  c.generation++; c.controlBusy = false; c.resizeBusy = false; c.abort?.abort(); c.abort = null; c.attachment = null; c.inputAbort?.abort(); c.inputAbort = null;
  c.put("controlled", false); c.send({type:"writable", value:false});
  if (aid) void terminals.detach(c.id, aid).catch(() => {});
}
/** A terminal tab that no longer shows leaves Core's attachment (the terminal itself keeps running). */
export function detachTerminal(id: string) { const c = ctls.get(id); if (c) detach(c); }
/** A terminal that is gone: forget its controller. */
export function forgetTerminal(id: string) { const c = ctls.get(id); if (!c) return; detach(c); ctls.delete(id); if (terminalSelected() === id) focusTerminal(null); }
/** Is a terminal attached (its pane is live)? */
export const terminalAttached = (id: string) => !!ctls.get(id)?.attachment;

/** Attach a terminal for its pane unless it already is, take control when it runs, and make it the selected one. */
export async function showTerminal(id: string, select = true) {
  const c = ctl(id);
  if (select) focusTerminal(id);
  if (c.attachment) return;
  await attach(c);
}
/** Select a terminal and attach it afresh (replay from Core). */
export async function selectTerminal(id: string) {
  const previous = selectedCtl();
  if (previous?.queuedInput) { terminalError.set("Wait for terminal input to be acknowledged"); return; }
  const c = ctl(id);
  if (c.queuedInput) { terminalError.set("Wait for terminal input to be acknowledged"); return; }
  detach(c); c.put("renaming", false);
  focusTerminal(id);
  await attach(c);
}
async function attach(c: Ctl) {
  const id = c.id;
  c.put("notice", ""); c.put("error", ""); c.seq = 0; c.inputSeq = 0;
  const item = itemOf(id);
  c.send({type:"reset", rows:item?.rows, cols:item?.cols});
  const gen = c.generation;
  try {
    const a = await terminals.attach(id, client);
    if (gen !== c.generation) { void terminals.detach(id, a.attachment_id).catch(() => {}); return; }
    c.attachment = a.attachment_id; c.abort = new AbortController(); c.inputAbort = new AbortController();
    void stream(c, a.attachment_id, gen, c.abort.signal);
    if (itemOf(id)?.status === "running" || item?.status === "running") await controlTerminal("acquire", id);
    if (gen === c.generation && terminalSelected() === id) c.send({type:"focus"});
  } catch (e) { if (gen === c.generation) c.fail(e); }
}
async function stream(c: Ctl, first: string, gen: number, signal: AbortSignal) {
  const id = c.id;
  let aid = first;
  let after: number | null = null;
  let uncertainWrite = false;
  while (gen === c.generation && !signal.aborted) {
    try {
      for await (const frame of terminalStream(id, aid, after, signal)) {
        if (gen !== c.generation) return;
        if (frame.type === "gap") {
          if (!validDimensions(frame.rows, frame.cols)) throw new Error("Invalid terminal dimensions.");
          c.put("notice", "Terminal history was discarded; screen may be incomplete.");
          c.send({type:"reset", rows:frame.rows, cols:frame.cols}); c.seq = frame.seq;
        } else {
          if (c.seq && frame.seq <= c.seq) continue;
          if (c.seq && frame.seq !== c.seq + 1) throw new Error("Terminal stream lost frames.");
          if (frame.type === "output") {
            if (typeof frame.data !== "string") throw new Error("Missing terminal output data.");
            uncertainWrite = true; await c.send({type:"write", data:frame.data}); uncertainWrite = false;
          }
          if (frame.type === "resize") {
            if (!validDimensions(frame.rows, frame.cols)) throw new Error("Invalid terminal dimensions.");
            c.send({type:"dimensions", rows:frame.rows, cols:frame.cols});
          }
          if (gen !== c.generation || signal.aborted) return;
          c.seq = frame.seq;
          if (frame.type === "discard") c.put("notice", "Terminal history was discarded; screen may be incomplete.");
          if (frame.type === "exit") {
            c.put("controlled", false); c.send({type:"writable", value:false});
            terminalItems.update(items => items.map(t => t.id === id ? {...t, status:frame.status === "closed" ? "closed" : frame.status === "ended" ? "ended" : "exited", controller:null} : t));
          }
          if (["control", "exit", "rename"].includes(frame.type)) void refreshTerminals();
        }
      }
    } catch (e) { if (signal.aborted || gen !== c.generation) return; c.fail(e); }
    if (signal.aborted || gen !== c.generation) return;
    c.put("controlled", false); c.attachment = null; c.controlBusy = false; c.resizeBusy = false; c.inputAbort?.abort(); c.inputAbort = null; c.send({type:"writable",value:false});
    if (uncertainWrite) { c.seq = 0; c.send({type:"reset"}); uncertainWrite = false; }
    c.put("notice", "Terminal disconnected; resynchronizing. Take control again to type.");
    await new Promise<void>(resolve => setTimeout(resolve, 500));
    if (signal.aborted || gen !== c.generation) return;
    try {
      const next = await terminals.attach(id, client);
      if (signal.aborted || gen !== c.generation) { void terminals.detach(id, next.attachment_id).catch(() => {}); return; }
      aid = next.attachment_id; c.attachment = aid; c.inputAbort = new AbortController(); c.inputSeq = 0; after = c.seq || null;
    } catch (e) { c.fail(e); return; }
  }
}
/** Create a terminal in the viewed session's directory (or the project's). The layout opens its tab; the pane attaches. */
export async function newTerminal(): Promise<TerminalResource | null> {
  const project_id = currentProject(); if (!project_id || !terminalAvailable) return null;
  const session = viewed(); const project = model.projects.get(project_id);
  const workspace = session?.project_id === project_id ? session.directory : project?.path;
  if (!workspace) { terminalError.set("No workspace selected."); return null; }
  try {
    const t = await terminals.create({project_id, workspace, ...(session?.project_id === project_id ? {session_id:session.id} : {}), client_id:client, rows:lastRows, cols:lastCols});
    refreshGeneration++;
    terminalItems.update(items => [...items.filter(i => i.id !== t.id), t]);
    void refreshTerminals();
    return t;
  } catch (e) { terminalError.set(message(e)); return null; }
}
export async function selectCreatedTerminal(item: TerminalResource, project: string) {
  if (currentProject() !== project) return;
  refreshGeneration++;
  terminalItems.update(items => [...items.filter(t => t.id !== item.id), item]);
  await selectTerminal(item.id);
}
export async function renameTerminal(id = terminalSelected()) {
  if (!id) return;
  const c = ctl(id);
  try { changeItem(await terminals.rename(id, terminalView(id).name())); if (terminalView(id).renaming()) c.put("renaming", false); }
  catch (e) { c.fail(e); }
}
export async function terminateTerminal(id = terminalSelected()) {
  if (!id) return;
  try { changeItem(await terminals.close(id)); const c = ctls.get(id); if (c) detach(c); await refreshTerminals(); }
  catch (e) { ctl(id).fail(e); }
}
export async function controlTerminal(action: "acquire" | "release", id = terminalSelected()) {
  if (!id) return;
  const c = ctl(id), aid = c.attachment; if (!aid || c.controlBusy) return;
  if (c.queuedInput) { c.put("error", "Wait for terminal input to be acknowledged"); return; }
  c.controlBusy = true;
  try {
    const item = await terminals.control(id, aid, action);
    if (aid !== c.attachment) return;
    const writable = action === "acquire" && itemOf(id)?.status === "running";
    if (itemOf(id)?.status === "running") changeItem(item);
    c.put("controlled", writable); c.send({type:"writable",value:writable});
    if (writable) { c.send({type:"fit"}); await flushResize(c); }
  } catch (e) { if (aid === c.attachment) c.fail(e); }
  finally { if (aid === c.attachment) c.controlBusy = false; }
}
async function flushResize(c: Ctl) {
  const id = c.id, aid = c.attachment;
  if (!aid || !c.signals.controlled() || c.resizeBusy) return;
  c.resizeBusy = true;
  try {
    for (;;) {
      const sentRows = c.rows, sentCols = c.cols;
      await terminals.resize(id, aid, sentRows, sentCols);
      if (aid !== c.attachment || !c.signals.controlled() || (sentRows === c.rows && sentCols === c.cols)) return;
    }
  } catch (e) { if (aid === c.attachment && c.signals.controlled()) c.fail(e); }
  finally { if (aid === c.attachment) c.resizeBusy = false; }
}
export function resizeTerminal(r: number, c: number, id = terminalSelected()) {
  if (!validDimensions(r, c) || !id) return;
  const t = ctl(id);
  t.rows = r; t.cols = c; lastRows = r; lastCols = c;
  void flushResize(t);
}
export function inputTerminal(value: string, id = terminalSelected()) {
  if (!id) return;
  const c = ctl(id), aid = c.attachment, gen = c.generation, signal = c.inputAbort?.signal;
  if (!aid || !c.signals.controlled()) return;
  const bytes = new TextEncoder().encode(value);
  if (c.queuedInput + bytes.length > 1024 * 1024) { c.put("error", "Terminal input queue is full; input was not sent."); return; }
  c.queuedInput += bytes.length;
  c.inputs = c.inputs.then(async () => {
    for (let offset = 0; offset < bytes.length; offset += 16384) {
      if (c.generation !== gen || c.attachment !== aid || !c.signals.controlled()) return;
      let binary = ""; for (const b of bytes.subarray(offset, offset + 16384)) binary += String.fromCharCode(b);
      const data = btoa(binary), n = ++c.inputSeq;
      for (let attempt = 0; ; attempt++) {
        c.inputInFlight = true;
        try { await terminals.input(id, aid, n, data, signal); break; }
        catch (e) {
          if (c.generation !== gen || c.attachment !== aid) { c.put("error", "Terminal input outcome is indeterminate."); return; }
          if (attempt < 2 && e instanceof ApiError && e.isUnreachable) continue;
          c.put("error", "Terminal input outcome is indeterminate; reconnect before typing again.");
          detach(c); return;
        } finally { c.inputInFlight = false; }
      }
    }
  }).finally(() => {
    c.queuedInput -= bytes.length;
    if (!c.queuedInput && terminalView(id).error() === "Wait for terminal input to be acknowledged") c.put("error", "");
  });
}
export function beginRenameTerminal(id = terminalSelected()) { if (!id) return; const c = ctl(id); c.put("name", itemOf(id)?.name ?? ""); c.put("renaming", true); }
export function currentTerminal() { return terminalSelected() ? itemOf(terminalSelected()!) : undefined; }
/** Close: a running terminal asks first, then terminates; an exited one just closes. The tab goes with the resource. */
export function requestTerminalClose(id = terminalSelected()) {
  const t = terminalItems().find(item => item.id === id); if (!t) return;
  if (t.status === "running") confirmDialog.set({title:"Close running terminal?",body:"",confirm:"Close terminal",destructive:true,bracketActions:true,run:() => terminateTerminal(t.id)});
  else void terminateTerminal(t.id);
}
export function requestTerminalCopy(id = terminalSelected()) { if (id) ctls.get(id)?.copy?.(); }
/** Ctrl+]: leave the terminal screen for its group's tab strip. */
export function focusTerminalList(id = terminalSelected()) { if (id) ctls.get(id)?.focusList?.(); }
export function terminalNameSet(id: string, value: string) { ctl(id).put("name", value); }
export function terminalRenameCancel(id: string) { ctl(id).put("renaming", false); }
