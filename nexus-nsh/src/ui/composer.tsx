import { Image, Text, View, effect, signal, untracked } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, Icon, IconButton, FOCUS, Status, T, midTruncate } from "./kit";
import { availability, bindOpenKeys, byId, commands, draftKey, focusComposer, focusWaiting, keyLabel, otherWaits, run, runSlashCommand, sendPrompt, term, toKeystroke, waitItems } from "../commands";
import type { Cmd } from "../commands";
import type { FileEntry } from "../store";
import {
  agentCatalog, contextBudget, pendingInputs, loadInputs, loadPromptHistory, promptHistory, centre, config, connection, currentProject, defaultModel, draft, info, isNarrow, live, m, model, modelCatalog, tick,
  projectWorktrees, sessionDraft, sessionWorktrees, userHome, worktreeForDirectory, viewed, waitIndex, toast,
} from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { Approval, Question } from "../core/api";
import { agentColor, agentRailColor } from "../agent-color";
import { detailOf, fmtDuration } from "./timeline";
import { groupRequests, latestUsage, sumUsage, turnsIn } from "../core/turns";
import type { ToolDetailLine } from "./timeline";
import { DiffView, diffItems } from "./diff";

import { imageDrafts, readImage } from "../images";
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
  let ref: any;
  focusComposer.fn = () => { try { ref?.focus(); } catch {} };
  const h = signal(44);
  const key = () => draftKey();
  const text = () => draft()[key()] ?? "";
  const images = () => imageDrafts()[key()] ?? [];
  const pasting = signal(false);
  const appendImages = (target: string, added: import("../images").DraftImage[]) => {
    const all = [...(imageDrafts()[target] ?? []), ...added];
    const bytes = all.reduce((n, i) => n + (i.data_url.length - i.data_url.indexOf(",") - 1) * 3 / 4, 0);
    if (all.length > 8 || bytes > 8 * 1024 * 1024) throw new Error("At most eight images and 8 MiB per message");
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
    } catch (e) { toast("error", String(e)); }
    finally { pasting.set(false); }
  };
  const ses = () => viewed();
  const busy = () => { m(); return ses()?.status === "busy"; };
  const reason = (): string | null => {
    const c = connection();
    if (c.kind !== "live") return c.kind === "reconnecting" ? "Reconnecting to Core: read-only" : c.kind === "offline" ? "Core is offline: read-only" : c.kind === "auth_failed" ? "Authentication failed: read-only" : c.kind === "incompatible" ? "Incompatible API: blocked" : "Connecting to Core…";
    if (!currentProject() && centre() === "new_session") return "Add or choose a project first";
    if (ses()?.archived) return "This session is archived: unarchive it to continue";
    return null;
  };
  const canSend = () => dictationPhase() === "idle" && !reason() && !pasting() && (text().trim().length > 0 || images().length > 0);
  const canDictate = () => live() && !ses()?.archived;
  const send = async (mode: "send" | "steer" | "queue" = busy() ? "steer" : "send") => {
    const t = text().trim();
    if (t && !images().length && runSlashCommand(t)) { draft.update((d) => ({ ...d, [key()]: "" })); return; }
    if (!canSend()) { const r = reason(); if (r) toast("info", r); return; }
    const target = key();
    const sent = images();
    const ok = await sendPrompt(t, sent, mode);
    if (ok && ses()) void loadInputs(ses()!.id).catch(() => {});
    if (ok) { const p = currentProject(); if (p) void loadPromptHistory(p).catch(() => {}); }
    if (ok) {
      draft.update((d) => ({ ...d, [target]: (d[target] ?? "").trim() === t ? "" : d[target] ?? "" }));
      imageDrafts.update((d) => ({ ...d, [target]: (d[target] ?? []).filter((i) => !sent.includes(i)) }));
    }
  };
  const interrupted = () => term("session.interrupted");
  const agent = () => { m(); return ses()?.agent ?? sessionDraft().agent ?? "build"; };
  const modelRef = () => {
    m();
    const s = ses();
    const chosen = !s ? sessionDraft().model : undefined;
    if (s?.model) return String(s.model);
    if (chosen) return chosen;
    const entry = agentCatalog().agents.find((a) => a.name === agent());
    return entry?.model || defaultModel();
  };
  const modelParts = () => {
    const ref = modelRef();
    if (!ref) return { name: "default", provider: "" };
    const entry = modelCatalog().find((item) => item.ref === ref);
    if (!entry) return { name: ref.split("/").at(-1) ?? ref, provider: "" };
    return { name: entry.name || ref.split("/").at(-1) || ref, provider: entry.provider || ref.split("/")[0] };
  };
  const effort = () => {
    const s: any = ses();
    return s ? (s.effort || s.extra?.effort || "default") : (sessionDraft().effort || "default");
  };
  /** The latest request's input occupancy, as a count and a share of the model's window. */
  const usage = (): { input: number; pct: number | null } | null => {
    m();
    const id = model.selected?.sessionId;
    const budget = id ? contextBudget()[id] : null;
    if (!budget || !Number.isFinite(budget.used)) return null;
    return { input: budget.used, pct: budget.window && budget.window > 0 ? 100 * budget.used / budget.window : null };

  };
  const usageLabel = () => {
    const u = usage();
    if (!u) return "";
    const prefix = model.selected && contextBudget()[model.selected.sessionId]?.exact === false ? "~" : "";
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
  // Prompt history: Up in an empty composer recalls older prompts, Down returns toward the draft.
  // The caret position is not readable here, so Up and Down are taken only while the draft is empty or
  // is exactly the recalled entry.
  const recall = signal<{ at: number; scratch: string } | null>(null);
  const recalled = () => { const r = recall(); const e = promptHistory().entries; return r && e[r.at] === text() ? r : null; };
  effect(() => {
    const project = currentProject();
    viewed()?.id;
    if (project) void loadPromptHistory(project).catch(() => {});
  });
  effect(() => {
    if (!focused() || open()) return;
    const entries = promptHistory().entries;
    const set = (v: string) => draft.update((d) => ({ ...d, [key()]: v }));
    if (!entries.length || (text() !== "" && !recalled())) return;
    return bindOpenKeys({
      up: () => {
        const r = recalled();
        const at = r ? Math.min(entries.length - 1, r.at + 1) : 0;
        recall.set({ at, scratch: r ? r.scratch : text() });
        set(entries[at]);
      },
      down: () => {
        const r = recalled();
        if (!r) return;
        if (r.at === 0) { recall.set(null); set(r.scratch); return; }
        recall.set({ at: r.at - 1, scratch: r.scratch });
        set(entries[r.at - 1]);
      },
    });
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
    if (untracked(() => atListing()?.key) === k) return;
    const seq = ++atSeq;
    void request<{ entries: FileEntry[] }>("GET", `/api/project/${project}/files`, { query: { path: c.dir.replace(/\/$/, ""), session } })
      .then((d) => { if (seq === atSeq) atListing.set({ key: k, entries: d.entries ?? [] }); })
      .catch(() => { if (seq === atSeq) atListing.set({ key: k, entries: [] }); });
  });

  const agentName = () => capitalize(agent());
  return (
    <View class="px-4 pb-2 pt-3 items-center" testID="composer-area">
      <View class="w-full max-w-[760px]">
        <If when={busy()}><WorkingRow /></If>
        <PendingStack />
        <If when={open()}>
          {() => {
            const c = () => completion()!;
            const rows = () => c().rows.slice(0, MENU_LIMIT);
            return (
              <View class="w-full bg-popover rounded-t-[10px] pt-1.5 pb-1 overflow-hidden" testID="completion" accessibilityLabel="Completion">
                <Each of={rows()} key={(r) => r.key}>
                  {(r, i) => {
                    const sel = () => i() === Math.min(selected(), rows().length - 1);
                    return (
                      <Button onPress={() => accept(r())} selected={sel()} testID={`completion-${r().key}`}
                        class={`flex-row items-center gap-3 h-8 px-3 hover:bg-accent ${FOCUS}`}>
                        <If when={sel()}>{() => <View class="absolute left-0 top-1 bottom-1 w-0.5 rounded-full bg-primary" />}</If>
                        <Text style={{ numberOfLines: 1 }} class={`font-mono text-[12.5px] leading-5 font-medium ${r().muted ? "text-muted-foreground" : "text-foreground"}`}>{r().value}</Text>
                        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[12px] leading-4 text-muted-foreground">{r().muted ? `${r().help} · ${r().muted}` : r().help}</Text>
                        <If when={!!r().hint}><Text class="text-xs leading-4 text-muted-foreground">{r().hint ?? ""}</Text></If>
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
        <View
          class={`w-full rounded-(--radius-card) border bg-card px-4 pt-5 pb-3 ${focused() ? "border-ring" : "border-border"}`}
          accessibilityLabel="Composer" testID="composer-panel"
        >
          <If when={images().length > 0}>
            <View class="flex-row flex-wrap gap-2 pb-3" testID="image-previews">
              <Each of={images()} key={(image) => image.id}>
                {(image) => <View class="gap-1 w-28">
                  <Image src={image().data_url} style={{ width: 112, height: 84, objectFit: "contain" }} accessibilityLabel={image().name} />
                  <Text style={{ numberOfLines: 1 }} class={T.meta}>{image().name}</Text>
                  <Btn label="Remove image" onPress={() => imageDrafts.update((d) => ({ ...d, [key()]: images().filter((i) => i !== image()) }))} />
                </View>}
              </Each>
            </View>
          </If>
          <If when={pasting()}><Text class={T.meta}>Preparing image…</Text></If>
          <DictationStatus />
          <Input
            onPaste={(e) => void pasteImages((e.files ?? []).filter((f) => f.type.startsWith("image/")))}
            ref={(n: any) => (ref = n)} multiline value={text()} testID="composer-input" accessibilityLabel="Message"
            placeholder={reason() ?? (centre() === "new_session" ? "Message to start a new session" : "Type a message…")}
            readonly={!live() || !!ses()?.archived}
            submitBehavior="submit" onSubmit={() => {
              if (dictationPhase() === "recording") { void stopDictation(); return; }
              if (dictationPhase() !== "idle") return;
              void send();
            }}
            onChange={({ value: t }) => { selected.set(0); draft.update((d) => ({ ...d, [key()]: t })); }}
            onFocus={() => focused.set(true)} onBlur={() => focused.set(false)}
            onKeyPress={(e: { key: string }) => {
              if (String(e.key).toLowerCase() !== "escape") return;
              if (dictationPhase() !== "idle") { cancelDictation(); return; }
              if (open()) dismissed.set(text());
            }}
            onContentSizeChange={(s) => h.set(Math.min(220, Math.max(44, Math.ceil(s.size?.height ?? (s as any).height ?? 28) + 16)))}
            style={{ height: h() }}
            class="px-0 py-0 text-[14px] leading-[22px] text-foreground placeholder:text-muted-foreground"
          />
          <View class="h-2" />
          <View class="flex-row items-center gap-1 min-w-0 min-h-8">
            <View class="flex-1 min-w-0 flex-row items-center gap-1">
              <Selector command="agent.change" label={agentName()} dot={agentRailColor(agent())} labelClass={`font-semibold ${agentColor(agent())}`} testID="composer-agent" />
              <Selector command="model.change" label={modelParts().name} sub={isNarrow() || !modelParts().provider ? undefined : modelParts().provider} testID="composer-model" />
              <If when={!isNarrow()}>
                <Selector command="model.effort" label={effort()} testID="composer-effort" />
              </If>
              <If when={!busy() && interrupted()}>
                <Btn label="Continue" icon="play" onPress={() => run("session.continue")} disabled={!availability(byId.get("session.continue")!).ok} reason={availability(byId.get("session.continue")!).reason} testID="continue" />
              </If>
            </View>
            <View class="shrink-0 flex-row items-center gap-3">
              <If when={!!usageLabel() && !isNarrow()}>
                <View class="flex-row items-center gap-2" testID="composer-meter">
                  <If when={usage()?.pct !== null && usage()?.pct !== undefined}>
                    <View class="w-20 h-1 rounded-full bg-border overflow-hidden">
                      <View class={`h-full rounded-full ${agentRailColor(agent())}`} style={{ width: `${meterPct()}%` }} />
                    </View>
                  </If>
                  <Text style={{ numberOfLines: 1 }} class={T.meta} testID="composer-usage">{usageLabel()}</Text>
                </View>
              </If>
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
        </View>
        <LocationStrip />
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
              <Text class="flex-1 text-[13px] leading-5 text-foreground">Downloading speech recognition model…</Text>
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
            <Text class="text-[13px] leading-5 font-medium text-foreground">{phase() === "stopping" ? "Finalizing transcript…" : "Listening…"}</Text>
            <View class="h-1.5 w-20 overflow-hidden rounded-full bg-border" testID="dictation-level-meter">
              <View class="h-full rounded-full bg-primary" style={{ width: `${level()}%` }} />
            </View>
            <Text class={T.meta}>{reducedMotion() ? "Reduced motion" : "Mic level"}</Text>
          </View>
          <If when={!!dictationPreview().trim()}>
            <Text testID="dictation-preview" style={{ userSelect: "text" }} class="rounded-(--radius) bg-muted px-3 py-2 text-[13px] leading-5 text-foreground-secondary italic">
              {`Pending transcript · ${dictationPreview()}`}
            </Text>
          </If>
          <Text class={T.meta}>Enter or Stop inserts editable text · Esc cancels</Text>
        </If>
      </View>
    </If>
  );
}

/** `82k`, `11.1k`, `1.2M`: the composer's compact counts. */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
function capitalize(s: string): string { return s ? `${s[0].toUpperCase()}${s.slice(1)}` : "Build"; }

/** A pill selector on the control row: an optional dot, the label, a muted sub-label and a chevron. */
function Selector(p: { command: string; label: string; dot?: string; labelClass?: string; sub?: string; testID: string }) {
  const c = byId.get(p.command);
  const state = () => c ? availability(c) : { ok: false, reason: "Command is unavailable." };
  const disabled = () => !state().ok;
  const reason = () => state().reason;
  return (
    <Button
      onPress={() => void run(p.command)} disabled={disabled()} testID={p.testID}
      accessibilityLabel={`${p.label}${disabled() ? `, unavailable: ${reason()}` : ""}`}
      tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{disabled() ? `${p.label}: ${reason()}` : p.label}</Text>}
      class={`flex-row items-center gap-1.5 min-w-0 shrink h-7 px-2 rounded-(--radius) hover:bg-accent disabled:opacity-50 ${FOCUS}`}
    >
      <If when={!!p.dot}>{() => <View class={`size-2 shrink-0 rounded-full ${p.dot}`} />}</If>
      <Text style={{ numberOfLines: 1 }} class={`text-[13px] leading-5 ${p.labelClass ?? "text-foreground-secondary"}`}>{p.label}</Text>
      <If when={!!p.sub}><Text style={{ numberOfLines: 1 }} class="text-[13px] leading-5 text-muted-foreground">{p.sub!}</Text></If>
      <Icon name="chevron-down" size={12} class="tint-muted-foreground" />
    </Button>
  );
}

/** `◐ Working · 12s · ↓4.1k` above the card while the session runs; ticks once a second. */
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
    <View class="flex-row items-center gap-2 h-7 mb-1 px-1" testID="working-row">
      <Status kind="running" />
      <Text class="text-[13px] leading-5 font-semibold text-foreground">Working</Text>
      <Text class={T.sec}>{`· ${fmtDuration(elapsed())}`}</Text>
      <Text class={T.sec} testID="working-output">{`· ↓${compact(output())}`}</Text>
      <View class="flex-1" />

    </View>
  );
}

function PendingStack() {
  const session = () => viewed();
  const stack = () => pendingInputs()[session()?.id ?? ""];
  const perform = async (id: string, action: "remove" | "steer" | "up" | "down") => {
    const s = session(); if (!s) return;
    try {
      if (action === "remove") {
        const k = draftKey();
        if ((draft()[k] ?? "").trim() || (imageDrafts()[k] ?? []).length) { toast("warning", "Clear the draft before restoring a pending input"); return; }
        const result = await request<any>("DELETE", `/api/session/${s.id}/input/${id}`);
        const parts = result.parts ?? result.input?.parts ?? [];
        const text = parts.filter((p: any) => p.type === "text" && !/^\[Image [A-Z]\]$/.test(p.text ?? "")).map((p: any) => p.text).join("\n");
        const images = parts.filter((p: any) => p.type === "image").map((p: any, i: number) => ({ id: `restored-${id}-${i}`, name: p.name ?? "image.png", data_url: p.data_url }));
        draft.update((d) => ({ ...d, [k]: [text, d[k] ?? ""].filter(Boolean).join("\n") })); imageDrafts.update((d) => ({ ...d, [k]: [...images, ...(d[k] ?? [])] })); focusComposer.fn?.();
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
  return <If when={(stack()?.inputs.length ?? 0) > 0}><View class="mb-2 gap-1" testID="pending-inputs">
    <Text class={T.meta}>{stack()?.held ? "Queue held · enter steer · tab queue" : "Pending · enter steer · tab queue"}</Text>
    <Each of={stack()?.inputs ?? []} key={(i) => i.id}>{(i) => <View class="flex-row items-center gap-1" testID={`pending-${i().id}`}>
      <Text class={`flex-1 ${T.sec}`}>{`${i().mode === "steer" ? "steer" : "queued"} · ${i().parts.find((p: any) => p.type === "text")?.text?.split("\n")[0] ?? "[image]"}`}</Text>
      <Btn label="Edit" onPress={() => void perform(i().id, "remove")} /><Btn label="Remove" onPress={() => void perform(i().id, "remove")} />
      <If when={i().mode === "queue"}><Btn label="Steer" onPress={() => void perform(i().id, "steer")} /></If>
      <Btn label="↑" onPress={() => void perform(i().id, "up")} /><Btn label="↓" onPress={() => void perform(i().id, "down")} />
    </View>}</Each>
    <If when={stack()?.held}><Btn label="Send next" onPress={() => { const s = session(); if (s) void request("POST", `/api/session/${s.id}/input/next`, { body: {} }).then(() => loadInputs(s.id)).catch((e) => toast("error", String(e))); }} /></If>
  </View></If>;
}

/** The location strip shows the path, branch and linked-worktree icon. */
function LocationStrip() {
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
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
    const home = userHome() || (stateHome.endsWith("/.nexus10") ? stateHome.slice(0, -9) : "");
    const short = home && (dir === home || dir.startsWith(`${home.replace(/\/$/, "")}/`)) ? `~${dir.slice(home.replace(/\/$/, "").length)}` : dir;
    return midTruncate(short, isNarrow() ? 32 : 54);
  };
  const directoryAvailability = () => {
    const c = byId.get("session.change_directory");
    return c ? availability(c) : { ok: false, reason: "Directory changes are not available in NSH." };
  };
  const worktreeAvailability = () => {
    const c = byId.get("session.worktrees");
    return c ? availability(c) : { ok: false, reason: "Worktree controls are not available in NSH." };
  };
  const tooltip = (text: string) => <Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{text}</Text>;
  return (
    <If when={project() && directory()}>
      <View class="w-full flex-row items-center gap-2 min-h-7 pt-1.5 px-1 min-w-0" testID="location-strip">
        <If when={!!session()}>
          <Button
            disabled={!directoryAvailability().ok} onPress={() => void run("session.change_directory")} testID="location-directory"
            accessibilityLabel={`Directory ${path()}, unavailable: ${directoryAvailability().reason ?? ""}`}
            tooltip={tooltip(directoryAvailability().reason ?? "Change session directory")}
            class={`min-w-0 shrink rounded-(--radius) disabled:opacity-50 ${FOCUS}`}
          ><Text style={{ numberOfLines: 1 }} class={`${T.meta} min-w-0`}>{path()}</Text></Button>
        </If>
        <If when={!session()}><Text testID="location-directory" style={{ numberOfLines: 1 }} class={`min-w-0 shrink ${T.meta}`}>{path()}</Text></If>
        <If when={!!branch()}>
          <If when={!!session()} else={<Text style={{ numberOfLines: 1 }} class={`${T.sec} shrink`}>{`⌥ ${branch()!}`}</Text>}>
            <Button
              disabled={!worktreeAvailability().ok} onPress={() => void run("session.worktrees")} testID="location-branch"
              accessibilityLabel={`Branch ${branch()}, unavailable: ${worktreeAvailability().reason ?? ""}`}
              tooltip={tooltip(worktreeAvailability().reason ?? "Worktree controls")}
              class={`min-w-0 shrink rounded-(--radius) disabled:opacity-50 ${FOCUS}`}
            ><Text style={{ numberOfLines: 1 }} class={T.sec}>{`⌥ ${branch()!}`}</Text></Button>
          </If>
        </If>
        <If when={linked()}><View testID="location-worktree" accessibilityLabel="Linked worktree"><Icon name="branch" size={14} class="tint-muted-foreground" /></View></If>
        <View class="flex-1" />
      </View>
    </If>
  );
}

// --- waiting card -----------------------------------------------------------------------------
const answers = signal<Record<string, { selected: string[]; text: string }>>({});
const busyWait = signal(false);
const waitError = signal<string | null>(null);

async function reply(a: Approval, allow: boolean) {
  busyWait.set(true); waitError.set(null);
  try { await request("POST", `/api/session/${a.session_id}/permission/${a.id}`, { body: { reply: allow ? "allow" : "deny" }, key: newKey() }); }
  catch (e) { waitError.set(e instanceof ApiError ? e.message : String(e)); }
  busyWait.set(false);
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
  const others = () => { m(); return otherWaits(); };
  return (
    <View class="px-4 items-center" testID="waiting-slot">
      <If when={others().length > 0 && items().length === 0}>
        <View class="w-full max-w-[760px] flex-row items-center gap-2 rounded-(--radius-card) border border-border bg-card px-3 py-2 mb-2">
          <Status kind="waiting" />
          <Text class={`flex-1 ${T.sec}`}>{`${others().length} other session${others().length === 1 ? " is" : "s are"} waiting for input`}</Text>
          <Btn label="Go to" onPress={() => run("waiting.go_to")} testID="waiting-go-to" />
        </View>
      </If>
      <If when={cur()}>{(w) => <WaitBody w={w} count={items().length} />}</If>
    </View>
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
    if (a && live()) bind("waiting.deny", () => void reply(a, false));
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
    return bindOpenKeys(map);
  });
  return (
    <View class="w-full max-w-[760px] mb-2 flex-row rounded-(--radius-card) bg-card overflow-hidden" testID="waiting-card" accessibilityLabel="Waiting for your input" role="alert">
      <View class="w-[3px] self-stretch bg-primary" testID="waiting-rail" />
      <View class="flex-1 min-w-0 gap-2.5 px-3.5 py-3">
        <View>
          <View class="flex-row items-center gap-2 min-h-6">
            <Text class="text-[15px] leading-5 text-primary">◆</Text>
            <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[15px] leading-5 font-semibold text-foreground" testID="waiting-title">
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
            {`waiting on you · ${clock(elapsed())}${question() ? ` · question 1 of ${question()!.questions.length}` : ""}${child() ? ` · in child: ${child()}` : ""}`}
          </Text>
        </View>
        <If when={approval()}>{() => <ApprovalBody a={approval()!} />}</If>
        <If when={question()}>{() => <QuestionBody q={question()!} />}</If>
        <If when={waitError()}><View class="flex-row gap-1.5 items-center"><Status kind="error" /><Text class="text-[13px] leading-5 text-error">{waitError()!}</Text></View></If>
      </View>
    </View>
  );
}

type Choice = { id: string; title: string; cmd: string; run?: () => void; why?: string };
const approvalChoices = (a: Approval): Choice[] => [
  { id: "allow-once", title: "Allow once", cmd: "waiting.allow_once", run: () => void reply(a, true) },
  { id: "allow-session", title: "Allow for session", cmd: "waiting.allow_session", why: "not offered by Core yet" },
  { id: "deny", title: "Deny", cmd: "waiting.deny", run: () => void reply(a, false) },
  { id: "deny-note", title: "Deny with a note", cmd: "waiting.deny_note", why: "Core does not support notes yet" },
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
      <If when={parts().table.length > 0}>
        <View class="rounded-(--radius) bg-background px-3 py-2 gap-0.5" testID="waiting-table">
          <Each of={parts().table} key={(r) => r[0]}>
            {(r) => (
              <View class="flex-row gap-3 min-h-5">
                <Text class="w-16 shrink-0 text-xs leading-5 text-muted-foreground">{r()[0]}</Text>
                <Text style={{ numberOfLines: 3, userSelect: "text" }} class="flex-1 min-w-0 font-mono text-[13px] leading-5 text-foreground">{r()[1]}</Text>
              </View>
            )}
          </Each>
        </View>
      </If>
      <If when={parts().diff.length > 0}>
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
                  class={`group/row relative flex-row items-center gap-2.5 rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "min-h-8"} ${off() ? "" : "hover:bg-accent"} ${FOCUS}`}>
                  <Text class={`w-3 text-[13px] leading-5 ${off() ? "text-transparent" : "text-primary"}`}>{i() === choice() && !off() ? "▸" : " "}</Text>
                  <Text style={{ numberOfLines: 1 }} class={`w-16 shrink-0 text-center text-xs leading-5 font-bold rounded-(--radius) px-1.5 ${off() ? "bg-muted text-muted-foreground" : "bg-raised text-foreground"}`}>{keyLabel(byId.get(r().cmd)!) ?? ""}</Text>
                  <Text class={`text-[13px] leading-5 ${off() ? "text-muted-foreground" : r().id === "allow-once" ? "font-semibold text-foreground" : "text-foreground"}`}>{r().title}</Text>
                  <If when={off()}><Text testID={`${r().id}-reason`} class={`flex-1 min-w-0 ${T.meta}`}>{reason()}</Text></If>
                </Button>
              );
            }}
          </Each>
        </View>
      </If>
    </View>
  );
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
              <Text class="text-[14px] leading-[22px] font-medium text-foreground">{qq().question}</Text>
              <Each of={qq().options} key={(o) => o.id}>
                {(o) => (
                  <Button role={qq().multiple ? "checkbox" : "radio"} selected={sel(o().id)} accessibilityLabel={o().label}
                    onPress={() => upd(id(), (a) => ({ ...a, selected: qq().multiple ? (a.selected.includes(o().id) ? a.selected.filter((x) => x !== o().id) : [...a.selected, o().id]) : [o().id] }))}
                    class={`flex-row items-center gap-2 rounded-(--radius) border border-border px-2.5 min-h-8 hover:bg-accent selected:border-ring ${FOCUS}`}>
                    <Text class="text-[13px] text-foreground-secondary w-4">{qq().multiple ? (sel(o().id) ? "[x]" : "[ ]") : sel(o().id) ? "(•)" : "( )"}</Text>
                    <Text class="flex-1 text-[13px] leading-5 text-foreground">{o().label}</Text>
                  </Button>
                )}
              </Each>
              <If when={qq().allow_free_text}>
                <Input placeholder="Or type an answer" onChange={({ value: t }) => upd(id(), (a) => ({ ...a, text: t }))} accessibilityLabel="Free-text answer" class="rounded-(--radius) border border-border bg-background px-2.5 h-8 text-[13px] text-foreground" />
              </If>
            </View>
          );
        }}
      </Each>
      <View class="flex-row items-center gap-3">
        <Btn label="Submit answers" kind="primary" busy={busyWait()} onPress={() => void submitQuestion(q)} testID="submit-answers" disabled={!live()} />
        <Text class={T.meta}>{`${keyLabel(byId.get("waiting.submit_answers")!) ?? "enter"} submit · tab next question`}</Text>
      </View>
    </View>
  );
}
