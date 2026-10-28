import { Text, View } from "nsh";
import { Button } from "nsh/base";
import { Btn, Icon, T, FOCUS } from "./kit";
import { run, availability, byId } from "../commands";
import { banners, compat, connection, dismissedBanners, dismissToast, info, isNarrow, tick, toasts } from "../store";
import { statusGlyphs } from "../gen/theme";

type Level = "info" | "warning" | "error" | "loading";
interface Alert { key: string; level: Level; text: string; sub?: string; actions: { label: string; cmd?: string; run?: () => void }[]; dismissible: boolean }

const GLYPH: Record<Level, { g: string; tone: string; word: string }> = {
  info: { g: "i", tone: "text-info", word: "info" },
  warning: { g: "!", tone: "text-warning", word: "warning" },
  error: { g: statusGlyphs.error.unicode, tone: "text-error", word: "error" },
  loading: { g: statusGlyphs.running.unicode, tone: "text-foreground", word: "loading" },
};
const rank: Record<Level, number> = { error: 3, warning: 2, info: 1, loading: 0 };

export function currentAlerts(): Alert[] {
  const out: Alert[] = [];
  const c = connection();
  const hidden = dismissedBanners();
  switch (c.kind) {
    case "connecting": out.push({ key: "conn", level: "loading", text: "Connecting to Nexus Core…", actions: [], dismissible: false }); break;
    case "starting": out.push({ key: "conn", level: "loading", text: "Starting Nexus Core…", sub: "Running `nexus daemon start`.", actions: [], dismissible: false }); break;
    case "reconnecting": {
      const s = Math.max(0, Math.ceil((c.retryAt - (tick(), Date.now())) / 1000));
      out.push({ key: "conn", level: "error", text: `Disconnected from Core · reconnecting${s > 0 ? ` in ${s} s` : "…"}`, sub: `${c.reason}. The view is read-only until Core is back.`, actions: [{ label: "Retry now", cmd: "app.reconnect" }], dismissible: false });
      break;
    }
    case "offline": out.push({
      key: "conn", level: "error", text: c.noDaemon ? "Nexus Core is not running" : "Nexus Core is not reachable",
      sub: c.noDaemon ? "Start it with `nexus daemon start` (or the button), then this view resyncs by itself." : `${c.reason}. Still retrying; the view is read-only.`,
      actions: [...(c.noDaemon ? [{ label: "Start Core", cmd: "app.start_daemon" }] : []), { label: "Retry now", cmd: "app.reconnect" }], dismissible: false,
    }); break;
    case "auth_failed": out.push({ key: "conn", level: "error", text: "Authentication with Core failed", sub: `${c.reason}. The host re-reads daemon.json on every request; check NEXUS10_HOME of the host process.`, actions: [{ label: "Retry now", cmd: "app.reconnect" }], dismissible: false }); break;
    default: break;
  }
  for (const b of banners()) {
    const key = `b-${b.kind}`;
    if (hidden.includes(key)) continue;
    if (b.kind === "api_unreported") out.push({ key, level: "info", text: "Core does not report an API version yet", sub: "Compatibility is unchecked until Core adds api_version to /api/info (Step 12a).", actions: [], dismissible: true });
    if (b.kind === "newer_core") out.push({ key, level: "warning", text: `Core has a newer API (${b.core}) than this client`, sub: "Some features may be missing. Update NSH.", actions: [], dismissible: true });
    if (b.kind === "daemon_restarted") out.push({ key, level: "warning", text: "Core restarted: state was resynced", sub: "Turns that were running show as interrupted.", actions: [], dismissible: true });
    if (b.kind === "resynced") out.push({ key, level: "info", text: "Resynced with Core", sub: b.why, actions: [], dismissible: true });
  }
  return out.sort((a, b) => rank[b.level] - rank[a.level]);
}

export function Banners() {
  const all = () => currentAlerts();
  const top = () => all()[0];
  return (
    <If when={top()}>
      {(a) => (
        <View class="border-b border-border bg-card" role="alert" testID="banner" accessibilityLabel={`${GLYPH[a().level].word}: ${a().text}`}>
          <View class="flex-row items-center gap-2.5 px-4 py-2">
            <Text class={`w-4 text-center text-[13px] leading-5 font-semibold ${GLYPH[a().level].tone}`}>{GLYPH[a().level].g}</Text>
            <View class="flex-1 min-w-0">
              <Text class={`text-[13px] leading-5 ${GLYPH[a().level].tone} font-medium`}>{a().text}</Text>
              <If when={a().sub}><Text class={T.sec}>{a().sub!}</Text></If>
            </View>
            <If when={all().length > 1}><Text class={T.meta}>{`+${all().length - 1}`}</Text></If>
          </View>
          <If when={a().actions.length > 0 || a().dismissible}>
            <View class="flex-row items-center justify-end gap-2 px-4 pb-2 -mt-0.5">
              <Each of={a().actions} key={(x) => x.label}>
                {(x) => <Btn label={x().label} onPress={() => (x().cmd ? void run(x().cmd!) : x().run?.())} disabled={x().cmd ? !availability(byId.get(x().cmd!)!).ok : false} />}
              </Each>
              <If when={a().dismissible}><Btn label="Hide" kind="ghost" onPress={() => dismissedBanners.update((l) => [...l, a().key])} /></If>
            </View>
          </If>
        </View>
      )}
    </If>
  );
}

const TG = { success: { g: "✓", t: "text-success" }, info: { g: "i", t: "text-info" }, warning: { g: "!", t: "text-warning" }, error: { g: "✕", t: "text-error" } };
export function Toasts() {
  return (
    <View class={`absolute right-3 left-3 items-end gap-2 ${isNarrow() ? "top-2" : "bottom-[128px]"}`} style={{ pointerEvents: "box-none" }} testID="toasts" accessibilityLabel="Notifications">
      <Each of={toasts()} key={(t) => t.id}>
        {(t) => (
          <Button onPress={() => dismissToast(t().id)} role="alert" accessibilityLabel={`${t().level}: ${t().text}`} class={`flex-row items-center gap-2 rounded-(--radius-card) border border-border bg-popover px-3.5 min-h-10 max-w-[420px] shadow-lg ${FOCUS}`}>
            <Text class={`w-3.5 text-center text-[13px] font-semibold ${TG[t().level].t}`}>{TG[t().level].g}</Text>
            <Text class="shrink min-w-0 text-[13px] leading-5 text-popover-foreground">{t().text}</Text>
          </Button>
        )}
      </Each>
    </View>
  );
}
void Icon; void compat; void info;
