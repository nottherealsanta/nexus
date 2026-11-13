import { Scroll, Text, View, keymap, windowSize } from "nsh";
import { Button } from "nsh/base";
import { Modal, ModalTitle } from "./modal";
import { T } from "./kit";
import { run, escape } from "../commands";
import { isNarrow, usageDialog } from "../store";
import type { UsageProvider } from "../store";
import { usageGlyphs } from "../gen/theme";
import { gaugeCells, leftPercent, reservePercent, reserveText, until } from "../usage-format";

const windows = (p: UsageProvider) => (p.windows ?? []).filter((w) => p.id !== "copilot" || !["Chat (monthly)", "Completions (monthly)"].includes(w.label ?? ""));
const notes = (p: UsageProvider) => (p.notes ?? []).filter((n) => !(p.id === "codex" && n.startsWith("Limit reached:")) && !(p.id === "grok" && (n.startsWith("Prepaid credits:") || n.endsWith(" of credits"))) && !(p.id === "copilot" && ["AI credits used this period:", "Chat: ", "Completions: "].some((prefix) => n.startsWith(prefix))));
const glyphs = [usageGlyphs.empty, usageGlyphs.eighth_1, usageGlyphs.eighth_2, usageGlyphs.eighth_3, usageGlyphs.eighth_4, usageGlyphs.eighth_5, usageGlyphs.eighth_6, usageGlyphs.eighth_7, usageGlyphs.full];
const mono = "font-mono text-[13px]";
const sans = "font-(family-name:--font-ui) text-[13px] tabular-nums";

function Window(p: { w: NonNullable<UsageProvider["windows"]>[number]; now: number }) {
  const used = () => p.w.used_percent;
  const left = () => leftPercent(used()), reserve = () => reservePercent(used(), p.w.resets_at, p.w.period_seconds, p.now);
  const reserveLabel = () => reserveText(reserve()), expires = () => until(p.w.resets_at, p.now);
  const cells = () => gaugeCells(used());
  const filled = () => cells().map((n) => n ? glyphs[n].unicode : "").join("");
  const empty = () => cells().map((n) => n ? "" : glyphs[n].unicode).join("");
  const color = () => (used() ?? 0) >= 90 ? "text-error" : (used() ?? 0) >= 70 ? "text-warning" : "text-success";
  return (
    <View accessibilityLabel={`${p.w.label ?? "window"}, ${used() == null ? "unknown" : `${used()}%`} used, ${left()} left, reserve ${reserveLabel()}, expires ${expires()}`} class={isNarrow() ? "pl-3" : "flex-row items-center pl-3"}>
      <Text style={{ numberOfLines: 1 }} class={`${isNarrow() ? "" : "flex-1"} ${sans} text-foreground`}>{p.w.label ?? "window"}</Text>
      <View class="flex-row items-center">
        <Text accessible={false} class={`w-24 ${mono}`}><Text class={color()}>{filled()}</Text><Text class="text-muted-foreground">{empty()}</Text></Text>
        <Text class={`w-14 text-right ${sans} ${left() === "—" ? "text-muted-foreground" : "text-foreground"}`}>{left()}</Text>
        <Text class={`w-16 text-right ${sans} ${reserve() == null || reserveLabel() === "0%" ? "text-muted-foreground" : reserve()! > 0 ? "text-success" : "text-warning"}`}>{reserveLabel()}</Text>
        <Text style={{ numberOfLines: 1 }} class={`w-20 pl-3 ${sans} ${expires() === "—" ? "text-muted-foreground" : "text-foreground"}`}>{expires()}</Text>
      </View>
    </View>
  );
}

export function UsageDialog() {
  keymap.bind("r", () => { if (usageDialog()) run("app.usage"); }, { when: "notEditing" });
  return (
    <If when={usageDialog()}>{(d) => {
      const now = Date.now() / 1000;
      const age = () => d().fetched == null ? "" : now - d().fetched! < 60 ? `${Math.max(0, Math.floor(now - d().fetched!))}s` : until(now, d().fetched!);
      return <Modal size="large" tall label="Provider usage" testID="usage" onCancel={() => escape()} onOpenChange={(open) => { if (!open) usageDialog.set(null); }}>
        <View class="gap-2">
          <ModalTitle title="Provider usage" />
          <View class={isNarrow() ? "" : "flex-row items-center"}>
            <Text class={`${isNarrow() ? "" : "flex-1"} ${mono} text-muted-foreground`}>Window</Text>
            <View class="flex-row items-center">
            <Text class={`w-24 ${mono} text-muted-foreground`}>Progress</Text><Text class={`w-14 text-right ${sans} text-muted-foreground`}>Left</Text><Text class={`w-16 text-right ${sans} text-muted-foreground`}>Reserve</Text><Text class={`w-20 pl-3 ${sans} text-muted-foreground`}>Expires</Text>
            </View>
          </View>
          <Scroll focusable style={{ maxHeight: Math.max(48, Math.floor(windowSize().height * 0.9) - 200) }}>
          <Each of={d().providers} key={(x: UsageProvider) => x.id}>{(item) => (
            <View class="gap-1 pt-2" testID="usage-provider">
              <View class="flex-row items-baseline gap-2"><Text class="text-[13px] font-semibold text-popover-foreground">{item().label ?? item().id}</Text><If when={!!item().plan}><Text class={T.meta}>{item().plan!}</Text></If></View>
              <If when={!item().error} else={<Text class={T.meta}>{`Could not read the limits: ${item().error}`}</Text>}>
                <Each of={windows(item())} key={(w: any) => w.label ?? ""}>{(w) => <Window w={w()} now={now} />}</Each>
                <If when={!windows(item()).length && !notes(item()).length}><Text class={T.meta}>No limit windows reported.</Text></If>
                <Each of={notes(item())} key={(_: string, i: number) => `${i}`}>{(n) => <Text class={T.meta}>{n()}</Text>}</Each>
              </If>
            </View>
          )}</Each>
          <If when={!d().providers.length && !d().loading && !d().missing}><Text class={T.meta}>No provider is connected. Connect one in Settings.</Text></If>
          <If when={d().missing && !d().loading}><Text class={T.meta}>No usage snapshot yet. Press r to refresh.</Text></If>
          <If when={d().loading && !d().providers.length}><Text class={T.meta}>Reading provider usage…</Text></If>
          <If when={d().fetched != null}><Text class={T.meta}>{`Updated ${age()} ago`}</Text></If>
          </Scroll>
          <View class="flex-row items-center gap-3 pt-3"><Button onPress={() => run("app.usage")} accessibilityLabel="Refresh provider usage" testID="usage-refresh"><Text class={T.meta}>r refresh</Text></Button><Text class={T.meta}>· ↑↓ scroll · esc close</Text></View>
        </View>
      </Modal>;
    }}</If>
  );
}
