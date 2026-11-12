// The phone's Settings tab (plans/05 section 5.2): a connection card, then a grouped list whose rows push a page
// (the iOS pattern). The pages render the same rows as the desktop Settings; the manifest's `mobile` scope picks them.
import type { JSX } from "nsh";
import { Scroll, Text, View, effect, safeArea } from "nsh";
import { Button } from "nsh/base";
import { Btn, Icon, IconButton, FOCUS, T, TS } from "../kit";
import { PhoneProvidersPage, PhoneSettingRow, loadSettingsData, ProviderDetailModal, settingsSections } from "../settings";
import type { SettingsSection } from "../settings";
import { coreEditing } from "../connect";
import { connection, config, info, notificationSoundsOn, themeChoice, providerLogins } from "../../store";
import { healthSummary, hostLabel, traceBars, transportOf } from "../../mobile";
import { coreAddress, latency, probeLatency, settingsDrill } from "./state";

const GROUPS: { id: string; title?: string; sections: string[] }[] = [
  { id: "prefs", title: "Preferences", sections: ["appearance", "notifications", "dictation", "manager"] },
  { id: "accounts", title: "Accounts", sections: ["providers"] },
  { id: "about", sections: ["about"] },
];
const NAMES: Record<string, string> = { dictation: "Voice" };
const nameOf = (s: SettingsSection) => NAMES[s.id] ?? s.label;
const detailOf = (s: SettingsSection): string => {
  if (s.id === "appearance") return themeChoice()[0].toUpperCase() + themeChoice().slice(1);
  if (s.id === "notifications") return notificationSoundsOn() ? "Sound on" : "Sound off";
  return "";
};

function Latency() {
  const h = () => healthSummary(latency());
  const tone = () => { const n = h().now; return n === null ? "text-error" : n < 150 ? "text-success" : n < 500 ? "text-warning" : "text-error"; };
  return <View class="items-end" testID="core-latency" accessibilityLabel={h().now === null ? "Latency: unreachable" : `Latency ${h().now} milliseconds`}>
    <Text class={`font-(family-name:--font-ui) text-[20px] leading-7 font-semibold tabular-nums ${latency().length ? tone() : "text-muted-foreground"}`}>{latency().length ? (h().now === null ? "no reply" : `${h().now} ms`) : "…"}</Text>
    <Text class={T.meta}>{h().median !== null ? `median ${h().median} ms` : "latency"}</Text>
  </View>;
}

/** The health trace: one bar per probe, a failed probe drawn full height in the error colour. */
function Trace() {
  return <View class="flex-row items-end gap-[3px] h-8" testID="core-trace" accessibilityLabel="Connection health over the last probes">
    <Each of={traceBars(latency()).map((b, i) => ({ ...b, i }))} key={(b) => String(b.i)}>
      {(b) => <View class={`w-[6px] rounded-full ${b().failed ? "bg-error" : "bg-primary"}`} style={{ height: Math.round(b().h * 32) }} accessible={false} />}
    </Each>
  </View>;
}

/** The connection card: the Core's host, address, transport, live latency and a small health trace. */
function ConnectionCard() {
  effect(() => {
    void probeLatency();
    const id = setInterval(() => void probeLatency(), 5000);
    return () => clearInterval(id);
  });
  const addr = () => coreAddress();
  const live = () => connection().kind === "live";
  const state = () => { const k = connection().kind; return k === "live" ? "Connected" : k === "connecting" || k === "starting" ? "Connecting" : k === "reconnecting" ? "Reconnecting" : "Offline"; };
  return (
    <View class="rounded-2xl bg-raised p-4 gap-3" testID="connection-card" accessibilityLabel="Connection">
      <View class="flex-row items-center gap-3">
        <View class="flex-1 min-w-0 gap-0.5">
          <Text class={T.meta} testID="core-version">{info()?.version ? `Core · v${info()!.version}` : "Core"}</Text>
          <Text style={{ numberOfLines: 1 }} class="font-(family-name:--font-ui) text-[18px] leading-7 font-semibold text-foreground" testID="core-host">{addr() ? hostLabel(addr()) : "this device"}</Text>
          <Text style={{ numberOfLines: 1, userSelect: "text" }} class={`${T.mono} text-[13px] leading-5`} testID="core-address">{addr() || "same origin"}</Text>
        </View>
        <Latency />
      </View>
      <Trace />
      <View class="flex-row items-center gap-2">
        <View class="flex-row items-center gap-1.5 rounded-full bg-muted px-2.5 min-h-7">
          <View class={`size-2 rounded-full ${live() ? "bg-success" : "bg-warning"}`} accessible={false} />
          <Text class="font-(family-name:--font-ui) text-[13px] leading-5 text-foreground-secondary" testID="core-state">{state()}</Text>
        </View>
        <View class="rounded-full bg-muted px-2.5 min-h-7 justify-center">
          <Text class="font-(family-name:--font-ui) text-[13px] leading-5 text-foreground-secondary" testID="core-transport">{addr() ? transportOf(addr()) : "This device"}</Text>
        </View>
        <View class="flex-1" />
        <Btn label="Change" kind="default" onPress={() => coreEditing.set(true)} testID="core-change" class="min-h-11" />
      </View>
    </View>
  );
}

function Group(p: { title?: string; children?: JSX.Element }) {
  return (
    <View class="gap-1.5">
      {p.title ? <Text class={`${T.section} px-4`}>{p.title}</Text> : null}
      <View class="rounded-2xl bg-raised overflow-hidden">{p.children}</View>
    </View>
  );
}

function GroupRow(p: { s: SettingsSection; last: boolean }) {
  return (
    <Button onPress={() => settingsDrill.set(p.s.id)} testID={`phone-setting-${p.s.id}`} accessibilityLabel={nameOf(p.s)}
      class={`relative flex-row items-center gap-3 px-4 min-h-14 active:bg-selection ${FOCUS}`}>
      <Text class={`flex-1 min-w-0 ${TS.body} text-foreground`}>{nameOf(p.s)}</Text>
      <Text style={{ numberOfLines: 1 }} class={T.meta}>{detailOf(p.s)}</Text>
      <Icon name="chevron-right" size={16} class="tint-muted-foreground" />
      {!p.last ? <View class="absolute left-4 right-0 bottom-0 h-px bg-border" accessible={false} /> : null}
    </Button>
  );
}

function DrillPage(p: { sec: SettingsSection }) {
  const rows = () => (p.sec.row ?? []).filter((r) => r.clients !== "tui" || r.id === "appearance.timeline_detail");
  return (
    <View class="flex-1 min-h-0 bg-background" testID={`phone-settings-page-${p.sec.id}`}>
      <View class="flex-row items-center gap-1 px-1 pb-1 bg-sidebar" style={{ paddingTop: safeArea().top + 4 }} testID="phone-top-bar">
        <IconButton icon="arrow-left" label="Back to Settings" testID="phone-settings-back" onPress={() => settingsDrill.set(null)} />
        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.title} font-semibold text-foreground`} testID="phone-tab-title">{nameOf(p.sec)}</Text>
      </View>
      <Scroll class="flex-1 min-h-0">
        <View class="py-3 gap-1 w-full">
          <If when={!!p.sec.note}><Text class={`px-4 pb-1 ${T.meta}`}>{p.sec.note!}</Text></If>
          <If when={p.sec.page && p.sec.id === "providers"} else={<Each of={rows()} key={(r) => r.id}>{(r) => <PhoneSettingRow r={r() as any} sec={p.sec as any} />}</Each>}>
            <PhoneProvidersPage sec={p.sec as any} />
          </If>
          <View style={{ height: safeArea().bottom + 16 }} />
        </View>
      </Scroll>
      <If when={p.sec.id === "providers"}><ProviderDetailModal /></If>
    </View>
  );
}

export function PhoneSettings() {
  loadSettingsData();
  void providerLogins; void config;
  const secs = () => GROUPS.map((g) => ({ ...g, list: g.sections.map((id) => settingsSections.find((s) => s.id === id)).filter((s): s is SettingsSection => !!s) })).filter((g) => g.list.length);
  const drill = () => settingsSections.find((s) => s.id === settingsDrill()) ?? null;
  return (
    <View class="flex-1 min-h-0" testID="phone-settings">
      <If when={drill()} else={
        <Scroll class="flex-1 min-h-0" testID="phone-settings-list">
          <View class="px-4 pt-2 pb-6 gap-5 w-full">
            <ConnectionCard />
            <Each of={secs()} key={(g) => g.id}>
              {(g) => <Group title={g().title}><Each of={g().list} key={(s) => s.id}>{(s) => <GroupRow s={s()} last={g().list[g().list.length - 1].id === s().id} />}</Each></Group>}
            </Each>
          </View>
        </Scroll>
      }>{(d) => <DrillPage sec={d()} />}</If>
    </View>
  );
}
