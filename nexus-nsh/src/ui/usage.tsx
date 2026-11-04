import { Text, View, keymap } from "nsh";
import { Button } from "nsh/base";
import { Modal, ModalTitle } from "./modal";
import { T } from "./kit";
import { run, escape } from "../commands";
import { usageDialog } from "../store";
import type { UsageProvider } from "../store";

const windows = (p: UsageProvider) => (p.windows ?? []).filter((w) => p.id !== "copilot" || !["Chat (monthly)", "Completions (monthly)"].includes(w.label ?? ""));
const notes = (p: UsageProvider) => (p.notes ?? []).filter((n) => !(p.id === "grok" && n.startsWith("Prepaid credits:")) && !(p.id === "copilot" && ["AI credits used this period:", "Chat: ", "Completions: "].some((prefix) => n.startsWith(prefix))));

/** `3h 10m`, `2d 4h`, `45s`: how long until `at` (epoch seconds), in the two largest units. */
export const until = (at: number, now = Date.now() / 1000) => {
  const secs = Math.max(0, Math.floor(at - now));
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), mi = Math.floor((secs % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${mi}m` : mi ? `${mi}m` : `${secs}s`;
};
/** Bar colour by share used: green below 70, amber below 90, red above. */
const fill = (used: number) => (used >= 90 ? "bg-error" : used >= 70 ? "bg-warning" : "bg-success");

function Window(p: { w: NonNullable<UsageProvider["windows"]>[number] }) {
  const used = () => (p.w.used_percent == null ? null : Math.max(0, Math.min(100, Math.round(p.w.used_percent))));
  const reset = () => [p.w.resets_at ? `resets in ${until(p.w.resets_at)}` : "", p.w.detail ?? ""].filter(Boolean).join(" · ");
  return (
    <View class="flex-row items-center gap-3 min-h-7">
      <Text style={{ numberOfLines: 1 }} class={`w-44 shrink ${T.sec}`}>{p.w.label ?? "window"}</Text>
      <View class="flex-1 h-1 rounded-full bg-muted overflow-hidden">
        <View class={`h-1 rounded-full ${fill(used() ?? 0)}`} style={{ width: `${used() ?? 0}%` }} />
      </View>
      <Text class="w-10 text-right text-[13px] text-foreground">{used() == null ? "?" : `${used()}%`}</Text>
      <Text style={{ numberOfLines: 1 }} class={`w-36 ${T.meta}`}>{reset()}</Text>
    </View>
  );
}

/** `app.usage` (cmd+shift+u, `/usage`): every connected provider, one bar per limit window. */
export function UsageDialog() {
  keymap.bind("r", () => { if (usageDialog()) run("app.usage"); }, { when: "notEditing" });
  return (
    <If when={usageDialog()}>{(d) => (
      <Modal width={720} top="top-[14%]" label="Provider usage" testID="usage" onCancel={() => escape()} onOpenChange={(open) => { if (!open) usageDialog.set(null); }}>
        <View class="gap-2">
          <ModalTitle title="Provider usage" esc={false} />
          <Each of={d().providers} key={(x: UsageProvider) => x.id}>{(item) => (
            <View class="gap-1 pt-2" testID="usage-provider">
              <View class="flex-row items-baseline gap-2">
                <Text class="text-[13px] font-semibold text-popover-foreground">{item().label ?? item().id}</Text>
                <If when={!!item().plan}><Text class={T.meta}>{item().plan!}</Text></If>
              </View>
              <If when={!item().error} else={<Text class={T.meta}>{`Could not read the limits: ${item().error}`}</Text>}>
                <Each of={windows(item())} key={(w: any) => w.label ?? ""}>{(w) => <Window w={w()} />}</Each>
                <If when={!windows(item()).length && !notes(item()).length}><Text class={T.meta}>No limit windows reported.</Text></If>
                <Each of={notes(item())} key={(_: string, i: number) => `${i}`}>{(n) => <Text class={T.meta}>{n()}</Text>}</Each>
              </If>
            </View>
          )}</Each>
          <If when={!d().providers.length && !d().loading && !d().missing}><Text class={T.meta}>No provider is connected. Connect one in Settings.</Text></If>
          <If when={d().missing && !d().loading}><Text class={T.meta}>No usage snapshot yet. Press r to refresh.</Text></If>
          <If when={d().loading && !d().providers.length}><Text class={T.meta}>Reading provider usage…</Text></If>
          <If when={d().fetched != null}><Text class={T.meta}>{`Updated ${until(Date.now() / 1000, d().fetched!)} ago`}</Text></If>
          <View class="flex-row items-center gap-3 pt-3">
            <Button onPress={() => run("app.usage")} accessibilityLabel="Refresh provider usage" testID="usage-refresh"><Text class={T.meta}>r refresh</Text></Button>
            <Text class={T.meta}>· esc close</Text>
          </View>
        </View>
      </Modal>
    )}</If>
  );
}
