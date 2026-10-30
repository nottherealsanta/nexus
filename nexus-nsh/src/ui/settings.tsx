import type { JSX } from "nsh";
import { Scroll, Text, View, effect, setColorScheme, signal, openUrl, colorScheme, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, T, FOCUS } from "./kit";
import { Heading, Modal, ModalFooter, ModalTitle, SelectBar } from "./modal";
import { manifest } from "../gen/manifest";
import { byId, commands, escape, keyLabel, loadProviders, run } from "../commands";
import { catalogSource, info, compat, isNarrow, live, m, model, reloadModelCatalog, savePrefs, setTimelineDetail, settingsSection, themeChoice, centre, timelineDetail, toast, currentProject, config, loadConfig, patchConfig, picker } from "../store";
import { CLIENT_API, CLIENT_VERSION } from "../core/api";
import { request } from "../core/transport";
import { voiceInfoLines, voiceActionReason } from "../voice-settings";
import { voiceStatus } from "../voice-state";
import type { VoiceTarget } from "../voice-state";

interface Row { id: string; label: string; kind: string; scope: string; clients?: string; default?: unknown; options?: string[]; command?: string; note?: string; unavailable_reason?: string }
interface Section { id: string; label: string; unavailable_reason?: string; row?: Row[] }
const sections = (manifest.settings.section as unknown as Section[]).filter((section) => section.id !== "models");

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
            class={`rounded-[4px] px-3 ${isNarrow() ? "min-h-10" : "min-h-7"} items-center justify-center hover:bg-accent disabled:opacity-50 ${FOCUS}`}>
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
    case "providers.list": return d.providers ? lines(d.providers.map((x) => `${x.label ?? x.id} · ${x.connected ? "signed in" : "not signed in"}`), "none") : <Value text="loading…" />;
    case "models.catalog": {
      const label = d.models ? `${d.models.models?.length ?? 0} models · ${d.models.source || catalogSource() || "catalog"}` : "loading…";
      return <Button onPress={() => void run("model.change")} accessibilityLabel={`Model catalog: ${label}`} testID="setting-model-catalog"
        class={`flex-row items-center gap-2 rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}>
        <Text class="text-[13px] leading-5 text-foreground-secondary">{label}</Text>
        <Text class={T.meta}>Open</Text>
      </Button>;
    }
    case "models.refresh": return <Btn label="Refresh catalog" onPress={() => void refreshCatalog()} kind="ghost" testID="setting-model-refresh" />;
    case "agents.list": {
      const names = d.agents?.agents?.map((a: any) => a.name).filter(Boolean).join(", ") || "none";
      return <Button onPress={() => void run("agent.change")} accessibilityLabel={`Loaded agents: ${names}`} testID="setting-agents-list"
        class={`rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}>
        <Text class="text-[13px] leading-5 text-foreground-secondary">{d.agents ? names : "loading…"}</Text>
      </Button>;
    }
    case "agents.default_root": return <Value text="build" />;
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
const WIRED = new Set(["providers.sign_in", "providers.sign_out", "appearance.theme", "appearance.animations", "appearance.timeline_detail", "about.inspector", "providers.list", "models.catalog", "models.refresh", "agents.list", "agents.default_root", "tools.default_permission", "models.default_effort", "tools.defaults", "mcp.full_definitions", "mcp.max_listed_tools", "context.handoff_percent", "context.title_models", "keybindings.list", "about.versions", "about.daemon", "dictation.stt_model", "dictation.stt_download", "dictation.stt_remove", "dictation.tts_model", "dictation.tts_download", "dictation.tts_remove"]);

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

async function providerAction(p: any) {
  if (!p.connected && ["opencode", "openai", "groq", "nvidia"].includes(p.id)) {
    providerKey.set(""); keyEntry.set({ id: p.id, label: p.label ?? p.id }); return;
  }
  try {
    if (p.connected) {
      await request("DELETE", `/api/provider/${p.id}/login`);
      const providers = await loadProviders();
      data.update((d) => ({ ...d, providers }));
      refreshProviderModels();
      toast("success", `${p.label} signed out`);
      return;
    }
    await request("POST", `/api/provider/${p.id}/login`, { body: { method: p.id === "ollama" ? "local" : "device" } });
    toast("info", `Starting ${p.label} sign-in…`);
    let previous = "";
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const state = await request<any>("GET", `/api/provider/${p.id}/login`);
      if (state.message && state.message !== previous) { previous = state.message; toast(state.status === "failed" ? "error" : "info", state.message); }
      if (state.status !== "pending") {
        if (state.status === "connected") {
          const providers = await loadProviders();
          data.update((d) => ({ ...d, providers }));
          refreshProviderModels();
          toast("success", `${p.label} connected`);
        }
        break;
      }
    }
  } catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
}

function ProvidersPage() {
  return <View class="gap-1" testID="providers-page">
    <Text class={T.meta}>Connect a provider to use its models.</Text>
    <If when={!data().providers}><Text class={T.meta}>Loading providers…</Text></If>
    <Each of={data().providers ?? []} key={(p: any) => p.id}>{(p) =>
      <View class="flex-row items-center gap-3 px-3 py-2" testID={`provider-${p().id}`}>
        <View class="flex-1"><Text class="text-[14px] text-foreground">{p().label ?? p().id}</Text><Text class={T.meta}>{p().connected ? "Connected" : "Not connected"}</Text></View>
        <If when={p().id !== "mock"}><Btn label={p().connected ? "Disconnect" : "Connect"} disabled={!live()} onPress={() => void providerAction(p())} testID={`provider-action-${p().id}`} /></If>
      </View>
    }</Each>
  </View>;
}

function AgentModelsPage() {
  const agents = () => (data().agents?.agents ?? []).filter((a: any) => a.mode === "all" || a.mode === "subagent");
  const pool = (name: string): string[] => config()?.values?.agents?.models?.[name] ?? [];
  const save = (name: string, refs: string[]) => {
    const models = { ...(config()?.values?.agents?.models ?? {}) };
    if (refs.length) models[name] = refs; else delete models[name];
    void patchConfig("agents.models", models);
  };
  const fallbacks = (name: string): string[] => config()?.values?.agents?.fallbacks?.[name] ?? [];
  const saveFallbacks = (name: string, refs: string[]) => {
    const values = { ...(config()?.values?.agents?.fallbacks ?? {}) };
    if (refs.length) values[name] = refs; else delete values[name];
    void patchConfig("agents.fallbacks", values);
  };
  return <View class="gap-3" testID="agent-models-page">
    <Text class={T.meta}>For each new subagent, one default model is chosen at random with equal probability. Without a selection, the agent's own model or the daemon default is used.</Text>
    <If when={!data().agents}><Text class={T.meta}>Loading agents…</Text></If>
    <Each of={data().agents?.problems ?? []} key={(problem: any) => JSON.stringify(problem)}>{(problem) => <Text class={T.meta}>{typeof problem() === "string" ? problem() : JSON.stringify(problem())}</Text>}</Each>
    <Each of={agents()} key={(a: any) => a.name}>{(a) => <View class="gap-2 rounded-(--radius) border border-border p-3" testID={`agent-models-${a().name}`}>
      <Text class="text-[14px] font-medium text-foreground">{a().name}</Text>
      <Text class={T.meta}>{a().description}</Text>
      <If when={!pool(a().name).length}><Text class={T.meta}>Using agent or daemon default</Text></If>
      <Each of={pool(a().name)} key={(ref) => ref}>{(ref) => <View class="flex-row items-center gap-2">
        <Text class="flex-1 text-[13px] text-foreground-secondary">{ref()} · {(100 / pool(a().name).length).toFixed(pool(a().name).length === 3 ? 1 : 0)}%</Text>
        <Btn label="Remove" disabled={!live()} onPress={() => save(a().name, pool(a().name).filter((v) => v !== ref()))} testID={`agent-model-remove-${a().name}-${ref()}`} />
      </View>}</Each>
      <Btn label="Add default model" disabled={!live() || !data().models} testID={`agent-model-add-${a().name}`} onPress={() => {
        const options = (data().models?.models ?? []).filter((item: any) => item.ref && !pool(a().name).includes(item.ref));
        picker.set({ title: `Default models for ${a().name}`, items: options.map((item: any) => ({ id: item.ref, label: item.ref, run: async () => { picker.set(null); save(a().name, [...pool(a().name), item.ref]); } })) });
      }} />
      <Text class={T.meta}>Fallbacks are tried in order after a failure before response output.</Text>
      <Each of={fallbacks(a().name)} key={(ref) => ref}>{(ref) => <View class="flex-row items-center gap-2">
        <Text class="flex-1 text-[13px] text-foreground-secondary">{fallbacks(a().name).indexOf(ref()) + 1}. {ref()}</Text>
        <Btn label="Move up" disabled={!live() || fallbacks(a().name).indexOf(ref()) === 0} onPress={() => { const refs = [...fallbacks(a().name)]; const i = refs.indexOf(ref()); if (i > 0) { [refs[i - 1], refs[i]] = [refs[i], refs[i - 1]]; saveFallbacks(a().name, refs); } }} />
        <Btn label="Remove" disabled={!live()} onPress={() => saveFallbacks(a().name, fallbacks(a().name).filter((v) => v !== ref()))} />
      </View>}</Each>
      <Btn label="Add fallback model" disabled={!live() || !data().models} testID={`agent-fallback-add-${a().name}`} onPress={() => {
        const options = (data().models?.models ?? []).filter((item: any) => item.ref && !fallbacks(a().name).includes(item.ref));
        picker.set({ title: `Fallback models for ${a().name}`, items: options.map((item: any) => ({ id: item.ref, label: item.ref, run: async () => { picker.set(null); saveFallbacks(a().name, [...fallbacks(a().name), item.ref]); } })) });
      }} />
    </View>}</Each>
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
    return <Seg label="Theme" options={["system", "light", "dark", "named"]} value={themeChoice()} disabled={false}
      onPick={(v) => { if (v === "named") { toast("info", "Named themes: shared theme tokens arrive with Step 12.2."); return; } themeChoice.set(v as any); setColorScheme(v as "light" | "dark" | "system"); savePrefs(); }} />;
  }
  if (r.id === "appearance.animations") return <Seg label={r.label} options={["on", "off"]} value={config()?.values?.ui?.reduced_motion ? "off" : "on"} onPick={(v) => void patchConfig("ui.reduced_motion", v === "off")} />;
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
  const paths: Record<string, string> = { "models.default_effort": "effort", "agents.default_root": "agent" };
  if (paths[r.id]) {
    const key = paths[r.id];
    const value = config()?.values?.defaults?.[key] || "automatic";
    return <Button testID={`edit-${r.id}`} accessibilityLabel={`Change ${r.label}`} onPress={() => {
      const options: string[] = key === "model" ? (data().models?.models ?? []).map((m: any) => m.ref) : key === "agent" ? (data().agents?.agents ?? []).map((a: any) => a.name) : ["low", "medium", "high", "xhigh"];
      picker.set({ title: r.label, items: ["", ...options].map((v) => ({ id: v || "automatic", label: v || "automatic", run: async () => { picker.set(null); await patchConfig(`defaults.${key}`, v); } })) });
    }} class={`rounded-(--radius) px-2 min-h-8 hover:bg-accent ${FOCUS}`}><Value text={value} mono /></Button>;
  }
  if (r.id === "models.catalog" || r.id === "models.refresh" || r.id === "agents.list") return listFor(r.id);
  if (r.kind === "choice" && r.options) return <Seg label={r.label} options={r.options} value={String(r.default ?? r.options[0])} disabled />;
  if (r.kind === "toggle") return <Text class="text-[13px] leading-5 text-muted-foreground">{r.default ? "[x] on" : "[ ] off"}</Text>;
  if (r.kind === "number") return <Text class="text-[13px] leading-5 text-muted-foreground">{String(r.default ?? "—")}</Text>;
  if (r.kind === "choice") return <Text class="text-[13px] leading-5 text-muted-foreground">{String(r.default ?? "not set")}</Text>;
  if (r.kind === "action") return <Btn label={r.label} disabled reason={r.unavailable_reason} />;
  return listFor(r.id);
}

function SettingRow(p: { r: Row; sec: Section }) {
  const r = p.r;
  const reason = ["providers.sign_in", "providers.sign_out"].includes(r.id) ? undefined : r.unavailable_reason ?? p.sec.unavailable_reason;
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

export function SettingsView() {
  loadSettingsData();
  const cur = () => sections.find((s) => s.id === settingsSection()) ?? sections[0];
  const list = () => (cur().row ?? []).filter((r) => r.clients !== "tui" || r.id === "appearance.timeline_detail");
  void m; void model; void compat; void colorScheme; void byId; void keyLabel;
  return (
    <Modal width={820} top="top-[8%]" label="Settings" testID="settings" onCancel={() => escape()} onOpenChange={(open) => { if (!open && centre() === "settings") void run("app.settings"); }}>
      <View class="gap-3">
        <ModalTitle title="Settings" />
        <View class={`${isNarrow() ? "" : "flex-row gap-4"} min-h-0`} style={{ height: Math.max(240, Math.min(520, Math.round(windowSize().height * 0.76) - 48 - 32 - 12 - 12 - 28)) }}>
          <View class={isNarrow() ? "pb-2" : "w-[180px]"}>
            <Scroll horizontal={isNarrow()} scrollbar={false} class={isNarrow() ? "" : "flex-1"} contentClass={isNarrow() ? "flex-row gap-1" : "gap-0.5"}>
              <Each of={sections} key={(s) => s.id}>
                {(s) => (
                  <Button onPress={() => settingsSection.set(s().id)} selected={cur().id === s().id} testID={`section-${s().id}`} accessibilityLabel={s().label + (s().unavailable_reason ? " (unavailable)" : "")}
                    class={`relative flex-row items-center rounded-(--radius) px-3 hover:bg-accent ${isNarrow() ? "min-h-10" : "min-h-8"} ${FOCUS}`}>
                    <SelectBar on={cur().id === s().id} />
                    <Text class={`text-[13px] leading-5 ${cur().id === s().id ? "text-foreground font-medium" : s().unavailable_reason ? "text-muted-foreground" : "text-foreground-secondary"}`}>{s().label}</Text>
                  </Button>
                )}
              </Each>
            </Scroll>
          </View>
          <Scroll class="flex-1 min-h-0" contentClass="pb-4 w-full">
            <Heading text={cur().label} first />
            <If when={keyEntry()}><View class="gap-2 p-3">
              <Text class={T.sec}>{keyEntry()?.label} API key</Text>
              <Input masked autoFocus accessibilityLabel={`${keyEntry()?.label} API key`} placeholder="API key" onChange={({ value }) => providerKey.set(value)} class="h-9 rounded-(--radius) border border-border bg-background px-3 text-foreground" />
              <View class="flex-row gap-2"><Btn label="Sign in" onPress={() => { const key = providerKey(), provider = keyEntry(); providerKey.set(""); keyEntry.set(null); if (!provider) return; void request("POST", `/api/provider/${provider.id}/login`, { body: { api_key: key } }).then(async () => { const providers = await loadProviders(); data.update((d) => ({ ...d, providers })); refreshProviderModels(); toast("success", `${provider.label} signed in`); }).catch(() => toast("error", `${provider.label} sign-in failed`)); }} /><Btn label="Cancel" onPress={() => { providerKey.set(""); keyEntry.set(null); }} /></View>
            </View></If>
            <Each of={config()?.errors ?? []} key={(e: string) => e}>{(e) => <Text class={T.meta}>{e()}</Text>}</Each>
            <If when={cur().unavailable_reason}><View class="flex-row gap-2 items-start pt-1 pb-1"><Text class="text-[13px] text-muted-foreground">⊖</Text><Text class={`flex-1 ${T.sec}`}>{cur().unavailable_reason!}</Text></View></If>
            <If when={cur().id === "providers"}><ProvidersPage /></If>
            <If when={cur().id === "agents"}><AgentModelsPage /></If>
            <If when={cur().id !== "providers" && cur().id !== "agents"}><Each of={list()} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each></If>
          </Scroll>
        </View>
        <ModalFooter hints="esc close" right={`Core ${info()?.version ?? "—"}`} />
      </View>
    </Modal>
  );
}
