import { Scroll, Text, View, keymap } from "nsh";
import { Button } from "nsh/base";
import { Btn, Icon, IconButton, T, FOCUS } from "./kit";
import { Modal, ModalFooter, ModalTitle } from "./modal";
import { run, availability, byId, copyText, escape } from "../commands";
import { alertDetail, banners, compat, connection, dismissedBanners, dismissToast, info, isNarrow, native, openAlert, splitAlert, tick, TOAST_VISIBLE, toasts } from "../store";
import type { Toast } from "../store";
import { statusGlyphs } from "../gen/theme";
import { TEXT_INSET, TimelineColumn } from "./reading-column";

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
      out.push({ key: "conn", level: "warning", text: `Disconnected from Core · reconnecting${s > 0 ? ` in ${s} s` : "…"}`, sub: `${c.reason}. The view is read-only until Core is back.`, actions: [{ label: "Retry now", cmd: "app.reconnect" }], dismissible: false });
      break;
    }
    case "offline": out.push({
      key: "conn", level: "error", text: c.noDaemon ? "Nexus Core is not running" : "Nexus Core is not reachable",
      sub: c.noDaemon ? "Start it with `nexus daemon start` (or the button), then this view resyncs by itself." : `${c.reason}. Still retrying; the view is read-only.`,
      actions: [...(c.noDaemon ? [{ label: "Start Core", cmd: "app.start_daemon" }] : []), { label: "Retry now", cmd: "app.reconnect" }], dismissible: false,
    }); break;
    case "auth_failed": out.push({
      key: "conn", level: "error", text: "Authentication with Core failed",
      sub: native
        ? `${c.reason}. This Core requires a token: start it with NEXUS_HOST=0.0.0.0 so a native app connects without one, or check the address on the Core button.`
        : `${c.reason}. The host re-reads daemon.json on every request; check NEXUS10_HOME of the host process.`,
      actions: [{ label: "Retry now", cmd: "app.reconnect" }], dismissible: false,
    }); break;
    default: break;
  }
  for (const b of banners()) {
    const key = `b-${b.kind}`;
    if (hidden.includes(key)) continue;
    if (b.kind === "api_unreported") out.push({ key, level: "info", text: "Core does not report an API version", sub: "Compatibility cannot be checked.", actions: [], dismissible: true });
    if (b.kind === "newer_core") out.push({ key, level: "warning", text: `Core has a newer API (${b.core}) than this client`, sub: "Some features may be missing. Update NSH.", actions: [], dismissible: true });
    if (b.kind === "daemon_restarted") out.push({ key, level: "warning", text: "Core restarted: state was resynced", sub: "Turns that were running show as interrupted.", actions: [], dismissible: true });
    if (b.kind === "resynced") out.push({ key, level: "info", text: "Resynced with Core", sub: b.why, actions: [], dismissible: true });
  }
  return out.sort((a, b) => rank[b.level] - rank[a.level]);
}

/**
 * The banner (D15): one strip on the panel surface, pushing the timeline down; never over it. Like a toast,
 * only the glyph carries the level; the words stay in the text colour, so a transient `reconnecting` does not
 * read as a failure. Its content sits in the reading column, on the same text and right edges as the dock.
 */
export function Banners() {
  const all = () => currentAlerts();
  const top = () => all()[0];
  return (
    <If when={top()}>
      {(a) => (
        <View class="shrink-0 border-b border-border bg-card" role="alert" testID="banner" accessibilityLabel={`${GLYPH[a().level].word}: ${a().text}${a().sub ? `. ${a().sub}` : ""}`}>
          <TimelineColumn>
            <View class={`${TEXT_INSET} ${isNarrow() ? "py-2 gap-2" : "min-h-10 flex-row items-center gap-2.5 py-1"}`}>
              <View class={`flex-row items-center gap-2.5 ${isNarrow() ? "min-h-6" : "shrink min-w-0"}`}>
                <Text class={`w-4 text-center text-[13px] leading-5 font-semibold ${GLYPH[a().level].tone}`}>{GLYPH[a().level].g}</Text>
                <Text style={{ numberOfLines: isNarrow() ? 2 : 1 }} class="flex-1 shrink min-w-0 text-[13px] leading-5 font-semibold text-foreground">{a().text}</Text>
                <If when={isNarrow() && a().dismissible}><IconButton icon="x" label="Hide" onPress={() => dismissedBanners.update((l) => [...l, a().key])} testID="banner-hide" /></If>
              </View>
              <If when={!isNarrow() && !!a().sub}><Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${T.sec}`}>{a().sub!}</Text></If>
              <If when={!isNarrow() && !a().sub}><View class="flex-1" /></If>
              <If when={!isNarrow() || a().actions.length > 0 || all().length > 1}><View class={`flex-row items-center gap-2 ${isNarrow() ? "justify-end" : ""}`}>
                <If when={all().length > 1}><Text class={T.meta}>{`+${all().length - 1}`}</Text></If>
                <Each of={a().actions} key={(x) => x.label}>
                  {(x) => <Btn label={x().label} onPress={() => (x().cmd ? void run(x().cmd!) : x().run?.())} disabled={x().cmd ? !availability(byId.get(x().cmd!)!).ok : false} />}
                </Each>
                <If when={!isNarrow() && a().dismissible}><IconButton icon="x" label="Hide" onPress={() => dismissedBanners.update((l) => [...l, a().key])} testID="banner-hide" /></If>
              </View></If>
            </View>
          </TimelineColumn>
        </View>
      )}
    </If>
  );
}

const TG = { success: { g: "✓", t: "text-success", r: "bg-success" }, info: { g: "i", t: "text-info", r: "bg-info" }, warning: { g: "!", t: "text-warning", r: "bg-warning" }, error: { g: "✕", t: "text-error", r: "bg-error" } };

/** Toasts (D15): top-right under the header, oldest on top, three at most and then `+N more`; no lifetime hairline. */
export function Toasts() {
  const shown = () => toasts().slice(0, TOAST_VISIBLE);
  const more = () => Math.max(0, toasts().length - TOAST_VISIBLE);
  return (
    <View class={`absolute right-3 items-end gap-2 ${isNarrow() ? "left-3" : "w-[360px]"} ${currentAlerts().length > 0 ? "top-14" : "top-3"}`} style={{ pointerEvents: "box-none" }} testID="toasts" accessibilityLabel="Notifications">
      <Each of={shown()} key={(t) => t.id}>{(t) => <ToastCard t={t} />}</Each>
      <If when={more() > 0}><Text testID="toasts-more" class={`${T.meta} rounded-full bg-popover px-2.5 py-0.5`}>{`+${more()} more`}</Text></If>
    </View>
  );
}
function ToastCard(p: { t: () => Toast }) {
  const t = p.t;
  const sp = () => splitAlert(t().text);
  return (
    <View class="flex-row w-full rounded-(--radius-card) bg-popover overflow-hidden" style={{ boxShadow: "0 8px 24px rgb(0 0 0 / 0.3)" }} testID="toast">
      <View class={`w-[3px] self-stretch ${TG[t().level].r}`} />
      <Button onPress={() => openAlert(t())} role="alert" accessibilityLabel={`${t().level}: ${t().text}`} testID="toast-open" class={`flex-1 min-w-0 flex-row items-start gap-2.5 py-2.5 pl-3 hover:bg-accent ${FOCUS}`}>
        <Text class={`w-3.5 pt-px text-center text-[13px] leading-5 font-semibold ${TG[t().level].t}`}>{TG[t().level].g}</Text>
        <View class="flex-1 min-w-0">
          <If when={sp().inline} else={
            <View>
              <Text style={{ numberOfLines: 2 }} class="text-[13px] leading-5 font-semibold text-popover-foreground">{sp().title}</Text>
              <If when={!!sp().body}><Text style={{ numberOfLines: 2 }} class={T.sec}>{sp().body}</Text></If>
            </View>
          }>
            <Text style={{ numberOfLines: 3 }} class={T.sec}><Text class="font-semibold text-popover-foreground">{sp().title}</Text>{sp().body}</Text>
          </If>
        </View>
      </Button>
      <View class="pr-1 pt-1"><IconButton icon="x" label="Dismiss" onPress={() => dismissToast(t().id)} testID="toast-dismiss" /></View>
    </View>
  );
}

/** The full text of an alert (D15, D13): a modal titled by the toast, the whole text wrapped and scrollable; `c` copies it. */
export function AlertDetail() {
  return <If when={alertDetail()}>{(a) => <AlertDetailBody a={a} />}</If>;
}
function AlertDetailBody(p: { a: () => Toast }) {
  const a = p.a;
  const copy = () => copyText(a().text, "Copied the notification");
  keymap.bind("c", copy, { when: "notEditing" });
  return (
    <Modal width={520} top="top-[14%]" label="Notification" testID="alert-detail" onCancel={() => escape()} onOpenChange={(open) => { if (!open) alertDetail.set(null); }}>
      <View class="gap-2" role="alert" accessibilityLabel={`${a().level}: ${a().text}`}>
        <View class="flex-row items-center min-h-8 gap-3">
          <Text class={`w-4 text-center text-[15px] font-semibold ${TG[a().level].t}`}>{TG[a().level].g}</Text>
          <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[15px] leading-5 font-semibold text-popover-foreground">{splitAlert(a().text).title}</Text>
        </View>
        <Scroll class="max-h-[320px]">
          <Text testID="alert-detail-text" style={{ userSelect: "text" }} class="text-[13px] leading-5 text-foreground">{a().text}</Text>
        </Scroll>
        <View class="flex-row justify-end"><Btn label="Copy" onPress={copy} testID="alert-detail-copy" /></View>
        <ModalFooter hints="c copy · esc close" right={new Date(a().at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} />
      </View>
    </Modal>
  );
}
void Icon; void compat; void info; void ModalTitle;
