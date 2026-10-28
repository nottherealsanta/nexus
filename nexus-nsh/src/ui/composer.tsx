import { Text, View, effect, signal, untracked } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, Icon, IconButton, FOCUS, Status, T, midTruncate } from "./kit";
import { availability, bindOpenKeys, byId, commands, draftKey, focusComposer, focusWaiting, keyLabel, otherWaits, run, runSlashCommand, sendPrompt, term, toKeystroke, waitItems } from "../commands";
import type { Cmd } from "../commands";
import type { FileEntry } from "../store";
import {
  agentCatalog, centre, connection, currentProject, defaultModel, draft, info, isNarrow, live, m, model, modelCatalog, tick,
  projectWorktrees, sessionDraft, sessionWorktrees, userHome, worktreeForDirectory, viewed, waitIndex, toast,
} from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { Approval, Question } from "../core/api";
import { agentColor, agentRailColor } from "../agent-color";
import { detailOf, fmtDuration } from "./timeline";
import type { ToolDetailLine } from "./timeline";
import { DiffView, diffItems } from "./diff";

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
  const ses = () => viewed();
  const busy = () => { m(); return ses()?.status === "busy"; };
  const reason = (): string | null => {
    const c = connection();
    if (c.kind !== "live") return c.kind === "reconnecting" ? "Reconnecting to Core: read-only" : c.kind === "offline" ? "Core is offline: read-only" : c.kind === "auth_failed" ? "Authentication failed: read-only" : c.kind === "incompatible" ? "Incompatible API: blocked" : "Connecting to Core…";
    if (!currentProject() && centre() === "new_session") return "Add or choose a project first";
    if (ses()?.archived) return "This session is archived: unarchive it to continue";
    if (busy()) return "Steering and queueing need Core (PLAN.md Step 12a); a prompt while busy is refused";
    return null;
  };
  const canSend = () => !reason() && text().trim().length > 0;
  const send = async () => {
    const t = text().trim();
    if (t && runSlashCommand(t)) { draft.update((d) => ({ ...d, [key()]: "" })); return; }
    if (!canSend()) { const r = reason(); if (r) toast("info", r); return; }
    const ok = await sendPrompt(t);
    if (ok) draft.update((d) => ({ ...d, [key()]: "" }));
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
    const events = model.selected?.events ?? [];
    const latest = [...events].reverse().find((event) => event.type === "usage");
    const input = Number(latest?.data?.input_tokens);
    if (!Number.isFinite(input) || input < 0) return null;
    const ref = modelRef();
    const entry = modelCatalog().find((item) => item.ref === ref);
    const window = Number(entry?.context);
    return { input, pct: Number.isFinite(window) && window > 0 ? (100 * input) / window : null };
  };
  const usageLabel = () => {
    const u = usage();
    if (!u) return "";
    if (u.pct === null) return compact(u.input);
    return `${compact(u.input)} · ${u.pct >= 10 ? Math.round(u.pct) : u.pct.toFixed(1)}%`;
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
    <View class="px-4 pb-2 pt-1 items-center" testID="composer-area">
      <View class="w-full max-w-[760px]">
        <If when={busy()}><WorkingRow /></If>
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
                        class={`flex-row items-center gap-3 h-8 px-3 hover:bg-accent selected:bg-selection ${FOCUS}`}>
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
          class={`w-full rounded-(--radius-card) border bg-card px-4 py-3 ${focused() ? "border-ring" : "border-border"}`}
          accessibilityLabel="Composer" testID="composer-panel"
        >
          <Input
            ref={(n: any) => (ref = n)} multiline value={text()} testID="composer-input" accessibilityLabel="Message"
            placeholder={reason() ?? (centre() === "new_session" ? "Message to start a new session" : "Type a message…")}
            readonly={!live() || !!ses()?.archived}
            submitBehavior="submit" onSubmit={() => void send()}
            onChange={({ value: t }) => { selected.set(0); draft.update((d) => ({ ...d, [key()]: t })); }}
            onFocus={() => focused.set(true)} onBlur={() => focused.set(false)}
            onKeyPress={(e: { key: string }) => { if (String(e.key).toLowerCase() === "escape" && open()) dismissed.set(text()); }}
            onContentSizeChange={(s) => h.set(Math.min(220, Math.max(44, Math.ceil(s.size.height) + 16)))}
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
              <IconButton icon="arrow-up" label="Send message" testID="send" disabled={!canSend()} onPress={() => void send()} class={canSend() ? "bg-primary hover:bg-primary" : "bg-raised"} tint={canSend() ? "tint-on-accent" : undefined} reason={!canSend() ? (reason() ?? "Type a message first") : undefined} />
            </View>
          </View>
        </View>
        <LocationStrip />
      </View>
    </View>
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
    if (!u) return 0;
    return (model.selected?.events ?? []).reduce((sum, e) => {
      if (e.type !== "usage" || !(e.time >= u.created)) return sum;
      const n = Number(e.data?.output_tokens);
      return sum + (Number.isFinite(n) && n > 0 ? n : 0);
    }, 0);
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
      <Text class={T.meta}>steering needs Core (Step 12a)</Text>
      <Btn label="Stop" icon="stop" onPress={() => run("session.interrupt")} disabled={!availability(byId.get("session.interrupt")!).ok} reason={availability(byId.get("session.interrupt")!).reason} testID="interrupt" />
    </View>
  );
}

/** The project strip under the card: project, home-shortened path, branch, checkout or worktree; palette hint when idle. */
function LocationStrip() {
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
  const session = () => { m(); return viewed(); };
  const busy = () => { m(); return session()?.status === "busy"; };
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
  const paletteHint = () => {
    const c = byId.get("palette.open");
    return `${c ? keyLabel(c) ?? "cmd+p" : "cmd+p"} commands`;
  };
  const tooltip = (text: string) => <Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{text}</Text>;
  return (
    <If when={project() && directory()}>
      <View class="w-full flex-row items-center gap-2 min-h-7 pt-1.5 px-1 min-w-0" testID="location-strip">
        <Text style={{ numberOfLines: 1 }} class="shrink-0 max-w-[30%] text-[13px] leading-5 font-semibold text-foreground">{project()?.name ?? ""}</Text>
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
        <If when={linked()} else={<Text testID="location-checkout" class={`shrink-0 ${T.meta}`}>checkout</Text>}>
          <Text testID="location-worktree" class="shrink-0 text-[12px] leading-4 font-bold text-agent-violet">worktree</Text>
        </If>
        <View class="flex-1" />
        <If when={!busy()}><Text class={T.meta}>{paletteHint()}</Text></If>
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
  effect(() => {
    const k = (id: string) => { const key = byId.get(id)?.keys?.nsh; return key ? toKeystroke(key) : null; };
    const map: Record<string, () => void> = {};
    const bind = (id: string, fn: () => void) => { const key = k(id); if (key && /(cmd|ctrl|alt|mod)\+/.test(key)) map[key] = fn; };
    if (p.count > 1) { bind("waiting.next", () => run("waiting.next")); bind("waiting.prev", () => run("waiting.prev")); }
    const a = approval(), q = question();
    if (a && live()) bind("waiting.deny", () => void reply(a, false));
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

function ApprovalBody(p: { a: Approval }) {
  const a = p.a;
  const parts = () => approvalParts(a);
  const rows = (): { id: string; title: string; cmd: string; run?: () => void; why?: string }[] => [
    { id: "allow-once", title: "Allow once", cmd: "waiting.allow_once", run: () => void reply(a, true) },
    { id: "allow-session", title: "Allow for session", cmd: "waiting.allow_session", why: "not offered by Core yet" },
    { id: "deny", title: "Deny", cmd: "waiting.deny", run: () => void reply(a, false) },
    { id: "deny-note", title: "Deny with a note", cmd: "waiting.deny_note", why: "Core does not support notes yet" },
  ];
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
                  class={`group/row relative flex-row items-center gap-2.5 rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "min-h-8"} ${i() === 0 && !off() ? "bg-selection" : ""} ${off() ? "" : "hover:bg-selection"} ${FOCUS}`}>
                  <Text class={`w-3 text-[13px] leading-5 ${off() ? "text-transparent" : "text-primary"}`}>{i() === 0 && !off() ? "▸" : " "}</Text>
                  <Text class={`w-10 shrink-0 text-center text-xs leading-5 font-bold rounded-(--radius) px-1.5 ${off() ? "bg-muted text-muted-foreground" : "bg-raised text-foreground"}`}>{keyLabel(byId.get(r().cmd)!) ?? ""}</Text>
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
                    class={`flex-row items-center gap-2 rounded-(--radius) border border-border px-2.5 min-h-8 hover:bg-accent selected:bg-selection selected:border-ring ${FOCUS}`}>
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
