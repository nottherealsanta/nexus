import { Text, View, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, Icon, IconButton, Status, T, FOCUS, midTruncate } from "./kit";
import { availability, byId, draftKey, focusComposer, focusWaiting, otherWaits, run, sendPrompt, term, waitItems } from "../commands";
import {
  agentCatalog, centre, connection, currentProject, defaultModel, draft, info, isNarrow, live, m, model,
  modelCatalog, projectWorktrees, sessionWorktrees, userHome, worktreeForDirectory, viewed, waitIndex, toast,
} from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { Approval, Question } from "../core/api";
import { agentColor } from "../agent-color";

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
    if (!canSend()) { const r = reason(); if (r) toast("info", r); return; }
    const t = text();
    const ok = await sendPrompt(t);
    if (ok) draft.update((d) => ({ ...d, [key()]: "" }));
  };
  const interrupted = () => term("session.interrupted");
  const agent = () => { m(); return ses()?.agent ?? "build"; };
  const modelRef = () => {
    m();
    const s = ses();
    if (s?.model) return String(s.model);
    const entry = agentCatalog().agents.find((a) => a.name === (s?.agent ?? "build"));
    return entry?.model || defaultModel();
  };
  const modelLabel = () => {
    const ref = modelRef();
    if (!ref) return "default";
    const entry = modelCatalog().find((item) => item.ref === ref);
    if (!entry) return ref.split("/").at(-1) ?? ref;
    const name = entry.name || ref.split("/").at(-1) || ref;
    const provider = entry.provider || ref.split("/")[0];
    return `${name}${provider && !isNarrow() ? ` ${provider}` : ""}`;
  };
  const effort = () => {
    const s: any = ses();
    return s?.effort || s?.extra?.effort || "default";
  };
  const usageLabel = () => {
    m();
    const events = model.selected?.events ?? [];
    const latest = [...events].reverse().find((event) => event.type === "usage");
    const input = Number(latest?.data?.input_tokens);
    if (!Number.isFinite(input) || input < 0) return "";
    const ref = modelRef();
    const entry = modelCatalog().find((item) => item.ref === ref)
      ?? modelCatalog().find((item) => String(item.ref ?? "").split("/").at(-1) === ref.split("/").at(-1));
    const values = [composerCount(input)];
    const window = Number(entry?.context);
    const tiers: number[] = Array.isArray(entry?.tiers) ? [...new Set<number>((entry.tiers as unknown[]).map(Number))]
      .filter((tier) => Number.isFinite(tier) && tier > 0 && tier !== window)
      .sort((a, b) => a - b) : [];
    if (!isNarrow()) values.push(...tiers.map((tier) => `${(100 * input / tier).toFixed(1)}%`));
    if (Number.isFinite(window) && window > 0) values.push(`${(100 * input / window).toFixed(1)}%`);
    return values.join(" · ");
  };
  return (
    <View class="px-3 pb-2 pt-1 items-center" testID="composer-area">
      <View class="w-full max-w-[792px] bg-card px-3 pt-3 pb-4" accessibilityLabel="Composer" testID="composer-panel">
        <Input
          ref={(n: any) => (ref = n)} multiline value={text()} testID="composer-input" accessibilityLabel="Message"
          placeholder={reason() ?? (centre() === "new_session" ? "Message to start a new session" : "Type a message…")}
          readonly={!live() || !!ses()?.archived}
          submitBehavior="submit" onSubmit={() => void send()} onChange={(t) => draft.update((d) => ({ ...d, [key()]: t }))}
          onContentSizeChange={(s) => h.set(Math.min(220, Math.max(44, Math.ceil(s.height) + 16)))}
          style={{ height: h() }}
          class="px-1 py-1 text-[14px] leading-[22px] text-foreground placeholder:text-muted-foreground"
        />
        <View class="h-2" />
        <View class="flex-row items-center gap-2 min-w-0 min-h-8">
          <View class="flex-1 min-w-0 flex-row items-center gap-1">
            <Selector command="agent.change" label={capitalize(agent())} color={agentColor(agent())} testID="composer-agent" />
            <Text class={T.meta}>·</Text>
            <Selector command="model.change" label={modelLabel()} testID="composer-model" />
            <If when={!isNarrow()}>
              <Text class={T.meta}>·</Text>
              <Selector command="model.effort" label={effort()} muted testID="composer-effort" />
            </If>
            <If when={busy()}>
              <Text class={T.meta}>· esc</Text>
              <Btn label="Stop" icon="stop" onPress={() => run("session.interrupt")} disabled={!availability(byId.get("session.interrupt")!).ok} reason={availability(byId.get("session.interrupt")!).reason} testID="interrupt" />
            </If>
            <If when={!busy() && interrupted()}>
              <Text class={T.meta}>·</Text>
              <Btn label="Continue" icon="play" onPress={() => run("session.continue")} disabled={!availability(byId.get("session.continue")!).ok} reason={availability(byId.get("session.continue")!).reason} testID="continue" />
            </If>
          </View>
          <View class="shrink-0 flex-row items-center gap-2">
            <If when={!!usageLabel()}><Text class={T.meta} numberOfLines={1} testID="composer-usage">{usageLabel()}</Text></If>
            <IconButton icon="arrow-up" label="Send message" testID="send" disabled={!canSend()} onPress={() => void send()} class={canSend() ? "bg-primary hover:bg-primary" : ""} tint={canSend() ? "tint-on-accent" : undefined} reason={!canSend() ? (reason() ?? "Type a message first") : undefined} />
          </View>
        </View>
      </View>
      <LocationStrip />
    </View>
  );
}

function composerCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}
function capitalize(s: string): string { return s ? `${s[0].toUpperCase()}${s.slice(1)}` : "Build"; }
function Selector(p: { command: string; label: string; color?: string; muted?: boolean; testID: string }) {
  const c = byId.get(p.command);
  const state = () => c ? availability(c) : { ok: false, reason: "Command is unavailable." };
  const disabled = () => !viewed() || !state().ok;
  const reason = () => !viewed() ? "Open a session first." : state().reason;
  return (
    <Button
      onPress={() => void run(p.command)} disabled={disabled()} testID={p.testID}
      accessibilityLabel={`${p.label}${disabled() ? `, unavailable: ${reason()}` : ""}`}
      tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{disabled() ? `${p.label}: ${reason()}` : p.label}</Text>}
      class={`min-w-0 shrink rounded-(--radius) px-1 py-1 hover:bg-accent disabled:opacity-50 ${FOCUS}`}
    >
      <Text numberOfLines={1} class={`text-[12px] leading-4 ${p.color ?? (p.muted ? "text-muted-foreground" : "text-foreground-secondary")}`}>{p.label}</Text>
    </Button>
  );
}

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
  return (
    <If when={project() && directory()}>
      <View class="w-full max-w-[792px] pt-2" testID="location-strip">
        <View class="flex-row items-end">
          <Text testID="location-project" numberOfLines={1} class={`${T.title} border-b-2 border-primary pb-1`}>{project()!.name}</Text>
          <View class="flex-1 h-px bg-border mb-px" />
        </View>
        <View class="flex-row items-center gap-1.5 min-w-0 pt-1">
          <If when={!!branch()}>
            <If when={!!session()} else={<Text numberOfLines={1} class={`${T.sec} shrink`}>{branch()!}</Text>}>
              <Button
                disabled={!worktreeAvailability().ok} onPress={() => void run("session.worktrees")} testID="location-branch"
                accessibilityLabel={`Branch ${branch()}, unavailable: ${worktreeAvailability().reason ?? ""}`}
                tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{worktreeAvailability().reason ?? "Worktree controls"}</Text>}
                class={`min-w-0 shrink rounded-(--radius) px-0.5 disabled:opacity-50 ${FOCUS}`}
              ><Text numberOfLines={1} class={T.sec}>{branch()!}</Text></Button>
            </If>
            <Text class={T.meta}>·</Text>
          </If>
          <If when={!!session()}>
            <Button
              disabled={!directoryAvailability().ok} onPress={() => void run("session.change_directory")} testID="location-directory"
              accessibilityLabel={`Directory ${path()}, unavailable: ${directoryAvailability().reason ?? ""}`}
              tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{directoryAvailability().reason ?? "Change session directory"}</Text>}
              class={`min-w-0 shrink rounded-(--radius) px-0.5 disabled:opacity-50 ${FOCUS}`}
            ><Text numberOfLines={1} class={`${T.meta} min-w-0`}>{path()}</Text></Button>
          </If>
          <If when={!session()}><Text numberOfLines={1} class={`${T.meta} min-w-0 shrink`}>{path()}</Text></If>
          <If when={linked()}><Text testID="location-worktree" class="shrink-0 text-[12px] leading-4 font-bold text-agent-violet">worktree</Text></If>
        </View>
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
    <View class="px-3 items-center" testID="waiting-slot">
      <If when={others().length > 0 && items().length === 0}>
        <View class="w-full max-w-[792px] flex-row items-center gap-2 rounded-(--radius-card) border border-border bg-card px-3 py-2 mb-2">
          <Status kind="waiting" />
          <Text class={`flex-1 ${T.sec}`}>{`${others().length} other session${others().length === 1 ? " is" : "s are"} waiting for input`}</Text>
          <Btn label="Go to" onPress={() => run("waiting.go_to")} testID="waiting-go-to" />
        </View>
      </If>
      <If when={cur()}>{(w) => <WaitBody w={w} count={items().length} />}</If>
    </View>
  );
}

function WaitBody(p: { w: () => { kind: "approval" | "question"; id: string; session_id: string }; count: number }) {
  const approval = () => { m(); return model.approvals.get(p.w().id); };
  const question = () => { m(); return model.questions.get(p.w().id); };
  const from = () => { const s = model.sessions.get(p.w().session_id); return s && s.id !== viewed()?.id ? ` · from ${s.title || "child"}` : ""; };
  return (
    <View class="w-full max-w-[792px] mb-2 flex-row rounded-(--radius-card) border border-border bg-card overflow-hidden" testID="waiting-card" accessibilityLabel="Waiting for your input" role="alert">
      <View class="w-[3px] bg-primary" />
      <View class="flex-1 min-w-0 gap-2 px-3.5 py-3">
        <View class="flex-row items-center gap-2">
          <Status kind="waiting" word />
          <Text numberOfLines={1} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground">
            <If when={approval()}>{() => <Text>{`${approval()!.tool}${from()}`}</Text>}</If>
            <If when={question()}>{() => <Text>{`question${from()}`}</Text>}</If>
          </Text>
          <If when={p.count > 1}>
            <View class="flex-row items-center gap-2">
              <Text class={T.meta}>{`${(waitIndex() % p.count) + 1} of ${p.count}`}</Text>
              <Btn label="Next" kind="ghost" onPress={() => run("waiting.next")} />
            </View>
          </If>
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
  const cmd = typeof a.args?.command === "string" ? (a.args.command as string) : JSON.stringify(a.args, null, 2);
  const noSession = "Core's approval reply takes only allow or deny (Step 12a adds more choices).";
  return (
    <View class="gap-2.5">
      <View class="rounded-(--radius) border border-border bg-background px-3 py-2 max-h-40 overflow-hidden">
        <Text selectable class="font-mono text-xs leading-[18px] text-foreground">{cmd}</Text>
      </View>
      <If when={!busyWait()} else={<View class="flex-row items-center gap-2"><Status kind="running" /><Text class={T.sec}>sending…</Text></View>}>
        <View class="flex-row flex-wrap items-center gap-2">
          <Btn label="Allow once" kind="primary" onPress={() => void reply(a, true)} testID="allow-once" disabled={!live()} reason="Core is not connected" />
          <Btn label="Allow for session" disabled reason={noSession} testID="allow-session" />
          <Btn label="Deny" onPress={() => void reply(a, false)} testID="deny" disabled={!live()} />
          <Btn label="Deny with note" disabled reason="Core's approval reply has no note field yet." />
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
                <Input placeholder="Or type an answer" onChange={(t) => upd(id(), (a) => ({ ...a, text: t }))} accessibilityLabel="Free-text answer" class="rounded-(--radius) border border-border bg-background px-2.5 h-8 text-[13px] text-foreground" />
              </If>
            </View>
          );
        }}
      </Each>
      <View class="flex-row gap-2"><Btn label="Submit answers" kind="primary" busy={busyWait()} onPress={() => void submitQuestion(q)} testID="submit-answers" disabled={!live()} /></View>
    </View>
  );
}
void Icon; void waitItems;
