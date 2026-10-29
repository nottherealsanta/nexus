import { Text, View, keymap } from "nsh";
import { Modal, ModalFooter, ModalTitle } from "./modal";
import { T } from "./kit";
import { run, escape } from "../commands";
import { usageDialog } from "../store";
import type { UsageProvider } from "../store";

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
      <Text style={{ numberOfLines: 1 }} class={`w-16 ${T.sec}`}>{p.w.label ?? "window"}</Text>
      <View class="flex-1 h-1 rounded-full bg-muted overflow-hidden">
        <View class={`h-1 rounded-full ${fill(used() ?? 0)}`} style={{ width: `${used() ?? 0}%` }} />
      </View>
      <Text class="w-10 text-right text-[13px] text-foreground">{used() == null ? "?" : `${used()}%`}</Text>
      <Text style={{ numberOfLines: 1 }} class={`w-36 ${T.meta}`}>{reset()}</Text>
    </View>
  );
}

/** `app.usage` (cmd+shift+u, `/usage`): the plan limits of the signed-in providers, one bar per window. */
export function UsageDialog() {
  keymap.bind("r", () => { if (usageDialog()) run("app.usage"); }, { when: "notEditing" });
  return (
    <If when={usageDialog()}>{(d) => (
      <Modal width={520} top="top-[14%]" label="Plan limits" testID="usage" onCancel={() => escape()} onOpenChange={(open) => { if (!open) usageDialog.set(null); }}>
        <View class="gap-2">
          <ModalTitle title="Plan limits" />
          <Each of={d().providers} key={(x: UsageProvider) => x.id}>{(item) => { const p = item(); return (
            <View class="gap-1 pt-2" testID="usage-provider">
              <View class="flex-row items-baseline gap-2">
                <Text class="text-[13px] font-semibold text-popover-foreground">{p.label ?? p.id}</Text>
                <If when={!!p.plan}><Text class={T.meta}>{p.plan!}</Text></If>
              </View>
              <If when={!p.error} else={<Text class={T.meta}>{`Could not read the limits: ${p.error}`}</Text>}>
                <Each of={p.windows ?? []} key={(w: any) => w.label ?? ""}>{(w) => <Window w={w()} />}</Each>
              </If>
            </View>
          ); }}</Each>
          <If when={!d().providers.length && !d().loading}><Text class={T.meta}>No signed-in provider reports plan limits (codex and copilot do).</Text></If>
          <If when={d().loading && !d().providers.length}><Text class={T.meta}>Reading plan limits…</Text></If>
          <ModalFooter hints="r refresh · esc close" />
        </View>
      </Modal>
    )}</If>
  );
}
