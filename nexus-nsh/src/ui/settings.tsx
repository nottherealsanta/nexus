import type { JSX } from "nsh";
import { Scroll, Text, View, effect, keymap, setColorScheme, signal, openUrl, colorScheme, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, FocusEdge, Icon, T, TS, FOCUS, Seg } from "./kit";
import { Heading, Modal, ModalFooter, ModalTitle } from "./modal";
import { manifest } from "../gen/manifest";
import { mobileSettings } from "../mobile";
import { byId, commands, copyText, escape, keyLabel, loadProviders, run } from "../commands";
import { archivedFilter, leftOpen, rightOpen, scopeAll, catalogSource, info, compat, isNarrow, live, m, model, reloadModelCatalog, savePrefs, scrollBarOn, setScrollBar, setTimelineDetail, settingsNavFocused, settingsPage, settingsPageOpen, settingsQuery, settingsReload, settingsSection, themeChoice, centre, timelineDetail, toast, currentProject, config, loadConfig, patchConfig, picker, providerDetail, providerLogins, confirmDialog, notificationSoundsOn, setNotificationSounds, phone } from "../store";
import { CLIENT_API, CLIENT_VERSION } from "../core/api";
import { request } from "../core/transport";
import { voiceInfoLines, voiceActionReason } from "../voice-settings";
import { voiceStatus } from "../voice-state";
import { searchStartReason, searchStatusLines } from "../search-service";
import { MANAGER_ROWS, managerControl } from "./manager/settings";
import type { VoiceTarget } from "../voice-state";

interface Row { id: string; label: string; kind: string; scope: string; clients?: string; default?: unknown; options?: string[]; command?: string; note?: string; unavailable_reason?: string }
interface Section { id: string; label: string; unavailable_reason?: string; page?: boolean; note?: string; row?: Row[] }
const sections = phone ? mobileSettings(manifest.settings.section as unknown as Section[]) : manifest.settings.section as unknown as Section[];
const focusedSetting = signal<string | null>(null);
const focusSetting = (id: string) => { settingsNavFocused.set(false); focusedSetting.set(id); };

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

function Value(p: { text: string; mono?: boolean }) { return <Text style={{ userSelect: "text" }} class={`${p.mono ? TS.mono : TS.base} text-foreground-secondary ${isNarrow() ? "text-left" : "text-right"}`}>{p.text}</Text>; }

function listFor(id: string): JSX.Element {
  const d = data();
  const lines = (xs: string[], none: string) => (xs.length ? <View class={`gap-1 ${isNarrow() ? "items-start" : "items-end"}`}>{xs.map((x) => <Value text={x} />)}</View> : <Value text={none} />);
  switch (id) {
    case "tools.defaults": return <Value text="—" />;
    case "dictation.push_to_talk": { const c = byId.get("voice.dictate"); return <Value text={(c && keyLabel(c)) ?? "not set"} />; }
    case "keybindings.list": return <Value text={`${commands.filter((c) => c.keys?.nsh).length} commands with keys (see the palette)`} />;
    case "about.versions": {
      const i = info();
      return lines([`client ${CLIENT_VERSION} (API ${CLIENT_API.major}.${CLIENT_API.minor})`, `Core ${i?.version ?? "—"}`, `API ${i?.api_version ? JSON.stringify(i.api_version) : "not reported"}`, `NSH 0.4.0`], "—");
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
const WIRED = new Set(["appearance.sidebar", "appearance.details", "appearance.session_scope", "appearance.show_archived", "dictation.push_to_talk", "appearance.theme", "appearance.animations", "appearance.timeline_detail", "appearance.scrollbar", "notifications.sound", "about.inspector", "agents.default_root", "tools.default_permission", "agents.default_effort", "tools.defaults", "tools.search_status", "tools.search_start", "mcp.full_definitions", "mcp.max_listed_tools", "context.handoff_percent", "context.title_models", "keybindings.list", "about.versions", "about.daemon", "dictation.stt_model", "dictation.stt_download", "dictation.stt_remove", "dictation.tts_model", "dictation.tts_download", "dictation.tts_remove", ...MANAGER_ROWS]);

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
  if (id === "tools.search_start") return live() ? searchStartReason() : "Core is not connected: the view is read-only.";
  const target = voiceTarget(id), action = voiceAction(id);
  if (!target || !action) return undefined;
  if (!live()) return "Core is not connected: the view is read-only.";
  return voiceActionReason(target, action, voiceStatus()?.[target]);
}

function UsageRefreshMinutes() {
  const value = signal(String(config()?.values?.usage?.refresh_minutes ?? 10));
  const revision = signal<any>(config()?.revision);
  effect(() => {
    const current = config();
    if (current && current.revision !== revision()) { revision.set(current.revision); value.set(String(current.values?.usage?.refresh_minutes ?? 10)); }
  });
  const save = () => {
    const parsed = Number(value());
    if (!value().trim() || !Number.isInteger(parsed) || parsed < 1 || parsed > 1440) { toast("warning", "Enter a whole number from 1 to 1440."); return; }
    void patchConfig("usage.refresh_minutes", parsed);
  };
  return <View class="flex-row items-center gap-2">
    <Input value={value()} placeholder="1–1440" accessibilityLabel="Refresh interval (minutes)" testID="usage-refresh-minutes"
      onChange={({ value: text }) => value.set(text)} onSubmit={save} submitBehavior="submit"
      class={`h-8 w-24 rounded-none bg-background px-2 text-right ${TS.mono} text-foreground`} />
    <Btn label="Save" onPress={save} testID="usage-refresh-minutes-save" />
  </View>;
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
        class={`h-8 w-24 rounded-none bg-background px-2 text-right ${TS.mono} text-foreground`} />
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
        class={`h-8 min-w-0 flex-1 rounded-none bg-background px-2 ${TS.mono} text-foreground`} />
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
        class={`h-8 w-24 rounded-none bg-background px-2 text-right ${TS.mono} text-foreground`} />
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

/** The row that starts one provider sign-in method; Core lists the methods in order. */
const SIGN_IN_LABELS: Record<string, string> = {
  browser: "Sign in with browser",
  device: "Sign in with a device code",
  cli: "Sign in with Claude Code",
  local: "Use the local server",
};

async function connect(p: any, method?: string) {
  const methods: string[] = p.methods ?? [];
  if (method === "api_key" || (!method && methods.includes("api_key"))) { providerKey.set(""); keyEntry.set({ id: p.id, label: p.label ?? p.id }); return; }
  const chosen = method ?? (methods.includes("device") ? "device" : methods[0]);
  if (!chosen) { toast("info", `Sign in to ${p.label} from a terminal: nexus login ${p.id}`); return; }
  try {
    const state = await request<any>("POST", `/api/provider/${p.id}/login`, { body: { method: chosen } });
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

/** One provider's connection and sign-in details, opened from its row. */
export function ProviderDetailModal() {
  const id = () => providerDetail();
  const provider = () => (data().providers ?? []).find((x: any) => x.id === id());
  const login = () => providerLogins()[id() ?? ""];
  const pending = () => login()?.status === "pending";
  const failed = () => login()?.status === "failed";
  const keying = () => keyEntry()?.id === id();
  const close = () => { providerDetail.set(null); providerKey.set(""); keyEntry.set(null); };
  const open = (url: string) => void openUrl(url).catch(() => toast("info", "Open this link in your browser."));
  return <If when={!!id() && !!provider()}>{() => <Modal size="medium" label={`${provider()!.label ?? id()} connection`} testID="provider-detail" onCancel={close} onOpenChange={(o) => { if (!o) close(); }}>
    <View class="gap-3">
      <ModalTitle title={provider()!.label ?? id()!} />
      <Text class={T.sec}>{pending() ? "Sign-in pending · waiting for you" : failed() ? "Sign-in failed · open" : providerStatus(provider())}</Text>
      <If when={provider()!.connected}>{() => <View class="gap-2">
        <If when={provider()!.key_source === "env"}>{() => <Text class={T.meta}>{`Using $${provider()!.key_env}`}</Text>}</If>
        <View class="flex-row"><Btn label="Disconnect" kind="danger" disabled={!live()} onPress={() => void disconnect(provider())} testID="provider-detail-disconnect" /></View>
      </View>}</If>
      <If when={pending()}>{() => <View class="gap-2">
        <If when={login()?.verification_uri}>{() => <Text class={T.meta}>{login()?.user_code ? "Open the link and enter the code below." : "Open the link to sign in."}</Text>}</If>
        <If when={login()?.verification_uri}>{() => <View class="flex-row items-center gap-2">
          <Button onPress={() => open(login()!.verification_uri!)} accessibilityLabel={`Open ${login()!.verification_uri}`} testID="provider-detail-link"
            class={`flex-1 rounded-none px-2 min-h-9 items-start justify-center hover:bg-accent ${FOCUS}`}>
            <Text style={{ userSelect: "text" }} class={`${TS.mono} text-info underline`}>{login()!.verification_uri!}</Text>
          </Button>
          <Btn label="Open" kind="ghost" icon="web" onPress={() => open(login()!.verification_uri!)} testID="provider-detail-open" />
        </View>}</If>
        <If when={login()?.user_code}>{() => <View class="flex-row items-center gap-2">
          <Text class={T.sec}>Code</Text>
          <Text style={{ userSelect: "text" }} class={`${TS.title} font-mono font-semibold text-foreground`}>{login()!.user_code!}</Text>
          <Btn label="Copy" kind="ghost" icon="copy" onPress={() => copyText(login()!.user_code!, "Code copied")} testID="provider-detail-copy" />
        </View>}</If>
        <If when={!login()?.user_code && !login()?.verification_uri}><Text class={T.meta}>{login()?.message || "Starting sign-in…"}</Text></If>
        <Text class={T.meta}>Waiting for authorization…</Text>
        <View class="flex-row"><Btn label="Cancel sign-in" onPress={() => void request("DELETE", `/api/provider/${id()}/login`).then(() => providerLogins.update((all) => { const { [id()!]: _, ...rest } = all; return rest; }))} testID="provider-detail-cancel" /></View>
      </View>}</If>
      <If when={!provider()!.connected && !pending()}>{() => <View class="gap-2">
        <If when={failed()}><Text class={`${TS.base} text-error`}>{login()?.message ?? "Sign-in failed"}</Text></If>
        <If when={(provider()!.methods ?? []).includes("api_key")}>{() => <View class="flex-row"><Btn label="Add API key" kind="primary" disabled={!live()} onPress={() => void connect(provider())} testID="provider-detail-key" /></View>}</If>
        {(provider()!.methods ?? []).filter((method: string) => method !== "api_key").map((method: string) => (
          <View class="flex-row"><Btn label={SIGN_IN_LABELS[method] ?? `Sign in (${method})`} disabled={!live()} onPress={() => void connect(provider(), method)} testID={`provider-detail-${method}`} /></View>
        ))}
        <If when={provider()!.key_env}><Text class={T.meta}>{`Or set $${provider()!.key_env}`}</Text></If>
        <If when={provider()!.key_hint}><Text class={T.meta}>{provider()!.key_hint}</Text></If>
      </View>}</If>
      <If when={keying()}>{() => <View class="gap-2">
        <Input masked autoFocus accessibilityLabel={`${provider()!.label} API key`} placeholder={`API key ${provider()!.key_hint ?? ""}`} onChange={({ value }) => providerKey.set(value)}
          class={`h-9 rounded-none bg-background px-3 ${TS.mono} text-foreground`} testID={`provider-key-${id()}`} />
        <View class="flex-row items-center gap-2">
          <Btn label="Save key" kind="primary" onPress={() => void saveKey()} testID={`provider-key-save-${id()}`} />
          <Btn label="Cancel" onPress={() => { providerKey.set(""); keyEntry.set(null); }} />
        </View>
      </View>}</If>
      <View class="flex-row items-center justify-between pt-1">
        <ModalFooter hints="esc close" />
        <Btn label="Close" kind="ghost" onPress={close} testID="provider-detail-close" />
      </View>
    </View>
  </Modal>}</If>;
}

function ProviderRow(p: { p: any }) {
  const id = p.p.id;
  const login = () => providerLogins()[id];
  const pending = () => login()?.status === "pending";
  const failed = () => login()?.status === "failed";
  return <View testID={`provider-${id}`}>
    <Button onPress={() => { keyEntry.set(null); providerKey.set(""); providerDetail.set(id); }} testID={`provider-open-${id}`}
      accessibilityLabel={`${p.p.label ?? id}: connection and sign-in`}
      class={`flex-row items-center gap-3 rounded-none px-3 py-2 hover:bg-accent ${FOCUS}`}>
      <View class="flex-1">
        <Text class={T.base}>{p.p.label ?? id}</Text>
        <Text class={T.meta}>{pending() ? "Sign-in pending · waiting for you" : failed() ? "Sign-in failed · open" : providerStatus(p.p)}</Text>
      </View>
      <Text class={T.meta}>Open</Text>
    </Button>
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

/** The agent and model whose effort the arrow keys step: set when its stepper is pressed or focused. */
const effortTarget = signal<{ agent: string; ref: string; step: (by: number) => void } | null>(null);
let effortKeysBound = false;
function bindEffortKeys() {
  if (effortKeysBound) return;
  effortKeysBound = true;
  for (const [key, by] of [[manifest.settings.navigation.value_key, 1], [manifest.settings.navigation.previous_value_key, -1]] as const) {
    try { keymap.bind(key, () => { const t = effortTarget(); if (t && settingsPage() === `agents/${t.agent}`) t.step(by); }, { when: "always" }); } catch { /* the platform keeps this keystroke */ }
  }
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
  const efforts = (): Record<string, string> => config()?.values?.agents?.efforts?.[name()] ?? {};
  const levels = (ref: string): string[] => ((data().models?.models ?? []).find((item: any) => item.ref === ref)?.efforts ?? []).map(String);
  /** Advancing past the last effort returns to "default". */
  const stepEffort = (ref: string, by: number) => {
    const all = levels(ref);
    if (!all.length) { toast("info", `${ref} lists no effort levels`); return; }
    const at = efforts()[ref] ? all.indexOf(efforts()[ref]) + 1 : 0;
    const next = by > 0 ? (at + by) % (all.length + 1) : Math.max(0, Math.min(all.length, at + by));
    if (next === at) return;
    const mine = { ...efforts() };
    if (next === 0) delete mine[ref]; else mine[ref] = all[next - 1];
    const values = { ...(config()?.values?.agents?.efforts ?? {}) };
    if (Object.keys(mine).length) values[name()] = mine; else delete values[name()];
    void patchConfig("agents.efforts", values);
  };
  const EffortStepper = (q: { model: string }) => {
    const target = () => { bindEffortKeys(); effortTarget.set({ agent: name(), ref: q.model, step: (by) => stepEffort(q.model, by) }); };
    return <If when={levels(q.model).length || efforts()[q.model]}>
      <View focusable onFocus={target} accessibilityLabel={`Effort for ${q.model}`} testID={`agent-effort-${name()}-${q.model}`} class="flex-row items-center gap-1">
        <Btn label="‹" kind="ghost" disabled={!live()} onPress={() => { target(); stepEffort(q.model, -1); }} testID={`agent-effort-down-${name()}-${q.model}`} />
        <Text class={`w-24 text-center ${T.meta}`}>{`effort ${efforts()[q.model] ?? "default"}`}</Text>
        <Btn label="›" kind="ghost" disabled={!live()} onPress={() => { target(); stepEffort(q.model, 1); }} testID={`agent-effort-up-${name()}-${q.model}`} />
      </View>
    </If>;
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
    <View class="flex-row items-baseline gap-2 pt-2"><Text class={T.label}>Models</Text><Text class={T.meta}>one picked at random</Text></View>
    <If when={!pool().length}><Text class={T.sec} testID={`agent-models-empty-${name()}`}>{`None: uses ${agentDefault(p.a)}.`}</Text></If>
    <Each of={pool()} key={(ref) => ref}>{(ref) => <View class="flex-row items-center gap-2" testID={`agent-model-${name()}-${ref()}`}>
      <Text class={`flex-1 ${T.mono}`}>{ref()}</Text>
      <If when={!connectedRef(ref())}><Text class={`${TS.base} text-warning`}>not connected · skipped</Text></If>
      <If when={connectedRef(ref())}><EffortStepper model={ref()} /><Text class={`w-12 text-right ${T.meta}`}>{odds(ref())}</Text></If>
      <Btn label="Remove" kind="ghost" disabled={!live()} onPress={() => save("models", pool().filter((v) => v !== ref()))} testID={`agent-model-remove-${name()}-${ref()}`} />
    </View>}</Each>
    <View class="flex-row"><Btn label="Add model" disabled={!live() || !data().models} testID={`agent-model-add-${name()}`} onPress={() => add("models")} /></View>
    <View class="flex-row items-baseline gap-2 pt-2"><Text class={T.label}>Fallbacks</Text><Text class={T.meta}>tried in order on failure</Text></View>
    <Each of={fallbacks()} key={(ref) => ref}>{(ref) => <View class="flex-row items-center gap-2" testID={`agent-fallback-${name()}-${ref()}`}>
      <Text class={`w-5 ${T.meta}`}>{`${fallbacks().indexOf(ref()) + 1}.`}</Text>
      <Text class={`flex-1 ${T.mono}`}>{ref()}</Text>
      <If when={!connectedRef(ref())}><Text class={`${TS.base} text-warning`}>not connected · skipped</Text></If>
      <If when={connectedRef(ref())}><EffortStepper model={ref()} /></If>
      <Btn label="Up" kind="ghost" disabled={!live() || fallbacks().indexOf(ref()) === 0} onPress={() => move(ref(), -1)} />
      <Btn label="Down" kind="ghost" disabled={!live() || fallbacks().indexOf(ref()) === fallbacks().length - 1} onPress={() => move(ref(), 1)} />
      <Btn label="Remove" kind="ghost" disabled={!live()} onPress={() => save("fallbacks", fallbacks().filter((v) => v !== ref()))} testID={`agent-fallback-remove-${name()}-${ref()}`} />
    </View>}</Each>
    <View class="flex-row"><Btn label="Add fallback" disabled={!live() || !data().models} testID={`agent-fallback-add-${name()}`} onPress={() => add("fallbacks")} /></View>
    <Text class={`pt-2 ${T.meta}`}>←/→ sets this model's effort. Changes apply to new subagents; running ones keep what they started with.</Text>
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
    <View class="px-3"><Heading text="Subagents" /></View>
    <If when={!data().agents}><Text class={`px-3 ${T.meta}`}>Loading agents…</Text></If>
    <Each of={data().agents?.problems ?? []} key={(problem: any) => JSON.stringify(problem)}>{(problem) => <Text class={`px-3 ${TS.base} text-error`}>{typeof problem() === "string" ? problem() : `${problem().path}: ${problem().error}`}</Text>}</Each>
    <View>
      <Each of={agents()} key={(a: any) => a.name}>{(a) => (
        <Button onPress={() => { settingsNavFocused.set(false); settingsPageOpen.set(true); settingsPage.set(`agents/${a().name}`); }} testID={`agent-row-${a().name}`} accessibilityLabel={`${a().name}: ${agentSummary(a()).text}`}
          class={`flex-row items-center gap-6 rounded-none px-3 py-2 hover:bg-accent ${FOCUS}`}>
          <View class="flex-1"><Text class={T.base}>{a().name}</Text><Text class={T.meta} style={{ numberOfLines: 1 }}>{a().description}</Text></View>
          <Text class={`${TS.base} ${agentSummary(a()).warn ? "text-warning" : "text-foreground-secondary"}`}>{agentSummary(a()).text}</Text>
          <Icon name="chevron-right" size={14} class="tint-muted-foreground" />
        </Button>
      )}</Each>
    </View>
  </View>;
}

function control(r: Row): JSX.Element {
  const managed = managerControl(r.id, r.label, r.options);
  if (managed) return managed;
  const target = voiceTarget(r.id);
  if (target && (r.id.endsWith("_model"))) {
    return (
      <View testID={`voice-status-${target}`} class={`gap-1 ${isNarrow() ? "items-start" : "items-end"}`}>
        <Each of={voiceInfoLines(target)} key={(line) => line}>{(line) => <Text style={{ userSelect: "text", numberOfLines: 2 }} class={`${TS.base} text-foreground-secondary`}>{line()}</Text>}</Each>
      </View>
    );
  }
  if (r.id === "tools.search_status") {
    return (
      <View testID="search-status" class={`gap-1 ${isNarrow() ? "items-start" : "items-end"}`}>
        <Each of={searchStatusLines()} key={(line) => line}>{(line) => <Text style={{ userSelect: "text", numberOfLines: 2 }} class={`${TS.base} text-foreground-secondary`}>{line()}</Text>}</Each>
      </View>
    );
  }
  if (r.id === "tools.search_start") {
    const reason = voiceSettingReason(r.id);
    return <Btn onFocus={() => focusSetting(r.id)} label={r.label} onPress={() => void run(r.command!)} disabled={!!reason} reason={reason} testID={`setting-${r.id}-action`} />;
  }
  const action = voiceAction(r.id);
  if (target && action && r.command) {
    const reason = voiceSettingReason(r.id);
    return <Btn onFocus={() => focusSetting(r.id)} label={r.label} onPress={() => void run(r.command!)} disabled={!!reason} reason={reason} testID={`setting-${r.id}-action`} />;
  }
  if (r.id === "appearance.theme") {
    return <Seg onFocus={() => focusSetting(r.id)} label="Theme" options={["system", "light", "dark"]} value={themeChoice()} disabled={false} format={(v) => `${v[0].toUpperCase()}${v.slice(1)}`}
      onPick={(v) => { themeChoice.set(v as any); setColorScheme(v as "light" | "dark" | "system"); savePrefs(); }} />;
  }
  if (r.id === "appearance.sidebar") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["full", "rail"]} value={leftOpen() ? "full" : "rail"} onPick={(v) => { leftOpen.set(v === "full"); savePrefs(); }} />;
  if (r.id === "appearance.details") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={rightOpen() ? "on" : "off"} onPick={(v) => { rightOpen.set(v === "on"); savePrefs(); }} />;
  if (r.id === "appearance.scrollbar") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={scrollBarOn() ? "on" : "off"} onPick={(v) => setScrollBar(v === "on")} />;
  if (r.id === "appearance.session_scope") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["this project", "all projects"]} value={scopeAll() ? "all projects" : "this project"} onPick={(v) => scopeAll.set(v === "all projects")} />;
  if (r.id === "appearance.show_archived") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={archivedFilter() ? "on" : "off"} onPick={(v) => archivedFilter.set(v === "on")} />;
  if (r.id === "appearance.animations") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["on", "off"]} value={config()?.values?.ui?.reduced_motion ? "off" : "on"} onPick={(v) => void patchConfig("ui.reduced_motion", v === "off")} />;
  if (r.id === "notifications.sound") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={notificationSoundsOn() ? "on" : "off"} onPick={(v) => setNotificationSounds(v === "on")} />;
  if (r.id === "mcp.full_definitions") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={config()?.values?.mcp?.full_definitions ? "on" : "off"} onPick={(v) => void patchConfig("mcp.full_definitions", v === "on")} />;
  if (r.id === "mcp.max_listed_tools") return <McpToolsLimit />;
  if (r.id === "context.handoff_percent") return <HandoffPercent />;
  if (r.id === "context.usage_refresh_minutes") return <UsageRefreshMinutes />;
  if (r.id === "context.title_models") return <TitleModels />;
  if (r.id === "appearance.timeline_detail") {
    return <Seg onFocus={() => focusSetting(r.id)} label="Timeline detail" options={r.options ?? ["low", "medium", "high"]} value={timelineDetail()} disabled={false}
      onPick={(v) => setTimelineDetail(v as "low" | "medium" | "high")} />;
  }

  if (r.id === "about.inspector") return <Btn onFocus={() => focusSetting(r.id)} label="Open inspector" onPress={() => { void run("app.open_inspector"); }} />;
  if (r.id === "tools.default_permission") return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={["allow", "ask"]} value={config()?.values?.defaults?.permission ?? "allow"} onPick={(v) => void patchConfig("defaults.permission", v)} />;
  if (r.id === "tools.defaults") {
    const disabled: string[] = config()?.values?.tools?.disabled ?? [];
    return <View class="w-full flex-row flex-wrap justify-end gap-1">{["read", "grep", "glob", "ls", "bash", "edit", "patch", "write", "web_search", "web_fetch", "ask", "code"].map((name) => {
      const off = disabled.includes(name);
      return <View focusable onFocus={() => focusSetting(r.id)} role="switch" accessibilityState={{ selected: !off }} testID={`default-tool-${name}`} accessibilityLabel={`${name} ${off ? "off" : "on"}`} onPress={() => void patchConfig("tools.disabled", off ? disabled.filter((x) => x !== name) : [...disabled, name])} class={`rounded-none px-2 ${isNarrow() ? "min-h-11" : "min-h-7"} justify-center hover:bg-accent ${FOCUS}`}>
        <Text class={`${TS.mono} ${off ? "text-muted-foreground line-through opacity-60" : "text-foreground-secondary"}`}>{name}</Text>
      </View>;
    })}</View>;
  }
  const paths: Record<string, string> = { "agents.default_effort": "effort", "agents.default_root": "agent" };
  if (paths[r.id]) {
    const key = paths[r.id];
    const value = config()?.values?.defaults?.[key] || "automatic";
    return <View role="button" focusable onFocus={() => focusSetting(r.id)} testID={`edit-${r.id}`} accessibilityLabel={`Change ${r.label}`} onPress={() => {
      const options: string[] = key === "agent" ? (data().agents?.agents ?? []).filter((a: any) => a.mode !== "subagent").map((a: any) => a.name) : ["low", "medium", "high", "xhigh"];
      picker.set({ title: r.label, items: ["", ...options].map((v) => ({ id: v || "automatic", label: v || "automatic", run: async () => { picker.set(null); await patchConfig(`defaults.${key}`, v); } })) });
    }} class={`rounded-none px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Value text={value} mono /></View>;
  }
  if (r.kind === "choice" && r.options) return <Seg onFocus={() => focusSetting(r.id)} label={r.label} options={r.options} value={String(r.default ?? r.options[0])} disabled />;
  if (r.kind === "toggle") return <Text class={`${TS.base} text-muted-foreground`}>{r.default ? "[x] on" : "[ ] off"}</Text>;
  if (r.kind === "number") return <Text class={`${TS.base} text-muted-foreground`}>{String(r.default ?? "—")}</Text>;
  if (r.kind === "choice") return <Text class={`${TS.base} text-muted-foreground`}>{String(r.default ?? "not set")}</Text>;
  if (r.kind === "action") return <Btn onFocus={() => focusSetting(r.id)} label={r.label} disabled reason={r.unavailable_reason} />;
  return listFor(r.id);
}

function SettingRow(p: { r: Row; sec: Section }) {
  const r = p.r;
  const reason = r.unavailable_reason ?? p.sec.unavailable_reason;
  const wired = WIRED.has(r.id);
  const modelReason = voiceSettingReason(r.id);
  const disabled = !!reason || !wired || !!modelReason;
  const notEditable = "Not editable in this NSH increment yet.";
  const why = reason ?? modelReason ?? (wired ? "" : notEditable);
  const tag = r.clients === "nsh" ? "NSH only" : r.clients === "tui" && r.id !== "appearance.timeline_detail" ? "TUI only" : "";
  const tooltip = <Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{notEditable}</Text>;
  return (
    <View focusable onFocus={() => focusSetting(r.id)}
      onPress={() => { settingsNavFocused.set(false); focusedSetting.set(r.id); }}
      class={`relative ${isNarrow() ? "gap-2" : "flex-row gap-3 items-center"} px-3 py-2 rounded-none hover:bg-settings-selection focus-within:bg-settings-selection ${focusedSetting() === r.id ? "bg-settings-selection" : ""}`} testID={`setting-${r.id}`} accessible accessibilityLabel={`${r.label}${disabled ? `, unavailable: ${why}` : ""}`}>
      <If when={!settingsNavFocused() && focusedSetting() === r.id}><FocusEdge testID="settings-row-focus" /></If>
      <View class={isNarrow() ? "" : "w-[44%] pr-2"}>
        <View class="flex-row items-center gap-2">
          <Text class={`${TS.base} ${disabled ? "text-muted-foreground" : "text-foreground"}`}>{r.label}</Text>
          <If when={tag}><Text class={`${TS.base} text-muted-foreground rounded-none px-1.5`}>{tag}</Text></If>
        </View>
        {/* A row with no control yet keeps that note in its tooltip, not in every row's second line. */}
        <If when={disabled && why && wired}><Text class={T.meta}>{why}</Text></If>
        <If when={!disabled && r.note}><Text class={T.meta}>{r.note!}</Text></If>
      </View>
      <View class={isNarrow() ? "items-start" : "flex-1 items-end"}>
        <If when={!wired} else={disabled && r.kind !== "list" && r.kind !== "info" && r.kind !== "keymap" ? <View class="opacity-60">{control(r)}</View> : control(r)}>
          <View tooltip={tooltip}>{disabled && r.kind !== "list" && r.kind !== "info" && r.kind !== "keymap" ? <View class="opacity-60">{control(r)}</View> : control(r)}</View>
        </If>
      </View>
    </View>
  );
}

export function SettingsView(p: { inline?: boolean }) {
  loadSettingsData();
  let seen = settingsReload();
  effect(() => { const n = settingsReload(); if (n !== seen) { seen = n; loadSettingsData(); } });
  const query = () => settingsQuery().trim().toLowerCase();
  const pageTerms = (s: Section): string[] => {
    if (s.id === "providers") return (data().providers ?? []).flatMap((p: any) => [p.id ?? "", p.label ?? ""]);
    if (s.id === "agents") {
      const names = (data().agents?.agents ?? []).filter((a: any) => a.mode !== "primary").map((a: any) => a.name);
      const pools = Object.values(config()?.values?.agents?.models ?? {}).flat();
      const fallbacks = Object.values(config()?.values?.agents?.fallbacks ?? {}).flat();
      return [...names, ...pools, ...fallbacks].filter((x): x is string => typeof x === "string");
    }
    return [];
  };
  const hit = (s: Section) => {
    const q = query();
    if (!q) return true;
    return s.label.toLowerCase().includes(q) || (s.note ?? "").toLowerCase().includes(q)
      || (s.row ?? []).some((r) => r.label.toLowerCase().includes(q) || (r.note ?? "").toLowerCase().includes(q))
      || pageTerms(s).some((term) => term.toLowerCase().includes(q));
  };
  const shown = () => sections.filter(hit);
  const cur = () => shown().find((s) => s.id === settingsSection()) ?? shown()[0] ?? sections[0];
  const sectionRefs = new Map<string, { focus: () => void }>();
  const focusSection = () => {
    if (providerDetail() || picker()) return;
    settingsNavFocused.set(true);
    settingsPageOpen.set(false);
    effortTarget.set(null);
    sectionRefs.get(cur().id)?.focus();
  };
  keymap.bind(manifest.settings.navigation.section_key, focusSection, { when: "notEditing" });
  effect(() => {
    if (!settingsNavFocused() || providerDetail() || picker()) return;
    const offs = ([ ["up", -1], ["down", 1] ] as const).map(([key, by]) => keymap.bind(key, () => {
      const index = shown().findIndex((s) => s.id === cur().id);
      const next = shown()[Math.max(0, Math.min(shown().length - 1, index + by))];
      if (next) { settingsNavFocused.set(true); settingsSection.set(next.id); settingsPage.set(null); settingsPageOpen.set(false); sectionRefs.get(next.id)?.focus(); }
    }, { when: "notEditing" }));
    return () => offs.forEach((off) => off());
  });
  const agent = () => !query() && settingsPage()?.startsWith("agents/") ? settingsPage()!.slice("agents/".length) : null;
  const agentDef = () => (data().agents?.agents ?? []).find((a: any) => a.name === agent());
  const list = () => (cur().row ?? []).filter((r) => (r.clients !== "tui" || r.id === "appearance.timeline_detail") && (!query() || r.label.toLowerCase().includes(query()) || (r.note ?? "").toLowerCase().includes(query())));
  const page = () => (query() ? null : (settingsPage() || settingsPageOpen()) && cur().page ? cur().id : null);
  /** The TUI's hint row for the current pane, one key/verb pair at a time. */
  const footerHints = (): string[] => {
    if (settingsNavFocused()) return ["↑↓ section", "enter open", "esc close"];
    if (agent()) return ["enter select", "→ effort", "d remove", "K/J reorder", "← sections", "esc back"];
    if (page() === "providers" || page() === "agents") return ["enter open", "esc back"];
    if (page()) return ["enter select", "esc back"];
    return ["→ change", "enter select", "← sections", "esc back"];
  };
  effect(() => { focusedSetting.set(list()[0]?.id ?? null); });
  void m; void model; void compat; void colorScheme; void byId; void keyLabel;
  const content = () => <View class={p.inline ? "flex-1 min-h-0 gap-3 p-4" : "gap-3"}>
        <If when={!p.inline}><ModalTitle title="Settings" /></If>
        <View class="flex-row items-center gap-2 px-3">
          <Icon name="search" size={14} class="tint-muted-foreground" />
          <Input value={settingsQuery()} placeholder="Filter settings…" accessibilityLabel="Filter settings" testID="settings-search"
            onChange={({ value }) => settingsQuery.set(value)}
            class={`h-8 min-w-0 flex-1 rounded-none bg-background px-2 ${TS.base} text-foreground`} />
          <If when={settingsQuery()}>{() => <Btn label="Clear" kind="ghost" onPress={() => settingsQuery.set("")} testID="settings-search-clear" />}</If>
        </View>
        <View class={`${isNarrow() ? "" : "flex-row gap-4"} min-h-0 ${p.inline ? "flex-1" : ""}`} style={p.inline ? {} : { height: Math.max(64, Math.min(520, Math.floor(windowSize().height * 0.70) - 180)) }}>
          <View class={isNarrow() ? "pb-2" : "w-[180px]"}>
            <Scroll horizontal={isNarrow()} scrollbar={false} class={isNarrow() ? "" : "flex-1"}>
              <View class={isNarrow() ? "flex-row gap-2" : "gap-0"}>
              <Each of={shown()} key={(s) => s.id}>
                {(s) => (
                  <View role="button" focusable ref={(n: any) => { if (n) sectionRefs.set(s().id, n); else sectionRefs.delete(s().id); }}
                    onPress={() => { settingsNavFocused.set(!s().page); settingsPageOpen.set(!!s().page); settingsSection.set(s().id); settingsPage.set(null); settingsQuery.set(""); }} accessibilityState={{ selected: cur().id === s().id }} testID={`section-${s().id}`} accessibilityLabel={s().label}
                    class={`relative flex-row items-center rounded-none px-3 hover:bg-settings-selection ${cur().id === s().id ? "bg-settings-selection" : ""} ${isNarrow() ? "min-h-10" : "min-h-8"}`}>
                    <If when={cur().id === s().id && settingsNavFocused()}><FocusEdge testID="settings-section-focus" /></If>
                    <Text class={`flex-1 ${TS.base} ${cur().id === s().id ? "text-foreground font-medium" : "text-foreground-secondary"}`}>{s().label}</Text>
                  </View>
                )}
              </Each>
              </View>
            </Scroll>
          </View>
          <View class="flex-1 min-h-0" testID={`settings-page-${page() ?? cur().id}`}>
            <Scroll class="flex-1 min-h-0">
              <View class="pb-4 w-full">
                <Each of={config()?.errors ?? []} key={(e: string) => e}>{(e) => <Text class={`px-3 ${T.meta}`}>{e()}</Text>}</Each>
                <If when={!shown().length}><Text class={`px-3 ${T.meta}`} testID="settings-no-matches">No settings match.</Text></If>
                <If when={page() === "providers"}>{() => <ProvidersPage sec={cur()} />}</If>
                <If when={page() === "agents" && !agent()}>{() => <AgentsPage sec={cur()} />}</If>
                <If when={page() === "agents" && agent()}>{() => <View class="gap-2 pb-2">
                  <View class="flex-row items-center gap-2 px-3">
                    <Button onPress={() => settingsPage.set(null)} testID="settings-page-back" accessibilityLabel="Back to Agents"
                      class={`flex-row items-center gap-1 rounded-none px-2 min-h-8 hover:bg-accent ${FOCUS}`}>
                      <Icon name="arrow-left" size={14} class="tint-muted-foreground" />
                      <Text class={`${TS.base} text-foreground-secondary`}>Agents</Text>
                    </Button>
                    <Text class={`${TS.base} text-muted-foreground`}>/</Text>
                    <Text class={T.label}>{agent()}</Text>
                  </View>
                  <If when={agentDef()}>{() => <AgentCard a={agentDef()} />}</If>
                </View>}</If>
                <If when={!page()}>
                  <View class="px-3"><Heading text={cur().label} first /></View>
                  <If when={query() && cur().page}>{() => <View class="px-3 pb-1">
                    <Btn label={`Open ${cur().label}`} onPress={() => settingsQuery.set("")} testID={`open-page-${cur().id}`} />
                  </View>}</If>
                  <If when={cur().note}><Text class={`px-3 ${T.meta}`}>{cur().note!}</Text></If>
                  <Each of={list()} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each>
                </If>
              </View>
            </Scroll>
          </View>
        </View>
        <If when={!p.inline}><ModalFooter hints={footerHints()} right={`Core ${info()?.version ?? "—"}`} /></If>
      </View>;
  return p.inline ? content() : <Modal size="large" label="Settings" testID="settings" onCancel={() => escape()} onOpenChange={(open) => { if (!open && centre() === "settings") void run("app.settings"); }}>{content()}</Modal>;
}
