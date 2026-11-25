// First-run onboarding (plans/52): a full-window tour that connects a provider, sets models for agents and teaches
// the basics. The TUI draws the same steps (nexus-tui/src/onboarding.rs); both read them from interface/manifest.toml.
import { Scroll, Text, View, effect, keymap } from "nsh";
import { Btn, Icon, T, TS } from "./kit";
import { ProviderDetailModal, loadSettingsData, openProviderDetail, providerStatus, settingsData } from "./settings";
import { byId, keyLabel, run } from "../commands";
import { centre, config, confirmDialog, currentProject, live, patchConfig, picker, providerDetail, providerLogins, settingsReload } from "../store";
import { ensureSearchStarted, refreshSearchService, searchService, searchServiceError, searchStartError, searchStartReason, searchStarting, searchStatusLines, startSearchService } from "../search-service";
import {
  ONBOARDING, agentModelLine, agentModelsPatch, closeOnboarding, currentStep, fillKeys, lastStep, nextStep, onboardingOpen, onboardingStep,
  onboardingSteps, previousStep, rootAgent,
} from "../onboarding";
import type { OnboardingAgent } from "../onboarding";

/** The actionable row buttons of the open step, in order; ↑↓ move among them, Enter presses the focused one. */
let rows: { focus: () => void }[] = [];
const rowRef = (i: number) => (n: any) => { if (n) rows[i] = n; else rows.length = Math.min(rows.length, i); };
const keyText = (text: string) => fillKeys(text, (id) => { const c = byId.get(id); return c ? keyLabel(c) : null; });
const providers = () => (settingsData().providers ?? []).filter((p: any) => p.id !== "mock");
const connected = () => providers().filter((p: any) => p.connected);
const models = (): any[] => settingsData().models?.models ?? [];

/** Mark the tour finished for this Core home (both clients read it), then close it. */
async function finish(open: boolean) {
  if (live()) await patchConfig("ui.onboarded", true);
  closeOnboarding();
  if (open && !currentProject()) void run("project.add");
}

function ProviderStep() {
  const objective = () => (connected().length ? `✓ ${ONBOARDING.quest_provider}` : `○ ${ONBOARDING.quest_provider}`);
  return <View class="gap-2" testID="onboarding-providers">
    <Text class={connected().length ? `${TS.base} font-medium text-success` : `${TS.base} font-medium text-foreground`} testID="onboarding-objective">{objective()}</Text>
    <If when={!settingsData().providers}><Text class={T.meta}>Loading providers…</Text></If>
    <Each of={providers()} key={(p: any) => p.id}>{(p) => <View class="flex-row items-center gap-3 rounded-(--radius) bg-raised px-3 py-2" testID={`onboarding-provider-${p().id}`}>
      <View class="flex-1">
        <Text class={T.base}>{p().label ?? p().id}</Text>
        <Text class={providerLogins()[p().id]?.status === "pending" ? `${TS.base} text-warning` : T.meta}>{providerLogins()[p().id]?.status === "pending" ? "Sign-in pending · waiting for you" : providerStatus(p())}</Text>
      </View>
      <Btn ref={rowRef(providers().findIndex((x: any) => x.id === p().id))} label={p().connected ? "Manage" : "Connect"} kind={p().connected ? "default" : "primary"} disabled={!live()} onPress={() => openProviderDetail(p().id)} testID={`onboarding-connect-${p().id}`} />
    </View>}</Each>
  </View>;
}

function chooseModel(agent: OnboardingAgent) {
  const items = [
    { id: "none", label: "Use the session's model", hint: "no model set for this agent", run: async () => { picker.set(null); await patchConfig("agents.models", agentModelsPatch(config(), agent.name, null)); } },
    ...models().map((m: any) => ({ id: m.ref, label: m.ref, hint: m.name ?? undefined, run: async () => { picker.set(null); await patchConfig("agents.models", agentModelsPatch(config(), agent.name, m.ref)); } })),
  ];
  picker.set({ title: `Model for ${agent.name}`, note: "Connected providers only", items });
}

function AgentRow(p: { a: OnboardingAgent; index: number }) {
  const root = p.a.kind === "root";
  const none = () => !connected().length;
  return <View class="gap-1 rounded-(--radius) bg-raised px-3 py-2" testID={`onboarding-agent-${p.a.name}`}>
    <View class="flex-row items-center gap-3">
      <View class="flex-1">
        <Text class={T.label}>{root ? "Default agent" : p.a.name}</Text>
        <Text class={T.sec}>{p.a.role}</Text>
      </View>
      <If when={root} else={<Btn ref={rowRef(p.index)} label={agentModelLine(config(), p.a.name)} disabled={!live() || none()} reason={none() ? ONBOARDING.no_provider : undefined}
        onPress={() => chooseModel(p.a)} testID={`onboarding-model-${p.a.name}`} />}>
        <Btn ref={rowRef(p.index)} label={rootAgent(config(), p.a.options ?? [])} disabled={!live()} testID="onboarding-root-agent"
          onPress={() => { const o = p.a.options ?? []; void patchConfig("defaults.agent", o[(o.indexOf(rootAgent(config(), o)) + 1) % o.length]); }} />
      </If>
    </View>
    <Text class={T.meta}>{p.a.hint}</Text>
  </View>;
}

function AgentsStep() {
  return <View class="gap-2" testID="onboarding-agents">
    <If when={!connected().length}><Text class={`${TS.base} text-warning`} testID="onboarding-no-provider">{ONBOARDING.no_provider}</Text></If>
    <Each of={ONBOARDING.agent} key={(a: OnboardingAgent) => a.name}>{(a) => <AgentRow a={a()} index={ONBOARDING.agent.indexOf(a())} />}</Each>
  </View>;
}

/** SearXNG's state from Core, then Retry. Core starts it in the background; the tour never waits for it. */
function SearchStep() {
  const state = () => searchStarting() ? "starting" : searchService()?.state ?? (searchServiceError() || searchStartError() ? "unavailable" : "loading");
  const tone = () => state() === "running" ? "text-success" : state() === "starting" ? "text-warning" : state() === "unavailable" ? "text-error" : "text-foreground-secondary";
  const reason = () => !live() ? "Core is not connected: the view is read-only." : searchStartReason();
  return <View class="gap-2" testID="onboarding-search">
    <View class="gap-1" testID="onboarding-search-status">
      <Each of={searchStatusLines()} key={(line: string) => line}>{(line, i) => <Text style={{ userSelect: "text" }} class={`${TS.base} ${i() === 0 ? tone() : "text-foreground-secondary"}`}>{line()}</Text>}</Each>
    </View>
    <View class="flex-row">
      <Btn ref={rowRef(0)} label={ONBOARDING.search_retry} disabled={!live() || !!reason()} reason={reason()} onPress={() => void startSearchService()} testID="onboarding-search-retry" />
    </View>
  </View>;
}

function Dots() {
  return <View class="flex-row items-center gap-1.5" accessibilityLabel={`Step ${onboardingStep() + 1} of ${onboardingSteps.length}`} testID="onboarding-dots">
    <Each of={onboardingSteps} key={(s) => s.id}>{(s) => <View class={`h-2 rounded-full ${onboardingSteps.indexOf(s()) === onboardingStep() ? "w-5 bg-primary" : onboardingSteps.indexOf(s()) < onboardingStep() ? "w-2 bg-primary/60" : "w-2 bg-border"}`} />}</Each>
  </View>;
}

export function Onboarding() {
  return <If when={onboardingOpen()}>{() => <OnboardingBody />}</If>;
}

function OnboardingBody() {
  const step = () => currentStep();
  loadSettingsData();
  let seen = settingsReload();
  effect(() => { const n = settingsReload(); if (n !== seen) { seen = n; loadSettingsData(); } });
  // Enter goes on, unless a picker, the provider modal or a confirmation has the key.
  // Enter presses the focused control. A step with rows focuses its first row, any other the primary button (Next or Finish).
  let primary: { focus: () => void } | null = null;
  let at = 0;
  const free = () => onboardingOpen() && !picker() && !providerDetail() && !confirmDialog() && centre() !== "settings";
  const rowStep = () => step().kind === "provider" || step().kind === "agents" || step().kind === "search";
  effect(() => { onboardingStep(); at = 0; queueMicrotask(() => { if (!free()) return; if (rowStep() && rows[0]) rows[0].focus(); else primary?.focus(); }); });
  // The search step starts SearXNG in the background on entry and follows it until it runs; leaving stops the poll.
  effect(() => {
    const kind = step().kind;
    if (!onboardingOpen() || kind !== "search") return;
    void ensureSearchStarted();
    const timer = setInterval(() => { if (onboardingOpen() && searchService()?.state !== "running") void refreshSearchService(); }, 2000);
    return () => clearInterval(timer);
  });
  effect(() => {
    const move = (by: number) => { if (!free() || !rowStep() || !rows.length) return; at = Math.max(0, Math.min(rows.length - 1, at + by)); rows[at]?.focus(); };
    const offs = [
      keymap.bind("down", () => move(1), { when: "notEditing" }),
      keymap.bind("up", () => move(-1), { when: "notEditing" }),
      keymap.bind("right", () => { if (free() && !lastStep()) nextStep(); }, { when: "notEditing" }),
      keymap.bind("left", () => { if (free()) previousStep(); }, { when: "notEditing" }),
      keymap.bind("s", () => { if (free()) void finish(false); }, { when: "notEditing" }),
    ];
    return () => offs.forEach((off) => off());
  });
    return (
    <View testID="onboarding" class="absolute inset-0 bg-background flex-col" accessibilityLabel="Onboarding">
      <View class="flex-row items-center gap-3 px-6 pt-5 pb-3">
        <Text class={`${TS.title} font-semibold text-foreground`}>nexus</Text>
        <View class="flex-1 items-center"><Dots /></View>
        <Text class={T.meta} testID="onboarding-count">{`${onboardingStep() + 1} / ${onboardingSteps.length}`}</Text>
        <Btn label="Skip" kind="ghost" onPress={() => void finish(false)} testID="onboarding-skip" />
      </View>
      <Scroll class="flex-1 min-h-0">
        <View class="w-full max-w-[640px] self-center gap-4 px-6 py-6" testID={`onboarding-step-${step().id}`}>
          <Text class={`${TS.title} text-[22px] leading-8 font-semibold text-foreground`} testID="onboarding-title">{step().title}</Text>
          <Each of={step().lines} key={(line: string) => line}>{(line) => <Text class={T.sec} style={{ userSelect: "text" }}>{keyText(line())}</Text>}</Each>
          <If when={step().kind === "provider"}><ProviderStep /></If>
          <If when={step().kind === "agents"}><AgentsStep /></If>
          <If when={step().kind === "search"}><SearchStep /></If>
          <If when={step().tip}><View class="flex-row items-start gap-2 pt-1"><Icon name="info" size={14} class="tint-muted-foreground" /><Text class={`flex-1 ${T.meta}`}>{keyText(step().tip!)}</Text></View></If>
          <If when={step().kind === "done" && !currentProject()}><Text class={`${TS.base} text-warning`}>{ONBOARDING.finish_no_project}</Text></If>
        </View>
      </Scroll>
      <View class="flex-row items-center justify-between gap-3 px-6 py-4">
        <Btn label="Back" kind="ghost" disabled={onboardingStep() === 0} onPress={previousStep} testID="onboarding-back" />
        <Text class={T.meta} testID="onboarding-hints">{step().kind === "provider" || step().kind === "agents" ? ONBOARDING.hints_rows : step().kind === "search" ? ONBOARDING.hints_search : ONBOARDING.hints_info}</Text>
        <If when={!lastStep()} else={<Btn ref={(n) => { primary = n; }} label="Finish" kind="primary" onPress={() => void finish(true)} testID="onboarding-finish" />}>
          <Btn ref={(n) => { primary = n; }} label="Next" kind="primary" onPress={nextStep} testID="onboarding-next" />
        </If>
      </View>
      <ProviderDetailModal />
    </View>
  );
}
