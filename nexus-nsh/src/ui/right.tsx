import type { JSX } from "nsh";
import { Scroll, Text, View } from "nsh";
import { IconButton, Status, T, relTime, midTruncate } from "./kit";
import { manifest } from "../gen/manifest";
import { run } from "../commands";
import { agentCatalog, defaultModel, gitStatus, m, model, modelCatalog, viewed } from "../store";
import { agentColor } from "../agent-color";

const sections = manifest.navigation.right_sidebar.section as unknown as { id: string; label: string; order: number; unavailable_reason?: string }[];

function Field(p: { k: string; v?: string | null; mono?: boolean; children?: JSX.Element }) {
  return (
    <View class="flex-row items-start gap-3 py-1">
      <Text class={`w-[76px] shrink-0 ${T.meta} pt-0.5`}>{p.k}</Text>
      <View class="flex-1 min-w-0">
        {p.children ?? <Text selectable class={`${p.mono ? "font-mono text-xs leading-5" : "text-[13px] leading-5"} text-foreground`}>{p.v || "—"}</Text>}
      </View>
    </View>
  );
}
function Section(p: { title: string; children?: JSX.Element }) {
  return (
    <View class="px-4 py-3 border-b border-border">
      <Text class="text-[13px] leading-5 font-semibold text-foreground pb-1.5">{p.title}</Text>
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
function tokenCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && Number.isInteger(value) && value >= 0 ? value : 0;
}
function messageText(message: { parts: any[] }): string {
  return message.parts.filter((part) => part.type === "text").map((part) => part.text ?? "").join("");
}
function catalogModel(ref: string, models: any[]): any | undefined {
  const bare = ref.split("/").at(-1);
  return models.find((entry) => entry.ref === ref || String(entry.ref ?? "").split("/").at(-1) === bare);
}
function effectiveModel(session: any): string {
  if (typeof session.model === "string" && session.model && session.model !== "default") return session.model;
  const agent = agentCatalog().agents.find((item) => item.name === session.agent);
  return agent?.model || defaultModel();
}
function sessionTotals(messages: any[], events: any[], models: any[], fallback: string) {
  let turns = 0, calls = 0, input = 0, cached = 0, output = 0, reasoning = 0;
  let reported = false, lastInput: number | null = null, lastModel = fallback;
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
  for (const event of events) {
    const data = event.data ?? {};
    if (event.type === "wire_request") {
      if (typeof data.body?.model === "string") lastModel = data.body.model;
      continue;
    }
    if (event.type !== "usage") continue;
    reported = true;
    calls++;
    const requestInput = tokenCount(data.input_tokens);
    const requestCached = tokenCount(data.cached_input_tokens);
    const requestOutput = tokenCount(data.output_tokens);
    input += requestInput;
    cached += requestCached;
    output += requestOutput;
    reasoning += tokenCount(data.reasoning_tokens);
    lastInput = requestInput;
    const entry = catalogModel(lastModel, models);
    const inputCost = entry?.input_cost, outputCost = entry?.output_cost;
    if (typeof inputCost !== "number" || !Number.isFinite(inputCost) || typeof outputCost !== "number" || !Number.isFinite(outputCost)) {
      partialCost = true;
      continue;
    }
    const cacheCost = typeof entry.cache_read_cost === "number" && Number.isFinite(entry.cache_read_cost) ? entry.cache_read_cost : inputCost;
    usd += (Math.max(0, requestInput - requestCached) * inputCost + requestCached * cacheCost + requestOutput * outputCost) / 1_000_000;
    priced = true;
  }
  return { turns, calls, tools, input, cached, output, reasoning, reported, lastInput, lastModel, cost: priced ? usd : null, partialCost };
}
function money(usd: number): string { return usd < 0.01 ? "<$0.01" : `$${usd.toFixed(2)}`; }
function ContextBar(p: { used: number; window: number }) {
  const percent = p.used / p.window * 100;
  const shown = Math.min(100, Math.max(0, percent));
  const tone = percent >= 95 ? "bg-error" : percent >= 80 ? "bg-warning" : "bg-secondary";
  return (
    <View class="flex-row items-center gap-2 min-w-0" testID="session-context">
      <View class="h-2 flex-1 min-w-8 overflow-hidden rounded-full bg-muted">
        <View class={`h-full rounded-full ${tone}`} style={{ width: `${shown}%` }} />
      </View>
      <Text class="w-10 text-right text-[13px] leading-5 text-foreground-secondary">{`${percent.toFixed(0)}%`}</Text>
    </View>
  );
}

export function RightSidebar(p: { drawer?: boolean }) {
  const s = () => viewed();
  return (
    <View class={`bg-background ${p.drawer ? "flex-1" : "w-[288px] border-l border-border"}`} testID="right-sidebar" accessibilityLabel="Right sidebar">
      <Scroll class="flex-1" contentClass="">
        <Section title={sections[0].label}>
          <If when={s()} else={<Text class={T.sec}>Select a session to see its details.</Text>}>
            {() => {
              const ses = () => { m(); return s()!; };
              const selected = () => { m(); return model.selected?.sessionId === ses().id ? model.selected : null; };
              const msgs = () => selected()?.messages ?? [];
              const events = () => selected()?.events ?? [];
              const totals = () => sessionTotals(msgs(), events(), modelCatalog(), effectiveModel(ses()));
              const descendants = () => model.tree(ses().id).slice(1).map((id) => model.sessions.get(id)).filter(Boolean) as any[];
              const kids = () => descendants().length;
              const runningKids = () => descendants().filter((child) => child.status === "busy" || model.running.has(child.id)).length;
              const failedKids = () => descendants().filter((child) => child.status === "failed" || model.failedSessions.has(child.id)).length;
              return (
                <View>
                  <Field k="Title" v={ses().title || "Untitled"} />
                  <Field k="Id"><View class="flex-row items-center gap-1"><Text selectable class="flex-1 min-w-0 font-mono text-xs leading-5 text-foreground">{midTruncate(ses().id, 18)}</Text><IconButton icon="copy" label="Copy session id" onPress={() => run("session.copy_id")} class="size-7" /></View></Field>
                  <Field k="Status"><View class="flex-row"><Status kind={ses().status === "busy" ? "running" : ses().archived ? "cancelled" : "done"} word /></View></Field>
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
                  <If when={totals().reported}>
                    <Field k="Input" v={fmtCount(totals().input)} />
                    <Field k="Cached" v={fmtCount(totals().cached)} />
                    <Field k="Output" v={fmtCount(totals().output)} />
                    <Field k="Reasoning" v={fmtCount(totals().reasoning)} />
                    {() => {
                      const models = modelCatalog();
                      const window = Number(catalogModel(totals().lastModel, models)?.context);
                      return totals().lastInput !== null && Number.isFinite(window) && window > 0
                        ? <Field k="Context"><ContextBar used={totals().lastInput!} window={window} /></Field>
                        : null;
                    }}
                    <If when={totals().cost !== null}>
                      <Field k="Cost" v={`${money(totals().cost!)}${totals().partialCost ? "+" : ""}`} />
                    </If>
                  </If>
                  <If when={!totals().reported}><Field k="Tokens" v="not reported" /></If>
                  <Text class={`${T.meta} pt-2`}>Kernel state is not reported by Core yet.</Text>
                </View>
              );
            }}
          </If>
        </Section>
        <Section title={sections[1].label}>
          <If when={s()} else={<Text class={T.sec}>Select a session to see Git status.</Text>}>
            <If when={gitStatus()} else={<Text class={T.sec}>Loading Git status…</Text>}>
              <If when={gitStatus()?.error}><Text class={T.sec}>{gitStatus()?.error}</Text></If>
              <If when={gitStatus()?.data}><View>
                <Text class={T.sec}>{gitStatus()?.data.branch} ↑{gitStatus()?.data.ahead} ↓{gitStatus()?.data.behind}</Text>
                <Each of={gitStatus()?.data.changes ?? []} key={(change: any) => change.path}>{(change: any) => <Text class={T.sec}>{change().path} +{change().added} −{change().removed}</Text>}</Each>
              </View></If>
            </If>
          </If>
        </Section>
      </Scroll>
    </View>
  );
}
