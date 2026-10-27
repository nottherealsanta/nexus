import { Text, View, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, Icon, IconButton, Status, T, FOCUS } from "./kit";
import { availability, byId, draftKey, focusComposer, focusWaiting, keyLabel, otherWaits, run, sendPrompt, term, waitItems } from "../commands";
import { centre, connection, currentProject, draft, isNarrow, live, m, model, viewed, waitIndex, toast, selectSession, bump } from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { Approval, Question } from "../core/api";
import { structure } from "../gen/theme";

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
  return (
    <View class="px-3 pb-3 pt-1 items-center" testID="composer-area">
      <View class="w-full max-w-[792px] rounded-(--radius-card) border border-border bg-card focus-within:border-ring" accessibilityLabel="Composer">
        <Input
          ref={(n: any) => (ref = n)} multiline value={text()} testID="composer-input" accessibilityLabel="Message"
          placeholder={reason() ?? (centre() === "new_session" ? "Ask the agent… (starts a new session)" : "Ask the agent…")}
          readonly={!live() || !!ses()?.archived}
          submitBehavior="submit" onSubmit={() => void send()} onChange={(t) => draft.update((d) => ({ ...d, [key()]: t }))}
          onContentSizeChange={(s) => h.set(Math.min(220, Math.max(44, Math.ceil(s.height) + 16)))}
          style={{ height: h() }}
          class="px-3.5 py-2 text-[14px] leading-[22px] text-foreground placeholder:text-muted-foreground"
        />
        <View class={`flex-row items-center gap-2 px-2 pb-2 ${isNarrow() ? "flex-wrap" : ""}`}>
          <View class="flex-1 min-w-0 flex-row items-center gap-3 pl-1.5">
            <Pair k="agent" v={ses()?.agent ?? "build"} />
            <Pair k="model" v={(ses()?.model as string) || "default"} />
          </View>
          <If when={!isNarrow()}><Text class={T.meta} numberOfLines={1}>{busy() ? `${keyLabel(byId.get("session.interrupt")!) ?? "esc"} stop` : "enter send · shift+enter newline"}</Text></If>
          <If when={busy()}>
            <View class="flex-row items-center gap-2">
              <Status kind="running" word />
              <Btn label="Stop" icon="stop" onPress={() => run("session.interrupt")} disabled={!availability(byId.get("session.interrupt")!).ok} testID="interrupt" />
            </View>
          </If>
          <If when={!busy() && interrupted()}>
            <Btn label="Continue" icon="play" onPress={() => run("session.continue")} disabled={!availability(byId.get("session.continue")!).ok} testID="continue" />
          </If>
          <IconButton icon="arrow-up" label="Send message" testID="send" disabled={!canSend()} onPress={() => void send()} class={canSend() ? "bg-primary hover:bg-primary" : ""} tint={canSend() ? "tint-on-accent" : undefined} reason={!canSend() ? (reason() ?? "Type a message first") : undefined} />
        </View>
      </View>
    </View>
  );
}
function Pair(p: { k: string; v: string }) {
  return <Text numberOfLines={1} class={`${T.meta} shrink min-w-0`}>{p.k} <Text class="text-foreground-secondary">{p.v}</Text></Text>;
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
void Icon; void currentProject; void selectSession; void bump; void waitItems;
