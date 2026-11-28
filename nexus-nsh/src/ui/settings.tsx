import type { JSX } from "nsh";
import { Scroll, Text, View, computed, effect, keymap, setColorScheme, signal, openUrl, colorScheme, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, CursorMark, Icon, T, TS, FOCUS, Seg } from "./kit";
import { Heading, Modal, ModalFooter, ModalTitle } from "./modal";
import { manifest } from "../gen/manifest";
import { mobileSettings } from "../mobile";
import { byId, commands, copyText, escape, keyLabel, loadProviders, run } from "../commands";
import { archivedFilter, leftOpen, rightOpen, scopeAll, catalogSource, info, compat, connection, coreUrl, isNarrow, live, m, model, reloadModelCatalog, savePrefs, scrollBarOn, setScrollBar, turnRailOn, setTurnRail, setTimelineDetail, settingsEdit, settingsSegs, settingsNavFocused, settingsPage, settingsSearchActive, settingsQuery, settingsReload, settingsSection, themeChoice, centre, timelineDetail, toast, currentProject, config, loadConfig, patchConfig, picker, providerDetail, providerLogins, confirmDialog, notificationSoundsOn, setNotificationSounds, phone } from "../store";
import { timeFormat } from "../time";
import { coreEditing } from "./connect";
import { CLIENT_API, CLIENT_VERSION } from "../core/api";
import { request } from "../core/transport";
import { voiceInfoLines, voiceActionReason } from "../voice-settings";
import { voiceStatus } from "../voice-state";
import { refreshSearchService, searchService, searchStartReason, searchStatusLines } from "../search-service";
import { browserStatusLines, refreshBrowserStatus } from "../browser-status";
import { MANAGER_ROWS, managerControl } from "./manager/settings";
import { McpPage, loadMcp, mcpDetail, mcpServers } from "./mcp-settings";
import { agentEfforts, catalogEfforts, effortLabel, effortRowLabel, effortText, openEffortPicker, writeAgentEffort } from "../effort";
import type { VoiceTarget } from "../voice-state";

interface Row { id: string; label: string; kind: string; scope: string; clients?: string; default?: unknown; options?: string[]; command?: string; note?: string; unavailable_reason?: string }
interface Section { id: string; label: string; unavailable_reason?: string; page?: boolean; note?: string; row?: Row[] }
const sections = phone ? mobileSettings(manifest.settings.section as unknown as Section[]) : manifest.settings.section as unknown as Section[];
const rowsOf = (s: Section) => (s.row ?? []).filter((r) => r.clients !== "tui" || r.id === "appearance.timeline_detail");
const rail = manifest.settings.rail.map((group) => ({ label: group.label, sections: group.sections.flatMap((id) => sections.filter((s) => s.id === id && (s.page || rowsOf(s).length))) }));
const railSections = rail.flatMap((group) => group.sections);
const focusedSetting = signal<string | null>(null);
const focusSetting = (id: string) => { settingsNavFocused.set(false); focusedSetting.set(id); };
const settingRowRefs = new Map<string, { focus: () => void }>();
/** The open Settings pane's key handler (SettingsView sets it), for bindings made elsewhere. */
let settingsKeys: ((key: string) => boolean) | null = null;

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
  void loadMcp();
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
const WIRED = new Set(["appearance.sidebar", "appearance.details", "appearance.session_scope", "appearance.show_archived", "dictation.push_to_talk", "appearance.theme", "appearance.animations", "appearance.timeline_detail", "appearance.time_format", "appearance.scrollbar", "appearance.turn_rail", "notifications.sound", "about.inspector", "about.onboarding", "agents.default_root", "tools.default_permission", "agents.default_effort", "tools.defaults", "tools.search_status", "tools.search_start", "mcp.full_definitions", "browser.use_my_chrome", "browser.status", "mcp.max_listed_tools", "workers.claude_backend", "workers.antigravity", "context.handoff_percent", "context.title_models", "keybindings.list", "about.versions", "about.daemon", "dictation.stt_model", "dictation.stt_download", "dictation.stt_remove", "dictation.tts_model", "dictation.tts_download", "dictation.tts_remove", ...MANAGER_ROWS]);

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
      class={`h-8 w-24 rounded-(--radius) bg-background px-2 text-right ${TS.base} tabular-nums text-foreground`} />
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
        class={`h-8 w-24 rounded-(--radius) bg-background px-2 text-right ${TS.base} tabular-nums text-foreground`} />
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
        class={`h-8 min-w-0 flex-1 rounded-(--radius) bg-background px-2 ${TS.base} text-foreground`} />
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
        class={`h-8 w-24 rounded-(--radius) bg-background px-2 text-right ${TS.base} tabular-nums text-foreground`} />
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
  "browser-long": "Sign in with browser (1-year token)",
  "cli-profile": "Use signed-in Claude Code profile (read-only)",
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
      <If when={provider()!.note}>{() => provider()!.id === "claude-direct" ? <View testID="provider-detail-note"><Each of={String(provider()!.note).split("\n")}>{(line) => <Text class={provider()!.connected ? T.meta : `${TS.base} text-warning`}>{line()}</Text>}</Each></View> : <Text class={provider()!.connected ? T.meta : `${TS.base} text-warning`} testID="provider-detail-note">{provider()!.note}</Text>}</If>
      <If when={provider()!.connected}>{() => <View class="gap-2">
        <If when={provider()!.key_source === "env"}>{() => <Text class={T.meta}>{`Using $${provider()!.key_env}`}</Text>}</If>
        <View class="flex-row"><Btn label="Disconnect" kind="danger" disabled={!live()} onPress={() => void disconnect(provider())} testID="provider-detail-disconnect" /></View>
      </View>}</If>
      <If when={pending()}>{() => <View class="gap-2">
        <If when={login()?.verification_uri}>{() => <Text class={T.meta}>{login()?.user_code ? "Open the link and enter the code below." : "Open the link to sign in."}</Text>}</If>
        <If when={login()?.verification_uri}>{() => <View class="flex-row items-center gap-2">
          <Button onPress={() => open(login()!.verification_uri!)} accessibilityLabel={`Open ${login()!.verification_uri}`} testID="provider-detail-link"
            class={`flex-1 rounded-(--radius) px-2 min-h-9 items-start justify-center hover:bg-accent ${FOCUS}`}>
            <Text style={{ userSelect: "text" }} class={`${TS.base} text-info underline`}>{login()!.verification_uri!}</Text>
          </Button>
          <Btn label="Open" kind="ghost" icon="web" onPress={() => open(login()!.verification_uri!)} testID="provider-detail-open" />
        </View>}</If>
        <If when={login()?.user_code}>{() => <View class="flex-row items-center gap-2">
          <Text class={T.sec}>Code</Text>
          <Text style={{ userSelect: "text" }} class={`${TS.title} tabular-nums tracking-wider font-semibold text-foreground`}>{login()!.user_code!}</Text>
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
          class={`h-9 rounded-(--radius) bg-background px-3 ${TS.base} text-foreground`} testID={`provider-key-${id()}`} />
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
    <Button onPress={() => { settingsNavFocused.set(false); keyEntry.set(null); providerKey.set(""); providerDetail.set(id); }} testID={`provider-open-${id}`}
      accessibilityLabel={`${p.p.label ?? id}: connection and sign-in`}
      class={`flex-row items-center gap-3 rounded-(--radius) px-3 py-2 hover:bg-accent ${FOCUS}`}>
      <View class="flex-1">
        <Text class={T.base}>{p.p.label ?? id}</Text>
        <Text class={T.meta}>{pending() ? "Sign-in pending · waiting for you" : failed() ? "Sign-in failed · open" : providerStatus(p.p)}</Text>
      </View>
      <Text class={T.meta}>Open</Text>
    </Button>
  </View>;
}

function ProvidersPage(p: { sec: Section; firstRef?: (node: any) => void }) {
  const shown = () => (data().providers ?? []).filter((x: any) => x.id !== "mock");
  return <View class="gap-1" testID="providers-page">
    <View class="flex-row items-center gap-3 px-3 pb-1">
      <Text class={`flex-1 ${T.meta}`}>{data().models ? `Model catalog: ${data().models.source || catalogSource() || "models.dev"}` : "Loading models…"}</Text>
      <Btn ref={p.firstRef} label="Refresh models" kind="ghost" onPress={() => void refreshCatalog()} testID="setting-model-refresh" />
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
    try { keymap.bind(key, () => { const t = effortTarget(); if (t && settingsPage() === `agents/${t.agent}`) t.step(by); else if (by > 0) settingsKeys?.("right"); }, { when: "always" }); } catch { /* the platform keeps this keystroke */ }
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
  const efforts = (): Record<string, string> => agentEfforts(config(), name());
  const levels = (ref: string): string[] => catalogEfforts(data().models, ref);
  const agentEffort = (): string | null => (typeof p.a.effort === "string" && p.a.effort ? p.a.effort : null);
  /** Advancing past the last effort returns to "default". */
  const stepEffort = (ref: string, by: number) => {
    const all = levels(ref);
    if (!all.length) {
      if (!efforts()[ref]) { toast("warning", effortText("effort.no_levels", { model: ref })); return; }
      void writeAgentEffort(name(), ref, null);
      return;
    }
    const at = efforts()[ref] ? all.indexOf(efforts()[ref]) + 1 : 0;
    const next = by > 0 ? (at + by) % (all.length + 1) : Math.max(0, Math.min(all.length, at + by));
    if (next === at) return;
    void writeAgentEffort(name(), ref, next === 0 ? null : all[next - 1]);
  };
  const EffortStepper = (q: { model: string }) => {
    const target = () => { bindEffortKeys(); effortTarget.set({ agent: name(), ref: q.model, step: (by) => stepEffort(q.model, by) }); };
    const open = () => { if (!live()) return; target(); openEffortPicker(name(), q.model, agentEffort(), levels(q.model)); };
    return <If when={levels(q.model).length || efforts()[q.model]}>
      <View accessibilityLabel={`Effort for ${q.model}`} testID={`agent-effort-${name()}-${q.model}`} class="flex-row items-center gap-1">
        <Btn label="‹" kind="ghost" disabled={!live()} onFocus={target} onPress={() => { target(); stepEffort(q.model, -1); }} testID={`agent-effort-down-${name()}-${q.model}`} />
        <View role="button" focusable disabled={!live()} onFocus={target} onPress={open} accessibilityLabel={`Set effort for ${q.model}: ${effortLabel(efforts()[q.model] ?? null, agentEffort())}`}
          testID={`agent-effort-pick-${name()}-${q.model}`} class={`rounded-(--radius) px-1 hover:bg-accent disabled:opacity-50 ${FOCUS}`}>
          <Text style={{ numberOfLines: 1 }} class={`w-48 text-center ${T.meta}`}>{effortRowLabel(efforts()[q.model] ?? null, agentEffort())}</Text>
        </View>
        <Btn label="›" kind="ghost" disabled={!live()} onFocus={target} onPress={() => { target(); stepEffort(q.model, 1); }} testID={`agent-effort-up-${name()}-${q.model}`} />
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
    <Each of={pool()} key={(ref) => ref}>{(ref) => <View class="gap-1" testID={`agent-model-${name()}-${ref()}`}>
      <View class="flex-row items-center gap-2">
        <Text class={`flex-1 ${T.sec}`} style={{ numberOfLines: 1 }}>{ref()}</Text>
        <If when={!connectedRef(ref())}><Text class={`${TS.base} text-warning`}>not connected · skipped</Text></If>
        <If when={connectedRef(ref())}><Text class={`w-12 text-right ${T.meta}`}>{odds(ref())}</Text></If>
        <Btn label="Remove" kind="ghost" disabled={!live()} onPress={() => save("models", pool().filter((v) => v !== ref()))} testID={`agent-model-remove-${name()}-${ref()}`} />
      </View>
      <View class="flex-row justify-end"><EffortStepper model={ref()} /></View>
    </View>}</Each>
    <View class="flex-row"><Btn label="Add model" disabled={!live() || !data().models} testID={`agent-model-add-${name()}`} onPress={() => add("models")} /></View>
    <View class="flex-row items-baseline gap-2 pt-2"><Text class={T.label}>Fallbacks</Text><Text class={T.meta}>tried in order on failure</Text></View>
    <Each of={fallbacks()} key={(ref) => ref}>{(ref) => <View class="gap-1" testID={`agent-fallback-${name()}-${ref()}`}>
      <View class="flex-row items-center gap-2">
        <Text class={`w-5 ${T.meta}`}>{`${fallbacks().indexOf(ref()) + 1}.`}</Text>
        <Text class={`flex-1 ${T.sec}`} style={{ numberOfLines: 1 }}>{ref()}</Text>
        <If when={!connectedRef(ref())}><Text class={`${TS.base} text-warning`}>not connected · skipped</Text></If>
        <Btn label="Up" kind="ghost" disabled={!live() || fallbacks().indexOf(ref()) === 0} onPress={() => move(ref(), -1)} />
        <Btn label="Down" kind="ghost" disabled={!live() || fallbacks().indexOf(ref()) === fallbacks().length - 1} onPress={() => move(ref(), 1)} />
        <Btn label="Remove" kind="ghost" disabled={!live()} onPress={() => save("fallbacks", fallbacks().filter((v) => v !== ref()))} testID={`agent-fallback-remove-${name()}-${ref()}`} />
      </View>
      <View class="flex-row justify-end"><EffortStepper model={ref()} /></View>
    </View>}</Each>
    <View class="flex-row"><Btn label="Add fallback" disabled={!live() || !data().models} testID={`agent-fallback-add-${name()}`} onPress={() => add("fallbacks")} /></View>
    <Text class={`pt-2 ${T.meta}`}>{effortText("effort.settings_hint")}</Text>
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
        <Button onPress={() => { settingsNavFocused.set(false); settingsPage.set(`agents/${a().name}`); }} testID={`agent-row-${a().name}`} accessibilityLabel={`${a().name}: ${agentSummary(a()).text}`}
          class={`flex-row items-center gap-6 rounded-(--radius) px-3 py-2 hover:bg-accent ${FOCUS}`}>
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
  if (r.id === "browser.status") {
    return (
      <View testID="browser-status" class={`gap-1 ${isNarrow() ? "items-start" : "items-end"}`}>
        <Each of={browserStatusLines()} key={(line) => line}>{(line) => <Text style={{ userSelect: "text", numberOfLines: 3 }} class={`${TS.base} text-foreground-secondary`}>{line()}</Text>}</Each>
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
    return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label="Theme" options={["system", "light", "dark"]} value={themeChoice()} disabled={false} format={(v) => `${v[0].toUpperCase()}${v.slice(1)}`}
      onPick={(v) => { themeChoice.set(v as any); setColorScheme(v as "light" | "dark" | "system"); savePrefs(); }} />;
  }
  if (r.id === "appearance.sidebar") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["full", "rail"]} value={leftOpen() ? "full" : "rail"} onPick={(v) => { leftOpen.set(v === "full"); savePrefs(); }} />;
  if (r.id === "appearance.details") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={rightOpen() ? "on" : "off"} onPick={(v) => { rightOpen.set(v === "on"); savePrefs(); }} />;
  if (r.id === "appearance.scrollbar") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={scrollBarOn() ? "on" : "off"} onPick={(v) => setScrollBar(v === "on")} />;
  if (r.id === "appearance.turn_rail") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={turnRailOn() ? "on" : "off"} onPick={(v) => setTurnRail(v === "on")} />;
  if (r.id === "appearance.session_scope") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["this project", "all projects"]} value={scopeAll() ? "all projects" : "this project"} onPick={(v) => scopeAll.set(v === "all projects")} />;
  if (r.id === "appearance.show_archived") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={archivedFilter() ? "on" : "off"} onPick={(v) => archivedFilter.set(v === "on")} />;
  if (r.id === "appearance.animations") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["on", "off"]} value={config()?.values?.ui?.reduced_motion ? "off" : "on"} onPick={(v) => void patchConfig("ui.reduced_motion", v === "off")} />;
  if (r.id === "appearance.time_format") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["24h", "12h", "system"]} value={timeFormat()} onPick={(v) => void patchConfig("ui.time_format", v)} />;
  if (r.id === "notifications.sound") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={notificationSoundsOn() ? "on" : "off"} onPick={(v) => setNotificationSounds(v === "on")} />;
  if (r.id === "browser.use_my_chrome") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={config()?.values?.browser?.use_my_chrome ? "on" : "off"} onPick={(v) => void patchConfig("browser.use_my_chrome", v === "on").then(refreshBrowserStatus)} />;
  if (r.id === "mcp.full_definitions") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "on"]} value={config()?.values?.mcp?.full_definitions ? "on" : "off"} onPick={(v) => void patchConfig("mcp.full_definitions", v === "on")} />;
  if (r.id === "workers.claude_backend") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["Agent SDK", "PTY"]} value={["cli", "zmx"].includes(config()?.values?.workers?.claude_backend) ? "PTY" : "Agent SDK"} onPick={(v) => void patchConfig("workers.claude_backend", v === "PTY" ? "cli" : "sdk")} />;
  if (r.id === "workers.antigravity") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["off", "ACP"]} value={config()?.values?.workers?.antigravity ? "ACP" : "off"} onPick={(v) => void patchConfig("workers.antigravity", v === "ACP")} />;
  if (r.id === "mcp.max_listed_tools") return <McpToolsLimit />;
  if (r.id === "context.handoff_percent") return <HandoffPercent />;
  if (r.id === "context.usage_refresh_minutes") return <UsageRefreshMinutes />;
  if (r.id === "context.title_models") return <TitleModels />;
  if (r.id === "appearance.timeline_detail") {
    return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label="Timeline detail" options={r.options ?? ["low", "medium", "high"]} value={timelineDetail()} disabled={false}
      onPick={(v) => setTimelineDetail(v as "low" | "medium" | "high")} />;
  }

  if (r.id === "about.onboarding") return <Btn onFocus={() => focusSetting(r.id)} label="Replay onboarding" onPress={() => { void run("app.onboarding"); }} testID="setting-about-onboarding" />;
  if (r.id === "about.inspector") return <Btn onFocus={() => focusSetting(r.id)} label="Open inspector" onPress={() => { void run("app.open_inspector"); }} />;
  if (r.id === "tools.default_permission") return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={["allow", "ask"]} value={config()?.values?.defaults?.permission ?? "allow"} onPick={(v) => void patchConfig("defaults.permission", v)} />;
  if (r.id === "tools.defaults") {
    const disabled: string[] = config()?.values?.tools?.disabled ?? [];
    return <View class="w-full flex-row flex-wrap justify-end gap-1">{["read", "grep", "glob", "ls", "bash", "edit", "patch", "write", "web_search", "web_fetch", "firecrawl", "ask", "code"].map((name) => {
      const off = disabled.includes(name);
      return <View focusable onFocus={() => focusSetting(r.id)} role="switch" accessibilityState={{ selected: !off }} testID={`default-tool-${name}`} accessibilityLabel={`${name} ${off ? "off" : "on"}`} onPress={() => void patchConfig("tools.disabled", off ? disabled.filter((x) => x !== name) : [...disabled, name])} class={`rounded-(--radius) px-2 ${isNarrow() ? "min-h-11" : "min-h-7"} justify-center hover:bg-accent ${FOCUS}`}>
        <Text class={`${TS.base} ${off ? "text-muted-foreground line-through opacity-60" : "text-foreground-secondary"}`}>{name}</Text>
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
    }} class={`rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Value text={value} /></View>;
  }
  if (r.kind === "choice" && r.options) return <Seg id={r.id} onFocus={() => focusSetting(r.id)} label={r.label} options={r.options} value={String(r.default ?? r.options[0])} disabled />;
  if (r.kind === "toggle") return <Text class={`${TS.base} text-muted-foreground`}>{r.default ? "[x] on" : "[ ] off"}</Text>;
  if (r.kind === "number") return <Text class={`${TS.base} text-muted-foreground`}>{String(r.default ?? "—")}</Text>;
  if (r.kind === "choice") return <Text class={`${TS.base} text-muted-foreground`}>{String(r.default ?? "not set")}</Text>;
  if (r.kind === "action") return <Btn onFocus={() => focusSetting(r.id)} label={r.label} disabled reason={r.unavailable_reason} />;
  return listFor(r.id);
}

/** Whether Settings cannot change this row (the reason, an unwired row, or a missing voice model). */
function rowDisabled(r: Row, sec: Section): boolean {
  const reason = r.unavailable_reason ?? sec.unavailable_reason ?? (r.id.startsWith("workers.") ? (config() as any)?.workers_unavailable ?? (config() as any)?.zmx_unavailable ?? undefined : undefined);
  return !!reason || !WIRED.has(r.id) || !!voiceSettingReason(r.id);
}

function SettingRow(p: { r: Row; sec: Section }) {
  const r = p.r;
  // External workers need POSIX PTYs; on another platform Core says why and both rows read as unavailable with it.
  const reason = r.unavailable_reason ?? p.sec.unavailable_reason ?? (r.id.startsWith("workers.") ? (config() as any)?.workers_unavailable ?? (config() as any)?.zmx_unavailable ?? undefined : undefined);
  const wired = WIRED.has(r.id);
  const modelReason = voiceSettingReason(r.id);
  const disabled = !!reason || !wired || !!modelReason;
  const notEditable = "Not editable in this NSH increment yet.";
  const why = reason ?? modelReason ?? (wired ? "" : notEditable);
  const tag = r.clients === "nsh" ? "NSH only" : r.clients === "tui" && r.id !== "appearance.timeline_detail" ? "TUI only" : "";
  const tooltip = <Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{notEditable}</Text>;
  return (
    <View focusable onFocus={() => focusSetting(r.id)} ref={(n: any) => { if (n) settingRowRefs.set(r.id, n); else settingRowRefs.delete(r.id); }}
      onPress={() => { settingsNavFocused.set(false); focusedSetting.set(r.id); }}
      class={`relative ${isNarrow() ? "gap-2" : "flex-row gap-3 items-center"} px-3 py-2 rounded-(--radius) hover:bg-row-selected focus-within:bg-row-selected ${!settingsNavFocused() && focusedSetting() === r.id ? "bg-row-selected" : ""}`} testID={`setting-${r.id}`} accessible accessibilityLabel={`${r.label}${disabled ? `, unavailable: ${why}` : ""}`}>
      <If when={!settingsNavFocused() && focusedSetting() === r.id}><CursorMark testID="settings-row-focus" /></If>
      <View class={isNarrow() ? "" : "w-[56%] pr-2"}>
        <View class="flex-row items-center gap-2">
          <Text class={`${TS.base} ${disabled ? "text-muted-foreground" : "text-foreground"}`}>{r.label}</Text>
          <If when={tag}><Text class={`${TS.base} text-muted-foreground rounded-(--radius) px-1.5`}>{tag}</Text></If>
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
  // The container start is asynchronous: follow it while Settings is open until SearXNG runs.
  effect(() => {
    void refreshSearchService();
    const timer = setInterval(() => { if (searchService()?.state !== "running") void refreshSearchService(); }, 2000);
    return () => clearInterval(timer);
  });
  const query = () => settingsQuery().trim().toLowerCase();
  const pageTerms = (s: Section): string[] => {
    if (s.id === "providers") return (data().providers ?? []).filter((p: any) => p.id !== "mock").flatMap((p: any) => [p.id ?? "", p.label ?? ""]);
    if (s.id === "mcp") return ["servers", "remote", "sign in", "oauth", "header", ...(mcpServers()?.servers ?? []).map((row) => row.server)];
    if (s.id === "agents") {
      const agents = (data().agents?.agents ?? []).filter((a: any) => a.mode === "all" || a.mode === "subagent");
      const values = config()?.values?.agents;
      return ["models", "fallbacks", "subagents", ...agents.flatMap(({ name }: { name: string }) => [name, ...(values?.models?.[name] ?? []), ...(values?.fallbacks?.[name] ?? [])])];
    }
    return [];
  };
  type Result = { section: Section; row?: Row; rank: number };
  const results = computed(() => {
    const q = query();
    if (!q || !settingsSearchActive()) return [];
    const matches = (value?: string) => !!value?.toLowerCase().includes(q);
    const hits: Result[] = [];
    for (const section of railSections) {
      for (const row of rowsOf(section)) {
        const rank = matches(row.label) ? 0 : row.options?.some(matches) ? 1 : matches(row.note) ? 3 : -1;
        if (rank >= 0) hits.push({ section, row, rank });
      }
      const rank = matches(section.label) ? 0 : matches(section.note) || (section.page && pageTerms(section).some(matches)) ? 2 : -1;
      if (rank >= 0) hits.push({ section, rank });
    }
    return hits.sort((a, b) => a.rank - b.rank);
  });
  const cur = () => railSections.find((s) => s.id === settingsSection()) ?? railSections[0];
  const list = () => rowsOf(cur());
  const page = () => cur().page ? cur().id : null;
  const overlayOpen = () => !!(providerDetail() || mcpDetail() || picker() || confirmDialog());
  type FocusNode = { focus: () => void };
  const sectionRefs = new Map<string, FocusNode>();
  const pageStarts = new Map<string, FocusNode>();
  const setPageStart = (id: string, node: FocusNode | null) => { if (node) pageStarts.set(id, node); else pageStarts.delete(id); };
  const focusContent = (row: string | null) => {
    focusedSetting.set(row);
    (row ? settingRowRefs.get(row) : pageStarts.get(cur().id))?.focus();
  };
  const resumeContent = (row: string | null) => {
    if (!settingsNavFocused() && !settingsSearchActive()) focusContent(row);
  };
  const focusSection = () => {
    if (overlayOpen()) return;
    settingsEdit.set(null);
    settingsNavFocused.set(true);
    effortTarget.set(null);
    sectionRefs.get(cur().id)?.focus();
  };
  const selectSection = (section: Section) => {
    settingsNavFocused.set(true);
    settingsEdit.set(null);
    settingsPage.set(null);
    settingsSection.set(section.id);
    focusedSetting.set(null);
    effortTarget.set(null);
    sectionRefs.get(section.id)?.focus();
  };
  const firstRow = () => page() && page() !== "agents" ? null : list()[0]?.id ?? null;
  const searchIndex = signal(0);
  const selectedResult = () => Math.min(searchIndex(), Math.max(0, results().length - 1));
  const bodyHeight = () => Math.max(64, Math.min(520, Math.floor(windowSize().height * 0.70) - 180));
  let searchInput: FocusNode | null = null;
  let searchScroll: any = null;
  let resumeFocus: (() => void) | null = null;
  const beginSearch = () => {
    if (settingsSearchActive() || overlayOpen() || settingsEdit()) return;
    const row = focusedSetting();
    resumeFocus = settingsNavFocused() ? () => { if (settingsNavFocused()) focusSection(); } : () => resumeContent(row);
    searchIndex.set(0);
    settingsSearchActive.set(true);
  };
  const clearSearch = () => { settingsQuery.set(""); settingsSearchActive.set(false); };
  const jump = (hit = results()[selectedResult()]) => {
    if (!hit) return;
    settingsSection.set(hit.section.id);
    settingsPage.set(null);
    settingsNavFocused.set(false);
    const row = hit.row?.id ?? firstRow();
    focusedSetting.set(row);
    resumeFocus = () => resumeContent(row);
    clearSearch();
  };
  effect(() => {
    if (settingsSearchActive() || !resumeFocus) return;
    const restore = resumeFocus;
    resumeFocus = null;
    const timer = setTimeout(restore, 0);
    return () => clearTimeout(timer);
  });
  const cycleDraft = (by: number) => {
    const e = settingsEdit(); const seg = e && settingsSegs.get(e.id);
    if (!e || !seg) { settingsEdit.set(null); return; }
    const at = Math.max(0, seg.options.indexOf(e.draft));
    settingsEdit.set({ id: e.id, draft: seg.options[(at + by + seg.options.length) % seg.options.length] });
  };
  /** Right or Enter transfers focus; moving through the rail already shows the real page. */
  const enterRows = () => {
    if (overlayOpen()) return;
    settingsEdit.set(null);
    settingsNavFocused.set(false);
    focusContent(list().some((row) => row.id === focusedSetting()) ? focusedSetting() : firstRow());
  };
  const settingsKey = (key: string): boolean => {
    if (overlayOpen() || settingsSearchActive()) return false;
    const edit = settingsEdit();
    if (edit) {
      if (key === "left" || key === "right") cycleDraft(key === "left" ? -1 : 1);
      else if (key === "enter") {
        const seg = settingsSegs.get(edit.id);
        settingsEdit.set(null);
        if (seg && seg.value() !== edit.draft) seg.pick(edit.draft);
      }
      return true;
    }
    if (key === "left") { focusSection(); return true; }
    if (settingsNavFocused()) {
      if (["right", "tab", "shift+tab", "enter"].includes(key)) { enterRows(); return true; }
      const i = railSections.indexOf(cur());
      const next = key === "home" ? 0 : key === "end" ? railSections.length - 1 : key === "up" ? i - 1 : key === "down" ? i + 1 : i;
      if (next !== i && railSections[next]) selectSection(railSections[next]);
      return true;
    }
    if (page() && (key !== "enter" && key !== "space")) return false;
    if (key === "right") return true;
    if (key === "tab" || key === "shift+tab") { focusSection(); return true; }
    if (key === "up" || key === "down") {
      const i = list().findIndex((r) => r.id === focusedSetting());
      const row = list()[Math.max(0, Math.min(list().length - 1, i + (key === "up" ? -1 : 1)))];
      if (row) focusContent(row.id);
      return true;
    }
    if (key === "enter" || key === "space") {
      const id = focusedSetting(); const seg = id ? settingsSegs.get(id) : undefined;
      const row = list().find((r) => r.id === id);
      if (!id || !seg || !row || rowDisabled(row, cur())) return false;
      if (seg.options.length === 2 && seg.options.includes("on") && seg.options.includes("off")) seg.pick(seg.value() === "on" ? "off" : "on");
      else if (seg.options.length > 1) settingsEdit.set({ id, draft: seg.value() });
      return true;
    }
    return false;
  };
  const agent = () => settingsPage()?.startsWith("agents/") ? settingsPage()!.slice("agents/".length) : null;
  const agentDef = () => (data().agents?.agents ?? []).find((a: any) => a.name === agent());
  const phoneDetail = signal(false);
  if (!phone) {
    settingsKeys = settingsKey;
    effect(() => () => { settingsKeys = null; settingsEdit.set(null); });
    effect(() => {
      if (overlayOpen()) return;
      if (settingsSearchActive()) {
        const move = (by: number) => {
          const previous = selectedResult();
          const next = Math.max(0, Math.min(results().length - 1, previous + by));
          searchIndex.set(next);
          // Keep the input focused while revealing the keyboard-selected result.
          if (next !== previous) {
            const visible = Math.max(1, Math.floor((bodyHeight() - 40) / 32));
            if (next < previous || next >= visible) searchScroll?.scrollTo({ y: 32 * Math.max(0, next - visible + 1), animated: false });
          }
        };
        const offs = [["up", -1], ["down", 1]].map(([key, by]) => keymap.bind(String(key), () => move(Number(by)), { when: "always" }));
        return () => offs.forEach((off) => off());
      }
      const bind = (key: string, passThrough = false) => keymap.bind(key, () => settingsKey(key), { when: "notEditing", passThrough });
      const offs = [bind(manifest.settings.navigation.section_key)];
      if (!settingsEdit()) offs.push(keymap.bind(manifest.settings.navigation.search_key, () => { beginSearch(); searchInput?.focus(); }, { when: "notEditing" }));
      const onRail = settingsNavFocused();
      if (onRail || !page()) offs.push(...["right", "tab", "shift+tab", "up", "down"].map((key) => bind(key)));
      if (onRail) offs.push(bind("home"), bind("end"));
      for (const key of ["enter", "space"]) offs.push(bind(key, !onRail));
      return () => offs.forEach((off) => off());
    });
    effect(() => {
      if (!settingsNavFocused() || settingsSearchActive()) return;
      const id = cur().id;
      const timer = setTimeout(() => sectionRefs.get(id)?.focus(), 0);
      return () => clearTimeout(timer);
    });
  }
  const footerHints = (): string[] => {
    if (settingsSearchActive()) return ["↑↓ result", "enter jump", "esc back"];
    if (settingsNavFocused()) return ["↑↓ section", "enter focus", "esc close"];
    if (settingsEdit()) return ["←→ choose", "enter set", "esc cancel"];
    if (agent()) return ["enter select", "→ effort", "d remove", "K/J reorder", "← sections", "esc back"];
    if (page()) return ["enter open", "esc back"];
    return ["↑↓ move", "enter change", "esc back"];
  };
  void m; void model; void compat; void colorScheme; void byId; void keyLabel;
  const content = () => <View class={p.inline ? "flex-1 min-h-0 gap-2 p-4" : "gap-2"}>
        <If when={!p.inline}><ModalTitle title="Settings" /></If>
        <View class={`flex-row items-center gap-2 rounded-(--radius) border px-3 ${settingsSearchActive() ? "border-ring" : "border-border"}`}>
          <Icon name="search" size={14} class="tint-muted-foreground" />
          <Input ref={(n: any) => { searchInput = n; }} value={settingsQuery()} placeholder="Search settings…" accessibilityLabel="Search settings" testID="settings-search"
            onFocus={beginSearch} onChange={({ value }) => { beginSearch(); settingsQuery.set(value); searchIndex.set(0); searchScroll?.scrollTo({ y: 0, animated: false }); }}
            onSubmit={() => jump()} submitBehavior="submit"
            class={`h-8 min-w-0 flex-1 ${TS.base} text-foreground placeholder:text-muted-foreground`} />
          <If when={settingsSearchActive()} else={<Text class={T.meta}>{manifest.settings.navigation.search_key}</Text>}>
            <Btn label="Clear" kind="ghost" onPress={clearSearch} testID="settings-search-clear" />
          </If>
        </View>
        <View class={p.inline ? "flex-1 min-h-0" : "min-h-0"} style={p.inline ? {} : { height: bodyHeight() }}>
          <If when={settingsSearchActive()} else={<View class="flex-1 min-h-0 flex-row gap-4">
            <Scroll scrollbar={false} class={`${isNarrow() ? "w-[148px]" : "w-[180px]"} shrink-0 border-r border-border pr-2`}>
              <View>
                <Each of={rail} key={(group) => group.label}>{(group, i) => <View class={i() > 0 ? "mt-2" : ""}>
                  <If when={group().label}><Text class={`px-3 py-0.5 ${T.section} uppercase`}>{group().label}</Text></If>
                  <Each of={group().sections} key={(s) => s.id}>{(s) => (
                    <View role="button" focusable ref={(n: any) => { if (n) sectionRefs.set(s().id, n); else sectionRefs.delete(s().id); }}
                      onPress={() => selectSection(s())} accessibilityState={{ selected: cur().id === s().id }} testID={`section-${s().id}`} accessibilityLabel={s().label}
                      class={`relative flex-row items-center rounded-(--radius) min-h-7 px-4 hover:bg-row-selected ${cur().id === s().id && settingsNavFocused() ? "bg-row-selected" : ""}`}>
                      <If when={cur().id === s().id && settingsNavFocused()}><CursorMark testID="settings-section-focus" /></If>
                      <Text class={`flex-1 ${TS.base} ${cur().id === s().id ? "text-foreground font-medium" : "text-foreground-secondary"}`}>{s().label}</Text>
                    </View>
                  )}</Each>
                </View>}</Each>
              </View>
            </Scroll>
            <Scroll class="flex-1 min-h-0" testID={`settings-page-${cur().id}`}>
              <View class="pb-4 w-full">
                <Each of={config()?.errors ?? []} key={(e: string) => e}>{(e) => <Text class={`px-3 ${T.meta}`}>{e()}</Text>}</Each>
                <If when={!agent()}><View class="px-3 pt-1 pb-2"><Heading text={cur().label} first /></View></If>
                <If when={page() === "providers"}>{() => <ProvidersPage sec={cur()} firstRef={(node) => setPageStart("providers", node)} />}</If>
                <If when={page() === "mcp"}>{() => <McpPage firstRef={(node) => setPageStart("mcp", node)}><Each of={cur().row ?? []} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each></McpPage>}</If>
                <If when={page() === "agents" && !agent()}>{() => <AgentsPage sec={cur()} />}</If>
                <If when={page() === "agents" && agent()}>{() => <View class="gap-2 pb-2">
                  <View class="flex-row items-center gap-2 px-3">
                    <Button onPress={() => settingsPage.set(null)} testID="settings-page-back" accessibilityLabel="Back to Agents"
                      class={`flex-row items-center gap-1 rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}>
                      <Icon name="arrow-left" size={14} class="tint-muted-foreground" />
                      <Text class={`${TS.base} text-foreground-secondary`}>Agents</Text>
                    </Button>
                    <Text class={`${TS.base} text-muted-foreground`}>/</Text>
                    <Text class={T.label}>{agent()}</Text>
                  </View>
                  <If when={agentDef()}>{() => <AgentCard a={agentDef()} />}</If>
                </View>}</If>
                <If when={!page()}>
                  <If when={cur().note}><Text class={`px-3 ${T.meta}`}>{cur().note!}</Text></If>
                  <Each of={list()} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each>
                </If>
              </View>
            </Scroll>
          </View>}>
            <View class="flex-1 min-h-0" testID="settings-results">
              <Text class={`px-3 h-8 ${T.meta}`} testID={query() && !results().length ? "settings-no-matches" : undefined}>
                {!query() ? "Type a setting or value to search." : !results().length ? "No settings match." : "Search results"}
              </Text>
              <Scroll ref={(node: any) => { searchScroll = node; }} class="flex-1 min-h-0">
                <View>
                  <Each of={results()} key={(hit) => hit.row?.id ?? hit.section.id}>{(hit, i) => {
                    const label = () => hit().row?.label ?? hit().section.label;
                    const path = () => hit().row ? hit().section.label : "Section";
                    const selected = () => selectedResult() === i();
                    return <View role="button" focusable onPress={() => jump(hit())} accessibilityLabel={`${label()} · ${path()}`} accessibilityState={{ selected: selected() }} testID={`settings-result-${hit().row?.id ?? hit().section.id}`}
                      class={`relative h-8 flex-row items-center gap-3 px-4 rounded-(--radius) hover:bg-row-selected ${selected() ? "bg-row-selected" : ""}`}>
                      <If when={selected()}><CursorMark testID="settings-result-focus" /></If>
                      <Text class={`flex-1 ${selected() ? T.label : T.base}`} style={{ numberOfLines: 1 }}>{label()}</Text>
                      <Text class={T.meta} style={{ numberOfLines: 1 }}>{path()}</Text>
                    </View>;
                  }}</Each>
                </View>
              </Scroll>
            </View>
          </If>
        </View>
        <If when={!p.inline}><ModalFooter hints={footerHints()} right={`Core ${info()?.version ?? "—"}`} /></If>
      </View>;
  const phoneContent = () => <View class="flex-1 min-h-0" testID="phone-settings">
    <If when={!phoneDetail()} else={<View class="flex-1 min-h-0">
      <Button onPress={() => { phoneDetail.set(false); settingsPage.set(null); }} accessibilityLabel="Back to Settings" testID="phone-settings-back" class={`flex-row items-center gap-2 min-h-11 px-4 ${FOCUS}`}>
        <Icon name="arrow-left" size={16} class="tint-foreground" /><Text style={{ fontFamily: "Google Sans" }} class="text-[16px] text-foreground">Settings</Text>
      </Button>
      <Scroll class="flex-1" testID={`settings-page-${page() ?? cur().id}`}>
        <View class="px-4 pb-6 gap-2">
          <Text style={{ fontFamily: "Google Sans" }} class="text-[24px] leading-8 font-semibold text-foreground px-3">{cur().label}</Text>
          <If when={page() === "providers"}><ProvidersPage sec={cur()} /></If>
          <If when={!page()}>
            <If when={cur().note}><Text class={`px-3 ${T.meta}`}>{cur().note!}</Text></If>
            <Each of={list()} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each>
          </If>
        </View>
      </Scroll>
    </View>}>
      <Scroll class="flex-1" contentStyle={{ padding: 16, paddingBottom: 24 }}>
        <View class="gap-5">
          <Button onPress={() => coreEditing.set(true)} testID="phone-core-card" accessibilityLabel="Change Core address" class={`rounded-(--radius-card) bg-raised p-4 gap-2 ${FOCUS}`}>
            <View class="flex-row items-center gap-2"><Text style={{ fontFamily: "Google Sans" }} class="flex-1 text-[16px] leading-6 font-semibold text-foreground">Core connection</Text><Text style={{ fontFamily: "Google Sans" }} class={`text-[13px] leading-5 ${live() ? "text-success" : "text-warning"}`}>{live() ? "Connected" : connection().kind === "connecting" ? "Connecting" : "Disconnected"}</Text></View>
            <Text style={{ numberOfLines: 2 }} class={`${T.sec}`}>{coreUrl() ?? "No address configured"}</Text>
          </Button>
          <View class="gap-1 rounded-(--radius-card) bg-raised overflow-hidden">
            <Each of={sections} key={(s) => s.id}>{(s) => <Button onPress={() => { settingsSection.set(s().id); settingsPage.set(null); phoneDetail.set(true); }} testID={`section-${s().id}`} accessibilityLabel={s().label} class={`flex-row items-center min-h-14 px-4 gap-2 ${FOCUS}`}>
              <Text style={{ fontFamily: "Google Sans" }} class="flex-1 text-[16px] leading-6 font-medium text-foreground">{s().label}</Text><Icon name="chevron-right" size={16} class="tint-muted-foreground" />
            </Button>}</Each>
          </View>
        </View>
      </Scroll>
    </If>
    <ProviderDetailModal />
  </View>;
  return p.inline && phone ? phoneContent() : p.inline ? content() : <Modal size="large" label="Settings" testID="settings" onCancel={() => escape()} onOpenChange={(open) => { if (!open && centre() === "settings") void run("app.settings"); }}>{content()}</Modal>;
}

/** Onboarding (ui/onboarding.tsx) lists providers the way Settings does and opens the same detail modal. */
export const settingsData = data;
export { providerStatus };
export function openProviderDetail(id: string) { keyEntry.set(null); providerKey.set(""); providerDetail.set(id); }

// The phone's Settings tab (ui/mobile/settings.tsx) renders these same rows and pages as drill-in screens.
export { SettingRow as PhoneSettingRow, ProvidersPage as PhoneProvidersPage, sections as settingsSections };
export type { Section as SettingsSection };
