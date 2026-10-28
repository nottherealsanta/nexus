import { agentColor } from "../agent-color";
import { List, Scroll, Text, View, keymap, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { Heading, Modal, ModalFooter, ModalTitle, SelectBar } from "./modal";
import type { JSX } from "nsh";
import { Btn, IconButton, T, FOCUS } from "./kit";
import { availability, commands, escape, keyLabel, run, Cmd } from "../commands";
import { addProjectOpen, compat, confirmDialog, connection, currentProject, drawer, isNarrow, notifications, paletteOpen, picker, selectProject, toast, unread } from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { LeftSidebar } from "./sidebar";
import { RightSidebar } from "./right";

// --- command palette ---------------------------------------------------------------------------
export function Palette() {
  return <If when={paletteOpen()}><PaletteBody /></If>;
}
function PaletteBody() {
  const q = signal("");
  const idx = signal(0);
  const items = () => {
    const needle = q().trim().toLowerCase();
    const all = commands.filter((c) => c.placement.includes("palette") && !c.key_context?.startsWith("escape"));
    const scored = all.map((c) => ({ c, a: availability(c) })).filter(({ c }) => !needle || c.title.toLowerCase().includes(needle) || c.id.includes(needle) || (c.slash ?? "").includes(needle.replace(/^\//, "")));
    // available first (manifest order inside each group), then disabled with their reason
    return [...scored.filter((x) => x.a.ok), ...scored.filter((x) => !x.a.ok)];
  };
  const clamp = (n: number) => Math.max(0, Math.min(items().length - 1, n));
  const pick = (c: Cmd) => {
    const a = availability(c);
    if (!a.ok) { toast("info", `${c.title}: ${a.reason}`); return; }
    paletteOpen.set(false);
    setTimeout(() => void run(c.id), 0);
  };
  const offs = [keymap.bind("down", () => idx.set(clamp(idx() + 1)), { when: "always" }), keymap.bind("up", () => idx.set(clamp(idx() - 1)), { when: "always" })];
  void offs;
  return (
    <Modal width={560} top="top-[12%]" label="Command palette" testID="palette" onCancel={() => escape()} onOpenChange={(open) => { if (!open) paletteOpen.set(false); }}>
      <View role="menu" class="gap-2">
        <ModalTitle title="Commands" />
        <View class="border-b border-border">
          <Input autoFocus placeholder="Type a command…" accessibilityLabel="Search commands" testID="palette-input" onChange={({ value: t }) => { q.set(t); idx.set(0); }} onSubmit={() => { const it = items()[idx()]; if (it) pick(it.c); }} submitBehavior="submit" class="h-10 text-[14px] text-foreground placeholder:text-muted-foreground" />
        </View>
        <If when={items().length > 0} else={<View class="py-4"><Text class={T.sec}>No command matches “{q()}”.</Text></View>}>
          <Scroll class="max-h-[380px]" contentClass="gap-0.5" testID="palette-list">
            <Each of={items()} key={(x) => x.c.id}>
              {(it, i) => {
                const c = () => it().c;
                const dis = () => !it().a.ok;
                return (
                  <Button onPress={() => pick(c())} selected={idx() === i()} accessibilityLabel={`${c().title}${dis() ? `, unavailable: ${it().a.reason}` : ""}`} testID={`cmd-${c().id}`}
                    class={`group/row relative rounded-(--radius) px-3 py-1 min-h-8 justify-center selected:bg-selection hover:bg-accent ${FOCUS}`}>
                    <SelectBar on={idx() === i()} hover />
                    <View class="flex-row items-center gap-3">
                      <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 text-[14px] leading-6 ${dis() ? "text-muted-foreground" : "text-popover-foreground"}`}>{c().title}</Text>
                      <Text class={T.meta}>{keyLabel(c()) ?? (c().slash ? `/${c().slash}` : "")}</Text>
                    </View>
                    <If when={dis() && idx() === i()}><Text style={{ numberOfLines: 2 }} class={T.meta}>{it().a.reason!}</Text></If>
                  </Button>
                );
              }}
            </Each>
          </Scroll>
        </If>
        <ModalFooter hints="↑↓ move · enter run · esc close" right={`${items().length} commands`} />
      </View>
    </Modal>
  );
}

// --- catalog picker (model, effort, agent) ------------------------------------------------------
export function Picker() {
  return <If when={picker() !== null}>{() => <PickerBody />}</If>;
}
function PickerBody() {
  const p = () => picker()!;
  const runItem = (i: { id: string; label: string; run: () => void | Promise<void> }) => { void i.run(); };
  return (
    <Modal width={520} top="top-[14%]" label={p().title} onCancel={() => escape()} onOpenChange={(open) => { if (!open) picker.set(null); }}>
      <View testID="picker" accessibilityLabel={p().title} role="menu" class="gap-2">
        <ModalTitle title={p().title} />
        <If when={p().items.length > 0} else={<View class="py-3"><Text class={T.sec}>{p().note ?? "Nothing to choose from."}</Text></View>}>
          <Scroll class="max-h-[340px]" contentClass="gap-0.5">
            <Each of={p().items} key={(x) => x.id}>
              {(it, i) => (
                <Button onPress={() => runItem(it())} accessibilityLabel={it().label} testID={`pick-${i()}`}
                  class={`group/row relative rounded-(--radius) px-3 min-h-8 justify-center hover:bg-selection ${FOCUS}`}>
                  <SelectBar hover />
                  <View class="flex-row items-center gap-3">
                    <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 text-[14px] leading-6 ${p().title === "Change agent" ? agentColor(it().id) : "text-popover-foreground"}`}>{it().label}</Text>
                    <If when={!!it().hint}><Text class={T.meta}>{it().hint}</Text></If>
                  </View>
                </Button>
              )}
            </Each>
          </Scroll>
        </If>
        <ModalFooter hints="enter choose · esc close" right={p().items.length ? `${p().items.length} of ${p().items.length}` : undefined} />
      </View>
    </Modal>
  );
}

// --- confirm -----------------------------------------------------------------------------------
export function Confirm() {
  return (
    <If when={confirmDialog()}>
      {(d) => (
        <Modal width={440} top="top-[22%]" label={d().title} onCancel={() => escape()} closeOnBackdropPress={false}>
          <ConfirmKeys />
          <View testID="confirm-dialog" role="alert" accessibilityLabel={d().title} class="gap-4">
            <View class="gap-1.5">
              <Text class="text-[15px] leading-5 font-semibold text-popover-foreground">{d().title}</Text>
              <Text class={T.sec}>{d().body}</Text>
              <If when={d().error}><Text class="text-[13px] leading-5 text-error">{"✕ " + d().error}</Text></If>
            </View>
            <View class="flex-row justify-end gap-2">
              <Btn label="Cancel" initial onPress={() => run("alert.cancel")} disabled={!!d().busy} testID="confirm-cancel" />
              <Btn label={d().confirm} kind={d().destructive ? "danger" : "primary"} testID="confirm-ok" busy={!!d().busy}
                onPress={() => void runConfirm()} />
            </View>
            <Text class={T.meta}>{d().destructive ? "enter cancel · tab to the red button to confirm · esc cancel" : "enter confirm · esc cancel"}</Text>
          </View>
        </Modal>
      )}
    </If>
  );
}

function ConfirmKeys() {
  // Enter confirms, but never within 400 ms of the dialog opening: the Enter that chose the command must not also confirm it.
  const opened = Date.now();
  // A destructive confirm has Cancel as its default (D13): Enter cancels, Delete is a deliberate press.
  keymap.bind("enter", () => { const d = confirmDialog(); if (d && !d.busy && Date.now() - opened > 400) { if (d.destructive) void run("alert.cancel"); else void runConfirm(); } }, { when: "always" });
  return <View />;
}
async function runConfirm() {
  const d = confirmDialog(); if (!d) return;
  confirmDialog.update((x) => x && { ...x, busy: true, error: undefined });
  try { await d.run(); confirmDialog.set(null); }
  catch (e) { confirmDialog.update((x) => x && { ...x, busy: false, error: e instanceof ApiError ? e.message : String(e) }); }
}

// --- blocking: incompatible API --------------------------------------------------------------------
export function Blocking() {
  const c = () => connection();
  return (
    <If when={c().kind === "incompatible"}>
      <Modal width={460} top="top-[24%]" label="Incompatible Core" scrim="bg-black/60" onCancel={() => escape()} closeOnBackdropPress={false}>
        <View testID="blocking-dialog" role="alert" accessibilityLabel="Incompatible Core" class="gap-2">
          <View class="flex-row items-center gap-2"><Text class="text-error text-[15px]">✕</Text><Text class="text-[15px] leading-5 font-semibold text-popover-foreground">Incompatible Nexus Core</Text></View>
          <Text class={T.sec}>{(() => { const k = c(); return k.kind === "incompatible" ? `Core reports API ${k.core}; this client speaks 1.x. ${k.update === "client" ? "Update NSH (this app)." : "Update Nexus Core."} No requests are sent until this is fixed.` : ""; })()}</Text>
          <Text class={T.meta}>This dialog cannot be dismissed; it clears itself when Core changes.</Text>
        </View>
      </Modal>
    </If>
  );
}

// --- add project ----------------------------------------------------------------------------------
export function AddProject() {
  return <If when={addProjectOpen()}><AddProjectBody /></If>;
}
function AddProjectBody() {
  const path = signal("");
  const err = signal<string | null>(null);
  const busy = signal(false);
  const submit = async () => {
    if (!path().trim()) { err.set("Enter an absolute directory path."); return; }
    busy.set(true); err.set(null);
    try {
      const p = await request<any>("POST", "/api/project", { body: { path: path().trim() }, key: newKey() });
      addProjectOpen.set(false);
      toast("success", `Project ${p.name} added`);
      setTimeout(() => selectProject(p.id), 50);
    } catch (e) { err.set(e instanceof ApiError ? e.message : String(e)); }
    busy.set(false);
  };
  return (
    <Modal width={480} top="top-[22%]" label="Add project" onCancel={() => escape()} onOpenChange={(open) => { if (!open) addProjectOpen.set(false); }}>
      <View testID="add-project-dialog" class="gap-4" accessibilityLabel="Add project">
        <View class="gap-1.5">
          <Text class="text-[15px] leading-5 font-semibold text-popover-foreground">Add project</Text>
          <Text class={T.sec}>Absolute path of a directory on the machine running Core. Core creates the project if it is new.</Text>
        </View>
        <Input autoFocus placeholder="/path/to/repo" accessibilityLabel="Project path" testID="project-path" onChange={({ value: t }) => path.set(t)} onSubmit={() => void submit()} submitBehavior="submit" class={`h-9 rounded-(--radius) border bg-background px-3 font-mono text-[13px] text-foreground ${err() ? "border-error" : "border-border"}`} />
        <If when={err()}><Text class="text-[13px] leading-5 text-error">{"✕ " + err()}</Text></If>
        <View class="flex-row justify-end gap-2">
          <Btn label="Cancel" onPress={() => addProjectOpen.set(false)} />
          <Btn label="Add project" kind="primary" busy={busy()} onPress={() => void submit()} testID="project-add-ok" />
        </View>
      </View>
    </Modal>
  );
}

// --- drawers (narrow layout) -------------------------------------------------------------------------
/** Narrow layouts: a sidebar is a drawer over the centre column. It starts below the header, so both toggles (the
 * navigation anchors) stay visible and usable; Esc closes it through the manifest's escape chain (rule `drawer`).
 * Built from plain views because NSH 0.3.0's `Sheet` breaks Escape routing for dialogs opened after it (README "Gaps"),
 * and a `Dialog` would cover the header. */
function EdgeDrawer(p: { side: "left" | "right"; width: number; label: string; children?: JSX.Element }) {
  return (
    <View class="absolute inset-0" testID={`drawer-${p.side}`} role="none">
      <View onPress={() => drawer.set(null)} accessibilityLabel="Close drawer" focusable={false} class="absolute inset-0 bg-black/50" />
      <View class={`absolute top-0 bottom-0 ${p.side === "left" ? "left-0 border-r" : "right-0 border-l"} border-border bg-card shadow-2xl`} style={{ width: p.width, maxWidth: "92%" }} accessibilityLabel={p.label}>{p.children}</View>
    </View>
  );
}
export function Drawers() {
  const side = (s: "left" | "right" | "notifications") => drawer() === s;
  return (
    <View class="absolute inset-0" style={{ pointerEvents: "box-none" }}>
      <If when={isNarrow() && side("left")}><EdgeDrawer side="left" width={320} label="Left sidebar drawer"><LeftSidebar drawer /></EdgeDrawer></If>
      <If when={isNarrow() && side("right")}><EdgeDrawer side="right" width={340} label="Right sidebar drawer"><RightSidebar drawer /></EdgeDrawer></If>
      <If when={side("notifications")}><EdgeDrawer side="right" width={isNarrow() ? 340 : 380} label="Notifications drawer"><NotificationList /></EdgeDrawer></If>
    </View>
  );
}
const GL = { success: "✓", info: "i", warning: "!", error: "✕" } as const;
function NotificationList() {
  return (
    <View class="h-full w-full bg-card" testID="notifications" accessibilityLabel="Notifications">
      <View class="flex-row items-center gap-2 border-b border-border px-4 h-12">
        <Text class="flex-1 text-[15px] leading-5 font-semibold text-foreground">Notifications</Text>
        <Btn label="Mark all read" kind="ghost" onPress={() => run("notifications.mark_all_read")} disabled={!availability(commands.find((c) => c.id === "notifications.mark_all_read")!).ok} />
        <IconButton icon="x" label="Close" onPress={() => drawer.set(null)} />
      </View>
      <If when={notifications().length > 0} else={<View class="p-4"><Text class={T.sec}>Nothing yet. Toasts and alerts are kept here, newest first.</Text></View>}>
        <List each={notifications()} keyExtractor={(n) => String(n.id)} class="flex-1" contentStyle={{ padding: 8, gap: 4 }}>
          {(n) => (
            <View class="flex-row gap-2 rounded-(--radius) px-2.5 py-2 hover:bg-accent">
              <Text class="w-3.5 text-center text-[13px] leading-5 text-foreground-secondary">{GL[n().level]}</Text>
              <Text class="flex-1 text-[13px] leading-5 text-foreground">{n().text}</Text>
            </View>
          )}
        </List>
      </If>
    </View>
  );
}
void compat; void currentProject; void unread;
