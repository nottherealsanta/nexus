import { Image, Scroll, Text, View, effect, keymap, safeArea, signal, untracked, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, Icon, IconButton, FOCUS, Shimmer, Status, StatusRail, T, midTruncate, shortPath } from "./kit";
import { dockHeight } from "../core/manager";
import { availability, bindOpenKeys, blurComposer, byId, commands, draftKey, focusComposer, focusWaiting, keyLabel, otherWaits, restorePendingParts, resumeArchived, run, runSlashCommand, sendPrompt, term, toKeystroke, waitItems } from "../commands";
import type { Cmd } from "../commands";
import type { FileEntry } from "../store";
import { compactApproval } from "../mobile";
import {
  agentCatalog, contextBudget, contextDetail, draftContext, draftContextKey, draftPlanContext, pendingInputs, loadInputs, loadPromptHistory, promptHistory, centre, config, confirmDialog, connection, currentProject, draft, rememberedModel, info, isNarrow, live, m, model, touch, modelCatalog, tick,
  phone, projectWorktrees, sessionDraft, sessionWorktrees, userHome, worktreeForDirectory, viewed, waitIndex, toast, annotating,
  bashJobs, loadResources, resourceCounts, viewResources, resourcesOpen, resourcesKind, composerFocused, flowStage, flowProject,
} from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { Approval, Question } from "../core/api";
import { agentColor, agentSlot } from "../agent-color";
import { detailOf, fmtDuration } from "./timeline";
import { groupRequests, latestUsage, sumUsage, turnsIn } from "../core/turns";
import type { ToolDetailLine } from "./timeline";
import { DiffView, diffItems } from "./diff";
import { TEXT_INSET, TimelineColumn } from "./reading-column";
import { structure } from "../gen/theme";
import { openTerminal, refreshTerminals, restoreTerminalDock, terminalAvailable, terminalItems, terminalOpen } from "../terminal-panel";

import { imageDrafts, readImage } from "../images";
import { isBigPaste, makePaste, normalisePaste, pasteDrafts, pasteLabel, prunePastes } from "../pastes";
import { cancelDictation, stopDictation } from "../dictation";
import { formatBytes } from "../dictation-logic";
import { dictationLevel, dictationPhase, dictationPreview, voiceDownloadProgress } from "../voice-state";

// Composer (DESIGN_PLAN D11, I5): a card on the panel surface, pill selectors, the context meter,
// a Working row above it while busy, the completion block on its top edge and the project strip below.
const focused = signal(false);
const selected = signal(0);
/** The completion token the user closed with Esc; it reopens when the token changes. */
const dismissed = signal<string | null>(null);
const atListing = signal<{ key: string; entries: FileEntry[] } | null>(null);
let atSeq = 0;
/** The listing being fetched: model bumps re-run the effect and must not ask again meanwhile. */
let atAsking = "";
const MENU_LIMIT = 10;

type CompletionRow = { key: string; value: string; help: string; hint?: string; muted?: string; accept: string };
type Completion = { kind: "slash" | "file"; query: string; dir: string; rows: CompletionRow[] };

/** `/` completes commands when the draft is a single word; `@` completes project paths at the end of the draft. */
function completionFor(text: string): Completion | null {
  if (text.includes("\n")) return null;
  if (/^\/\S*$/.test(text)) {
    const query = text.slice(1).toLowerCase();
    const rows = commands
      .filter((c: Cmd) => !!c.slash && c.slash.toLowerCase().startsWith(query))
      .map((c: Cmd): CompletionRow => {
        const a = availability(c);
        return { key: c.id, value: `/${c.slash}`, help: c.title, hint: keyLabel(c) ?? undefined, muted: a.ok ? undefined : a.reason, accept: `/${c.slash} ` };
      });
    return { kind: "slash", query, dir: "", rows };
  }
  const at = text.match(/(?:^|\s)@([^\s]*)$/);
  if (at) {
    const query = at[1];
    const slash = query.lastIndexOf("/");
    const dir = slash < 0 ? "" : query.slice(0, slash + 1);
    const base = query.slice(dir.length).toLowerCase();
    const listing = atListing();
    const entries = listing && listing.key.endsWith(`|${dir}`) ? listing.entries : [];
    const rows = entries
      .filter((e) => !e.ignored && e.name.toLowerCase().startsWith(base))
      .sort((a, b) => (a.kind === "directory" ? 0 : 1) - (b.kind === "directory" ? 0 : 1) || a.name.localeCompare(b.name))
      .map((e): CompletionRow => {
        const isDir = e.kind === "directory";
        return { key: e.name, value: `@${dir}${e.name}${isDir ? "/" : ""}`, help: isDir ? "directory" : e.kind, accept: `@${dir}${e.name}${isDir ? "/" : " "}` };
      });
    return { kind: "file", query, dir, rows };
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
  const h = signal(28);
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
  const reason = (): string | null => {
    const c = connection();
    if (c.kind !== "live") return c.kind === "reconnecting" ? "Reconnecting to Core: read-only" : c.kind === "offline" ? "Core is offline: read-only" : c.kind === "auth_failed" ? "Authentication failed: read-only" : c.kind === "incompatible" ? "Incompatible API: blocked" : "Connecting to Core…";
    if (!currentProject() && centre() === "new_session") return "Add or choose a project first";
    return null;
  };
  const canSend = () => dictationPhase() === "idle" && !reason() && !pasting() && (text().trim().length > 0 || images().length > 0);
  const canDictate = () => live() && !ses()?.archived;
  const send = async (mode: "send" | "steer" | "queue" = busy() ? "steer" : "send", resumed = false) => {
    const t = text().trim();
    if (t && !images().length && runSlashCommand(t)) {
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
  const completion = () => completionFor(text());
  const open = () => {
    const c = completion();
    return !!c && c.rows.length > 0 && dismissed() !== text();
  };
  const accept = (row: CompletionRow) => {
    const c = completion();
    if (!c) return;
    const t = text();
    const next = c.kind === "file" ? t.slice(0, t.length - (c.query.length + 1)) + row.accept : row.accept;
    draft.update((d) => ({ ...d, [key()]: next }));
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
  const recalled = () => { const r = recall(); const e = promptHistory().entries; return r && e[r.at] === text() ? r : null; };
  // The caret as the input reports it (UTF-16 units, clamped to the draft a replacement shortened);
  // a selection takes no arrow.
  const caret = signal({ start: 0, end: 0 });
  const caretAt = () => { const t = text(); const c = caret(); return c.start === c.end ? Math.min(c.end, t.length) : -1; };
  const onFirstLine = () => { const at = caretAt(); return at >= 0 && !text().slice(0, at).includes("\n"); };
  const onLastLine = () => { const at = caretAt(); return at >= 0 && !text().slice(at).includes("\n"); };
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
    if (!focused() || open()) return;
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
    return bindOpenKeys({ tab: () => { void send("queue"); } });
  });
  effect(() => {
    const c = completion();
    const project = currentProject();
    if (!c || c.kind !== "file" || !project) return;
    const session = viewed()?.id;
    const k = `${project}|${session ?? ""}|${c.dir}`;
    if (untracked(() => atListing()?.key) === k || atAsking === k) return;
    const seq = ++atSeq;
    atAsking = k;
    const done = (entries: FileEntry[]) => { if (seq === atSeq) { atAsking = ""; atListing.set({ key: k, entries }); } };
    void request<{ entries: FileEntry[] }>("GET", `/api/project/${project}/files`, { query: { path: c.dir.replace(/\/$/, ""), session } })
      .then((d) => done(d.entries ?? []), () => done([]));
  });

  const agentName = () => capitalize(agent());
  return (
    <View class="w-full" testID="composer-area">
    <TimelineColumn class="items-center pt-3">
      <View class="w-full">
        <If when={open()}>
          {() => {
            const c = () => completion()!;
            const rows = () => c().rows.slice(0, MENU_LIMIT);
            return (
              <View class="w-full bg-popover px-1 pt-1.5 pb-1 overflow-hidden" testID="completion" accessibilityLabel="Completion">
                <Each of={rows()} key={(r) => r.key}>
                  {(r, i) => {
                    const sel = () => i() === Math.min(selected(), rows().length - 1);
                    return (
                      <Button onPress={() => accept(r())} selected={sel()} testID={`completion-${r().key}`}
                        class={`flex-row items-center gap-3 rounded-(--radius) ${sel() ? "bg-selection" : ""} ${isNarrow() ? "min-h-11" : "h-8"} px-3 hover:bg-accent ${FOCUS}`}>
                        <Text style={{ numberOfLines: 1 }} class={`font-mono text-[16px] leading-6 font-medium ${r().muted ? "text-muted-foreground" : "text-foreground"}`}>{r().value}</Text>
                        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[16px] leading-6 text-muted-foreground">{r().muted ? `${r().help} · ${r().muted}` : r().help}</Text>
                        <If when={!!r().hint}><Text class="text-[16px] leading-6 text-muted-foreground">{r().hint ?? ""}</Text></If>
                      </Button>
                    );
                  }}
                </Each>
                <View class="flex-row items-center gap-3 h-7 px-3">
                  <Text class={T.meta}>↑↓ move</Text>
                  <Text class={T.meta}>tab accept</Text>
                  <Text class={T.meta}>esc close</Text>
                  <View class="flex-1" />
                  <Text class={T.meta}>{`${Math.min(selected(), rows().length - 1) + 1} of ${c().rows.length}`}</Text>
                </View>
              </View>
            );
          }}
        </If>
      </View>
    </TimelineColumn>
    <View class="w-full bg-background" style={{ paddingBottom: safeArea().bottom }} testID="composer-dock"
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
          <View class="w-full rounded-(--radius-card) bg-card px-4 pb-1" testID="composer-surface">
          <View class="h-6" testID="composer-top-padding" />
          <View class="w-full flex-row items-end gap-3" testID="composer-input-row">
          <Input
            onPaste={(e) => {
              const files = (e.files ?? []).filter((f) => f.type.startsWith("image/"));
              if (files.length) { void pasteImages(files); return; }
              pasteText(e.text ?? "");
            }}
            pasteBehavior="report"
            ref={(n: any) => (ref = n)} multiline value={text()} testID="composer-input" accessibilityLabel="Message"
            placeholder={reason() ?? (centre() === "new_session" || !ses() ? "Message to start a new session" : "Type a message…")}
            readonly={!live()}
            submitBehavior="submit" onSubmit={() => {
              if (dictationPhase() === "recording") { void stopDictation(); return; }
              if (dictationPhase() !== "idle") return;
              void send();
            }}
            onChange={({ value: t }) => { selected.set(0); draft.update((d) => ({ ...d, [key()]: t })); prunePastes(key(), t); }}
            onFocus={() => { focused.set(true); composerFocused.set(true); flowStage.set(null); flowProject.set(null); }} onBlur={() => { focused.set(false); composerFocused.set(false); }}
            onSelectionChange={({ start, end }) => caret.set({ start, end })}
            onKeyPress={(e: { key: string }) => {
              if (String(e.key).toLowerCase() !== "escape") return;
              if (dictationPhase() !== "idle") { cancelDictation(); return; }
              if (open()) dismissed.set(text());
            }}
            onContentSizeChange={(s) => h.set(Math.min(220, Math.max(28, Math.ceil(s.size?.height ?? (s as any).height ?? 22) + 4)))}
            style={{ height: h() }}
            class={`flex-1 min-w-0 px-0 py-0 ${T.body} placeholder:text-muted-foreground`}
          />
            <View class="shrink-0 flex-row items-center gap-3 pb-1">
              <If when={dictationPhase() === "recording" || dictationPhase() === "stopping"}>
                <IconButton icon="stop" label={dictationPhase() === "recording" ? "Stop and insert transcript" : "Finishing transcription"}
                  testID="dictation-stop" disabled={dictationPhase() !== "recording"} onPress={() => void run("voice.dictate")}
                  class="bg-primary hover:bg-primary" tint="tint-on-accent" />
                <Btn label="Cancel" kind="ghost" onPress={cancelDictation} testID="dictation-cancel" />
              </If>
              <If when={dictationPhase() === "idle"}>
                <IconButton icon="mic" label="Dictate into composer" testID="dictation-start" disabled={!canDictate()}
                  onPress={() => void run("voice.dictate")} reason={!canDictate() ? (ses()?.archived ? "Unarchive this session first" : "Core is not connected") : undefined} />
              </If>
              <If when={dictationPhase() !== "idle" && dictationPhase() !== "recording" && dictationPhase() !== "stopping"}>
                <Btn label="Cancel dictation" kind="ghost" onPress={cancelDictation} testID="dictation-cancel-pending" />
              </If>
              <IconButton icon="arrow-up" label="Send message" testID="send" disabled={!canSend()} onPress={() => void send()} class={canSend() ? "bg-primary hover:bg-primary" : "bg-raised"} tint={canSend() ? "tint-on-accent" : undefined} reason={!canSend() ? (reason() ?? "Type a message first") : undefined} />
            </View>
          </View>
          <View class="h-4" />
          <View class="flex-row items-center gap-1 min-w-0 min-h-8">
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
            <If when={!!usageLabel() && !phone}><View class="shrink-0" testID="composer-meter"><Text style={{ numberOfLines: 1 }} class={T.meta} testID="composer-usage">{usageLabel()}</Text></View></If>
          </View>
          </View>
        </View>
        <Text accessible={false} style={{ numberOfLines: 1 }} class="w-full font-mono leading-6 text-card" testID="composer-bottom-edge">{structure.card_bottom.unicode.repeat(Math.max(1, Math.min(4096, Math.floor(dockWidth() / 9.6))))}</Text>
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
      class={`flex-row items-center gap-1.5 ${p.fixed ? "shrink-0" : "min-w-0 shrink"} h-7 ${p.alignStart ? "pl-0 pr-2" : "px-2"} rounded-(--radius) hover:bg-accent disabled:opacity-50 ${FOCUS}`}
    >
      <Text style={{ numberOfLines: 1 }} class={`min-w-0 shrink ${T.sec} ${p.labelClass ?? ""}`}>{p.label}</Text>
      <If when={!!p.sub}><Text style={{ numberOfLines: 1 }} class={`min-w-0 shrink-[2] ${T.meta}`}>{p.sub!}</Text></If>
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
 * width glides back and forth on a cosine, three seconds a sweep. At rest the `418K · 42%` text already
 * says how full the context is, so the line stays out of sight until the context is nearly full (80%, then
 * 95%) and then fills to it in the warning or error colour.
 */
function ActivityBar(p: { busy: boolean; fraction: number; slot: string }) {
  const phase = signal(0.5);
  const trackWidth = signal(0);
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  effect(() => {
    if (!p.busy || reduced()) { phase.set(0.5); return; }
    const start = Date.now();
    const id = setInterval(() => phase.set(((Date.now() - start) / 3000) % 1), 33);
    return () => clearInterval(id);
  });
  const left = () => 40 * (1 - Math.cos(phase() * Math.PI * 2));
  const full = () => !p.busy && p.fraction >= 0.8;
  const tone = () => (p.fraction >= 0.95 ? "bg-error" : "bg-warning");
  // The 1 px track is always laid out, so the dock does not move when the line appears.
  return <View class="w-full">
      <View class="relative w-full" testID="activity-bar" accessibilityLabel={p.busy ? "Working" : `Context ${Math.round(p.fraction * 100)}% used`} onLayout={(b: any) => trackWidth.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
        <Text accessible={false} style={{ numberOfLines: 1 }} class="w-full font-mono leading-6 text-border">{structure.rule.unicode.repeat(Math.max(1, Math.min(4096, Math.floor(trackWidth() / 9.6))))}</Text>
        <If when={p.busy}>
          <View class={`absolute -top-px h-0.5 ${AGENT_TEXT[p.slot] ?? ""}`} testID="activity-pulse" style={{ left: `${left()}%`, width: "20%", backgroundImage: "linear-gradient(90deg, transparent, currentColor, transparent)" }} />
        </If>
        <If when={full()}>
          <View class={`absolute -top-px h-0.5 left-0 ${tone()}`} testID="activity-fill" style={{ width: `${Math.min(100, p.fraction * 100)}%`, boxShadow: [{ x: 0, y: 0, blur: 4 }] }} />
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
  const output = () => {
    m();
    const u = turn();
    const sel = model.selected;
    if (!u || !sel) return 0;
    // The running turn: its loaded summary (joined mid-turn) completed by the live usage.
    const group = turnsIn(sel.turns, u.seq, Infinity);
    return sumUsage(groupRequests(group, sel.scratch)).output;
  };
  effect(() => {
    const id = setInterval(() => tick.set(Date.now()), 1000);
    return () => clearInterval(id);
  });
  return (
    <View class="shrink-0 flex-row items-center gap-2 h-7" testID="working-row">
      <Status kind="running" />
      <Shimmer text="Working" reduced={!!config()?.values?.ui?.reduced_motion} />
      <Text class={T.sec}>{`· ${fmtDuration(elapsed())}`}</Text>
      <Text class={T.sec} testID="working-output">{`· ↓${compact(output())}`}</Text>
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
  const stack = () => pendingInputs()[session()?.id ?? ""];
  const perform = async (id: string, action: "edit" | "remove" | "steer" | "up" | "down") => {
    const s = session(); if (!s) return;
    try {
      if (action === "remove") {
        const item = (stack()?.inputs ?? []).find((i: any) => i.id === id);
        const text = pendingLabel(item);
        const kind = item?.mode === "steer" ? "steer" : "queued message";
        const sid = s.id;
        confirmDialog.set({
          title: "Remove pending input",
          body: `Remove the ${kind} "${text}"?\n\nThis cannot be undone.`,
          confirm: "Remove", destructive: true,
          run: async () => { await request("DELETE", `/api/session/${sid}/input/${id}`); await loadInputs(sid); },
        });
        return;
      }
      if (action === "edit") {
        const k = draftKey();
        if ((draft()[k] ?? "").trim() || (imageDrafts()[k] ?? []).length) { toast("warning", "Clear the draft before restoring a pending input"); return; }
        const result = await request<any>("DELETE", `/api/session/${s.id}/input/${id}`);
        restorePendingParts(k, result.parts ?? result.input?.parts ?? []);
        focusComposer.fn?.();
      } else if (action === "steer") await request("PATCH", `/api/session/${s.id}/input/${id}`, { body: { mode: "steer" } });
      else {
        const inputs = stack()?.inputs ?? []; const at = inputs.findIndex((i) => i.id === id);
        const before = action === "up" ? inputs[at - 1]?.id : id;
        if (action === "up" && at <= 0 || action === "down" && at >= inputs.length - 1) return;
        const moving = action === "down" ? inputs[at + 1].id : id;
        await request("POST", `/api/session/${s.id}/input/${moving}/move`, { body: { before } });
      }
      await loadInputs(s.id);
    } catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
  };
  // Quiet ghost actions: icons that appear on hover (and on focus) on a pointer, always shown below the
  // text on touch and on a narrow window.
  const always = () => touch || isNarrow();
  const reveal = () => always() ? "" : "opacity-0 group-hover/pend:opacity-100 focus-visible:opacity-100";
  // Where there is no hover a tap on the row opens its actions, one row at a time.
  const opened = signal<string | null>(null);
  const PendingActions = (q: { i: () => { id: string; mode: string } }) => <>
    <IconButton icon="pencil" label="Edit" class={reveal()} testID={`pending-edit-${q.i().id}`} onPress={() => void perform(q.i().id, "edit")} />
    <IconButton icon="x" label="Remove" class={reveal()} testID={`pending-remove-${q.i().id}`} onPress={() => void perform(q.i().id, "remove")} />
    <If when={q.i().mode === "queue"}><Btn label="Steer" kind="ghost" class={reveal()} testID={`pending-steer-${q.i().id}`} onPress={() => void perform(q.i().id, "steer")} /></If>
    <IconButton icon="arrow-up" label="Move up" class={reveal()} testID={`pending-up-${q.i().id}`} onPress={() => void perform(q.i().id, "up")} />
    <IconButton icon="chevron-down" label="Move down" class={reveal()} testID={`pending-down-${q.i().id}`} onPress={() => void perform(q.i().id, "down")} />
  </>;
  return <If when={(stack()?.inputs.length ?? 0) > 0}><View class="mb-3" testID="pending-inputs">
    <View class="flex-row items-center min-h-6">
      <Text class={`flex-1 min-w-0 ${T.meta}`} style={{ numberOfLines: 1 }}>{stack()?.held ? "Queue held · enter steer · tab queue" : "Pending · enter steer · tab queue"}</Text>
      <If when={stack()?.held}><View class="-mr-3"><Btn label="Send next" kind="ghost" testID="pending-send-next" onPress={() => { const s = session(); if (s) void request("POST", `/api/session/${s.id}/input/next`, { body: {} }).then(() => loadInputs(s.id)).catch((e) => toast("error", String(e))); }} /></View></If>
    </View>
    <Each of={stack()?.inputs ?? []} key={(i) => i.id}>{(i) => <View class="group/pend relative" testID={`pending-${i().id}`}>
      <Button disabled={!always()} onPress={() => opened.set(opened() === i().id ? null : i().id)} selected={opened() === i().id}
        accessibilityLabel={`${i().mode === "steer" ? "Steer" : "Queued"}: ${pendingLabel(i())}`}
        class={`flex-row items-center gap-3 min-h-8 rounded-(--radius) ${FOCUS}`}>
        <Text class={`w-12 shrink-0 ${T.meta}`}>{i().mode === "steer" ? "steer" : "queued"}</Text>
        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${T.sec}`}>{pendingLabel(i())}</Text>
      </Button>
      <If when={always()} else={<View class="absolute right-0 top-0 bottom-0 flex-row items-center gap-0.5 pl-2 group-hover/pend:bg-sidebar"><PendingActions i={i} /></View>}>
        <If when={opened() === i().id}><View class="flex-row items-center justify-end gap-0.5"><PendingActions i={i} /></View></If>
      </If>
    </View>}</Each>
  </View></If>;
}

let resourcesTimer: ReturnType<typeof setInterval> | undefined;

/** The location strip shows the path, branch, live kernel/shell counts and linked-worktree icon. */
function LocationStrip(p: { working: boolean }) {
  // The row's measured width decides how much path fits; a fixed character count overflows a
  // narrow column and truncates needlessly in a wide one.
  const stripWidth = signal(0);
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
  effect(() => { bashJobs(); if (terminalAvailable && currentProject()) { void refreshTerminals(); void restoreTerminalDock(); } });
  const legacyShells = () => {
    const terminals = terminalItems();
    return viewResources().jobs.filter(j => j.status === "running" && !terminals.some(t => t.id === j.terminal_id || t.background_job_id === j.id)).length;
  };
  const session = () => { m(); return viewed(); };
  const directory = () => session()?.directory ?? project()?.path ?? "";
  const selectedTree = () => {
    const s = session(), p = currentProject();
    const listing = s ? sessionWorktrees()[s.id] : p ? projectWorktrees()[p] : null;
    return worktreeForDirectory(directory(), listing);
  };
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
    const chip = (c.kernels ? 70 : 0) + (legacyShells() ? 70 : 0) + (terminalItems().length ? 140 : 0);
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
      <View class="w-full flex-row items-center gap-2 min-w-0 min-h-7 pt-1 pb-1.5" testID="location-strip"
        onLayout={(b: any) => stripWidth.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
        {/* A phone keeps only the working status here: directory, branch, kernel and shell are desktop surfaces. */}
        <View class={phone ? "flex-1 w-0 overflow-hidden opacity-0" : "flex-1 w-0 min-w-0 overflow-hidden flex-row items-center gap-2"} accessible={!phone}>
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
          <If when={linked()}><Text testID="location-worktree" accessibilityLabel="Linked worktree" class={T.mono}>⑂</Text></If>
        </View>
        <View class="shrink-0 flex-row items-center gap-2" testID="location-indicators">
        <If when={resourceCounts().kernels > 0 && !phone}>
          <Button onPress={() => openResources("kernels")} accessibilityLabel="kernel" testID="location-resources"
            class={`rounded-(--radius) bg-muted px-2 min-h-6 justify-center hover:bg-secondary ${FOCUS}`}>
            <Text style={{ numberOfLines: 1 }} class="text-xs leading-4 text-foreground-secondary">kernel</Text>
          </Button>
        </If>
        <If when={legacyShells() > 0 && !phone}><Button onPress={() => openResources("shells")} accessibilityLabel={`${legacyShells()} shell`} testID="location-background-shell" class={`rounded-(--radius) bg-muted px-2 min-h-6 justify-center hover:bg-secondary ${FOCUS}`}><Text class="text-xs leading-4 text-foreground-secondary tabular-nums">{`${legacyShells()} shell`}</Text></Button></If>
        <If when={terminalAvailable && terminalItems().length > 0 && !phone}><Button onPress={() => void openTerminal()} selected={terminalOpen()} accessibilityLabel={`${terminalItems().length} terminal shell`} testID="location-shell" class={`rounded-(--radius) bg-muted px-2 min-h-6 justify-center hover:bg-secondary ${FOCUS}`}><Text class="text-xs leading-4 text-foreground-secondary tabular-nums">{`${terminalItems().length} terminal shell`}</Text></Button></If>
        </View>
        <View class="flex-1 w-0 min-w-0 flex-row items-center justify-end overflow-hidden"><If when={p.working}><WorkingRow /></If></View>
      </View>
    </If>
  );
}

// --- waiting card -----------------------------------------------------------------------------
const answers = signal<Record<string, { selected: string[]; text: string }>>({});
const busyWait = signal(false);
const waitError = signal<string | null>(null);

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
async function submitQuestion(q: Question) {
  busyWait.set(true); waitError.set(null);
  try {
    const items = q.questions.map((qq) => { const a = answers()[`${q.id}:${qq.id}`] ?? { selected: [], text: "" }; return { question_id: qq.id, selected: a.selected, text: a.text || null }; });
    await request("POST", `/api/session/${q.session_id}/question/${q.id}`, { body: { answers: items }, key: newKey() });
  } catch (e) { waitError.set(e instanceof ApiError ? e.message : String(e)); }
  busyWait.set(false);
}

export function WaitingCard() {
  focusWaiting.fn = () => {};
  const items = () => { m(); return waitItems(); };
  const cur = () => { const l = items(); return l.length ? l[waitIndex() % l.length] : null; };
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
  const args = a.args && typeof a.args === "object" ? Object.entries(a.args).filter(([k]) => !skip.includes(k)) : [];
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
  effect(() => { p.w().id; choice.set(0); });
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
    if (q && live()) bind("waiting.submit_answers", () => void submitQuestion(q));
    const offs: (() => void)[] = [];
    const letter = (key: string, fn: () => void) => { try { offs.push(keymap.bind(key, fn, { when: "notEditing" })); } catch { /* the platform keeps this keystroke */ } };
    if (a && live()) for (const row of approvalChoices(a)) { const key = byId.get(row.cmd)?.keys?.tui; if (key && row.run) letter(key, row.run); }
    const one = q?.questions[0];
    if (q && one && !one.multiple && live()) one.options.slice(0, 9).forEach((o, i) => letter(String(i + 1), () => answers.update((all) => ({ ...all, [`${q.id}:${one.id}`]: { selected: [o.id], text: all[`${q.id}:${one.id}`]?.text ?? "" } }))));
    const off = bindOpenKeys(map);
    return () => { off(); offs.forEach((f) => f()); };
  });
  const multi = () => question()?.questions.some((x) => x.multiple) ?? false;
  return (
    <View class="relative w-full mb-2 rounded-(--radius-card) bg-raised" testID="waiting-card" accessibilityLabel="Waiting for your input" role="alert">
      <StatusRail color={multi() ? "bg-multi" : "bg-primary"} testID="waiting-rail" />
      <If when={question()}><Text class={`${T.meta} pl-4 pr-4 pt-2`}>Scroll or Page Up/Down to read questions and choices</Text></If>
      <Scroll focusable ref={(n: any) => { focusWaiting.fn = () => { try { n?.focus(); } catch { /* not focusable here */ } }; }} testID="waiting-scroll" style={{ maxHeight: Math.round(windowSize().height * 0.6) }}>
        <View class="pl-4 pr-4 py-3 gap-2.5">
          <View>
          <View class="flex-row items-center gap-2 min-h-6">
            <Text class={`text-[16px] leading-6 ${multi() ? "text-multi" : "text-primary"}`}>◆</Text>
            <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[16px] leading-6 font-semibold text-foreground" testID="waiting-title">
              {approval() ? `Allow ${approval()!.tool}?` : "Nexus asks"}
            </Text>
            <If when={p.count > 1}>
              <Button onPress={() => run("waiting.next")} accessibilityLabel="Next waiting item" testID="waiting-next" class={`flex-row items-center gap-1.5 rounded-(--radius) px-1.5 hover:bg-accent ${FOCUS}`}>
                <Text class={T.meta}>{`${(waitIndex() % p.count) + 1} of ${p.count} ·`}</Text>
                <Text class={`${T.meta} rounded-(--radius) bg-muted px-1`}>{keyLabel(byId.get("waiting.next")!) ?? "next"}</Text>
                <Text class={T.meta}>next</Text>
              </Button>
            </If>
          </View>
          <Text class={`${T.sec} text-muted-foreground`} testID="waiting-sub">
            {`waiting on you · ${clock(elapsed())}${child() ? ` · in child: ${child()}` : ""}`}
          </Text>
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
  return (
    <View class="gap-2.5">
      {/* A phone shows a compact card: the command or path, not the parameter table or the diff. */}
      <If when={phone}><View class="rounded-(--radius) bg-background px-3 py-2" testID="waiting-compact"><Text style={{ numberOfLines: 4, userSelect: "text" }} class={`${T.mono} text-foreground`}>{compactApproval(a)}</Text></View></If>
      <If when={!phone && parts().table.length > 0}>
        <View class="rounded-(--radius) bg-background px-3 py-2 gap-0.5" testID="waiting-table">
          <Each of={parts().table} key={(r) => r[0]}>
            {(r) => (
              <View class="flex-row gap-3 min-h-5">
                <Text class={`w-16 shrink-0 ${T.meta}`}>{r()[0]}</Text>
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
      <If when={!busyWait()} else={<View class="flex-row items-center gap-2"><Status kind="running" /><Text class={T.sec}>sending…</Text></View>}>
        <View role="list" accessibilityLabel="Choices" class="gap-0.5">
          <Each of={rows()} key={(r) => r.id}>
            {(r, i) => {
              const off = () => !!r().why || (!!r().run && !live());
              const reason = () => r().why ?? (r().run && !live() ? "Core is not connected" : "");
              return (
                <Button onPress={() => r().run?.()} disabled={off()} testID={r().id}
                  accessibilityLabel={off() ? `${r().title}, unavailable: ${reason()}` : r().title}
                  class={`group/row relative flex-row items-center gap-2.5 rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "min-h-8"} ${i() === choice() && !off() ? "bg-selection" : ""} ${off() ? "" : "hover:bg-accent"} ${FOCUS}`}>
                  <If when={!phone}><Text class={`shrink-0 w-5 text-center rounded-(--radius) bg-muted text-[16px] leading-6 font-medium ${off() ? "text-muted-foreground" : "text-foreground"}`}>{byId.get(r().cmd)?.keys?.tui ?? ""}</Text></If>
                  <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 text-[16px] leading-6 ${off() ? "text-muted-foreground" : "text-foreground"}`}>{r().title}</Text>
                  <If when={off()}><Text testID={`${r().id}-reason`} class={T.meta}>{reason()}</Text></If>
                </Button>
              );
            }}
          </Each>
        </View>
        <AnswerHint />
      </If>
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
  const upd = (id: string, f: (a: { selected: string[]; text: string }) => { selected: string[]; text: string }) =>
    answers.update((all) => ({ ...all, [id]: f(all[id] ?? { selected: [], text: "" }) }));
  return (
    <View class="gap-3">
      <Each of={q.questions} key={(x) => x.id}>
        {(qq) => {
          const id = () => `${q.id}:${qq().id}`;
          const sel = (o: string) => (answers()[id()]?.selected ?? []).includes(o);
          return (
            <View class="gap-1.5">
              <Text class="text-[16px] leading-6 font-medium text-foreground">{qq().question}</Text>
              <Each of={qq().options} key={(o) => o.id}>
                {(o, n) => (
                  <Button role={qq().multiple ? "checkbox" : "radio"} selected={sel(o().id)} accessibilityLabel={[o().label, o().preview].filter(Boolean).join('. ')}
                    onPress={() => upd(id(), (a) => ({ ...a, selected: qq().multiple ? (a.selected.includes(o().id) ? a.selected.filter((x) => x !== o().id) : [...a.selected, o().id]) : [o().id] }))}
                    class={`flex-row items-center gap-2 rounded-(--radius) px-2.5 ${phone ? "min-h-11" : "min-h-8"} hover:bg-accent ${sel(o().id) ? "bg-selection" : ""} ${FOCUS}`}>
                    <If when={qq().multiple} else={<Text testID={`choice-mark-${o().id}`} class={`shrink-0 w-3.5 text-center text-[16px] leading-6 ${sel(o().id) ? "text-primary font-semibold" : "text-muted-foreground"}`}>{n() + 1}</Text>}><CheckMark on={sel(o().id)} testID={`choice-mark-${o().id}`} /></If>
                    <View class="flex-1 min-w-0 py-1">
                      <Text class={`text-[16px] leading-6 ${sel(o().id) ? "font-semibold text-foreground" : "text-foreground"}`}>{o().label}</Text>
                      <If when={o().preview}><Text class={`${T.sec} text-muted-foreground`}>{o().preview}</Text></If>
                    </View>
                  </Button>
                )}
              </Each>
              <Input value={answers()[id()]?.text ?? ""} placeholder={qq().options.length ? "Add context, or type your own answer" : "Type your answer"} onChange={({ value: t }) => upd(id(), (a) => ({ ...a, text: t }))} accessibilityLabel="Free-text answer" class={`rounded-(--radius) bg-background px-2.5 ${phone ? "h-11" : "h-8"} text-[16px] text-foreground`} />
            </View>
          );
        }}
      </Each>
      <View class="flex-row items-center gap-3">
        <Btn label="Submit answers" kind="primary" busy={busyWait()} onPress={() => void submitQuestion(q)} testID="submit-answers" disabled={!live()} />
        <If when={!phone}><Text class={T.meta}>{`${keyLabel(byId.get("waiting.submit_answers")!) ?? "enter"} submit · tab next question`}</Text></If>
        <AnswerHint />
      </View>
    </View>
  );
}
