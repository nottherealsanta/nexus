import type { JSX } from "nsh";
import { Scroll, Text, View } from "nsh";
import { IconButton, Status, T, relTime, midTruncate } from "./kit";
import { manifest } from "../gen/manifest";
import { run } from "../commands";
import { contextBudget, gitStatus, m, model, modelCatalog, viewed } from "../store";
import { agentColor } from "../agent-color";
import type { SelectedView } from "../core/reduce";
import { latestUsage, ledgerTotals, unknownRequests } from "../core/turns";
import { attention } from "./sidebar";

const sections = manifest.navigation.right_sidebar.section as unknown as { id: string; label: string; order: number; unavailable_reason?: string }[];

function Field(p: { k: string; v?: string | null; mono?: boolean; children?: JSX.Element }) {
  return (
    <View class="flex-row items-start gap-3 min-h-6 py-0.5">
      <Text class={`w-24 shrink-0 ${T.meta} pt-0.5`}>{p.k}</Text>
      <View class="flex-1 min-w-0">
        {p.children ?? <Text style={{ userSelect: "text" }} class={`${p.mono ? "font-mono text-xs leading-5" : "text-[13px] leading-5"} text-foreground`}>{p.v || "—"}</Text>}
      </View>
    </View>
  );
}
const LABEL = "text-[11px] leading-4 font-semibold uppercase tracking-[0.06em] text-muted-foreground";
function Section(p: { title: string; children?: JSX.Element }) {
  return (
    <View class="px-4 pt-5 pb-1" testID={`right-section-${p.title.toLowerCase().replace(/ /g, "-")}`}>
      <Text class={`${LABEL} pb-2`}>{p.title}</Text>
      {p.children}
    </View>
  );
}
const date = (s?: number) => (s ? new Date(s * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");

function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 100_000) return `${Math.round(n / 1000)}K`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}
function messageText(message: { parts: any[] }): string {
  return message.parts.filter((part) => part.type === "text").map((part) => part.text ?? "").join("");
}
function catalogModel(ref: string, models: any[]): any | undefined {
  const bare = ref.split("/").at(-1);
  return models.find((entry) => entry.ref === ref || String(entry.ref ?? "").split("/").at(-1) === bare);
}
function effectiveModel(session: any): string {
  return typeof session.model === "string" ? session.model : "";
}
/**
 * Session totals: turns and tools from the messages; tokens, calls and cost from Core's spend
 * ledger (plus live usage it has not counted yet); the context bar from the newest request
 * with known usage. Unknown usage is reported as such, never as zero.
 */
function sessionTotals(view: SelectedView | null, messages: any[], models: any[], fallback: string) {
  let turns = 0, calls = 0, input = 0, cached = 0, output = 0, reasoning = 0;
  let priced = false, partialCost = false, usd = 0;
  const toolIds = new Set<string>();
  const toolCounts = new Map<string, number>();
  for (const message of messages) {
    if (message.role === "user" && !messageText(message).startsWith("[")) turns++;
    for (const part of message.parts) {
      if (part.type !== "tool_call" || !part.id || !part.name || toolIds.has(part.id)) continue;
      toolIds.add(part.id);
      toolCounts.set(part.name, (toolCounts.get(part.name) ?? 0) + 1);
    }
  }
  const tools = [...toolCounts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const scratch = view?.scratch ?? [];
  const rows = ledgerTotals(view?.usage ?? null, scratch, view?.usageMark ?? 0, fallback);
  for (const [ref, row] of rows) {
    calls += row.requests;
    input += row.input; cached += row.cached; output += row.output; reasoning += row.reasoning;
    const entry = catalogModel(ref, models);
    const inputCost = entry?.input_cost, outputCost = entry?.output_cost;
    if (typeof inputCost !== "number" || !Number.isFinite(inputCost) || typeof outputCost !== "number" || !Number.isFinite(outputCost)) {
      partialCost = true;
      continue;
    }
    const cacheCost = typeof entry.cache_read_cost === "number" && Number.isFinite(entry.cache_read_cost) ? entry.cache_read_cost : inputCost;
    usd += (Math.max(0, row.input - row.cached) * inputCost + row.cached * cacheCost + row.output * outputCost) / 1_000_000;
    priced = true;
  }
  const latest = view ? latestUsage(view.turns, scratch) : null;
  // A ledger that started counting well after the conversation did covers only part of it.
  const since = view?.usage?.since ?? null;
  const first = messages[0]?.created;
  const partialSince = since !== null && typeof first === "number" && since > first + 60 ? since : null;
  return {
    turns, calls, tools, input, cached, output, reasoning, reported: rows.size > 0,
    lastInput: latest ? latest.input : null, lastModel: latest?.model ?? fallback,
    cost: priced ? usd : null, partialCost, since: partialSince, unknown: view ? unknownRequests(view.turns) : 0,
  };
}
function money(usd: number): string { return usd < 0.01 ? "<$0.01" : `$${usd.toFixed(2)}`; }
function ContextBar(p: { used: number; window: number }) {
  const percent = p.used / p.window * 100;
  const shown = Math.min(100, Math.max(0, percent));
  const tone = percent >= 95 ? "bg-error" : percent >= 80 ? "bg-warning" : "bg-primary";
  return (
    <View class="flex-row items-center gap-2 min-w-0" testID="session-context">
      <View class="h-1 flex-1 min-w-8 overflow-hidden rounded-full bg-border">
        <View class={`h-full rounded-full ${tone}`} style={{ width: `${shown}%` }} />
      </View>
      <Text class="w-10 text-right text-[13px] leading-5 text-foreground-secondary">{`${percent.toFixed(0)}%`}</Text>
    </View>
  );
}

type Change = { path: string; status?: string; staged?: boolean; unstaged?: boolean; added?: number; removed?: number };
const LETTER_TONE: Record<string, string> = { M: "text-warning", A: "text-success", "?": "text-success", D: "text-error", R: "text-info" };
function GitGroup(p: { title: string; rows: Change[]; letter: (c: Change) => string }) {
  return (
    <If when={p.rows.length > 0}>
      <View class="pt-3" testID={`git-group-${p.title.toLowerCase()}`}>
        <View class="flex-row items-center min-h-6"><Text class={`${T.sec} flex-1`}>{p.title}</Text><Text class={T.meta}>{String(p.rows.length)}</Text></View>
        <Each of={p.rows} key={(c) => c.path}>
          {(row) => {
            const c = row();
            const l = p.letter(c);
            return (
              <View class="flex-row items-center gap-2 min-h-6 rounded-(--radius) px-1 -mx-1 hover:bg-accent" testID="git-row">
                <Text class={`w-3.5 text-center font-mono text-xs leading-5 font-semibold ${LETTER_TONE[l] ?? "text-muted-foreground"}`}>{l}</Text>
                <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground">{c.path}</Text>
                <Text class={`${T.meta} text-diff-add`}>{`+${c.added ?? 0}`}</Text>
                <Text class={`${T.meta} text-diff-del`}>{`−${c.removed ?? 0}`}</Text>
              </View>
            );
          }}
        </Each>
      </View>
    </If>
  );
}
function GitRows(p: { data: () => any }) {
  const d = () => p.data();
  const changes = (): Change[] => d().changes ?? [];
  const untracked = () => changes().filter((c) => c.status === "??");
  const staged = () => changes().filter((c) => c.staged);
  const unstaged = () => changes().filter((c) => c.unstaged && c.status !== "??");
  const ahead = () => Number(d().ahead) || 0, behind = () => Number(d().behind) || 0;
  return (
    <View>
      <View class="flex-row items-center gap-2 min-h-6">
        <Text class="text-[13px] leading-5 font-medium text-foreground">{`⌥ ${d().branch ?? "—"}`}</Text>
        <If when={ahead() > 0}><Text class={T.meta}>{`↑${ahead()}`}</Text></If>
        <If when={behind() > 0}><Text class={T.meta}>{`↓${behind()}`}</Text></If>
      </View>
      <If when={changes().length === 0}><Text class={`${T.sec} pt-2`}>Working tree clean.</Text></If>
      <GitGroup title="Staged" rows={staged()} letter={(c) => (c.status ?? "M")[0]} />
      <GitGroup title="Unstaged" rows={unstaged()} letter={(c) => (c.status ?? " M")[1] || "M"} />
      <GitGroup title="Untracked" rows={untracked()} letter={() => "?"} />
    </View>
  );
}

export function RightSidebar(p: { drawer?: boolean }) {
  const s = () => viewed();
  return (
    <View class={`bg-sidebar ${p.drawer ? "flex-1" : "w-[300px]"}`} testID="right-sidebar" accessibilityLabel="Right sidebar">
      <Scroll class="flex-1">
        <Section title={sections[0].label}>
          <If when={s()} else={<Text class={T.sec}>Select a session to see its details.</Text>}>
            {() => {
              const ses = () => { m(); return s()!; };
              const selected = () => { m(); return model.selected?.sessionId === ses().id ? model.selected : null; };
              const msgs = () => selected()?.messages ?? [];
              const totals = () => sessionTotals(selected(), msgs(), modelCatalog(), effectiveModel(ses()));
              const descendants = () => model.tree(ses().id).slice(1).map((id) => model.sessions.get(id)).filter(Boolean) as any[];
              const kids = () => descendants().length;
              const runningKids = () => descendants().filter((child) => child.status === "busy" || model.running.has(child.id)).length;
              const failedKids = () => descendants().filter((child) => child.status === "failed" || model.failedSessions.has(child.id)).length;
              const state = () => {
                const id = ses().id;
                if (attention(id) === "waiting") return { kind: "waiting" as const, word: "needs input" };
                if (model.failedSessions.has(id)) return { kind: "error" as const, word: "failed" };
                if (ses().status === "busy" || model.running.has(id)) return { kind: "running" as const, word: "running" };
                if (ses().archived) return { kind: "cancelled" as const, word: "archived" };
                if (model.unreadSessions.has(id)) return { kind: "done" as const, word: "finished" };
                return { kind: "done" as const, word: "idle" };
              };
              return (
                <View>
                  <Field k="Title" v={ses().title || "Untitled"} />
                  <Field k="Id"><View class="flex-row items-center gap-1"><Text style={{ userSelect: "text" }} class="flex-1 min-w-0 font-mono text-xs leading-5 text-foreground">{midTruncate(ses().id, 15)}</Text><IconButton icon="copy" label="Copy session id" onPress={() => run("session.copy_id")} class="size-7" /></View></Field>
                  <Field k="Status"><View class="flex-row"><Status kind={state().kind} word text={state().word} /></View></Field>
                  <Field k="Agent"><Text class={agentColor(ses().agent)}>{ses().agent}</Text></Field>
                  <Field k="Model" v={`${ses().model || "default"}${ses().model_source ? ` (${ses().model_source})` : ""}`} mono />
                  <Field k="Effort" v={(ses().effort as string) || "default"} />
                  <Field k="Directory" v={midTruncate(ses().directory, 30)} mono />
                  <Field k="Started" v={date(ses().created)} />
                  <Field k="Last activity" v={`${date(ses().updated)} · ${relTime(ses().updated)}`} />
                  <Field k="Turns" v={`${totals().turns}${totals().calls ? ` · ${totals().calls} calls` : ""}`} />
                  <If when={totals().tools.length > 0}>
                    <Field k="Used" v={`${totals().tools.slice(0, 2).map(([name, count]) => `${name} ${count}`).join(" · ")}${totals().tools.length > 2 ? ` · +${totals().tools.length - 2}` : ""}`} />
                  </If>
                  <If when={Array.isArray(ses().tools) && (ses().tools as string[]).length > 0}>
                    {() => {
                      const all = ses().tools as string[];
                      const off = Array.isArray(ses().disabled_tools) ? (ses().disabled_tools as string[]).length : 0;
                      const on = Math.max(0, all.length - off);
                      return <Field k="Tools" v={off ? `${on} on · ${off} off` : `${on} on`} />;
                    }}
                  </If>
                  <Field k="Children" v={kids() ? `${kids()} (${runningKids()} running, ${failedKids()} failed)` : "none"} />
                  <Text class={`${LABEL} pt-5 pb-2`}>Context</Text>
                  <If when={totals().reported}>
                    <Field k="Input" v={fmtCount(totals().input)} />
                    <Field k="Cached" v={fmtCount(totals().cached)} />
                    <Field k="Output" v={fmtCount(totals().output)} />
                    <Field k="Reasoning" v={fmtCount(totals().reasoning)} />
                    {() => {
                      const budget = contextBudget()[s()?.id ?? ""];
                      return budget?.window ? <View class="py-1"><ContextBar used={budget.used} window={budget.window} /></View> : null;
                    }}
                    <If when={totals().cost !== null}>
                      <Field k="Cost" v={`${money(totals().cost!)}${totals().partialCost ? "+" : ""}`} />
                    </If>
                    <If when={totals().since !== null}>
                      <Text class={`${T.meta} pt-1`} testID="usage-since">{`Totals since ${date(totals().since!)}`}</Text>
                    </If>
                  </If>
                  <If when={totals().unknown > 0}>
                    <Text class={`${T.meta} pt-1`} testID="usage-unknown">{`${totals().unknown} request${totals().unknown === 1 ? "" : "s"} reported no usage (unknown, not zero)`}</Text>
                  </If>
                  <If when={!totals().reported}><Field k="Tokens" v="not reported" /></If>
                  <Text class={`${T.meta} pt-3`}>Kernel state is not reported by Core yet.</Text>
                </View>
              );
            }}
          </If>
        </Section>
        <Section title={sections[1].label}>
          <If when={s()} else={<Text class={T.sec}>Select a session to see Git status.</Text>}>
            <If when={gitStatus()} else={<Text class={T.sec}>Loading Git status…</Text>}>
              <If when={gitStatus()?.error}><Text class={T.sec}>{gitStatus()?.error}</Text></If>
              <If when={gitStatus()?.data}><GitRows data={() => gitStatus()!.data} /></If>
            </If>
          </If>
        </Section>
      </Scroll>
    </View>
  );
}
