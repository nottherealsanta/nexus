import { Image, Scroll, Text, View, computed, effect, keyboardFrame, keymap, safeArea, signal, untracked, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { sendChoice } from "./mobile/state";
import { Btn, Icon, IconButton, FOCUS, UI_FONT, Shimmer, Status, StatusRail, T, TS, midTruncate, shortPath } from "./kit";
import { dockHeight } from "../core/manager";
import { availability, bindOpenKeys, blurComposer, byId, commands, draftKey, focusComposer, focusWaiting, pasteClipboard, keyLabel, otherWaits, questionLocked, restorePendingParts, resumeArchived, run, runSlashCommand, sendPrompt, term, toKeystroke, waitItems } from "../commands";
import type { Cmd } from "../commands";
import { compactApproval } from "../mobile";
import {
  agentCatalog, contextBudget, contextDetail, draftContext, draftContextKey, draftPlanContext, pendingInputs, loadInputs, loadPromptHistory, promptHistory, centre, config, confirmDialog, connection, currentProject, draft, rememberedModel, info, isNarrow, live, m, model, touch, modelCatalog, tick,
  phone, projectWorktrees, sessionDraft, sessionWorktrees, currentWorktree, userHome, viewResources, viewed, waitIndex, toast, annotating,
  bashJobs, loadResources, resourceCounts, resourcesOpen, resourcesKind, composerFocused, flowStage, flowProject,
} from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { Approval, Question, QuestionItem } from "../core/api";
import { agentColor, agentSlot } from "../agent-color";
import { detailOf, fmtDuration } from "./timeline";
import type { ToolDetailLine } from "./timeline";
import { DiffView, diffItems } from "./diff";
import { TEXT_INSET, TimelineColumn } from "./reading-column";
import { activityOffsetPercent, activityPhase } from "./activity-motion";
import { RetryNotices } from "./retry-notices";
import { refreshTerminals, terminalAvailable, terminalFocused, terminalItems, terminalOpen } from "../terminal-panel";
import { openTerminalTab } from "../layout-state";

import { imageDrafts, readImage } from "../images";
import { isBigPaste, makePaste, normalisePaste, pasteDrafts, pasteLabel, prunePastes } from "../pastes";
import { cancelDictation, stopDictation } from "../dictation";
import { formatBytes } from "../dictation-logic";
import { dictationLevel, dictationPhase, dictationPreview, voiceDownloadProgress } from "../voice-state";
import { PhoneApprovalChoices } from "./mobile/approval";
import { fileCandidates, fileMenu } from "../file-completion";

// Composer (DESIGN_PLAN D11, I5): a card on the panel surface, pill selectors, the context meter,
// the Working row on the line below it while busy, the completion layer floating over the timeline from its top edge and the project strip below.
declare const navigator: any;
const focused = signal(false);
const pendingSelection = signal<{ session: string; id: string } | null>(null);
const selected = signal(0);
/** The completion token the user closed with Esc; it reopens when the token changes. */
const dismissed = signal<string | null>(null);
const atListing = signal<{ scope: string; cache: { prefix: string; paths: string[] }[]; pool: string[] | null }>({ scope: "", cache: [], pool: null });
const atAsking = new Set<string>();
const MENU_LIMIT = 8;

type CompletionRow = { key: string; value: string; help: string; hint?: string; muted?: string; accept: string; file?: { name: string; dir: string; folder: boolean } };
type Completion = { kind: "slash" | "file"; query: string; dir: string; prefix: string; rows: CompletionRow[] };

/** `/` completes a first-word command; `@` completes the token at the caret. */
function completionFor(text: string, prefix: string): Completion | null {
  if (prefix === text && /^\/\S*$/.test(text)) {
    const query = text.slice(1).toLowerCase();
    const rows = commands
      .filter((c: Cmd) => !!c.slash && c.slash.toLowerCase().startsWith(query))
      .map((c: Cmd): CompletionRow => {
        const a = availability(c);
        return { key: c.id, value: `/${c.slash}`, help: c.title, hint: keyLabel(c) ?? undefined, muted: a.ok ? undefined : a.reason, accept: `/${c.slash} ` };
      });
    return { kind: "slash", query, dir: "", prefix, rows };
  }
  const at = !text.startsWith("!") && prefix.match(/(?:^|\s)@([^\s]*)$/);
  if (at) {
    const query = at[1];
    const slash = query.lastIndexOf("/");
    const dir = slash < 0 ? "" : query.slice(0, slash + 1);
    const scope = JSON.stringify([currentProject(), viewed()?.id ?? null]);
    const listing = atListing();
    const hits = listing.scope === scope ? fileCandidates(listing.cache, prefix) : null;
    const paths = hits ? fileMenu(query, hits, listing.pool) : [];
    const rows = paths
      .map((path): CompletionRow => {
        const isDir = path.endsWith("/");
        const trimmed = isDir ? path.slice(0, -1) : path;
        const split = trimmed.lastIndexOf("/");
        const name = trimmed.slice(split + 1) + (isDir ? "/" : "");
        const parent = split < 0 ? "" : trimmed.slice(0, split + 1);
        return { key: path, value: `@${path}`, help: isDir ? "directory" : "file", file: { name, dir: parent, folder: isDir }, accept: `@${path}${isDir ? "" : " "}` };
      });
    return { kind: "file", query, dir, prefix, rows };
  }
  return null;
}

export function Composer() {
  return <If when={viewed()?.kind !== "manager"} else={<View class="px-5 py-4"><Text class={T.meta}>Manager session · read-only debug view</Text></View>}>{() => <EditableComposer />}</If>;
}
function EditableComposer() {
  let ref: any;
  focusComposer.fn = () => { try { ref?.focus(); } catch {} };
  blurComposer.fn = () => { try { ref?.blur(); } catch {} };
  const h = signal(phone ? 28 : 22);
  // The dock's own width, measured rather than taken from the window: the provider label leaves
  // when the controls would overprint, which the window width alone cannot say.
  const dockWidth = signal(0);
  const key = () => draftKey();
  const text = () => draft()[key()] ?? "";
  const images = () => imageDrafts()[key()] ?? [];
  const pastes = () => (pasteDrafts()[key()] ?? []).filter((p) => text().includes(p.marker));
  const pasting = signal(false);
  const appendImages = (target: string, added: import("../images").DraftImage[]) => {
    const all = [...(imageDrafts()[target] ?? []), ...added];
    const bytes = all.reduce((n, i) => n + (i.data_url.length - i.data_url.indexOf(",") - 1) * 3 / 4, 0);
    if (all.length > 8 || bytes > 8 * 1024 * 1024) throw new Error("At most eight images and 8 MiB per message");
    // TUI parity: each attachment is `Image A`, `Image B`, … so its `[Image A]` marker means one image.
    const taken = new Set(all.map((i) => i.name));
    for (const image of added) {
      const letter = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").find((l) => !taken.has(`Image ${l}`));
      if (letter) { image.name = `Image ${letter}`; taken.add(image.name); }
    }
    imageDrafts.update((d) => ({ ...d, [target]: all }));
  };
  const pasteImages = async (files: File[]) => {
    if (!files.length || reason() || pasting()) return;
    const target = key();
    if ((imageDrafts()[target]?.length ?? 0) + files.length > 8) { toast("error", "At most eight images per message"); return; }
    pasting.set(true);
    try {
      const added = await Promise.all(files.map(readImage));
      appendImages(target, added);
      // A session switch during the file read keeps the attachment on its own draft: markers go at
      // the caret only for the draft still on screen, else at the end of the captured one.
      if (key() === target) {
        for (const image of added) insertAtCaret(`[${image.name}]`);
      } else {
        const markers = added.map((image) => `[${image.name}]`).join("");
        draft.update((d) => ({ ...d, [target]: (d[target] ?? "") + markers }));
      }
    } catch (e) { toast("error", String(e)); }
    finally { pasting.set(false); }
  };
  /**
   * Remove an attachment, then rename the rest in order (`Image A`, `Image B`, …) and rewrite their
   * markers: Core labels images by attachment order, so a gap would make the draft's letters lie.
   */
  const removeImage = (image: import("../images").DraftImage) => {
    const target = key();
    const kept = (imageDrafts()[target] ?? []).filter((i) => i.id !== image.id);
    const renamed = kept.map((i, n) => ({ ...i, name: `Image ${String.fromCharCode(65 + n)}` }));
    imageDrafts.update((d) => ({ ...d, [target]: renamed }));
    const moved = new Map<string, string>();
    kept.forEach((i, n) => {
      const from = i.name.replace(/^Image /, ""), to = renamed[n].name.replace(/^Image /, "");
      if (from !== to) moved.set(from, to);
    });
    // Drop the removed attachment's own marker before the surviving ones move up.
    const before = draft()[target] ?? "";
    const t = before.split(`[${image.name}]`).join("");
    const next = moved.size
      ? t.replace(/\[Image ([A-Z])\]/g, (m, letter: string) => moved.has(letter) ? `[Image ${moved.get(letter)!}]` : m)
      : t;
    if (next !== before) {
      draft.update((d) => ({ ...d, [target]: next }));
      prunePastes(target, next);
    }
  };
  const ses = () => viewed();
  const busy = () => { m(); return ses()?.status === "busy"; };
  // While this session waits on the user, the waiting card owns the row: `Working · 2m 01s` above it
  // would run into `waiting on you`.
  const working = () => { m(); return busy() && waitItems().length === 0; };
  /** A question (not an approval) is the card in view: the composer is locked. Shared with prompt sends. */
  const questionPending = () => { m(); return questionLocked(); };
  const reason = (): string | null => {
    const c = connection();
    if (c.kind !== "live") return c.kind === "reconnecting" ? "Reconnecting to Core: read-only" : c.kind === "offline" ? "Core is offline: read-only" : c.kind === "auth_failed" ? "Authentication failed: read-only" : c.kind === "incompatible" ? "Incompatible API: blocked" : "Connecting to Core…";
    // A question owns the answer: keep the draft, but no typing, send, steer or queue.
    if (questionPending()) return "Answer the question above";
    if (!currentProject() && centre() === "new_session") return "Add or choose a project first";
    return null;
  };
  const hasText = computed(() => text().trim().length > 0);
  const canSend = () => dictationPhase() === "idle" && !reason() && !pasting() && (hasText() || images().length > 0);
  const canDictate = () => live() && !ses()?.archived && !questionPending();
  const send = async (mode: "send" | "steer" | "queue" | "interrupt" = busy() ? "steer" : "send", resumed = false) => {
    const t = text().trim();
    // The question lock wins over a slash still in the draft; offline navigation stays available otherwise.
    if (t && !images().length && !questionPending() && runSlashCommand(t)) {
      draft.update((d) => ({ ...d, [key()]: "" }));
      prunePastes(key(), "");
      return;
    }
    if (!canSend()) { const r = reason(); if (r) toast("info", r); return; }
    if (ses()?.archived && !resumed) {
      resumeArchived(ses()!.id, () => send(mode, true));
      return;
    }
    const target = key();
    const sent = images();
    const ok = await sendPrompt(t, sent, mode);
    if (ok && ses()) void loadInputs(ses()!.id).catch(() => {});
    if (ok) { const p = currentProject(); if (p) void loadPromptHistory(p).catch(() => {}); }
    if (ok) {
      draft.update((d) => ({ ...d, [target]: (d[target] ?? "").trim() === t ? "" : d[target] ?? "" }));
      imageDrafts.update((d) => ({ ...d, [target]: (d[target] ?? []).filter((i) => !sent.includes(i)) }));
      prunePastes(target, draft()[target] ?? "");
    }
  };
  const interrupted = () => term("session.interrupted");
  const agent = () => { m(); return ses()?.agent ?? sessionDraft().agent ?? "build"; };
  const modelRef = () => {
    m();
    const s = ses();
    const chosen = !s ? sessionDraft().model : undefined;
    if (s) return s.model ? String(s.model) : "";
    if (chosen) return chosen;
    const entry = agentCatalog().agents.find((a) => a.name === agent());
    return entry?.model || rememberedModel(currentProject(), agent());
  };
  const modelParts = () => {
    const ref = modelRef();
    if (!ref) return { name: "default", provider: "" };
    const entry = modelCatalog().find((item) => item.ref === ref);
    if (!entry) return { name: ref.split("/").at(-1) ?? ref, provider: ref.includes("/") ? ref.split("/")[0] : "" };
    return { name: entry.name || ref.split("/").at(-1) || ref, provider: entry.provider || ref.split("/")[0] };
  };
  const effort = () => {
    const s: any = ses();
    const project = currentProject(), answer = draftContext();
    const planned = project && answer?.key === draftContextKey(project) ? answer.data.session?.effort : null;
    return s ? (s.effort || s.extra?.effort || "default") : ((sessionDraft().effort ?? planned) || "default");
  };
  /** The latest request's input occupancy, as a count and a share of the model's window. */
  const usage = (): { input: number; pct: number | null } | null => {
    m();
    const id = ses()?.id;
    const project = currentProject();
    const budget = id ? contextBudget()[id] : project ? draftPlanContext(project) : null;
    if (!budget || !Number.isFinite(budget.used)) return null;
    return { input: budget.used, pct: budget.window && budget.window > 0 ? 100 * budget.used / budget.window : null };

  };
  const usageLabel = () => {
    const u = usage();
    if (!u) return "";
    const id = ses()?.id;
    const prefix = !id || contextBudget()[id]?.exact === false ? "~" : "";
    if (u.pct === null) return prefix + compact(u.input);
    return `${prefix}${compact(u.input)} · ${u.pct >= 10 ? Math.round(u.pct) : u.pct.toFixed(1)}%`;
  };
  const meterPct = () => Math.min(100, usage()?.pct ?? 0);

  // Completion: the rows for the draft, the keys while it is open, and the file listing for `@`.
  // Memoised so a keystroke that leaves completion closed (the usual one) wakes nothing that depends on it.
  const caret = signal({ start: 0, end: 0 });
  const caretAt = () => { const t = text(); const c = caret(); return c.start === c.end ? Math.min(c.end, t.length) : -1; };
  const completion = computed(() => {
    const at = caretAt();
    return at < 0 ? null : completionFor(text(), text().slice(0, at));
  });
  const open = computed(() => {
    const c = completion();
    return !!c && c.rows.length > 0 && dismissed() !== (c.kind === "file" ? `@${c.query}` : c.prefix);
  });
  let lastCompletionToken = "";
  effect(() => {
    const c = completion();
    const token = c ? (c.kind === "file" ? `@${c.query}` : c.prefix) : "";
    if (token !== lastCompletionToken) { lastCompletionToken = token; dismissed.set(null); selected.set(0); }
  });
  const accept = (row: CompletionRow) => {
    const c = completion();
    if (!c) return;
    const t = text();
    const start = c.prefix.length - (c.query.length + 1);
    const next = c.kind === "file" ? t.slice(0, start) + row.accept + t.slice(c.prefix.length) : row.accept;
    const position = c.kind === "file" ? start + row.accept.length : row.accept.length;
    draft.update((d) => ({ ...d, [key()]: next }));
    caret.set({ start: position, end: position });
    try { ref?.setSelection(position, position); } catch { /* the platform keeps the caret */ }
    selected.set(0);
  };
  effect(() => {
    if (!open()) return;
    return bindOpenKeys({
      down: () => { const n = completion()?.rows.length ?? 0; selected.set(Math.min(Math.max(0, n - 1), selected() + 1)); },
      up: () => selected.set(Math.max(0, selected() - 1)),
      tab: () => { const c = completion(); const row = c?.rows[Math.min(selected(), (c?.rows.length ?? 1) - 1)]; if (row) accept(row); },
    });
  });
  // Prompt history: Up on the composer's first line recalls older prompts, Down on its last line
  // returns toward the draft. Elsewhere the arrows belong to the input and move the caret between
  // lines, so a recalled multi-line prompt is walked line by line before the one before it.
  const recall = signal<{ at: number; scratch: string } | null>(null);
  const recalled = computed(() => { const r = recall(); const e = promptHistory().entries; return r && e[r.at] === text() ? r : null; });
  // The caret as the input reports it (UTF-16 units, clamped to the draft a replacement shortened);
  // a selection takes no arrow.
  const onFirstLine = computed(() => { const at = caretAt(); return at >= 0 && !text().slice(0, at).includes("\n"); });
  const onLastLine = computed(() => { const at = caretAt(); return at >= 0 && !text().slice(at).includes("\n"); });
  const draftEmpty = computed(() => !(draft()[draftKey()] ?? "").length);
  // The input reports a paste instead of inserting it: small text goes in at the caret, and a
  // big paste becomes one atomic marker, as it does in the TUI.
  const insertAtCaret = (insert: string) => {
    const value = text();
    const c = caret();
    const start = Math.min(c.start, value.length);
    const end = Math.max(start, Math.min(c.end, value.length));
    draft.update((d) => ({ ...d, [key()]: value.slice(0, start) + insert + value.slice(end) }));
    caret.set({ start: start + insert.length, end: start + insert.length });
    try { ref?.setSelection(start + insert.length, start + insert.length); } catch { /* the platform keeps the caret */ }
  };
  const pasteText = (raw: string) => {
    if (!raw || reason()) return;
    const clean = normalisePaste(raw);
    if (!isBigPaste(clean)) { insertAtCaret(clean); return; }
    const target = key();
    const entry = makePaste(clean, pasteDrafts()[target] ?? [], text());
    pasteDrafts.update((d) => ({ ...d, [target]: [...(d[target] ?? []), entry] }));
    insertAtCaret(entry.marker);
  };
  // Ctrl+V (and the palette's "Paste"): image if the clipboard has one, else text, else a toast.
  pasteClipboard.fn = async () => {
    if (reason()) return;
    const empty = () => toast("info", "The clipboard is empty");
    try {
      const items = (await navigator.clipboard.read?.()) ?? [];
      for (const item of items) {
        const type = item.types.find((t: string) => t.startsWith("image/"));
        if (type) { const blob = await item.getType(type); void pasteImages([new File([blob], "clipboard.png", { type })]); return; }
      }
      for (const item of items) {
        if (item.types.includes("text/plain")) { const t = await (await item.getType("text/plain")).text(); if (t) { pasteText(t); return; } }
      }
      if (items.length) { empty(); return; }
    } catch { /* read() may be refused or missing; readText is the fallback */ }
    try {
      const t = await navigator.clipboard.readText();
      if (t) pasteText(t); else empty();
    } catch { toast("error", "Could not read the clipboard; use the platform paste shortcut"); }
  };
  // `viewed()` changes on every model bump; reload only when the project or the viewed session changes.
  let historyKey = "";
  effect(() => {
    const project = currentProject();
    const key = `${project ?? ""}|${viewed()?.id ?? ""}`;
    if (key === historyKey) return;
    historyKey = key;
    if (project) void loadPromptHistory(project).catch(() => { historyKey = ""; });
  });
  effect(() => {
    if (!focused() || open() || pendingSelection() || (draftEmpty() && (pendingInputs()[viewed()?.id ?? ""]?.inputs.length ?? 0) > 0)) return;
    const entries = promptHistory().entries;
    if (!entries.length) return;
    // A recalled prompt lands with its caret at the end: Up then walks its lines before the next one.
    const set = (v: string) => {
      draft.update((d) => ({ ...d, [key()]: v }));
      caret.set({ start: v.length, end: v.length });
      try { ref?.setSelection(v.length, v.length); } catch { /* the platform keeps the caret */ }
    };
    const keys: Record<string, () => void> = {};
    if (onFirstLine()) keys.up = () => {
      const r = recalled();
      const at = r ? Math.min(entries.length - 1, r.at + 1) : 0;
      recall.set({ at, scratch: r ? r.scratch : text() });
      set(entries[at]);
    };
    if (recalled() && onLastLine()) keys.down = () => {
      const r = recalled();
      if (!r) return;
      if (r.at === 0) { recall.set(null); set(r.scratch); return; }
      recall.set({ at: r.at - 1, scratch: r.scratch });
      set(entries[r.at - 1]);
    };
    return bindOpenKeys(keys);
  });
  effect(() => {
    if (!focused() || !busy() || !canSend() || open()) return;
    // Alt+Enter (Option+Enter on macOS): cancel the running turn and run this message first (plan 25).
    return bindOpenKeys({
      tab: () => { void send("queue"); },
      [toKeystroke(byId.get("composer.interrupt_send")?.keys?.nsh ?? "alt+enter")]: () => { void send("interrupt"); },
    });
  });
  // Shift+Tab cycles the composer's eligible agents, wrapping. A picker, modal, inline edit,
  // completion list or focused terminal owns the keys instead; an existing session still asks
  // Core first, so a mid-turn switch keeps the picker's cache warning.
  effect(() => {
    if (!focused() || open() || terminalFocused() || term("overlay.open") || term("dialog.open") || term("text_edit.active")) return;
    return bindOpenKeys({ "shift+tab": () => { void run("agent.cycle"); } });
  });
  effect(() => {
    const c = completion();
    const project = currentProject();
    if (!c || c.kind !== "file" || !project) return;
    const session = viewed()?.id;
    const scope = JSON.stringify([project, session ?? null]);
    if (untracked(() => atListing().scope) !== scope) atListing.set({ scope, cache: [], pool: null });
    const ask = (query: string, limit: number, prefix: string | null) => {
      const k = JSON.stringify([scope, prefix, limit]);
      if (atAsking.has(k)) return;
      atAsking.add(k);
      void request<{ paths: string[] }>("GET", `/api/project/${project}/files/search`, { query: { q: query, limit, directories: "1", session } })
        .then((data) => {
          const state = untracked(() => atListing());
          if (state.scope !== scope) return;
          if (prefix === null) atListing.set({ ...state, pool: data.paths ?? [] });
          else if (untracked(() => completion()?.prefix) === prefix) {
            atListing.set({ ...state, cache: [...state.cache.filter((entry) => entry.prefix !== prefix), { prefix, paths: data.paths ?? [] }].slice(-64) });
          }
        }).catch(() => {}).finally(() => { atAsking.delete(k); });
    };
    const state = untracked(() => atListing());
    if (!state.pool) ask("", 100, null);
    if (!state.cache.some((entry) => entry.prefix === c.prefix)) ask(c.query, 30, c.prefix);
  });

  const agentName = () => capitalize(agent());
  /** Dictation and send/stop. Desktop puts them at the end of the control row (30px, round); the phone keeps them beside the input. */
  const actions = () => (
    <View class={`shrink-0 flex-row items-center ${phone ? "gap-3 pb-1" : "gap-1 ml-2"}`}>
      <If when={dictationPhase() === "recording" || dictationPhase() === "stopping"}>
        <IconButton icon="stop" label={dictationPhase() === "recording" ? "Stop and insert transcript" : "Finishing transcription"}
          testID="dictation-stop" disabled={dictationPhase() !== "recording"} onPress={() => void run("voice.dictate")}
          class="bg-primary hover:bg-primary" tint="tint-on-accent" />
        <Btn label="Cancel" kind="ghost" onPress={cancelDictation} testID="dictation-cancel" />
      </If>
      <If when={dictationPhase() === "idle"}>
        <RoundButton icon="mic" label="Dictate into composer" testID="dictation-start" disabled={!canDictate()}
          onPress={() => void run("voice.dictate")} reason={!canDictate() ? (questionPending() ? "Answer the question above" : ses()?.archived ? "Unarchive this session first" : "Core is not connected") : undefined} />
      </If>
      <If when={dictationPhase() !== "idle" && dictationPhase() !== "recording" && dictationPhase() !== "stopping"}>
        <Btn label="Cancel dictation" kind="ghost" onPress={cancelDictation} testID="dictation-cancel-pending" />
      </If>
      <If when={!phone && busy() && !canSend() && dictationPhase() === "idle"}
        else={<RoundButton icon="arrow-up" label="Send message" testID="send" disabled={!canSend()} filled={canSend()} onPress={() => void send()} onLongPress={() => { if (phone && busy() && canSend()) sendChoice.set({ choose: (mode) => void send(mode) }); }} reason={!canSend() ? (reason() ?? "Type a message first") : undefined} />}>
        <RoundButton icon="stop" label="Interrupt" testID="stop" filled square onPress={() => void run("session.interrupt")} />
      </If>
    </View>
  );
  return (
    <View class="w-full" testID="composer-area">
    <View class="w-full bg-background" style={{ paddingBottom: phone && keyboardFrame().height > 0 ? 0 : safeArea().bottom }} testID="composer-dock"
      onLayout={(b: any) => dockHeight.set(b.bounds?.size?.height ?? b.size?.height ?? 0)}>
    <TimelineColumn class="items-center">
        <View
          class="w-full pt-3 pb-1"
          accessibilityLabel="Composer" testID="composer-panel"
          onLayout={(b: any) => dockWidth.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}
        >
          <PendingStack />
          <If when={pastes().length > 0}>
            <View class="gap-1.5 pb-3" testID="paste-previews">
              <Each of={pastes()} key={(p) => p.id}>
                {(p) => (
                  <Button onPress={() => contextDetail.set({ title: "Pasted text", detail: p().text })}
                    accessibilityLabel={`Show pasted text, ${p().lines} lines`} testID={`composer-paste-${p().id}`}
                    class={`flex-row items-center gap-2.5 rounded-(--radius) bg-raised px-2.5 ${isNarrow() ? "min-h-11" : "min-h-9"} hover:bg-accent ${FOCUS}`}>
                    <Text class={`shrink-0 ${T.body}`}>Pasted text</Text>
                    <View class="flex-1 min-w-0">
                      <Text style={{ numberOfLines: 1 }} class={T.meta}>{`${p().lines} lines · ~${p().tokens} tokens`}</Text>
                      <Text style={{ numberOfLines: 1 }} class={T.sec}>
                        {(p().text.split("\n").find((line) => line.trim()) ?? "").trimStart()}
                      </Text>
                    </View>
                    <Text class={`shrink-0 ${T.body} font-medium`}>Show</Text>
                  </Button>
                )}
              </Each>
            </View>
          </If>
          <If when={images().length > 0}>
            <View class="flex-row flex-wrap gap-2 pb-3" testID="image-previews">
              <Each of={images()} key={(image) => image.id}>
                {(image) => <View class="gap-1 w-20 shrink-0">
                  <View class="relative w-20 h-20">
                    <Button onPress={() => annotating.set({ key: key(), id: image().id })}
                      accessibilityLabel={`Mark up ${image().name}`} testID={`composer-image-${image().id}`}
                      class={`w-20 h-20 items-center justify-center border border-border-strong overflow-hidden ${FOCUS}`}>
                      <Image class="w-20 h-20" src={image().data_url} style={{ width: 80, height: 80, objectFit: "cover" }} accessibilityLabel={image().name} />
                    </Button>
                    <Button onPress={() => removeImage(image())}
                      accessibilityLabel="Remove image" testID={`remove-image-${image().id}`}
                      class={`absolute top-1 right-1 items-center justify-center rounded-full bg-popover hover:bg-accent ${isNarrow() ? "size-8" : "size-6"} ${FOCUS}`}>
                      <Icon name="x" size={12} />
                    </Button>
                  </View>
                  <Text style={{ numberOfLines: 1 }} class={T.meta}>{image().name}</Text>
                </View>}
              </Each>
            </View>
          </If>
          <If when={pasting()}><Text class={T.meta}>Preparing image…</Text></If>
          <DictationStatus />
          <If when={open()}>
            {() => {
              const c = () => completion()!;
              const total = () => c().rows.length;
              const at = () => Math.min(selected(), total() - 1);
              // Eight rows show; beyond that the window follows the selection (the TUI scrolls the same way).
              const first = () => Math.max(0, Math.min(at() - MENU_LIMIT + 1, total() - MENU_LIMIT));
              const rows = () => c().rows.slice(first(), first() + MENU_LIMIT);
              const files = () => c().kind === "file";
              // A layer over the timeline: its bottom sits 8px above the card, so opening it moves nothing.
              return (
                <View class="relative w-full h-0 z-50" testID="completion-anchor">
                  <View class="absolute left-0 bottom-2 bg-popover rounded-xl p-1.5 border border-border overflow-hidden z-50" style={{ width: "100%", boxShadow: "0 12px 40px rgb(0 0 0 / 0.45)" }}
                    testID="completion" accessibilityLabel="Completion">
                    <If when={files()}>
                      <View class="flex-row items-center gap-2 px-2.5 pt-1.5 pb-2" testID="completion-header">
                        <Text class={`${T.meta} font-medium`}>Files</Text>
                        <Text style={{ numberOfLines: 1 }} class={`font-mono text-[12px] leading-4 text-foreground-secondary min-w-0 shrink`}>{c().dir || "./"}</Text>
                        <View class="flex-1" />
                        <Text class={T.meta}>{`${c().rows.length} ${c().rows.length === 1 ? "match" : "matches"}`}</Text>
                      </View>
                    </If>
                    <Each of={rows()} key={(r) => r.key}>
                      {(r, i) => {
                        const sel = () => first() + i() === at();
                        return (
                          <Button onPress={() => accept(r())} selected={sel()} testID={`completion-${r().key}`}
                            class={`relative flex-row items-center gap-3 rounded-lg ${sel() ? "bg-selection" : ""} ${isNarrow() ? "min-h-11" : "h-[34px]"} px-2.5 hover:bg-accent ${FOCUS}`}>
                            <If when={sel()}><View class="absolute left-0 top-[7px] w-[3px] h-5 rounded-sm bg-primary" testID="completion-bar" /></If>
                            <If when={!!r().file} else={<>
                              <Text style={{ numberOfLines: 1, ...UI_FONT }} class={`w-24 shrink-0 text-[14px] leading-5 font-medium ${r().muted ? "text-muted-foreground" : "text-foreground"}`}>{r().value}</Text>
                              <Text style={{ numberOfLines: 1, ...UI_FONT }} class="flex-1 min-w-0 text-[14px] leading-5 text-muted-foreground">{r().muted ? `${r().help} · ${r().muted}` : r().help}</Text>
                              <If when={!!r().hint}><Text style={UI_FONT} class="text-[12px] leading-4 text-muted-foreground">{r().hint ?? ""}</Text></If>
                            </>}>
                              <Icon name={r().file!.folder ? "files" : "read"} size={14} class={sel() ? "tint-foreground-secondary" : "tint-muted-foreground"} />
                              <Text style={{ numberOfLines: 1, ...UI_FONT }} class={`shrink-0 text-[14px] leading-5 text-foreground ${sel() ? "font-medium" : ""}`}>{r().file!.name}</Text>
                              <Text style={{ numberOfLines: 1 }} class="font-mono text-[12px] leading-4 text-muted-foreground min-w-0 shrink">{r().file!.dir || "./"}</Text>
                              <View class="flex-1" />
                              <Text style={UI_FONT} class="text-[12px] leading-4 text-muted-foreground">{r().help}</Text>
                            </If>
                          </Button>
                        );
                      }}
                    </Each>
                    <View class="flex-row items-center gap-3.5 h-[30px] pt-1 px-2.5 mt-1 border-t border-border">
                      <Text class={T.meta}><Text class="text-foreground-secondary font-medium">↑↓</Text> move</Text>
                      <Text class={T.meta}><Text class="text-foreground-secondary font-medium">{files() ? "tab/enter" : "tab"}</Text> accept</Text>
                      <Text class={T.meta}><Text class="text-foreground-secondary font-medium">esc</Text> close</Text>
                      <View class="flex-1" />
                      <Text class={T.meta}>{`${at() + 1} of ${c().rows.length}`}</Text>
                    </View>
                  </View>
                </View>
              );
            }}
          </If>
          <View class={`w-full bg-card ${phone ? "rounded-3xl px-4 pb-2" : "rounded-[14px] px-5 pb-2"} border ${open() ? "border-border" : "border-transparent"}`} testID="composer-surface">
          <RetryNotices />
          <View class={phone ? "h-2" : "h-5"} testID="composer-top-padding" />
          <View class="w-full flex-row items-end gap-3" testID="composer-input-row">
          <Input
            onPaste={(e) => {
              const files = (e.files ?? []).filter((f) => f.type.startsWith("image/"));
              if (files.length) { void pasteImages(files); return; }
              pasteText(e.text ?? "");
            }}
            pasteBehavior="report"
            ref={(n: any) => (ref = n)} multiline value={text()} testID="composer-input" accessibilityLabel="Message"
            placeholder={reason() ?? (centre() === "new_session" || !ses() ? "Message to start a new session" : phone ? (busy() ? "Steer or queue a message…" : "Message") : busy() ? "Type a message… Enter steer · Tab queue · Alt+Enter interrupt" : "Type a message…")}
            readonly={!live() || questionPending()}
            submitBehavior="submit" onSubmit={() => {
              if (dictationPhase() === "recording") { void stopDictation(); return; }
              if (dictationPhase() !== "idle") return;
              // Enter belongs to the question card while it locks the composer.
              if (questionPending()) return;
              // A standalone `/command` runs the highlighted one, as if typed and sent (the TUI does the same).
              const c = open() ? completion() : null;
              const t = text();
              if (c?.kind === "file") {
                const row = c.rows[Math.min(selected(), c.rows.length - 1)];
                if (row) accept(row);
                return;
              }
              if (c && c.kind === "slash" && !/\s/.test(t)) {
                const row = c.rows[Math.min(selected(), c.rows.length - 1)];
                if (row && runSlashCommand(row.value)) {
                  draft.update((d) => ({ ...d, [key()]: "" }));
                  prunePastes(key(), "");
                  selected.set(0);
                  return;
                }
              }
              void send();
            }}
            onChange={({ value: t }) => { if (questionPending()) return; selected.set(0); draft.update((d) => ({ ...d, [key()]: t })); prunePastes(key(), t); }}
            onFocus={() => { focused.set(true); composerFocused.set(true); flowStage.set(null); flowProject.set(null); }} onBlur={() => { focused.set(false); composerFocused.set(false); }}
            onSelectionChange={({ start, end }) => caret.set({ start, end })}
            onKeyPress={(e: { key: string }) => {
              if (String(e.key).toLowerCase() !== "escape") return;
              if (dictationPhase() !== "idle") { cancelDictation(); return; }
              const c = completion();
              if (open() && c) dismissed.set(c.kind === "file" ? `@${c.query}` : c.prefix);
            }}
            onContentSizeChange={(s) => h.set(Math.min(220, Math.max(phone ? 28 : 22, Math.ceil(s.size?.height ?? (s as any).height ?? 22) + (phone ? 4 : 0))))}
            style={{ height: h(), ...UI_FONT }}
            class={`flex-1 min-w-0 px-0 py-0 ${phone ? T.body : T.body.replace("text-[14px]", "text-[16px]").replace("leading-6", "leading-[22px]")} placeholder:text-muted-foreground`}
          />
          <If when={phone}>{actions()}</If>
          </View>
          <View class={phone ? "h-2" : "h-2.5"} />
          <View class={`flex-row items-center gap-1 min-w-0 ${phone ? "min-h-8" : "min-h-[30px]"}`}>
            <View class="flex-1 min-w-0 flex-row items-center gap-1">
              <Selector command="agent.change" label={agentName()} labelClass={agentColor(agent())} testID="composer-agent" alignStart />
              <Text class={T.meta}>·</Text>
              <Selector command="model.change" label={modelParts().name} sub={dockWidth() < 560 || !modelParts().provider ? undefined : modelParts().provider} testID="composer-model" />
              <If when={!phone}>
                <Text class={T.meta}>·</Text>
                <Selector command="model.effort" label={effort()} testID="composer-effort" fixed />
              </If>
              <If when={!busy() && interrupted()}>
                <Btn label="Continue" icon="play" kind="ghost" onPress={() => run("session.continue")} disabled={!availability(byId.get("session.continue")!).ok} reason={availability(byId.get("session.continue")!).reason} testID="continue" />
              </If>
            </View>
            <If when={!!usageLabel() && !phone}><View class="shrink-0 flex-row items-center gap-1" testID="composer-meter"><Text style={{ numberOfLines: 1 }} class={T.meta} testID="composer-usage">{usageLabel()}</Text></View></If>
            <If when={!phone}>{actions()}</If>
          </View>
          </View>
        </View>
        <LocationStrip working={working()} />
        <ActivityBar busy={busy()} fraction={usage()?.pct == null ? 0 : meterPct() / 100} slot={agentSlot(agent())} />
    </TimelineColumn>
    </View>
    </View>
  );
}

/** Live transcript previews stay visibly pending; only a final result is inserted into the draft. */
function DictationStatus() {
  const phase = () => dictationPhase();
  const progress = () => voiceDownloadProgress();
  const downloading = () => phase() === "downloading" || progress()?.state === "downloading";
  const reducedMotion = () => !!config()?.values?.ui?.reduced_motion;
  const percent = () => {
    const p = progress();
    if (reducedMotion()) return 32;
    if (!p?.bytes_total) return 14;
    return Math.max(4, Math.min(100, ((p.bytes_done ?? 0) / p.bytes_total) * 100));
  };
  const level = () => reducedMotion() ? 32 : Math.max(5, Math.min(100, dictationLevel() * 100));
  const visible = () => phase() !== "idle" || downloading();
  return (
    <If when={visible()}>
      <View class="gap-2 pb-3" testID="dictation-status" accessibilityLabel="Dictation status">
        <If when={downloading()}>
          <View class="gap-1.5 rounded-(--radius) bg-muted px-3 py-2" testID="dictation-download-progress">
            <View class="flex-row items-center gap-2">
              <Status kind="running" />
              <Text class="flex-1 text-[16px] leading-6 text-foreground">Downloading speech recognition model…</Text>
              <If when={!!progress()?.bytes_total}>
                <Text class={T.meta}>{`${formatBytes(progress()?.bytes_done ?? 0)} / ${formatBytes(progress()!.bytes_total!)}`}</Text>
              </If>
            </View>
            <View class="h-1 overflow-hidden rounded-full bg-border">
              <View class="h-full rounded-full bg-primary" style={{ width: `${percent()}%` }} />
            </View>
          </View>
        </If>
        <If when={phase() === "checking"}><Text class={T.meta}>Checking speech recognition availability…</Text></If>
        <If when={phase() === "requesting"}><Text class={T.meta}>Requesting microphone access…</Text></If>
        <If when={phase() === "consent"}><Text class={T.meta}>Speech model download consent is required.</Text></If>
        <If when={phase() === "recording" || phase() === "stopping"}>
          <View class="flex-row items-center gap-2" testID="dictation-recording-indicator" accessibilityLabel={reducedMotion() ? "Recording audio" : "Recording audio; level meter"}>
            <View class="size-2 rounded-full bg-error" />
            <Text class="text-[16px] leading-6 font-medium text-foreground">{phase() === "stopping" ? "Finalizing transcript…" : "Listening…"}</Text>
            <View class="h-1.5 w-20 overflow-hidden rounded-full bg-border" testID="dictation-level-meter">
              <View class="h-full rounded-full bg-primary" style={{ width: `${level()}%` }} />
            </View>
            <Text class={T.meta}>{reducedMotion() ? "Reduced motion" : "Mic level"}</Text>
          </View>
          <If when={!!dictationPreview().trim()}>
            <Text testID="dictation-preview" style={{ userSelect: "text" }} class="rounded-(--radius) bg-muted px-3 py-2 text-[16px] leading-6 text-foreground-secondary italic">
              {`Pending transcript · ${dictationPreview()}`}
            </Text>
          </If>
          <Text class={T.meta}>Enter or Stop inserts editable text · Esc cancels</Text>
        </If>
      </View>
    </If>
  );
}

/** Compact counts for the context meter and working row. */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}K`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}
function capitalize(s: string): string { return s ? `${s[0].toUpperCase()}${s.slice(1)}` : "Build"; }

/** The composer's 30px round control: mic (bare), send (disabled: dim fill, enabled: foreground fill) and stop (filled square). */
function RoundButton(p: { icon: "mic" | "arrow-up" | "stop"; label: string; onPress?: () => void; onLongPress?: () => void; disabled?: boolean; filled?: boolean; square?: boolean; testID?: string; reason?: string }) {
  const size = isNarrow() ? "size-11" : "size-[30px]";
  const skin = p.filled ? "bg-foreground hover:bg-foreground" : p.icon === "arrow-up" ? "bg-raised" : "hover:bg-accent";
  const tint = p.filled ? "tint-background" : p.icon === "arrow-up" ? "tint-muted-foreground" : "tint-foreground-secondary";
  const face = p.square ? <View class="size-2.5 rounded-sm bg-background" /> : <Icon name={p.icon} size={p.icon === "mic" ? 16 : 15} class={tint} />;
  if (p.onLongPress) {
    return (
      <View role="button" focusable onPress={() => { if (!p.disabled) p.onPress?.(); }} onLongPress={() => { if (!p.disabled) p.onLongPress?.(); }}
        accessibilityLabel={p.label} testID={p.testID} class={`items-center justify-center rounded-full ${size} ${skin} active:bg-selection ${FOCUS}`}>{face}</View>
    );
  }
  return (
    <Button onPress={p.onPress} disabled={p.disabled} accessibilityLabel={p.label} testID={p.testID}
      tooltip={<Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{p.reason ? `${p.label}: ${p.reason}` : p.label}</Text>}
      class={`items-center justify-center rounded-full ${size} ${skin} ${p.icon === "mic" && p.disabled ? "opacity-50" : ""} ${FOCUS}`}>{face}</Button>
  );
}

/** A selector on the control row, with an optional muted sub-label. */
function Selector(p: { command: string; label: string; labelClass?: string; sub?: string; testID: string; alignStart?: boolean; fixed?: boolean }) {
  const c = byId.get(p.command);
  const state = () => c ? availability(c) : { ok: false, reason: "Command is unavailable." };
  const disabled = () => !state().ok;
  const reason = () => state().reason;
  return (
    <Button
      onPress={() => void run(p.command)} disabled={disabled()} testID={p.testID}
      accessibilityLabel={`${p.label}${disabled() ? `, unavailable: ${reason()}` : ""}`}
      tooltip={<Text class="text-xs text-popover-foreground bg-popover rounded-(--radius) px-2 py-1">{disabled() ? `${p.label}: ${reason()}` : p.label}</Text>}
      class={`flex-row items-center gap-1.5 ${p.fixed ? "shrink-0" : "min-w-0 shrink"} ${phone ? "min-h-11" : "h-7"} ${p.alignStart ? "pl-0 pr-2" : "px-2"} rounded-(--radius) hover:bg-accent disabled:opacity-50 ${FOCUS}`}
    >
      <Text style={{ numberOfLines: 1, ...UI_FONT, ...(phone ? {} : { fontSize: 14, lineHeight: 22 }) }} class={`min-w-0 shrink ${T.sec} ${p.labelClass ? `font-medium ${p.labelClass}` : ""}`}>{p.label}</Text>
      <If when={!!p.sub}><Text style={{ numberOfLines: 1, ...UI_FONT, ...(phone ? {} : { fontSize: 14, lineHeight: 22 }) }} class={`min-w-0 shrink-[2] ${T.sec}`}>{p.sub!}</Text></If>
    </Button>
  );
}

const AGENT_TEXT: Record<string, string> = {
  amber: "text-agent-amber", blue: "text-agent-blue", violet: "text-agent-violet", green: "text-agent-green",
  teal: "text-agent-teal", rose: "text-agent-rose", sky: "text-agent-sky", gold: "text-agent-gold",
};
const AGENT_TONE: Record<string, string> = {
  amber: "bg-agent-amber text-agent-amber", blue: "bg-agent-blue text-agent-blue", violet: "bg-agent-violet text-agent-violet",
  green: "bg-agent-green text-agent-green", teal: "bg-agent-teal text-agent-teal", rose: "bg-agent-rose text-agent-rose",
  sky: "bg-agent-sky text-agent-sky", gold: "bg-agent-gold text-agent-gold",
};
/**
 * The TUI's activity line (`draw_activity_line`) along the dock's foot, in the agent's colour with a slight
 * glow, here on the text edges rather than across the whole centre. While busy a soft pulse a fifth of the
 * width glides back and forth on a cosine, three seconds a sweep. At rest the line fills to the current
 * context occupancy, matching the TUI.
 */
function ActivityBar(p: { busy: boolean; fraction: number; slot: string }) {
  const phase = signal(0.5);
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  effect(() => {
    if (!p.busy || reduced()) { phase.set(0.5); return; }
    // Use an absolute clock so reactive effect restarts never jump the pulse back to its start.
    const id = setInterval(() => phase.set(activityPhase(Date.now())), 33);
    return () => clearInterval(id);
  });
  const left = () => activityOffsetPercent(phase());
  // This rule is the card's single bottom edge; the pulse is overlaid on that line.
  return <View class="w-full -mt-1" testID="composer-bottom-edge">
      <View class="relative w-full h-0.5 bg-border" testID="activity-bar" accessibilityLabel={p.busy ? "Working" : `Context ${Math.round(p.fraction * 100)}% used`}>
        <If when={p.busy}>
          <View class={`absolute top-0 h-0.5 ${AGENT_TEXT[p.slot] ?? ""}`} testID="activity-pulse" style={{ left: `${left()}%`, width: "20%", backgroundImage: "linear-gradient(90deg, transparent, currentColor, transparent)" }} />
        </If>
        <If when={!p.busy && p.fraction > 0}>
          <View class={`absolute top-0 h-0.5 left-0 ${AGENT_TONE[p.slot] ?? AGENT_TONE.blue}`} testID="activity-fill" style={{ width: `${Math.min(100, p.fraction * 100)}%`, boxShadow: [{ x: 0, y: 0, blur: 4 }] }} />
        </If>
      </View>
    </View>;
}

function WorkingRow() {
  const turn = () => { m(); const msgs = model.selected?.messages ?? []; return [...msgs].reverse().find((x) => x.role === "user"); };
  const elapsed = () => {
    tick();
    const u = turn();
    return u ? Math.max(0, Date.now() / 1000 - u.created) : 0;
  };
  effect(() => {
    const id = setInterval(() => tick.set(Date.now()), 1000);
    return () => clearInterval(id);
  });
  return (
    <View class="shrink-0 flex-row items-center gap-2 h-7" testID="working-row">
      <Status kind="running" />
      <Shimmer text="Working" reduced={!!config()?.values?.ui?.reduced_motion} mono />
      <Text class={T.mono}>{`· ${fmtDuration(elapsed())}`}</Text>
    </View>
  );
}

/** A queued item's one-line label: its first text line, or a collapsed paste's marker. */
function pendingLabel(item: any): string {
  const part = item?.parts?.find((p: any) => p.type === "text");
  if (!part) return "[image]";
  return part.collapsed === true ? pasteLabel(part.text ?? "") : (part.text ?? "").split("\n")[0];
}

function PendingStack() {
  const session = () => viewed();
  const stack = () => { m(); return pendingInputs()[viewed()?.id ?? ""] ?? { inputs: [], held: false }; };
  /** Queued inputs in their own order: the icons' up/down bounds never leave this group. */
  const queueIds = () => (stack()?.inputs ?? []).filter((i: any) => i.mode === "queue").map((i: any) => i.id);
  const perform = async (id: string, action: "edit" | "remove" | "up" | "down") => {
    const s = session(); if (!s) return;
    try {
      if (action === "remove") {
        const item = (stack()?.inputs ?? []).find((i: any) => i.id === id);
        const text = pendingLabel(item);
        const sid = s.id;
        confirmDialog.set({
          title: "Remove pending input",
          body: `Remove the ${item?.mode === "steer" ? "steer" : "queued message"} "${text}"?\n\nThis cannot be undone.`,
          confirm: "Remove", destructive: true,
          run: async () => { await request("DELETE", `/api/session/${sid}/input/${id}`); await loadInputs(sid); },
        });
        return;
      }
      if (action === "edit") {
        if (questionLocked()) { toast("info", "Answer the question above"); return; }
        const k = draftKey();
        if ((draft()[k] ?? "").trim() || (imageDrafts()[k] ?? []).length) { toast("warning", "Clear the draft before restoring a pending input"); return; }
        const result = await request<any>("DELETE", `/api/session/${s.id}/input/${id}`);
        restorePendingParts(k, result.parts ?? result.input?.parts ?? []);
        focusComposer.fn?.();
      } else {
        const ids = queueIds(), at = ids.indexOf(id);
        if (at < 0) return;
        if (action === "up" && at > 0) await request("POST", `/api/session/${s.id}/input/${id}/move`, { body: { before: ids[at - 1] } });
        else if (action === "down" && at < ids.length - 1) await request("POST", `/api/session/${s.id}/input/${ids[at + 1]}/move`, { body: { before: id } });
        else return;
      }
      await loadInputs(s.id);
    } catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
  };
  effect(() => {
    const s = session();
    const items = stack().inputs;
    const selected = pendingSelection();
    if (selected && (selected.session !== s?.id || !items.some((item) => item.id === selected.id))) pendingSelection.set(null);
  });
  effect(() => {
    const s = session();
    const items = stack().inputs;
    const selection = pendingSelection();
    if (!s || !items.length || !focused() || questionLocked() || !live() || term("overlay.open") || term("dialog.open")) return;
    const at = selection?.session === s.id ? items.findIndex((item) => item.id === selection.id) : -1;
    const choose = (index: number) => pendingSelection.set(index < 0 || index >= items.length ? null : { session: s.id, id: items[index].id });
    if (at < 0) {
      const k = draftKey();
      if ((draft()[k] ?? "").length || (imageDrafts()[k] ?? []).length) return;
      return bindOpenKeys({ up: () => choose(items.length - 1) });
    }
    const item = items[at];
    const move = async (direction: -1 | 1) => {
      const other = items[at + direction]; if (!other) return;
      try {
        const id = direction < 0 ? item.id : other.id;
        const before = direction < 0 ? other.id : item.id;
        await request("POST", `/api/session/${s.id}/input/${id}/move`, { body: { before } });
        await loadInputs(s.id);
      } catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
    };
    const steer = async () => {
      if (item.mode !== "queue") return;
      try { await request("PATCH", `/api/session/${s.id}/input/${item.id}`, { body: { mode: "steer" } }); await loadInputs(s.id); }
      catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
    };
    const next = async () => {
      if (!stack().held) return;
      try { await request("POST", `/api/session/${s.id}/input/next`, { body: {} }); await loadInputs(s.id); }
      catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
    };
    return bindOpenKeys({
      up: () => choose(Math.max(0, at - 1)), down: () => choose(at + 1), escape: () => choose(-1),
      enter: () => { choose(-1); void perform(item.id, "edit"); },
      backspace: () => { choose(-1); void perform(item.id, "remove"); },
      "shift+up": () => void move(-1), "shift+down": () => void move(1),
      s: () => void steer(), n: () => void next(),
    });
  });
  // Quiet ghost actions: icons that appear on hover (and on focus) on a pointer, always shown below the
  // text on touch and on a narrow window. Only queued rows carry actions.
  const always = () => touch || isNarrow();
  const reveal = () => always() ? "" : "opacity-0 group-hover/pend:opacity-100 group-focus-within/pend:opacity-100 focus-visible:opacity-100";
  // Where there is no hover a tap on the row opens its actions, one row at a time.
  const opened = signal<string | null>(null);
  const PendingActions = (q: { i: () => { id: string } }) => {
    const ids = () => queueIds();
    const at = () => ids().indexOf(q.i().id);
    return <>
      <IconButton icon="pencil" label="Edit" testID={`pending-edit-${q.i().id}`} onPress={() => void perform(q.i().id, "edit")} />
      <IconButton icon="arrow-up" label="Move up" testID={`pending-up-${q.i().id}`} disabled={at() <= 0} onPress={() => void perform(q.i().id, "up")} />
      <IconButton icon="arrow-down" label="Move down" testID={`pending-down-${q.i().id}`} disabled={at() < 0 || at() >= ids().length - 1} onPress={() => void perform(q.i().id, "down")} />
      <IconButton icon="x" label="Remove" testID={`pending-remove-${q.i().id}`} onPress={() => void perform(q.i().id, "remove")} />
    </>;
  };
  return <View><If when={stack().inputs.length > 0}><View class="mb-3 rounded-(--radius-card) border border-border bg-card px-2 py-1" testID="pending-inputs">
    <If when={stack().held}><View class="flex-row items-center justify-end min-h-6 -mr-3"><Btn label="Send next" kind="ghost" testID="pending-send-next" disabled={questionLocked() || !live()} onPress={() => { const s = session(); if (s && live() && !questionLocked()) void request("POST", `/api/session/${s.id}/input/next`, { body: {} }).then(() => loadInputs(s.id)).catch((e) => toast("error", String(e))); }} /></View></If>
    <Each of={stack().inputs} key={(i) => i.id}>{(i) => <View class="group/pend relative" testID={`pending-${i().id}`}>
      <If when={i().mode === "queue"} else={<View class="flex-row items-center gap-2 min-h-8 rounded-(--radius) px-0">
        <Text class={`w-5 shrink-0 ${TS.base} text-foreground`}>S</Text>
        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${pendingSelection()?.id === i().id ? T.body : T.sec} group-hover/pend:text-foreground`}>{`${pendingLabel(i())}${stack().held ? " held" : ""}${pendingSelection()?.id === i().id ? `  ${stack().held ? "n send next · " : ""}enter edit · backspace remove · shift+↑/↓ move` : ""}`}</Text>
      </View>}>
        <Button disabled={!always()} onPress={() => opened.set(opened() === i().id ? null : i().id)} selected={opened() === i().id}
          accessibilityLabel={`Queued: ${pendingLabel(i())}`}
          class={`flex-row items-center gap-2 min-h-8 rounded-(--radius) ${always() ? "" : "pr-36"} ${FOCUS}`}>
          <Text class={`w-5 shrink-0 ${TS.base} text-foreground`}>Q</Text>
          <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${pendingSelection()?.id === i().id ? T.body : T.sec} group-hover/pend:text-foreground`}>{`${pendingLabel(i())}${stack().held ? " held" : ""}${pendingSelection()?.id === i().id ? `  ${stack().held ? "n send next · " : ""}enter edit · backspace remove · s steer · shift+↑/↓ move` : ""}`}</Text>
        </Button>
        <If when={always()} else={<View class={`absolute right-0 top-0 bottom-0 flex-row items-center gap-0.5 pl-2 group-hover/pend:bg-sidebar ${pendingSelection()?.id === i().id ? "" : reveal()}`}><PendingActions i={i} /></View>}>
          <If when={opened() === i().id}><View class="flex-row items-center justify-end gap-0.5"><PendingActions i={i} /></View></If>
        </If>
      </If>
    </View>}</Each>
  </View></If></View>;
}

let resourcesTimer: ReturnType<typeof setInterval> | undefined;

/** The location strip shows the path, branch, live kernel/shell counts and linked-worktree icon. */
function LocationStrip(p: { working: boolean }) {
  // The row's measured width decides how much path fits; a fixed character count overflows a
  // narrow column and truncates needlessly in a wide one.
  const stripWidth = signal(0);
  const worktreeHover = signal(false);
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
  effect(() => { bashJobs(); if (terminalAvailable && currentProject()) { void refreshTerminals(); } });
  // The agent's running bash jobs; user terminals are counted by their own button.
  const runningShells = () => viewResources().jobs.filter(j => j.status === "running").length;
  const session = () => { m(); return viewed(); };
  const directory = () => session()?.directory ?? project()?.path ?? "";
  const selectedTree = () => { m(); return currentWorktree(); };
  const branch = () => selectedTree()?.branch as string | undefined;
  const linked = () => selectedTree()?.main === false;
  const path = () => {
    const dir = directory();
    const stateHome = info()?.home ?? "";
    const home = userHome() || (/\/\.nexus(10)?$/.test(stateHome) ? stateHome.replace(/\/\.nexus(10)?$/, "") : "");
    return midTruncate(shortPath(dir, home), pathLimit());
  };
  const pathLimit = () => {
    const w = stripWidth();
    if (!w) return isNarrow() ? 24 : 54;
    const c = resourceCounts();
    const chip = (c.kernels ? 70 : 0) + (runningShells() ? 70 : 0) + (terminalItems().length ? 140 : 0);
    const unit = 9.6;
    const reserved = 16 + (branch() ? (Array.from(branch()!).length + 3) * unit : 0) + (linked() ? 16 : 0);
    return Math.max(0, Math.floor(((w - chip) / 2 - reserved) / unit));
  };
  // A kernel appears while a code cell runs, and a shell finishes between turns:
  // refresh while the tree is busy or this list is open.
  effect(() => {
    const watch = session()?.status === "busy" || resourcesOpen();
    if (watch && resourcesTimer === undefined) {
      resourcesTimer = setInterval(() => { if (connection().kind === "live") void loadResources(); }, 2000);
    } else if (!watch && resourcesTimer !== undefined) {
      clearInterval(resourcesTimer);
      resourcesTimer = undefined;
    }
  });
  const directoryAvailability = () => {
    const c = byId.get("session.change_directory");
    return c ? availability(c) : { ok: false, reason: "Directory changes are not available in NSH." };
  };
  const worktreeAvailability = () => {
    const c = byId.get("session.worktrees");
    return c ? availability(c) : { ok: false, reason: "Worktree controls are not available in NSH." };
  };
  const tooltip = (text: string) => <Text class="text-xs text-popover-foreground bg-popover rounded-(--radius) px-2 py-1">{text}</Text>;
  const openResources = (kind: "kernels" | "shells") => {
    resourcesKind.set(kind);
    resourcesOpen.set(true);
    void loadResources();
  };
  return (
    <If when={phone ? p.working : (project() && directory()) || p.working}>
      <View class={`w-full flex-row items-center gap-2 min-w-0 min-h-7 pt-1 pb-1.5 ${phone ? "" : "px-4"}`} testID="location-strip"
        onLayout={(b: any) => stripWidth.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
        {/* A phone keeps only the working status here: directory, branch, kernel and shell are desktop surfaces. */}
        <View class={phone ? "size-0 overflow-hidden" : "flex-1 w-0 min-w-0 overflow-hidden flex-row items-center gap-2"} accessible={!phone}>
          <If when={!!session()}>
            <Button
              disabled={!directoryAvailability().ok} onPress={() => void run("session.change_directory")} testID="location-directory"
              accessibilityLabel={`Directory ${directory()}${directoryAvailability().ok ? "" : `, unavailable: ${directoryAvailability().reason ?? ""}`}`}
              tooltip={tooltip(directoryAvailability().reason ?? "Change session directory")}
              class={`min-w-0 shrink rounded-(--radius) ${FOCUS}`}
            ><Text style={{ numberOfLines: 1 }} class={`${T.mono} min-w-0`}>{path()}</Text></Button>
          </If>
          <If when={!session()}><Text testID="location-directory" style={{ numberOfLines: 1 }} class={`min-w-0 shrink ${T.mono}`}>{path()}</Text></If>
          <If when={!!branch()}>
            <If when={!!session()} else={<Text testID="location-branch" style={{ numberOfLines: 1 }} class={`${T.mono} min-w-0 shrink`}>{`⌥ ${branch()!}`}</Text>}>
              <Button
                disabled={!worktreeAvailability().ok} onPress={() => void run("session.worktrees")} testID="location-branch"
                accessibilityLabel={`Branch ${branch()}${worktreeAvailability().ok ? "" : `, unavailable: ${worktreeAvailability().reason ?? ""}`}`}
                tooltip={tooltip(worktreeAvailability().reason ?? "Worktree controls")}
                class={`min-w-0 shrink rounded-(--radius) ${FOCUS}`}
              ><Text style={{ numberOfLines: 1 }} class={`${T.mono}`}>{`⌥ ${branch()!}`}</Text></Button>
            </If>
          </If>
          <If when={linked()}>
            <View onMouseEnter={() => worktreeHover.set(true)} onMouseLeave={() => worktreeHover.set(false)}>
              <Button onPress={() => void run("session.worktree_info")} testID="location-worktree" accessibilityLabel="Worktree session, open details"
                tooltip={tooltip("Worktree session")} class={`rounded-(--radius) px-1 min-h-6 items-center justify-center hover:bg-secondary ${FOCUS}`}>
                <Icon name="worktree" size={14} class={worktreeHover() ? "tint-primary" : "tint-muted-foreground"} />
              </Button>
            </View>
          </If>
        </View>
        <View class="shrink-0 flex-row items-center gap-2" testID="location-indicators">
        <If when={resourceCounts().kernels > 0 && !phone}>
          <Button onPress={() => openResources("kernels")} accessibilityLabel="kernel" testID="location-resources"
            class={`rounded-(--radius) bg-muted px-2 min-h-6 justify-center hover:bg-secondary ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class="font-(family-name:--font-code) text-xs leading-4 text-foreground-secondary">kernel</Text>
          </Button>
        </If>
        <If when={runningShells() > 0 && !phone}><Button onPress={() => openResources("shells")} accessibilityLabel={`${runningShells()} shell`} testID="location-background-shell" class={`rounded-(--radius) bg-muted px-2 min-h-6 justify-center hover:bg-secondary ${FOCUS}`}><Text class="font-(family-name:--font-code) text-xs leading-4 text-foreground-secondary tabular-nums">{`${runningShells()} shell`}</Text></Button></If>
        <If when={terminalAvailable && terminalItems().length > 0 && !phone}><Button onPress={() => void openTerminalTab()} selected={terminalOpen()} accessibilityLabel={`${terminalItems().length} terminal shell`} testID="location-shell" class={`rounded-(--radius) bg-muted px-2 min-h-6 justify-center hover:bg-secondary ${FOCUS}`}><Text class="font-(family-name:--font-code) text-xs leading-4 text-foreground-secondary tabular-nums">{`${terminalItems().length} terminal shell`}</Text></Button></If>
        </View>
        <View class={`flex-1 min-w-0 flex-row items-center overflow-hidden ${phone ? "justify-start pl-1" : "w-0 justify-end"}`}><If when={p.working}><WorkingRow /></If></View>
      </View>
    </If>
  );
}

// --- waiting card -----------------------------------------------------------------------------
const answers = signal<Record<string, { selected: string[]; text: string }>>({});
const busyWait = signal(false);
const waitError = signal<string | null>(null);
/** Questions whose POST returned: a late stream event must not allow a duplicate submit. */
const submitted = signal<Set<string>>(new Set());
const submitting = new Set<string>();
/** The page of one `ask` request: 0..questions.length-1, then the review page. */
const askPages = signal<Record<string, number>>({});
const askPageOf = (id: string) => askPages()[id] ?? 0;
const setAskPage = (id: string, page: number) => askPages.update((all) => ({ ...all, [id]: page }));
const blankAnswer = () => ({ selected: [] as string[], text: "" });

/** `session` also grants the same call for the rest of the session (allow only); `note` goes to the model with a refusal (deny only). */
async function reply(a: Approval, allow: boolean, extra: { session?: boolean; note?: string } = {}) {
  busyWait.set(true); waitError.set(null);
  const body: Record<string, unknown> = { reply: allow ? "allow" : "deny" };
  if (allow && extra.session) body.scope = "session";
  if (!allow && extra.note) body.note = extra.note;
  try { await request("POST", `/api/session/${a.session_id}/permission/${a.id}`, { body, key: newKey() }); }
  catch (e) { waitError.set(e instanceof ApiError ? e.message : String(e)); }
  busyWait.set(false);
}
/** The note is the composer's text, as an answer to a question is. */
function denyWithNote(a: Approval) {
  const k = draftKey();
  const note = (draft()[k] ?? "").trim();
  if (!note) { waitError.set("Type the note in the composer, then choose Deny with a note"); return; }
  draft.update((d) => ({ ...d, [k]: "" }));
  void reply(a, false, { note });
}
/** One choice: a multi-select toggles; a single choice is the answer, and a lone question submits it at once. */
function chooseAnswer(q: Question, qq: QuestionItem, option: string) {
  const id = `${q.id}:${qq.id}`;
  if (qq.multiple) {
    answers.update((all) => {
      const a = all[id] ?? { selected: [], text: "" };
      return { ...all, [id]: { ...a, selected: a.selected.includes(option) ? a.selected.filter((x) => x !== option) : [...a.selected, option] } };
    });
    return;
  }
  const answer = { selected: [option], text: answers()[id]?.text ?? "" };
  answers.update((all) => ({ ...all, [id]: answer }));
  if (q.questions.length === 1) void submitQuestion(q, { itemId: qq.id, answer });
}

/** How the review shows one answer: selected labels in order, then the quoted free text. */
function answerLabel(qq: QuestionItem, a: { selected: string[]; text: string }): string {
  const parts = (qq.options ?? []).filter((o) => a.selected.includes(o.id)).map((o) => o.label);
  const text = (a.text ?? "").trim();
  if (text) parts.push(parts.length ? `"${text}"` : text);
  return parts.join(", ");
}

/** Why a page's answer is not ready to send, if it is not (the TUI's checks and wording). */
function answerProblem(qq: QuestionItem, a: { selected: string[]; text: string }): string | null {
  const selected = (qq.options ?? []).filter((o) => a.selected.includes(o.id)).length;
  const text = (a.text ?? "").trim().length > 0;
  if (!qq.options.length) return text ? null : "Type your answer in the question card";
  if (qq.multiple) return selected || text ? null : "Pick at least one option, or type an answer";
  if (selected > 1) return "This question takes one option";
  return selected || text ? null : "Pick an option or type an answer";
}

/**
 * Enter on a question page, as in the TUI: a single choice with nothing picked and no text takes the
 * highlighted option; then a lone question sends, another page locks its answer and moves on, and
 * the review sends every page.
 */
function confirmQuestion(q: Question) {
  if (submitted().has(q.id)) return;
  const total = q.questions.length;
  const page = Math.min(askPageOf(q.id), total);
  if (total > 1 && page >= total) { submitAllQuestions(q); return; }
  const qq = q.questions[page];
  const id = `${q.id}:${qq.id}`;
  const a = answers()[id] ?? blankAnswer();
  if (!qq.multiple && !a.selected.length && !a.text.trim() && qq.options.length) {
    const o = qq.options[Math.min(choice(), qq.options.length - 1)];
    answers.update((all) => ({ ...all, [id]: { ...a, selected: [o.id] } }));
  }
  if (total === 1) { submitAllQuestions(q); return; }
  const problem = answerProblem(qq, answers()[id] ?? blankAnswer());
  if (problem) { waitError.set(problem); return; }
  waitError.set(null);
  setAskPage(q.id, page + 1);
}
/** The question's free-text field has focus: Enter is its own submit. */
const questionFieldFocused = signal(false);

/** Send every page, first showing the first one that is not ready. */
function submitAllQuestions(q: Question) {
  for (const [i, qq] of q.questions.entries()) {
    const problem = answerProblem(qq, answers()[`${q.id}:${qq.id}`] ?? blankAnswer());
    if (problem) {
      if (q.questions.length > 1) setAskPage(q.id, i);
      waitError.set(problem);
      return;
    }
  }
  void submitQuestion(q);
}

async function submitQuestion(q: Question, override?: { itemId: string; answer: { selected: string[]; text: string } }) {
  if (submitting.has(q.id) || submitted().has(q.id)) return;
  const items = q.questions.map((qq) => {
    const a = override && override.itemId === qq.id ? override.answer : answers()[`${q.id}:${qq.id}`] ?? { selected: [], text: "" };
    return { question_id: qq.id, selected: a.selected, text: a.text || null };
  });
  if (q.questions.length === 1 && !items[0].selected.length && !(items[0].text ?? "").trim()) {
    waitError.set("Choose an answer or type one");
    return;
  }
  submitting.add(q.id);
  busyWait.set(true); waitError.set(null);
  try {
    await request("POST", `/api/session/${q.session_id}/question/${q.id}`, { body: { answers: items }, key: newKey() });
    submitted.update((all) => new Set(all).add(q.id));
  } catch (e) {
    // A failed POST keeps the chosen answer and free text for a retry.
    waitError.set(e instanceof ApiError ? e.message : String(e));
  } finally {
    submitting.delete(q.id);
    busyWait.set(false);
  }
}

export function WaitingCard() {
  focusWaiting.fn = () => {};
  const items = () => { m(); return waitItems(); };
  const cur = () => { const l = items(); return l.length ? l[waitIndex() % l.length] : null; };
  // A replied or cancelled question drops its draft and submit guard; other sessions' questions keep theirs.
  effect(() => {
    m();
    const live = new Set(model.questions.keys());
    const all = answers();
    const kept = Object.fromEntries(Object.entries(all).filter(([key]) => live.has(key.slice(0, key.indexOf(":")))));
    if (Object.keys(kept).length !== Object.keys(all).length) answers.set(kept);
    const done = new Set([...submitted()].filter((id) => live.has(id)));
    if (done.size !== submitted().size) submitted.set(done);
    const pages = askPages();
    const keptPages = Object.fromEntries(Object.entries(pages).filter(([id]) => live.has(id)));
    if (Object.keys(keptPages).length !== Object.keys(pages).length) askPages.set(keptPages);
  });
  return (
    <TimelineColumn class="items-center mt-2" testID="waiting-slot">
      <If when={cur()}>{(w) => <WaitBody w={w} count={items().length} />}</If>
    </TimelineColumn>
  );
}

/** When the client first saw each request: `waiting on you · 0:42` counts from here. */
const firstSeen = new Map<string, number>();
const since = (id: string) => { let t = firstSeen.get(id); if (t === undefined) { t = Date.now(); firstSeen.set(id, t); } return t; };
export const clock = (sec: number) => `${Math.floor(sec / 60)}:${String(Math.max(0, Math.floor(sec % 60))).padStart(2, "0")}`;

const WAIT_DIFF_ROWS = 12;
const WAIT_TABLE_ROWS = 6;
const DIFF_ARGS = { edit: ["patch", "old_string", "new_string", "old", "new"], patch: ["patch", "old_string", "new_string", "old", "new"], write: ["content"] } as Record<string, string[]>;

/** The parameter table (at most six rows; the diff's own arguments are left out) and, for edit, write and patch, the diff lines capped at 12 rows. */
export function approvalParts(a: Approval): { table: [string, string][]; diff: ToolDetailLine[]; more: number } {
  const diffAll = DIFF_ARGS[a.tool] ? diffItems(detailOf(a.tool, JSON.stringify(a.args ?? {}), undefined, false)) : [];
  const skip = diffAll.length ? DIFF_ARGS[a.tool] : [];
  const args = a.args && typeof a.args === "object" ? Object.entries(a.args).filter(([k]) => !skip.includes(k)).sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0)) : []; // sorted like the TUI (serde_json maps)
  const table = args.map(([k, v]): [string, string] => {
    const text = typeof v === "string" ? v : JSON.stringify(v);
    const lines = text.split("\n");
    return [k, lines.length > 1 ? `${lines[0]} …` : lines[0]];
  }).slice(0, WAIT_TABLE_ROWS);
  const cap = diffAll.length > WAIT_DIFF_ROWS ? WAIT_DIFF_ROWS - 1 : diffAll.length;
  const diff = diffAll.slice(0, cap).map((it): ToolDetailLine =>
    it.kind === "hunk" ? { text: it.text, tone: "info" } : it.kind === "add" ? { text: `+${it.text}`, tone: "add" } : it.kind === "del" ? { text: `-${it.text}`, tone: "del" } : { text: ` ${it.text}`, tone: "plain" });
  return { table, diff, more: diffAll.length - cap };
}

function WaitBody(p: { w: () => { kind: "approval" | "question"; id: string; session_id: string }; count: number }) {
  const approval = () => { m(); return model.approvals.get(p.w().id); };
  const question = () => { m(); return model.questions.get(p.w().id); };
  const child = () => { const s = model.sessions.get(p.w().session_id); return s && s.id !== viewed()?.id ? s.title || "child" : ""; };
  const elapsed = () => { tick(); return Math.floor((Date.now() - since(p.w().id)) / 1000); };
  effect(() => {
    const id = setInterval(() => tick.set(Date.now()), 1000);
    return () => clearInterval(id);
  });
  // The modified keys of the card, bound while it shows. Enter belongs to the focused choice, never a global binding.
  effect(() => { p.w().id; choice.set(0); waitError.set(null); });
  effect(() => {
    const k = (id: string) => { const key = byId.get(id)?.keys?.nsh; return key ? toKeystroke(key) : null; };
    const map: Record<string, () => void> = {};
    const bind = (id: string, fn: () => void) => { const key = k(id); if (key && /(cmd|ctrl|alt|mod)\+/.test(key)) map[key] = fn; };
    if (p.count > 1) { bind("waiting.next", () => run("waiting.next")); bind("waiting.prev", () => run("waiting.prev")); }
    const a = approval(), q = question();
    if (a && live()) { bind("waiting.deny", () => void reply(a, false)); bind("waiting.allow_session", () => void reply(a, true, { session: true })); bind("waiting.deny_note", () => denyWithNote(a)); }
    if (a && live()) {
      const move = (d: number) => {
        const list = approvalChoices(a);
        let i = choice();
        for (let n = 0; n < list.length; n++) { i = (i + d + list.length) % list.length; if (offered(list[i])) { choice.set(i); return; } }
      };
      map["down"] = () => move(1);
      map["up"] = () => move(-1);
      // Enter belongs to the composer while it holds text.
      map["enter"] = () => { if (!(draft()[draftKey()] ?? "").length) approvalChoices(a)[choice()]?.run?.(); };
    }
    if (q && live()) bind("waiting.submit_answers", () => submitAllQuestions(q));
    // Up/down move the highlighted option of the page in view; Enter confirms it (the free-text field keeps its own Enter).
    const askPage = q ? askPageOf(q.id) : 0;
    const shown = q && askPage < q.questions.length ? q.questions[askPage] : undefined;
    if (q && live() && !submitted().has(q.id)) {
      if (shown?.options.length) {
        const last = shown.options.length - 1;
        map["down"] = () => choice.set(Math.min(choice() + 1, last));
        map["up"] = () => choice.set(Math.max(choice() - 1, 0));
      }
      map["enter"] = () => { if (!questionFieldFocused()) confirmQuestion(q); };
    }
    const offs: (() => void)[] = [];
    const letter = (key: string, fn: () => void) => { try { offs.push(keymap.bind(key, fn, { when: "notEditing" })); } catch { /* the platform keeps this keystroke */ } };
    if (a && live()) for (const row of approvalChoices(a)) { const key = byId.get(row.cmd)?.keys?.tui; if (key && row.run) letter(key, row.run); }
    // Number keys act on the page in view; the review page has no choices.
    const page = q ? askPageOf(q.id) : 0;
    const one = q?.questions[page];
    if (q && one && !one.multiple && live()) one.options.slice(0, 9).forEach((o, i) => letter(String(i + 1), () => { choice.set(i); chooseAnswer(q, one, o.id); }));
    // Space picks (or, multi-select, ticks) the highlighted option.
    if (q && one && one.options.length && live()) letter("space", () => { const o = one.options[Math.min(choice(), one.options.length - 1)]; chooseAnswer(q, one, o.id); });
    const off = bindOpenKeys(map);
    return () => { off(); offs.forEach((f) => f()); };
  });
  const multi = () => question()?.questions.some((x) => x.multiple) ?? false;
  return (
    <View class={`relative w-full mb-2 ${phone ? "bg-muted rounded-[22px] overflow-hidden" : "bg-raised rounded-(--radius-card)"}`} testID="waiting-card" accessibilityLabel="Waiting for your input" role="alert">
      <If when={!phone}><StatusRail color={multi() ? "bg-multi" : "bg-primary"} testID="waiting-rail" /></If>
      <If when={question()}><View testID="waiting-reading-hint" class="shrink-0 pl-4 pr-4 pt-2 pb-2" style={{ minHeight: 40 }}><Text class={T.meta}>Scroll or Page Up/Down to read questions and choices</Text></View></If>
      <Scroll focusable ref={(n: any) => { focusWaiting.fn = () => { try { n?.focus(); } catch { /* not focusable here */ } }; }} testID="waiting-scroll" style={{ maxHeight: Math.round(windowSize().height * 0.6) }}>
        <View class={phone ? "p-4 gap-2.5" : "pl-4 pr-4 py-3 gap-2.5"}>
          <View>
          <View class="flex-row items-center gap-2 min-h-6">
            <Text class={`${phone ? "text-[11px] leading-6" : "text-[16px] leading-6"} ${multi() ? "text-multi" : "text-primary"}`}>◆</Text>
            <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${phone ? "text-[17px]" : "text-[16px]"} leading-6 font-semibold text-foreground ${"font-(family-name:--font-ui)"}`} testID="waiting-title">
              {approval() ? `Allow ${approval()!.tool}?` : "Nexus asks"}
            </Text>
            <If when={phone}><Text class="text-[13px] leading-5 text-muted-foreground tabular-nums" testID="waiting-timer">{clock(elapsed())}</Text></If>
            <If when={p.count > 1}>
              <Button onPress={() => run("waiting.next")} accessibilityLabel="Next waiting item" testID="waiting-next" class={`flex-row items-center gap-1.5 rounded-(--radius) px-1.5 hover:bg-accent ${FOCUS}`}>
                <Text class={T.meta}>{`${(waitIndex() % p.count) + 1} of ${p.count} ·`}</Text>
                <Text class={`${T.meta} rounded-(--radius) bg-muted px-1`}>{keyLabel(byId.get("waiting.next")!) ?? "next"}</Text>
                <Text class={T.meta}>next</Text>
              </Button>
            </If>
          </View>
          <If when={!phone || !!child()}>
          <Text class={`${T.sec} text-muted-foreground`} testID="waiting-sub">
            {phone ? `in child: ${child()}` : `waiting on you · ${clock(elapsed())}${child() ? ` · in child: ${child()}` : ""}`}
          </Text>
          </If>
        </View>
        <If when={approval()}>{() => <ApprovalBody a={approval()!} />}</If>
        <If when={question()}>{() => <QuestionBody q={question()!} />}</If>
        <If when={waitError()}><View class="flex-row gap-1.5 items-center"><Status kind="error" /><Text class="text-[16px] leading-6 text-error">{waitError()!}</Text></View></If>
        </View>
      </Scroll>
    </View>
  );
}

type Choice = { id: string; title: string; cmd: string; run?: () => void; why?: string };
const approvalChoices = (a: Approval): Choice[] => [
  { id: "allow-once", title: "Allow once", cmd: "waiting.allow_once", run: () => void reply(a, true) },
  { id: "allow-session", title: "Allow for session", cmd: "waiting.allow_session", run: () => void reply(a, true, { session: true }) },
  { id: "deny", title: "Deny", cmd: "waiting.deny", run: () => void reply(a, false) },
  { id: "deny-note", title: "Deny with a note", cmd: "waiting.deny_note", run: () => denyWithNote(a) },
];
/** The selected choice of the approval card (`▸`); arrows move it over the offered choices. */
const choice = signal(0);
const offered = (c: Choice) => !c.why && !!c.run && live();

function ApprovalBody(p: { a: Approval }) {
  const a = p.a;
  const parts = () => approvalParts(a);
  const rows = () => approvalChoices(a);
  // The label column fits the longest key (`background`), never wrapping it, up to a cap.
  const labelRoom = () => Math.min(144, Math.max(64, Math.ceil(Math.max(0, ...parts().table.map((r) => r[0].length)) * 7.4)));
  return (
    <View class="gap-2.5">
      {/* A phone shows a compact card: the command or path, not the parameter table or the diff. */}
      <If when={phone}>
        <PhoneApprovalChoices id={a.id} command={compactApproval(a)} busy={busyWait()} live={live()}
          onAllowOnce={() => void reply(a, true)} onAllowSession={() => void reply(a, true, { session: true })}
          onDeny={(note) => void reply(a, false, note ? { note } : {})} />
      </If>
      <If when={!phone && parts().table.length > 0}>
        <View class="rounded-(--radius) bg-background px-3 py-2 gap-0.5" testID="waiting-table">
          <Each of={parts().table} key={(r) => r[0]}>
            {(r) => (
              <View class="flex-row gap-3 min-h-5">
                <Text class={`shrink-0 ${T.meta}`} style={{ minWidth: labelRoom() }}>{r()[0]}</Text>
                <Text style={{ numberOfLines: 3, userSelect: "text" }} class={`flex-1 min-w-0 ${T.mono} text-foreground`}>{r()[1]}</Text>
              </View>
            )}
          </Each>
        </View>
      </If>
      <If when={!phone && parts().diff.length > 0}>
        <View class="rounded-(--radius) bg-background overflow-hidden" testID="waiting-diff">
          <DiffView lines={() => parts().diff} unified />
          <If when={parts().more > 0}><Text testID="waiting-diff-more" class={`${T.meta} px-3 py-1`}>{`${parts().more} more rows`}</Text></If>
        </View>
      </If>
      <If when={!phone}><If when={!busyWait()} else={<View class="flex-row items-center gap-2"><Status kind="running" /><Text class={T.sec}>sending…</Text></View>}>
        <View role="list" accessibilityLabel="Choices" class="gap-0.5">
          <Each of={rows()} key={(r) => r.id}>
            {(r, i) => {
              const off = () => !!r().why || (!!r().run && !live());
              const reason = () => r().why ?? (r().run && !live() ? "Core is not connected" : "");
              return (
                <Button onPress={() => r().run?.()} disabled={off()} testID={r().id}
                  accessibilityLabel={off() ? `${r().title}, unavailable: ${reason()}` : r().title}
                  style={phone ? { width: "48%" } : {}}
                  class={`group/row relative flex-row items-center gap-2.5 rounded-(--radius) px-2 ${phone ? "min-h-11 justify-center bg-background" : isNarrow() ? "min-h-11" : "min-h-8"} ${!phone && i() === choice() && !off() ? "bg-selection" : ""} ${off() ? "" : "hover:bg-accent"} ${FOCUS}`}>
                  <If when={!phone}><Text class={`shrink-0 w-5 text-center rounded-(--radius) bg-muted text-[16px] leading-6 font-medium ${off() ? "text-muted-foreground" : "text-foreground"}`}>{byId.get(r().cmd)?.keys?.tui ?? ""}</Text></If>
                  <Text style={{ numberOfLines: 1, fontFamily: "Google Sans" }} class={`${phone ? "text-center text-[14px] font-semibold" : "flex-1 text-[16px]"} min-w-0 leading-6 ${off() ? "text-muted-foreground" : "text-foreground"}`}>{r().title}</Text>
                  <If when={off()}><Text testID={`${r().id}-reason`} class={T.meta}>{reason()}</Text></If>
                </Button>
              );
            }}
          </Each>
        </View>
        <AnswerHint />
      </If></If>
    </View>
  );
}

function AnswerHint() {
  const c = byId.get("waiting.focus");
  return <If when={!!c && !phone}><Text class={T.meta} testID="waiting-answer-hint"><Text class="font-medium text-foreground">{c ? keyLabel(c) ?? "" : ""}</Text>{" answer"}</Text></If>;
}

/** A drawn radio mark: a 14 px ring with a filled centre when selected. */
function RadioMark(p: { on: boolean; testID?: string }) {
  return <View testID={p.testID} class={`size-3.5 shrink-0 items-center justify-center rounded-full border ${p.on ? "border-primary" : "border-muted-foreground"}`}>
    <If when={p.on}><View class="size-1.5 rounded-full bg-primary" /></If>
  </View>;
}
/** A drawn checkbox: a 14 px square box with a check icon when selected. */
function CheckMark(p: { on: boolean; testID?: string }) {
  return <View testID={p.testID} class={`size-3.5 shrink-0 items-center justify-center rounded-none border ${p.on ? "border-primary bg-primary" : "border-muted-foreground"}`}>
    <If when={p.on}><Icon name="check" size={10} class="tint-on-accent" /></If>
  </View>;
}

function QuestionBody(p: { q: Question }) {
  const q = p.q;
  const total = q.questions.length;
  const single = total === 1;
  const page = () => Math.min(askPageOf(q.id), total);
  const reviewing = () => !single && page() >= total;
  const current = () => q.questions[Math.min(page(), total - 1)];
  const answerOf = (qq: QuestionItem) => answers()[`${q.id}:${qq.id}`] ?? blankAnswer();
  const done = () => submitted().has(q.id);
  let freeRef: any;
  // Each page's string question takes focus when it appears, including after Enter advances.
  effect(() => {
    const cur = current();
    if (reviewing() || done() || !(cur.allow_free_text || cur.options.length === 0)) return;
    const timer = setTimeout(() => {
      if (askPageOf(q.id) !== page() || reviewing()) return;
      try { freeRef?.focus(); } catch { /* the page changed meanwhile */ }
    }, 0);
    return () => clearTimeout(timer);
  });
  // The highlight starts on the page's first picked option, else the first one.
  effect(() => {
    const cur = current();
    if (reviewing()) return;
    const first = cur.options.findIndex((o) => (answers()[`${q.id}:${cur.id}`]?.selected ?? []).includes(o.id));
    choice.set(Math.max(0, first));
  });
  const go = (to: number) => { waitError.set(null); setAskPage(q.id, to); };
  /** Enter on a question page locks its answer and moves on; the last page goes to the review. */
  const advance = () => {
    const cur = current();
    const problem = answerProblem(cur, answerOf(cur));
    if (problem) { waitError.set(problem); return; }
    go(page() + 1);
  };
  return (
    <View class="gap-3">
      <If when={done()}><View class="flex-row items-center gap-2"><Status kind="running" /><Text class={T.sec}>{reviewing() ? "sending answers…" : "sending answer…"}</Text></View></If>
      <If when={!single}>
        <View class="flex-row items-center gap-2" testID="question-page">
          <Text class={`${T.meta} flex-1`}>{reviewing() ? "Review answers" : `Question ${page() + 1} of ${total}`}</Text>
          <Text class={T.meta} testID="question-page-count">{`${reviewing() ? total : page() + 1} of ${total}`}</Text>
        </View>
      </If>
      <If when={reviewing()} else={
        <View class="gap-1.5">
          <Text class="text-[16px] leading-6 font-medium text-foreground">{current().question}</Text>
          <Each of={current().options} key={(o) => o.id}>
            {(o, n) => {
              const sel = () => (answerOf(current()).selected ?? []).includes(o().id);
              return (
                <Button role={current().multiple ? "checkbox" : "radio"} selected={sel()} disabled={done()}
                  accessibilityLabel={`${current().question} ${[o().label, o().preview].filter(Boolean).join('. ')}`}
                  onPress={() => { choice.set(n()); chooseAnswer(q, current(), o().id); }}
                  class={`flex-row items-center gap-2 rounded-(--radius) px-2.5 ${phone ? "min-h-11" : "min-h-8"} hover:bg-accent ${(phone ? sel() : n() === choice()) ? "bg-selection" : ""} ${FOCUS}`}>
                  <If when={current().multiple} else={<Text testID={`choice-mark-${o().id}`} class={`shrink-0 w-3.5 text-center text-[16px] leading-6 ${sel() ? "text-primary font-semibold" : "text-muted-foreground"}`}>{n() + 1}</Text>}><CheckMark on={sel()} testID={`choice-mark-${o().id}`} /></If>
                  <View class="flex-1 min-w-0 py-1">
                    <Text class={`text-[16px] leading-6 ${sel() ? "font-semibold text-foreground" : "text-foreground"}`}>{o().label}</Text>
                    <If when={o().preview}><Text class={`${T.sec} text-muted-foreground`}>{o().preview}</Text></If>
                  </View>
                </Button>
              );
            }}
          </Each>
          <Input ref={(n: any) => (freeRef = n)} value={answerOf(current()).text} autoFocus={current().allow_free_text || current().options.length === 0} readonly={done()}
            placeholder={current().options.length ? "Add context, or type your own answer" : "Type your answer"}
            onChange={({ value: t }) => {
              const cur = current();
              answers.update((all) => ({ ...all, [`${q.id}:${cur.id}`]: { selected: all[`${q.id}:${cur.id}`]?.selected ?? [], text: t } }));
            }}
            onSubmit={() => confirmQuestion(q)} submitBehavior="submit"
            onFocus={() => questionFieldFocused.set(true)} onBlur={() => questionFieldFocused.set(false)}
            accessibilityLabel={`${current().question} Free-text answer`} testID={`question-free-${current().id}`}
            class={`rounded-(--radius) bg-background px-2.5 ${phone ? "h-11" : "h-8"} text-[16px] text-foreground`} />
          <If when={!single}>
            <View class="flex-row items-center gap-2 pt-1">
              <Btn label="Back" onPress={() => go(Math.max(0, page() - 1))} disabled={page() === 0 || done()} testID="question-back" />
              <View class="flex-1" />
              <Btn label={page() + 1 < total ? "Next" : "Review"} kind="primary" onPress={advance} disabled={done()} testID="question-next" />
            </View>
          </If>
          <If when={single && current().multiple}>
            <View class="flex-row items-center gap-3">
              <Btn label="Submit answers" kind="primary" busy={busyWait() || done()} onPress={() => submitAllQuestions(q)} testID="submit-answers" disabled={!live() || done()} />
              <If when={!phone}><Text class={T.meta}>{`${keyLabel(byId.get("waiting.submit_answers")!) ?? "enter"} submit`}</Text></If>
              <AnswerHint />
            </View>
          </If>
        </View>
      }>
        <View class="gap-2" testID="question-review">
          <Each of={q.questions} key={(x) => x.id}>
            {(qq, i) => (
              <View class="gap-0.5">
                <Text class="text-[16px] leading-6 font-medium text-foreground">{`${i() + 1}. ${qq().question}`}</Text>
                <Text testID={`question-review-${qq().id}`} class={`${T.sec} text-muted-foreground`}>{answerLabel(qq(), answerOf(qq())) || "not answered"}</Text>
              </View>
            )}
          </Each>
          <View class="flex-row items-center gap-3 pt-1">
            <Btn label="Back" onPress={() => go(total - 1)} disabled={done()} testID="question-back" />
            <Btn label="Submit answers" kind="primary" busy={busyWait() || done()} onPress={() => submitAllQuestions(q)} testID="submit-answers" disabled={!live() || done()} />
            <If when={!phone}><Text class={T.meta}>{`${keyLabel(byId.get("waiting.submit_answers")!) ?? "enter"} submit`}</Text></If>
            <AnswerHint />
          </View>
        </View>
      </If>
    </View>
  );
}

/**
 * The composer of a session shown by an unfocused group (plan 45): the same card with that session's own draft,
 * agent, model and effort. A press focuses the group, which makes the session the viewed one, and puts the caret
 * in its live composer; the draft is the same text (drafts are kept per session).
 */
export function PeerComposer(p: { sid: string; onFocus: () => void }) {
  const ses = () => { m(); return model.sessions.get(p.sid) ?? null; };
  const text = () => draft()[p.sid] ?? "";
  const agent = () => ses()?.agent ?? "build";
  const busy = () => ses()?.status === "busy";
  const modelName = () => {
    const s = ses();
    const ref = s?.model ? String(s.model) : (agentCatalog().agents.find((a) => a.name === agent())?.model || rememberedModel(s?.project_id ?? null, agent()) || "");
    if (!ref) return { name: "default", provider: "" };
    const entry = modelCatalog().find((item) => item.ref === ref);
    if (!entry) return { name: ref.split("/").at(-1) ?? ref, provider: ref.includes("/") ? ref.split("/")[0] : "" };
    return { name: entry.name || ref.split("/").at(-1) || ref, provider: entry.provider || ref.split("/")[0] };
  };
  const effort = () => { const s: any = ses(); return (s?.effort || s?.extra?.effort || "default") as string; };
  const width = signal(0);
  const peerPath = () => {
    const dir = ses()?.directory ?? "";
    const stateHome = info()?.home ?? "";
    const home = userHome() || (/\/\.nexus(10)?$/.test(stateHome) ? stateHome.replace(/\/\.nexus(10)?$/, "") : "");
    return midTruncate(shortPath(dir, home), Math.max(24, Math.floor((width() - 32) / 9.6)));
  };
  const label = (t: string, cls = "") => <Text style={{ numberOfLines: 1, ...UI_FONT, fontSize: 14, lineHeight: 22 }} class={`min-w-0 shrink ${T.sec} ${cls}`}>{t}</Text>;
  const press = () => { p.onFocus(); setTimeout(() => focusComposer.fn(), 0); };
  return (
    <View class="w-full" testID={`peer-composer-${p.sid}`}>
      <View class="w-full bg-background">
        <TimelineColumn class="items-center">
          <View role="button" focusable onPress={press} accessibilityLabel={`Message ${ses()?.title || "Untitled"}`} testID={`peer-composer-press-${p.sid}`}
            class={`w-full pt-3 pb-1 ${FOCUS}`} onLayout={(b: any) => width.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
            <View class="w-full bg-card rounded-[14px] px-5 pb-2 border border-transparent hover:border-border">
              <View class="h-5" />
              <View class="w-full flex-row items-end gap-3">
                <Text style={{ numberOfLines: 8, ...UI_FONT, minHeight: 22 }} testID={`peer-composer-text-${p.sid}`}
                  class={`flex-1 min-w-0 ${T.body.replace("text-[14px]", "text-[16px]").replace("leading-6", "leading-[22px]")} ${text() ? "text-foreground" : "text-muted-foreground"}`}>
                  {text() || (busy() ? "Type a message… Enter steer · Tab queue · Alt+Enter interrupt" : "Type a message…")}
                </Text>
              </View>
              <View class="h-2.5" />
              <View class="flex-row items-center gap-1 min-w-0 min-h-[30px]">
                <View class="flex-1 min-w-0 flex-row items-center gap-1">
                  <View class="pl-0 pr-2 h-7 flex-row items-center">{label(capitalize(agent()), `font-medium ${agentColor(agent())}`)}</View>
                  <Text class={T.meta}>·</Text>
                  <View class="px-2 h-7 flex-row items-center gap-1.5 min-w-0 shrink">
                    {label(modelName().name)}
                    <If when={width() >= 560 && !!modelName().provider}>{label(modelName().provider)}</If>
                  </View>
                  <Text class={T.meta}>·</Text>
                  <View class="px-2 h-7 flex-row items-center shrink-0">{label(effort())}</View>
                </View>
                <View class="shrink-0 flex-row items-center gap-1 ml-2">
                  <View class={`items-center justify-center rounded-full ${isNarrow() ? "size-11" : "size-[30px]"} bg-raised`}><Icon name="arrow-up" size={15} class="tint-muted-foreground" /></View>
                </View>
              </View>
            </View>
          </View>
          {/* The session's folder, where the live composer has its location strip, so both cards line up. */}
          <View class="w-full flex-row items-center min-w-0 min-h-7 pt-1 pb-1.5 px-4" testID={`peer-location-${p.sid}`}>
            <Text style={{ numberOfLines: 1 }} class={`${T.mono} min-w-0 shrink`}>{peerPath()}</Text>
          </View>
          <ActivityBar busy={busy()} fraction={0} slot={agentSlot(agent())} />
        </TimelineColumn>
      </View>
    </View>
  );
}
