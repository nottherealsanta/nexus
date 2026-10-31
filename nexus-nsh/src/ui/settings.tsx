import type { JSX } from "nsh";
import { Scroll, Text, View, effect, setColorScheme, signal, openUrl, colorScheme, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, T, FOCUS } from "./kit";
import { Heading, Modal, ModalFooter, ModalTitle, SelectBar } from "./modal";
import { manifest } from "../gen/manifest";
import { byId, commands, copyText, escape, keyLabel, loadProviders, run } from "../commands";
import { catalogSource, info, compat, isNarrow, live, m, model, reloadModelCatalog, savePrefs, setTimelineDetail, settingsPage, settingsReload, settingsSection, themeChoice, centre, timelineDetail, toast, currentProject, config, loadConfig, patchConfig, picker, providerLogins, confirmDialog, notificationSoundsOn, setNotificationSounds } from "../store";
import { CLIENT_API, CLIENT_VERSION } from "../core/api";
import { request } from "../core/transport";
import { voiceInfoLines, voiceActionReason } from "../voice-settings";
import { voiceStatus } from "../voice-state";
import type { VoiceTarget } from "../voice-state";

interface Row { id: string; label: string; kind: string; scope: string; clients?: string; default?: unknown; options?: string[]; command?: string; note?: string; unavailable_reason?: string }
interface Section { id: string; label: string; unavailable_reason?: string; page?: boolean; note?: string; row?: Row[] }
const sections = manifest.settings.section as unknown as Section[];

const data = signal<{ providers: any[] | null; models: any | null; agents: any | null }>({ providers: null, models: null, agents: null });
const keyEntry = signal<{ id: string; label: string } | null>(null);
const providerKey = signal("");
let loaded = false;
export function loadSettingsData() {
  if (!config()) void loadConfig().catch(() => {});
  const p = currentProject();
  data.update((d) => ({ ...d, agents: null }));
  request<any>("GET", "/api/agent", { query: { project: p ?? undefined } }).then((r) => { if (currentProject() === p) data.update((d) => ({ ...d, agents: r })); }).catch(() => {});
  void loadProviders().then((providers) => data.update((d) => ({ ...d, providers }))).catch(() => {});
  if (loaded) return; loaded = true;
  request<any>("GET", "/api/model").then((r) => data.update((d) => ({ ...d, models: r }))).catch(() => {});
}

function Seg(p: { options: string[]; value: string; onPick?: (v: string) => void; disabled?: boolean; label: string }) {
  return (
    <View class="flex-row rounded-(--radius) border border-border bg-background p-0.5" role="radiogroup" accessibilityLabel={p.label}>
      <Each of={p.options} key={(o) => o}>
        {(o) => (
          <Button role="radio" selected={p.value === o()} disabled={p.disabled} onPress={() => p.onPick?.(o())} accessibilityLabel={`${p.label}: ${o()}`}
            class={`rounded-(--radius-card) px-3 ${isNarrow() ? "min-h-10" : "min-h-7"} items-center justify-center hover:bg-accent disabled:opacity-50 ${FOCUS}`}>
            <Text class={`text-[13px] leading-5 ${p.value === o() ? "text-foreground font-medium" : "text-foreground-secondary"}`}>{o()}</Text>
          </Button>
        )}
      </Each>
    </View>
  );
}

function Value(p: { text: string; mono?: boolean }) { return <Text style={{ userSelect: "text" }} class={`${p.mono ? "font-mono text-xs" : "text-[13px]"} leading-5 text-foreground-secondary ${isNarrow() ? "text-left" : "text-right"}`}>{p.text}</Text>; }

function listFor(id: string): JSX.Element {
  const d = data();
  const lines = (xs: string[], none: string) => (xs.length ? <View class={`gap-1 ${isNarrow() ? "items-start" : "items-end"}`}>{xs.map((x) => <Value text={x} />)}</View> : <Value text={none} />);
  switch (id) {
    case "tools.defaults": return <Value text="Per-session today (disabled_tools); no global default in Core." />;
    case "keybindings.list": return <Value text={`${commands.filter((c) => c.keys?.nsh).length} commands with keys (see the palette)`} />;
    case "about.versions": {
      const i = info();
      return lines([`client ${CLIENT_VERSION} (API ${CLIENT_API.major}.${CLIENT_API.minor})`, `Core ${i?.version ?? "—"}`, `API ${i?.api_version ? JSON.stringify(i.api_version) : "not reported"}`, `NSH 0.3.0`], "—");
    }
    case "about.daemon": { const i = info(); return lines([`pid ${i?.pid ?? "—"}`, i?.home ?? "—"], "—"); }
    default: return <Value text="—" />;
  }
}

async function refreshCatalog() {
  try {
    const refreshed = await reloadModelCatalog();
    data.update((d) => ({ ...d, models: refreshed }));
    toast("success", "Model catalog refreshed");
  } catch (e) {
    toast("error", e instanceof Error ? e.message : String(e));
  }
}

/** Rows whose behaviour this increment really implements. Everything else is shown disabled with a reason. */
const WIRED = new Set(["appearance.theme", "appearance.animations", "appearance.timeline_detail", "notifications.sound", "about.inspector", "agents.default_root", "tools.default_permission", "agents.default_effort", "tools.defaults", "mcp.full_definitions", "mcp.max_listed_tools", "context.handoff_percent", "context.title_models", "keybindings.list", "about.versions", "about.daemon", "dictation.stt_model", "dictation.stt_download", "dictation.stt_remove", "dictation.tts_model", "dictation.tts_download", "dictation.tts_remove"]);

function voiceTarget(id: string): VoiceTarget | null {
  if (id.startsWith("dictation.stt_")) return "stt";
  if (id.startsWith("dictation.tts_")) return "tts";
  return null;
}
function voiceAction(id: string): "download" | "remove" | null {
  if (id.endsWith("_download")) return "download";
  if (id.endsWith("_remove")) return "remove";
  return null;
}
function voiceSettingReason(id: string): string | undefined {
  const target = voiceTarget(id), action = voiceAction(id);
  if (!target || !action) return undefined;
  if (!live()) return "Core is not connected: the view is read-only.";
  return voiceActionReason(target, action, voiceStatus()?.[target]);
}

function HandoffPercent() {
  const value = signal(String(config()?.values?.context?.handoff_percent ?? 80));
  const revision = signal<any>(config()?.revision);
  effect(() => {
    const current = config();
    if (current && current.revision !== revision()) { revision.set(current.revision); value.set(String(current.values?.context?.handoff_percent ?? 80)); }
  });
  const save = () => {
    const parsed = Number(value());
    if (!value().trim() || !Number.isFinite(parsed)) { toast("warning", "Enter a number from 0 to 100 (0 turns it off)."); return; }
    const bounded = Math.max(0, Math.min(100, Math.round(parsed)));
    value.set(String(bounded));
    void patchConfig("context.handoff_percent", bounded);
  };
  return (
    <View class="flex-row items-center gap-2">
      <Input value={value()} placeholder="0–100" accessibilityLabel="Automatic handoff threshold (percent, 0 is off)" testID="handoff-percent"
        onChange={({ value: text }) => value.set(text)} onSubmit={save} submitBehavior="submit"
        class="h-8 w-24 rounded-(--radius) border border-border bg-background px-2 text-right font-mono text-[13px] text-foreground" />
      <Btn label="Save" onPress={save} testID="handoff-percent-save" />
    </View>
  );
}

function TitleModels() {
  const joined = () => ((config()?.values?.context?.title_models ?? []) as string[]).join(", ");
  const value = signal(joined());
  const revision = signal<any>(config()?.revision);
  effect(() => {
    const current = config();
    if (current && current.revision !== revision()) { revision.set(current.revision); value.set(joined()); }
  });
  const save = () => {
    const refs = value().split(",").map((x) => x.trim()).filter(Boolean);
    void patchConfig("context.title_models", refs);
  };
  return (
    <View class="flex-row items-center gap-2">
      <Input value={value()} placeholder="copilot/gpt-6-luna, codex/gpt-6-luna" accessibilityLabel="Title models, provider/model separated by commas" testID="title-models"
        onChange={({ value: text }) => value.set(text)} onSubmit={save} submitBehavior="submit"
        class="h-8 min-w-0 flex-1 rounded-(--radius) border border-border bg-background px-2 font-mono text-[13px] text-foreground" />
      <Btn label="Save" onPress={save} testID="title-models-save" />
    </View>
  );
}

function McpToolsLimit() {
  const initial = Number(config()?.values?.mcp?.max_listed_tools ?? 25);
  const value = signal(String(initial));
  const revision = signal<any>(config()?.revision);
  effect(() => {
    const current = config();
    if (current && current.revision !== revision()) {
      revision.set(current.revision);
      value.set(String(current.values?.mcp?.max_listed_tools ?? 25));
    }
  });
  const save = () => {
    if (!value().trim()) { toast("warning", "Enter a number from 1 to 1000."); return; }
    const parsed = Number(value());
    if (!Number.isFinite(parsed)) { toast("warning", "Enter a number from 1 to 1000."); return; }
    const bounded = Math.max(1, Math.min(1000, Math.round(parsed)));
    value.set(String(bounded));
    void patchConfig("mcp.max_listed_tools", bounded);
  };
  return (
    <View class="flex-row items-center gap-2">
      <Input value={value()} placeholder="1–1000" accessibilityLabel="Tools listed by name (limit)" testID="mcp-max-listed-tools"
        onChange={({ value: text }) => value.set(text)} onSubmit={save} submitBehavior="submit"
        class="h-8 w-24 rounded-(--radius) border border-border bg-background px-2 text-right font-mono text-[13px] text-foreground" />
      <Btn label="Save" onPress={save} testID="mcp-max-listed-tools-save" />
    </View>
  );
}

function refreshProviderModels() {
  void request<any>("GET", "/api/model").then((models) => data.update((d) => ({ ...d, models }))).catch(() => {});
}

async function reloadProviders() {
  const providers = await loadProviders();
  data.update((d) => ({ ...d, providers }));
  refreshProviderModels();
}

/** Agents whose model lists name this provider, for the disconnect warning. */
function poolUsers(provider: string): string[] {
  const agents = config()?.values?.agents ?? {};
  const uses = (refs: string[] | undefined) => (refs ?? []).some((ref) => ref.split("/")[0] === provider);
  return [...new Set([...Object.keys(agents.models ?? {}), ...Object.keys(agents.fallbacks ?? {})])]
    .filter((name) => uses(agents.models?.[name]) || uses(agents.fallbacks?.[name])).sort();
}

async function connect(p: any) {
  const methods: string[] = p.methods ?? [];
  if (methods.includes("api_key")) { providerKey.set(""); keyEntry.set({ id: p.id, label: p.label ?? p.id }); return; }
  const method = methods.includes("device") ? "device" : methods[0];
  if (!method) { toast("info", `Sign in to ${p.label} from a terminal: nexus login ${p.id}`); return; }
  try {
    const state = await request<any>("POST", `/api/provider/${p.id}/login`, { body: { method } });
    providerLogins.update((all) => ({ ...all, [p.id]: { provider: p.id, ...state } }));
    if (state.status === "connected") await reloadProviders();
  } catch (e) { providerLogins.update((all) => ({ ...all, [p.id]: { status: "failed", message: e instanceof Error ? e.message : String(e) } })); }
}

async function disconnect(p: any) {
  const users = poolUsers(p.id);
  const done = async () => {
    await request("DELETE", `/api/provider/${p.id}/login`);
    providerLogins.update((all) => { const { [p.id]: _, ...rest } = all; return rest; });
    await reloadProviders();
  };
  if (!users.length) { try { await done(); } catch (e) { toast("error", e instanceof Error ? e.message : String(e)); } return; }
  confirmDialog.set({ title: `Disconnect ${p.label}?`, destructive: true, confirm: "Disconnect", run: done,
    body: `Models from ${p.label} are listed for ${users.join(", ")}. They stay in those lists and are skipped while ${p.label} is not connected.` });
}

async function saveKey() {
  const key = providerKey(), provider = keyEntry();
  if (!provider) return;
  try {
    await request("POST", `/api/provider/${provider.id}/login`, { body: { method: "api_key", api_key: key } });
    providerKey.set(""); keyEntry.set(null);
    providerLogins.update((all) => { const { [provider.id]: _, ...rest } = all; return rest; });
    await reloadProviders();
  } catch (e) { providerLogins.update((all) => ({ ...all, [provider.id]: { status: "failed", message: e instanceof Error ? e.message : String(e) } })); }
}

function providerStatus(p: any): string {
  const count = (data().models?.models ?? []).filter((item: any) => item.ref?.startsWith(`${p.id}/`)).length;
  const parts = [p.connected ? (p.key_source === "env" ? `Using $${p.key_env}` : "Connected") : "Not connected"];
  if (p.connected && data().models) parts.push(`${count} model${count === 1 ? "" : "s"}`);
  if (!p.connected && (p.methods ?? []).includes("api_key")) parts.push("API key");
  if (!p.verified) parts.push("unverified");
  return parts.join(" · ");
}

function ProviderRow(p: { p: any }) {
  const login = () => providerLogins()[p.p.id];
  const pending = () => login()?.status === "pending";
  const failed = () => login()?.status === "failed";
  const keying = () => keyEntry()?.id === p.p.id;
  return <View class="gap-2 rounded-(--radius) px-3 py-2 hover:bg-accent" testID={`provider-${p.p.id}`}>
    <View class="flex-row items-center gap-3">
      <View class="flex-1">
        <Text class="text-[14px] leading-[22px] text-foreground">{p.p.label ?? p.p.id}</Text>
        <Text class={T.meta}>{providerStatus(p.p)}</Text>
      </View>
      <If when={p.p.connected}><Btn label="Disconnect" disabled={!live()} onPress={() => void disconnect(p.p)} testID={`provider-action-${p.p.id}`} /></If>
      <If when={!p.p.connected && !pending() && !keying()}>
        <Btn label={(p.p.methods ?? []).includes("api_key") ? "Add key" : "Connect"} disabled={!live()} onPress={() => void connect(p.p)} testID={`provider-action-${p.p.id}`} />
      </If>
      <If when={pending()}><Btn label="Cancel" onPress={() => void request("DELETE", `/api/provider/${p.p.id}/login`).then(() => providerLogins.update((all) => { const { [p.p.id]: _, ...rest } = all; return rest; }))} testID={`provider-cancel-${p.p.id}`} /></If>
    </View>
    <If when={pending()}><View class="gap-1 pl-0.5" testID={`provider-device-${p.p.id}`}>
      <If when={login()?.user_code}>{() => <View class="flex-row items-center gap-2">
        <Text class={T.sec}>Code</Text>
        <Text style={{ userSelect: "text" }} class="font-mono text-[15px] leading-5 font-semibold text-foreground">{login()!.user_code!}</Text>
        <Btn label="Copy" kind="ghost" icon="copy" onPress={() => copyText(login()!.user_code!, "Code copied")} />
      </View>}</If>
      <If when={login()?.verification_uri}>{() => <View class="flex-row items-center gap-2">
        <Text style={{ userSelect: "text" }} class={`flex-1 ${T.mono}`}>{login()!.verification_uri!}</Text>
        <Btn label="Open" kind="ghost" onPress={() => void openUrl(login()!.verification_uri!)} />
      </View>}</If>
      <If when={!login()?.user_code}><Text class={T.meta}>{login()?.message || "Starting sign-in…"}</Text></If>
    </View></If>
    <If when={keying()}><View class="gap-2">
      <Input masked autoFocus accessibilityLabel={`${p.p.label} API key`} placeholder={`API key ${p.p.key_hint ?? ""}`} onChange={({ value }) => providerKey.set(value)}
        class="h-9 rounded-(--radius) border border-border bg-background px-3 text-foreground" testID={`provider-key-${p.p.id}`} />
      <View class="flex-row items-center gap-2">
        <Btn label="Save key" kind="primary" onPress={() => void saveKey()} testID={`provider-key-save-${p.p.id}`} />
        <Btn label="Cancel" onPress={() => { providerKey.set(""); keyEntry.set(null); }} />
        <If when={p.p.key_env}><Text class={T.meta}>{`or set $${p.p.key_env}`}</Text></If>
      </View>
    </View></If>
    <If when={failed()}><Text class="text-xs leading-4 text-error" testID={`provider-error-${p.p.id}`}>{login()?.message ?? "Sign-in failed"}</Text></If>
  </View>;
}

function ProvidersPage(p: { sec: Section }) {
  const shown = () => (data().providers ?? []).filter((x: any) => x.id !== "mock");
  return <View class="gap-1" testID="providers-page">
    <View class="flex-row items-center gap-3 px-3 pb-1">
      <Text class={`flex-1 ${T.meta}`}>{data().models ? `Model catalog: ${data().models.source || catalogSource() || "models.dev"}` : "Loading models…"}</Text>
      <Btn label="Refresh models" kind="ghost" onPress={() => void refreshCatalog()} testID="setting-model-refresh" />
    </View>
    <If when={!data().providers}><Text class={`px-3 ${T.meta}`}>Loading providers…</Text></If>
    <Each of={shown()} key={(x: any) => x.id}>{(x) => <ProviderRow p={x()} />}</Each>
    <Each of={p.sec.row ?? []} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={p.sec} />}</Each>
  </View>;
}

function AgentCard(p: { a: any }) {
  const name = () => p.a.name;
  const live_ = () => new Set((data().providers ?? []).filter((x: any) => x.connected).map((x: any) => x.id));
  const connectedRef = (ref: string) => live_().has(ref.split("/")[0]);
  const pool = (): string[] => config()?.values?.agents?.models?.[name()] ?? [];
  const fallbacks = (): string[] => config()?.values?.agents?.fallbacks?.[name()] ?? [];
  const save = (key: "models" | "fallbacks", refs: string[]) => {
    const values = { ...(config()?.values?.agents?.[key] ?? {}) };
    if (refs.length) values[name()] = refs; else delete values[name()];
    void patchConfig(`agents.${key}`, values);
  };
  const odds = (ref: string) => {
    const eligible = pool().filter(connectedRef).length;
    return connectedRef(ref) && eligible ? `${Math.round((1000 / eligible)) / 10}%` : "0%";
  };
  const move = (ref: string, by: number) => {
    const refs = [...fallbacks()]; const i = refs.indexOf(ref), j = i + by;
    if (i < 0 || j < 0 || j >= refs.length) return;
    [refs[i], refs[j]] = [refs[j], refs[i]]; save("fallbacks", refs);
  };
  const add = (key: "models" | "fallbacks") => {
    const have = key === "models" ? pool() : fallbacks();
    const options = (data().models?.models ?? []).filter((item: any) => item.ref && !have.includes(item.ref));
    picker.set({ title: `${key === "models" ? "Models" : "Fallbacks"} for ${name()}`, note: "Connected providers only",
      items: options.map((item: any) => ({ id: item.ref, label: item.ref, hint: item.name ?? undefined, run: async () => { picker.set(null); save(key, [...have, item.ref]); } })) });
  };
  const tools = () => (p.a.tools ? p.a.tools.join(", ") : "all tools");
  return <View class="gap-2 px-3" testID={`agent-models-${name()}`}>
    <Text class={T.sec}>{p.a.description}</Text>
    <Text style={{ userSelect: "text" }} class={T.meta}>{[p.a.source, tools(), p.a.path].filter(Boolean).join(" · ")}</Text>
    <View class="flex-row items-baseline gap-2 pt-2"><Text class="text-[13px] font-medium leading-5 text-foreground">Models</Text><Text class={T.meta}>one picked at random</Text></View>
    <If when={!pool().length}><Text class={T.sec} testID={`agent-models-empty-${name()}`}>{`None: uses ${agentDefault(p.a)}.`}</Text></If>
    <Each of={pool()} key={(ref) => ref}>{(ref) => <View class="flex-row items-center gap-2" testID={`agent-model-${name()}-${ref()}`}>
      <Text class={`flex-1 ${T.mono}`}>{ref()}</Text>
      <If when={!connectedRef(ref())}><Text class="text-xs leading-4 text-warning">not connected · skipped</Text></If>
      <If when={connectedRef(ref())}><Text class={`w-12 text-right ${T.meta}`}>{odds(ref())}</Text></If>
      <Btn label="Remove" kind="ghost" disabled={!live()} onPress={() => save("models", pool().filter((v) => v !== ref()))} testID={`agent-model-remove-${name()}-${ref()}`} />
    </View>}</Each>
    <View class="flex-row"><Btn label="Add model" disabled={!live() || !data().models} testID={`agent-model-add-${name()}`} onPress={() => add("models")} /></View>
    <View class="flex-row items-baseline gap-2 pt-2"><Text class="text-[13px] font-medium leading-5 text-foreground">Fallbacks</Text><Text class={T.meta}>tried in order on failure</Text></View>
    <Each of={fallbacks()} key={(ref) => ref}>{(ref) => <View class="flex-row items-center gap-2" testID={`agent-fallback-${name()}-${ref()}`}>
      <Text class={`w-5 ${T.meta}`}>{`${fallbacks().indexOf(ref()) + 1}.`}</Text>
      <Text class={`flex-1 ${T.mono}`}>{ref()}</Text>
      <If when={!connectedRef(ref())}><Text class="text-xs leading-4 text-warning">not connected · skipped</Text></If>
      <Btn label="Up" kind="ghost" disabled={!live() || fallbacks().indexOf(ref()) === 0} onPress={() => move(ref(), -1)} />
      <Btn label="Down" kind="ghost" disabled={!live() || fallbacks().indexOf(ref()) === fallbacks().length - 1} onPress={() => move(ref(), 1)} />
      <Btn label="Remove" kind="ghost" disabled={!live()} onPress={() => save("fallbacks", fallbacks().filter((v) => v !== ref()))} testID={`agent-fallback-remove-${name()}-${ref()}`} />
    </View>}</Each>
    <View class="flex-row"><Btn label="Add fallback" disabled={!live() || !data().models} testID={`agent-fallback-add-${name()}`} onPress={() => add("fallbacks")} /></View>
    <Text class={`pt-2 ${T.meta}`}>Changes apply to new subagents; running ones keep what they started with.</Text>
  </View>;
}

function agentDefault(a: any): string {
  return a.subagent?.effective?.source === "agent" && a.subagent.effective.model ? `${a.subagent.effective.model} (agent file)` : "the parent session's model";
}

/** One subagent's line on the Agents page: its models in brief; `warn` when some are skipped. */
function agentSummary(a: any): { text: string; warn: boolean } {
  const live_ = new Set((data().providers ?? []).filter((x: any) => x.connected).map((x: any) => x.id));
  const pool: string[] = config()?.values?.agents?.models?.[a.name] ?? [];
  const fallbacks: string[] = config()?.values?.agents?.fallbacks?.[a.name] ?? [];
  const skipped = [...pool, ...fallbacks].filter((ref) => !live_.has(ref.split("/")[0])).length;
  const parts = [pool.length === 0 ? (agentDefault(a) === "the parent session's model" ? "parent's model" : agentDefault(a)) : pool.length === 1 ? pool[0] : `${pool.length} models`];
  if (fallbacks.length) parts.push(`${fallbacks.length} fallback${fallbacks.length === 1 ? "" : "s"}`);
  if (skipped) parts.push(`${skipped} skipped`);
  return { text: parts.join(" · "), warn: skipped > 0 };
}

function AgentsPage(p: { sec: Section }) {
  const agents = () => (data().agents?.agents ?? []).filter((a: any) => a.mode === "all" || a.mode === "subagent");
  return <View class="gap-3" testID="agent-models-page">
    <View>
      <Each of={p.sec.row ?? []} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={p.sec} />}</Each>
    </View>
    <Heading text="Subagents" />
    <If when={!data().agents}><Text class={`px-3 ${T.meta}`}>Loading agents…</Text></If>
    <Each of={data().agents?.problems ?? []} key={(problem: any) => JSON.stringify(problem)}>{(problem) => <Text class="px-3 text-xs leading-4 text-error">{typeof problem() === "string" ? problem() : `${problem().path}: ${problem().error}`}</Text>}</Each>
    <View>
      <Each of={agents()} key={(a: any) => a.name}>{(a) => (
        <Button onPress={() => settingsPage.set(`agents/${a().name}`)} testID={`agent-row-${a().name}`} accessibilityLabel={`${a().name}: ${agentSummary(a()).text}`}
          class={`flex-row items-center gap-6 rounded-(--radius) px-3 py-2 hover:bg-accent ${FOCUS}`}>
          <View class="flex-1"><Text class="text-[14px] leading-[22px] text-foreground">{a().name}</Text><Text class={T.meta} style={{ numberOfLines: 1 }}>{a().description}</Text></View>
          <Text class={`text-[13px] leading-5 ${agentSummary(a()).warn ? "text-warning" : "text-foreground-secondary"}`}>{agentSummary(a()).text}</Text>
          <Text class={T.meta}>›</Text>
        </Button>
      )}</Each>
    </View>
  </View>;
}

function control(r: Row): JSX.Element {
  const target = voiceTarget(r.id);
  if (target && (r.id.endsWith("_model"))) {
    return (
      <View testID={`voice-status-${target}`} class={`gap-1 ${isNarrow() ? "items-start" : "items-end"}`}>
        <Each of={voiceInfoLines(target)} key={(line) => line}>{(line) => <Text style={{ userSelect: "text", numberOfLines: 2 }} class="text-[12px] leading-4 text-foreground-secondary">{line()}</Text>}</Each>
      </View>
    );
  }
  const action = voiceAction(r.id);
  if (target && action && r.command) {
    const reason = voiceSettingReason(r.id);
    return <Btn label={r.label} onPress={() => void run(r.command!)} disabled={!!reason} reason={reason} testID={`setting-${r.id}-action`} />;
  }
  if (r.id === "appearance.theme") {
    return <Seg label="Theme" options={["system", "light", "dark"]} value={themeChoice()} disabled={false}
      onPick={(v) => { themeChoice.set(v as any); setColorScheme(v as "light" | "dark" | "system"); savePrefs(); }} />;
  }
  if (r.id === "appearance.animations") return <Seg label={r.label} options={["on", "off"]} value={config()?.values?.ui?.reduced_motion ? "off" : "on"} onPick={(v) => void patchConfig("ui.reduced_motion", v === "off")} />;
  if (r.id === "notifications.sound") return <Seg label={r.label} options={["off", "on"]} value={notificationSoundsOn() ? "on" : "off"} onPick={(v) => setNotificationSounds(v === "on")} />;
  if (r.id === "mcp.full_definitions") return <Seg label={r.label} options={["off", "on"]} value={config()?.values?.mcp?.full_definitions ? "on" : "off"} onPick={(v) => void patchConfig("mcp.full_definitions", v === "on")} />;
  if (r.id === "mcp.max_listed_tools") return <McpToolsLimit />;
  if (r.id === "context.handoff_percent") return <HandoffPercent />;
  if (r.id === "context.title_models") return <TitleModels />;
  if (r.id === "appearance.timeline_detail") {
    return <Seg label="Timeline detail" options={r.options ?? ["low", "medium", "high"]} value={timelineDetail()} disabled={false}
      onPick={(v) => setTimelineDetail(v as "low" | "medium" | "high")} />;
  }

  if (r.id === "about.inspector") return <Btn label="Open inspector" onPress={() => { void openUrl(new URL("/inspector", import.meta.url).href).catch(() => toast("info", "Open /inspector on the host page.")); }} />;
  if (r.id === "tools.default_permission") return <Seg label={r.label} options={["allow", "ask"]} value={config()?.values?.defaults?.permission ?? "allow"} onPick={(v) => void patchConfig("defaults.permission", v)} />;
  if (r.id === "tools.defaults") {
    const disabled: string[] = config()?.values?.tools?.disabled ?? [];
    return <View class="gap-1">{["read", "grep", "glob", "ls", "bash", "edit", "patch", "write", "web_search", "web_fetch", "ask", "code"].map((name) => <Button testID={`default-tool-${name}`} onPress={() => void patchConfig("tools.disabled", disabled.includes(name) ? disabled.filter((x) => x !== name) : [...disabled, name])} class={`rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Value text={`${disabled.includes(name) ? "off" : "on"} · ${name}`} /></Button>)}</View>;
  }
  const paths: Record<string, string> = { "agents.default_effort": "effort", "agents.default_root": "agent" };
  if (paths[r.id]) {
    const key = paths[r.id];
    const value = config()?.values?.defaults?.[key] || "automatic";
    return <Button testID={`edit-${r.id}`} accessibilityLabel={`Change ${r.label}`} onPress={() => {
      const options: string[] = key === "agent" ? (data().agents?.agents ?? []).filter((a: any) => a.mode !== "subagent").map((a: any) => a.name) : ["low", "medium", "high", "xhigh"];
      picker.set({ title: r.label, items: ["", ...options].map((v) => ({ id: v || "automatic", label: v || "automatic", run: async () => { picker.set(null); await patchConfig(`defaults.${key}`, v); } })) });
    }} class={`rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Value text={value} mono /></Button>;
  }
  if (r.kind === "choice" && r.options) return <Seg label={r.label} options={r.options} value={String(r.default ?? r.options[0])} disabled />;
  if (r.kind === "toggle") return <Text class="text-[13px] leading-5 text-muted-foreground">{r.default ? "[x] on" : "[ ] off"}</Text>;
  if (r.kind === "number") return <Text class="text-[13px] leading-5 text-muted-foreground">{String(r.default ?? "—")}</Text>;
  if (r.kind === "choice") return <Text class="text-[13px] leading-5 text-muted-foreground">{String(r.default ?? "not set")}</Text>;
  if (r.kind === "action") return <Btn label={r.label} disabled reason={r.unavailable_reason} />;
  return listFor(r.id);
}

function SettingRow(p: { r: Row; sec: Section }) {
  const r = p.r;
  const reason = r.unavailable_reason ?? p.sec.unavailable_reason;
  const wired = WIRED.has(r.id);
  const modelReason = voiceSettingReason(r.id);
  const disabled = !!reason || !wired || !!modelReason;
  const why = reason ?? modelReason ?? (wired ? "" : "Not editable in this NSH increment yet.");
  const tag = r.clients === "nsh" ? "NSH only" : r.clients === "tui" && r.id !== "appearance.timeline_detail" ? "TUI only" : "";
  return (
    <View class={`${isNarrow() ? "gap-2" : "flex-row gap-6 items-start"} px-3 py-2 rounded-(--radius) hover:bg-accent`} testID={`setting-${r.id}`} accessible accessibilityLabel={`${r.label}${disabled ? `, unavailable: ${why}` : ""}`}>
      <View class={isNarrow() ? "" : "w-[44%] pr-2"}>
        <View class="flex-row items-center gap-2">
          <Text class={`text-[14px] leading-[22px] ${disabled ? "text-muted-foreground" : "text-foreground"}`}>{r.label}</Text>
          <If when={tag}><Text class="text-xs leading-4 text-muted-foreground rounded-(--radius) border border-border px-1.5">{tag}</Text></If>
        </View>
        <If when={disabled && why}><Text class={T.meta}>{why}</Text></If>
        <If when={!disabled && r.note}><Text class={T.meta}>{r.note!}</Text></If>
      </View>
      <View class={isNarrow() ? "items-start" : "flex-1 items-end"}>{disabled && r.kind !== "list" && r.kind !== "info" && r.kind !== "keymap" ? <View class="opacity-60">{control(r)}</View> : control(r)}</View>
    </View>
  );
}

function pageSummary(sec: Section): string {
  if (sec.id === "providers") {
    const all = (data().providers ?? []).filter((x: any) => x.id !== "mock");
    return data().providers ? `${all.filter((x: any) => x.connected).length} of ${all.length} connected` : "Loading…";
  }
  if (sec.id === "agents") {
    const subs = (data().agents?.agents ?? []).filter((a: any) => a.mode !== "primary");
    const set = subs.filter((a: any) => (config()?.values?.agents?.models?.[a.name] ?? []).length).length;
    return data().agents ? `${subs.length} subagents · ${set} with their own models` : "Loading…";
  }
  return "";
}

/** A page section's entry in the section list: what it holds and a way in. */
function PageEntry(p: { sec: Section }) {
  return <View class="gap-2 px-3 py-2" testID={`page-entry-${p.sec.id}`}>
    <If when={p.sec.note}><Text class={T.sec}>{p.sec.note!}</Text></If>
    <Text class={T.meta}>{pageSummary(p.sec)}</Text>
    <View class="flex-row"><Btn label={`Open ${p.sec.label}`} icon="chevron-right" onPress={() => settingsPage.set(p.sec.id)} testID={`open-page-${p.sec.id}`} /></View>
  </View>;
}

function SettingsPage(p: { sec: Section }) {
  const agent = () => settingsPage()?.startsWith("agents/") ? settingsPage()!.slice("agents/".length) : null;
  const agentDef = () => (data().agents?.agents ?? []).find((a: any) => a.name === agent());
  return <View class="gap-3 min-h-0 flex-1" testID={`settings-page-${p.sec.id}`}>
    <View class="flex-row items-center gap-2">
      <Button onPress={() => settingsPage.set(agent() ? "agents" : null)} testID="settings-page-back" accessibilityLabel={agent() ? "Back to Agents" : "Back to Settings"}
        class={`flex-row items-center gap-1 rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}>
        <Text class="text-[13px] leading-5 text-foreground-secondary">{agent() ? "‹ Agents" : "‹ Settings"}</Text>
      </Button>
      <Text class="text-[13px] leading-5 text-muted-foreground">/</Text>
      <Text class="text-[13px] leading-5 font-medium text-foreground">{agent() ?? p.sec.label}</Text>
    </View>
    <Scroll class="flex-1 min-h-0" contentClass="pb-4 w-full gap-2">
      <Each of={config()?.errors ?? []} key={(e: string) => e}>{(e) => <Text class={`px-3 ${T.meta}`}>{e()}</Text>}</Each>
      <If when={p.sec.id === "providers"}><ProvidersPage sec={p.sec} /></If>
      <If when={p.sec.id === "agents" && !agent()}><AgentsPage sec={p.sec} /></If>
      <If when={agent() && agentDef()}>{() => <AgentCard a={agentDef()} />}</If>
    </Scroll>
  </View>;
}

export function SettingsView() {
  loadSettingsData();
  let seen = settingsReload();
  effect(() => { const n = settingsReload(); if (n !== seen) { seen = n; loadSettingsData(); } });
  const cur = () => sections.find((s) => s.id === settingsSection()) ?? sections[0];
  const page = () => sections.find((s) => s.page && s.id === settingsPage()?.split("/")[0]);
  const list = () => (cur().row ?? []).filter((r) => r.clients !== "tui" || r.id === "appearance.timeline_detail");
  void m; void model; void compat; void colorScheme; void byId; void keyLabel;
  return (
    <Modal width={820} top="top-[8%]" label="Settings" testID="settings" onCancel={() => escape()} onOpenChange={(open) => { if (!open && centre() === "settings") void run("app.settings"); }}>
      <View class="gap-3">
        <ModalTitle title="Settings" />
        <View class={`${isNarrow() || page() ? "" : "flex-row gap-4"} min-h-0`} style={{ height: Math.max(240, Math.min(520, Math.round(windowSize().height * 0.76) - 48 - 32 - 12 - 12 - 28)) }}>
          <If when={page()}>{() => <SettingsPage sec={page()!} />}</If>
          <If when={!page()}><View class={isNarrow() ? "pb-2" : "w-[180px]"}>
            <Scroll horizontal={isNarrow()} scrollbar={false} class={isNarrow() ? "" : "flex-1"} contentClass={isNarrow() ? "flex-row gap-1" : "gap-0.5"}>
              <Each of={sections} key={(s) => s.id}>
                {(s) => (
                  <Button onPress={() => { settingsSection.set(s().id); if (s().page) settingsPage.set(s().id); }} selected={cur().id === s().id} testID={`section-${s().id}`} accessibilityLabel={s().label + (s().page ? ", opens a page" : "") + (s().unavailable_reason ? " (unavailable)" : "")}
                    class={`relative flex-row items-center rounded-(--radius) px-3 hover:bg-accent ${isNarrow() ? "min-h-10" : "min-h-8"} ${FOCUS}`}>
                    <SelectBar on={cur().id === s().id} />
                    <Text class={`flex-1 text-[13px] leading-5 ${cur().id === s().id ? "text-foreground font-medium" : s().unavailable_reason ? "text-muted-foreground" : "text-foreground-secondary"}`}>{s().label}</Text>
                    <If when={s().page && !isNarrow()}><Text class={T.meta}>›</Text></If>
                  </Button>
                )}
              </Each>
            </Scroll>
          </View></If>
          <If when={!page()}><Scroll class="flex-1 min-h-0" contentClass="pb-4 w-full">
            <Heading text={cur().label} first />
            <Each of={config()?.errors ?? []} key={(e: string) => e}>{(e) => <Text class={T.meta}>{e()}</Text>}</Each>
            <If when={cur().unavailable_reason}><View class="flex-row gap-2 items-start pt-1 pb-1"><Text class="text-[13px] text-muted-foreground">⊖</Text><Text class={`flex-1 ${T.sec}`}>{cur().unavailable_reason!}</Text></View></If>
            <If when={cur().page}><PageEntry sec={cur()} /></If>
            <If when={!cur().page}><Each of={list()} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each></If>
          </Scroll></If>
        </View>
        <ModalFooter hints={page() ? "esc back" : "esc close"} right={`Core ${info()?.version ?? "—"}`} />
      </View>
    </Modal>
  );
}
