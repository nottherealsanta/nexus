import type { JSX } from "nsh";
import { Scroll, Text, View, setColorScheme, signal, openUrl, colorScheme, windowSize } from "nsh";
import { Button } from "nsh/base";
import { Btn, T, FOCUS } from "./kit";
import { Heading, Modal, ModalFooter, ModalTitle, SelectBar } from "./modal";
import { manifest } from "../gen/manifest";
import { byId, commands, escape, keyLabel, loadProviders, run } from "../commands";
import { catalogSource, info, compat, isNarrow, m, model, reloadModelCatalog, savePrefs, setTimelineDetail, settingsSection, themeChoice, centre, timelineDetail, toast, currentProject } from "../store";
import { CLIENT_API, CLIENT_VERSION } from "../core/api";
import { request } from "../core/transport";

interface Row { id: string; label: string; kind: string; scope: string; clients?: string; default?: unknown; options?: string[]; command?: string; note?: string; unavailable_reason?: string }
interface Section { id: string; label: string; unavailable_reason?: string; row?: Row[] }
const sections = (manifest.settings.section as unknown as Section[]);

const data = signal<{ providers: any[] | null; models: any | null; modelDefault: string | null; agents: any | null }>({ providers: null, models: null, modelDefault: null, agents: null });
let loaded = false;
export function loadSettingsData() {
  if (loaded) return; loaded = true;
  void loadProviders().then((p) => data.update((d) => ({ ...d, providers: p })));
  request<any>("GET", "/api/model").then((r) => data.update((d) => ({ ...d, models: r }))).catch(() => {});
  request<any>("GET", "/api/model/default").then((r) => data.update((d) => ({ ...d, modelDefault: r.model }))).catch(() => {});
  const p = currentProject();
  request<any>("GET", "/api/agent", { query: { project: p ?? undefined } }).then((r) => data.update((d) => ({ ...d, agents: r }))).catch(() => {});
}

function Seg(p: { options: string[]; value: string; onPick?: (v: string) => void; disabled?: boolean; label: string }) {
  return (
    <View class="flex-row rounded-(--radius) border border-border bg-background p-0.5" role="radiogroup" accessibilityLabel={p.label}>
      <Each of={p.options} key={(o) => o}>
        {(o) => (
          <Button role="radio" selected={p.value === o()} disabled={p.disabled} onPress={() => p.onPick?.(o())} accessibilityLabel={`${p.label}: ${o()}`}
            class={`rounded-[4px] px-3 ${isNarrow() ? "min-h-10" : "min-h-7"} items-center justify-center selected:bg-selection hover:bg-accent disabled:opacity-50 ${FOCUS}`}>
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
const WIRED = new Set(["appearance.theme", "appearance.timeline_detail", "about.inspector", "providers.list", "providers.default_model", "models.default", "models.catalog", "models.refresh", "agents.list", "agents.default_root", "tools.defaults", "keybindings.list", "about.versions", "about.daemon"]);

function control(r: Row): JSX.Element {
  if (r.id === "appearance.theme") {
    return <Seg label="Theme" options={["system", "light", "dark", "named"]} value={themeChoice()} disabled={false}
      onPick={(v) => { if (v === "named") { toast("info", "Named themes: shared theme tokens arrive with Step 12.2."); return; } themeChoice.set(v as any); setColorScheme(v as "light" | "dark" | "system"); savePrefs(); }} />;
  }
  if (r.id === "appearance.timeline_detail") {
    return <Seg label="Timeline detail" options={r.options ?? ["low", "medium", "high"]} value={timelineDetail()} disabled={false}
      onPick={(v) => setTimelineDetail(v as "low" | "medium" | "high")} />;
  }
  if (r.id === "about.inspector") return <Btn label="Open inspector" onPress={() => { void openUrl(new URL("/inspector", import.meta.url).href).catch(() => toast("info", "Open /inspector on the host page.")); }} />;
  if (r.id === "providers.default_model" || r.id === "models.default") return <Value text={data().modelDefault || "not reported"} mono />;
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
  const reason = r.unavailable_reason ?? p.sec.unavailable_reason;
  const wired = WIRED.has(r.id);
  const disabled = !!reason || !wired;
  const why = reason ?? (wired ? "" : "Not editable in this NSH increment yet.");
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
  const list = () => (cur().row ?? []);
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
                    class={`relative flex-row items-center rounded-(--radius) px-3 hover:bg-accent selected:bg-selection ${isNarrow() ? "min-h-10" : "min-h-8"} ${FOCUS}`}>
                    <SelectBar on={cur().id === s().id} />
                    <Text class={`text-[13px] leading-5 ${cur().id === s().id ? "text-foreground font-medium" : s().unavailable_reason ? "text-muted-foreground" : "text-foreground-secondary"}`}>{s().label}</Text>
                  </Button>
                )}
              </Each>
            </Scroll>
          </View>
          <Scroll class="flex-1 min-h-0" contentClass="pb-4 w-full">
            <Heading text={cur().label} first />
            <If when={cur().unavailable_reason}><View class="flex-row gap-2 items-start pt-1 pb-1"><Text class="text-[13px] text-muted-foreground">⊖</Text><Text class={`flex-1 ${T.sec}`}>{cur().unavailable_reason!}</Text></View></If>
            <Each of={list()} key={(r) => r.id}>{(r) => <SettingRow r={r()} sec={cur()} />}</Each>
          </Scroll>
        </View>
        <ModalFooter hints="esc close" right={`Core ${info()?.version ?? "—"}`} />
      </View>
    </Modal>
  );
}
