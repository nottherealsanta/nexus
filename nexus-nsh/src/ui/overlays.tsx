import { agentColor } from "../agent-color";
import { List, Scroll, Text, View, keymap, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { Heading, Modal, modalBodyHeight, ModalFooter, ModalTitle } from "./modal";
import type { JSX } from "nsh";
import { Btn, Empty, IconButton, T, TS, FOCUS } from "./kit";
import { availability, closeLearnChecklist, commands, escape, installLearnChecklist, keyLabel, renameSession, run, Cmd } from "../commands";
import { patchConfig, config, addProjectOpen, alertDetail, compat, confirmDialog, connection, currentProject, drawer, isNarrow, learnChecklist, notifications, paletteOpen, picker, selectProject, toast, unread, renaming, viewed } from "../store";
import { ApiError, newKey, request } from "../core/transport";
import { LeftSidebar } from "./sidebar";
import { RightSidebar } from "./right";
import { defaultSkillSelection, selectedSkillCounts } from "../skills-mcp";
import { structure } from "../gen/theme";

// --- command palette ---------------------------------------------------------------------------
export function Palette() {
  return <If when={paletteOpen()}><PaletteBody /></If>;
}
const PALETTE_ORDER = ["session", "project", "model", "composer", "tools", "timeline", "tool", "waiting", "navigation", "files", "editor", "git", "copy", "inspect", "notifications", "connection", "auth", "appearance", "dictation", "app"];
const paletteKey = (category: string) => category === "sessions" ? "session" : category === "agent" ? "model" : category;
const paletteRank = (category: string) => { const at = PALETTE_ORDER.indexOf(paletteKey(category)); return at < 0 ? PALETTE_ORDER.length : at; };
/** The palette heading for a manifest category: the session categories share one, and so do model and agent. */
const paletteGroup = (category: string) => { const key = paletteKey(category); return key === "model" ? "Model and agent" : key.charAt(0).toUpperCase() + key.slice(1); };

function PaletteBody() {
  const q = signal("");
  const idx = signal(0);
  const grouped = () => !q().trim();
  const items = () => {
    const needle = q().trim().toLowerCase();
    const all = commands.filter((c) => c.placement.includes("palette") && !c.key_context?.startsWith("escape") && c.key_context !== "dialog" && c.id !== "palette.open");
    const scored = all.map((c) => ({ c, a: availability(c) })).filter(({ c }) => !needle || c.title.toLowerCase().includes(needle) || c.id.includes(needle) || c.category.includes(needle) || (c.slash ?? "").includes(needle.replace(/^\//, "")));
    // Unfiltered: by group, in manifest order, each command with its reason when it is unavailable. Filtered: available first.
    return needle
      ? [...scored.filter((x) => x.a.ok), ...scored.filter((x) => !x.a.ok)]
      : scored.map((x, i) => ({ x, i })).sort((a, b) => paletteRank(a.x.c.category) - paletteRank(b.x.c.category) || a.i - b.i).map(({ x }) => x);
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
    <Modal size="medium" label="Command palette" testID="palette" onCancel={() => escape()} onOpenChange={(open) => { if (!open) paletteOpen.set(false); }}>
      <View role="menu" class="gap-2">
        <ModalTitle title="Command palette" />
        <View class="border-b border-border">
          <Input autoFocus placeholder="type to filter commands" accessibilityLabel="Search commands" testID="palette-input" onChange={({ value: t }) => { q.set(t); idx.set(0); }} onSubmit={() => { const it = items()[idx()]; if (it) pick(it.c); }} submitBehavior="submit" class={`h-10 ${TS.base} text-foreground placeholder:text-muted-foreground`} />
        </View>
        <If when={items().length > 0} else={<View class="py-4"><Text class={T.sec}>No command matches “{q()}”.</Text></View>}>
          <Scroll style={{ maxHeight: Math.min(380, modalBodyHeight(40)) }} testID="palette-list">
            <View class="gap-0.5">
            <Each of={items()} key={(x) => x.c.id}>
              {(it, i) => {
                const c = () => it().c;
                const dis = () => !it().a.ok;
                const heading = () => grouped() && (i() === 0 || paletteGroup(items()[i() - 1].c.category) !== paletteGroup(c().category));
                return (
                  <View>
                    <If when={heading()}><Text class={`px-3 ${TS.base} font-semibold uppercase text-muted-foreground ${i() === 0 ? "pb-1" : "pt-3 pb-1"}`} testID={`palette-group-${paletteGroup(c().category)}`}>{paletteGroup(c().category)}</Text></If>
                    <Button onPress={() => pick(c())} selected={idx() === i()} accessibilityLabel={`${c().title}${dis() ? `, unavailable: ${it().a.reason}` : ""}`} testID={`cmd-${c().id}`}
                      class={`group/row relative rounded-none px-3 py-0.5 min-h-7 justify-center hover:bg-accent ${idx() === i() ? "bg-selection" : ""} ${FOCUS}`}>
                      <If when={idx() === i()}><Text class={`absolute left-0 ${TS.base} text-foreground`} accessible={false}>{structure.focus_bar.unicode}</Text></If>
                      <View class="flex-row items-center gap-3">
                        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.base} ${dis() ? "text-muted-foreground" : "text-popover-foreground"}`}>{dis() ? `${c().title} · ${it().a.reason}` : c().title}</Text>
                        <Text class={T.meta}>{keyLabel(c()) ?? ""}</Text>
                      </View>
                    </Button>
                  </View>
                );
              }}
            </Each>
            </View>
          </Scroll>
        </If>
        <ModalFooter hints="↑↓ move · enter run · esc close" right={`${items().length ? idx() + 1 : 0} of ${items().length}`} />
      </View>
    </Modal>
  );
}

// --- catalog picker (model, effort, agent) ------------------------------------------------------
export function Picker() {
  return <If when={picker() !== null}>{() => <PickerBody />}</If>;
}

/** TUI-style fuzzy filter: the query's characters appear in order. */
function fuzzyMatch(query: string, text: string) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  let at = 0;
  for (const ch of text.toLowerCase()) if (ch === q[at]) at++;
  return at === q.length;
}

function PickerBody() {
  const p = () => picker()!;
  const query = signal("");
  const idx = signal(0);
  const isModels = () => p().title === "Change model";
  const favorites = () => (isModels() ? ((config()?.values?.ui?.favorite_models ?? []) as string[]) : []);
  const filtered = () => {
    const q = query();
    const rows = p().items.filter((it) => fuzzyMatch(q, it.label) || fuzzyMatch(q, it.id) || fuzzyMatch(q, it.hint ?? ""));
    if (!isModels()) return rows;
    const fav = favorites();
    return [...rows.filter((it) => fav.includes(it.id)), ...rows.filter((it) => !fav.includes(it.id))];
  };
  const toggleFavorite = () => {
    const id = chosen()?.id;
    if (!isModels() || !id) return;
    const fav = favorites();
    void patchConfig("ui.favorite_models", fav.includes(id) ? fav.filter((f) => f !== id) : [...fav, id]);
  };
  const clamp = (n: number) => Math.max(0, Math.min(filtered().length - 1, n));
  const chosen = () => filtered()[clamp(idx())];
  const choose = (item = chosen()) => { if (item) void item.run(); };
  const placeholder = () => {
    if (p().title === "Change model") return "Type to filter models…";
    if (p().title === "Change agent") return "Type to filter agents…";
    if (p().title === "Reasoning effort") return "Type to filter efforts…";
    return "Type to filter…";
  };
  const keys = [
    keymap.bind("down", () => idx.set(clamp(idx() + 1)), { when: "always" }),
    keymap.bind("up", () => idx.set(clamp(idx() - 1)), { when: "always" }),
    ...(p().title === "Change model" ? [keymap.bind("tab", toggleFavorite, { when: "always" })] : []),
  ];
  void keys;
  return (
    <Modal size="medium" label={p().title} onCancel={() => escape()} onOpenChange={(open) => { if (!open) picker.set(null); }}>
      <View testID="picker" accessibilityLabel={p().title} role="menu" class="gap-2">
        <ModalTitle title={p().title} />
        <View class="border-b border-border">
          <Input autoFocus value={query()} placeholder={placeholder()} accessibilityLabel={placeholder()} testID="picker-input"
            onChange={({ value }) => { query.set(value); idx.set(0); }} onSubmit={() => choose()} submitBehavior="submit"
            class={`h-10 ${TS.base} text-foreground placeholder:text-muted-foreground`} />
        </View>
        <If when={p().items.length > 0} else={<View class="py-3"><Text class={T.sec}>{p().note ?? "Nothing to choose from."}</Text></View>}>
          <If when={filtered().length > 0} else={<View class="py-3"><Text class={T.sec}>Nothing matches “{query()}”.</Text></View>}>
            <Scroll style={{ maxHeight: Math.min(340, modalBodyHeight(40 + (chosen()?.detail?.length ? 120 : 0))) }}>
              <View class="gap-0.5">
              <Each of={filtered()} key={(x) => x.id}>
                {(it, i) => (
                  <Button onPress={() => choose(it())} selected={idx() === clamp(i())} accessibilityLabel={it().label} testID={`pick-${i()}`}
                    class={`group/row relative rounded-none px-3 min-h-8 justify-center hover:bg-accent ${idx() === clamp(i()) ? "bg-selection" : ""} ${FOCUS}`}>
                    <View class="flex-row items-center gap-3">
                      <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.base} ${p().title === "Change agent" ? agentColor(it().id) : "text-popover-foreground"}`}>{favorites().includes(it().id) ? `★ ${it().label}` : it().label}</Text>
                      <If when={!!it().hint}><Text style={{ numberOfLines: 1 }} class={`${TS.base} text-muted-foreground tabular-nums`}>{it().hint}</Text></If>
                    </View>
                  </Button>
                )}
              </Each>
              </View>
            </Scroll>
          </If>
        </If>
        <If when={!!chosen()?.detail?.length}>
          <View class="gap-0.5 border-t border-border px-3 pt-2">
            <Text style={{ numberOfLines: 1 }} class={`${TS.mono} text-popover-foreground`}>{chosen()!.id}</Text>
            <Each of={chosen()!.detail!} key={(line) => line}>
              {(line) => <Text style={{ numberOfLines: 2 }} class={`${TS.base} text-muted-foreground tabular-nums`}>{line()}</Text>}
            </Each>
          </View>
        </If>
        <ModalFooter hints={isModels() ? "↑↓ move · tab favorite · enter choose · esc close" : "↑↓ move · enter choose · esc close"} right={filtered().length ? `${clamp(idx()) + 1} of ${filtered().length}` : undefined} />
      </View>
    </Modal>
  );
}

// --- /learn checklist ---------------------------------------------------------------------------
export function SkillChecklist() {
  return <If when={learnChecklist() !== null}>{() => <SkillChecklistBody />}</If>;
}
function SkillChecklistBody() {
  const plan = () => learnChecklist()!;
  const fetchId = plan().fetch_id;
  const selected = signal(defaultSkillSelection(plan().found));
  const active = signal(0);
  const names = () => plan().found.map((skill) => skill.name);
  const clamp = (n: number) => Math.max(0, Math.min(Math.max(0, names().length - 1), n));
  const toggle = (name = names()[active()]) => {
    if (!name) return;
    selected.update((current) => current.includes(name) ? current.filter((entry) => entry !== name) : [...current, name]);
  };
  const currentCounts = () => selectedSkillCounts(plan().found, selected());
  keymap.bind("down", () => { if (learnChecklist()?.fetch_id === fetchId) active.set(clamp(active() + 1)); }, { when: "always" });
  keymap.bind("up", () => { if (learnChecklist()?.fetch_id === fetchId) active.set(clamp(active() - 1)); }, { when: "always" });
  keymap.bind("space", () => { if (learnChecklist()?.fetch_id === fetchId) toggle(); }, { when: "always" });
  keymap.bind("enter", () => { if (learnChecklist()?.fetch_id === fetchId && !plan().busy) installLearnChecklist(selected()); }, { when: "always" });
  const cancel = () => { if (!plan().busy) closeLearnChecklist(); };
  return (
    <Modal size="large" label="Choose skills to install" testID="learn-checklist" onCancel={cancel} onOpenChange={(open) => { if (!open) cancel(); }}>
      <View class="gap-3" testID="learn-checklist-body">
        <ModalTitle title="Choose skills to install" />
        <Text class={T.meta}>{plan().source} · {plan().scope === "global" ? "every project" : "this project"}</Text>
        <View class="flex-row flex-wrap gap-1.5">
          <Btn label="Select all" onPress={() => selected.set(names())} testID="learn-select-all" />
          <Btn label="Select none" onPress={() => selected.set([])} testID="learn-select-none" />
          <Btn label="Invert" onPress={() => selected.set(names().filter((name) => !selected().includes(name)))} testID="learn-invert" />
        </View>
        <If when={plan().found.length > 0} else={<View class="py-3"><Text class={T.sec}>No skills were found in this source.</Text></View>}>
          <Scroll style={{ maxHeight: Math.min(320, modalBodyHeight(160)) }} testID="learn-skills">
            <View class="gap-0.5">
            <Each of={plan().found} key={(skill) => skill.name}>
              {(skill, i) => {
                const checked = () => selected().includes(skill().name);
                return (
                  <Button onPress={() => { active.set(i()); toggle(skill().name); }} selected={active() === i()} accessibilityLabel={`${skill().name}, ${checked() ? "selected" : "not selected"}${skill().installed ? ", installed, will be replaced" : ""}`} testID={`learn-skill-${skill().name}`}
                    class={`group/row relative rounded-none px-3 py-1.5 min-h-9 hover:bg-accent ${active() === i() ? "bg-selection" : ""} ${FOCUS}`}>
                    <View class="flex-row items-start gap-2">
                      <Text class={`w-4 text-center ${TS.base} ${checked() ? "text-primary" : "text-muted-foreground"}`}>{checked() ? "☑" : "☐"}</Text>
                      <View class="flex-1 min-w-0 gap-0.5">
                        <View class="flex-row items-center gap-2">
                          <Text class={`${TS.base} font-medium text-popover-foreground`}>{skill().name}</Text>
                          <If when={!!skill().installed}><Text class={`${TS.base} text-warning rounded-none px-1.5`}>installed</Text></If>
                        </View>
                        <If when={!!skill().description}><Text style={{ numberOfLines: 2 }} class={T.meta}>{skill().description}</Text></If>
                        <If when={!!skill().warnings?.length}><Text style={{ numberOfLines: 1 }} class={`${TS.base} text-warning`}>{skill().warnings!.join(" · ")}</Text></If>
                      </View>
                    </View>
                  </Button>
                );
              }}
            </Each>
            </View>
          </Scroll>
        </If>
        <If when={plan().problems.length > 0}><Text style={{ numberOfLines: 3 }} class={T.meta}>{plan().problems.map((problem: any) => typeof problem === "string" ? problem : problem.message ?? JSON.stringify(problem)).join(" · ")}</Text></If>
        <View class="flex-row justify-end gap-2">
          <Btn label="Cancel" onPress={cancel} disabled={!!plan().busy} testID="learn-cancel" />
          <Btn label="Install" kind="primary" busy={!!plan().busy} onPress={() => installLearnChecklist(selected())} testID="learn-install" />
        </View>
        <ModalFooter hints="↑↓ move · space toggle · enter install · esc cancel" right={`${currentCounts().newCount} new, ${currentCounts().replacedCount} will be replaced`} />
      </View>
    </Modal>
  );
}

// --- confirm -----------------------------------------------------------------------------------
export function Confirm() {
  return (
    <If when={confirmDialog()}>
      {(d) => (
        <Modal size="small" label={d().title} onCancel={() => escape()} closeOnBackdropPress={false}>
          <ConfirmKeys />
          <View testID="confirm-dialog" role="alert" accessibilityLabel={d().title} class="gap-3">
            <ModalTitle title={d().title} />
            <View class="gap-2">
              <Text class={T.sec}>{d().body}</Text>
              <If when={d().error}><Text class={`${TS.base} text-error`}>{"✕ " + d().error}</Text></If>
            </View>
            <View class="flex-row justify-end gap-2">
              <Btn label="Cancel" initial onPress={() => run("alert.cancel")} disabled={!!d().busy} testID="confirm-cancel" />
              <Btn label={d().confirm} kind={d().destructive ? "danger" : "primary"} testID="confirm-ok" busy={!!d().busy}
                onPress={() => void runConfirm()} />
            </View>
            <ModalFooter hints={d().destructive ? "enter cancel · tab to the red button to confirm · esc cancel" : "enter confirm · esc cancel"} />
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
      <Modal size="small" label="Incompatible Core" onCancel={() => escape()} closeOnBackdropPress={false}>
        <View testID="blocking-dialog" role="alert" accessibilityLabel="Incompatible Core" class="gap-3">
          <View class="flex-row items-center min-h-8 gap-2"><Text class={`text-error ${TS.title}`}>✕</Text><Text class={`${TS.title} font-semibold text-popover-foreground`}>Incompatible Nexus Core</Text></View>
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
    <Modal size="small" label="Add project" onCancel={() => escape()} onOpenChange={(open) => { if (!open) addProjectOpen.set(false); }}>
      <View testID="add-project-dialog" class="gap-3" accessibilityLabel="Add project">
        <ModalTitle title="Add project" />
        <Text class={T.sec}>Absolute path of a directory on the machine running Core. Core creates the project if it is new.</Text>
        <Input autoFocus placeholder="/path/to/repo" accessibilityLabel="Project path" testID="project-path" onChange={({ value: t }) => path.set(t)} onSubmit={() => void submit()} submitBehavior="submit" class={`h-10 rounded-none px-3 ${TS.mono} text-foreground ${err() ? "bg-error/10" : "bg-background"}`} />
        <If when={err()}><Text class={`${TS.base} text-error`}>{"✕ " + err()}</Text></If>
        <View class="flex-row justify-end gap-2">
          <Btn label="Cancel" onPress={() => addProjectOpen.set(false)} />
          <Btn label="Add project" kind="primary" busy={busy()} onPress={() => void submit()} testID="project-add-ok" />
        </View>
        <ModalFooter hints="enter add · esc cancel" />
      </View>
    </Modal>
  );
}

// --- drawers (narrow layout) -------------------------------------------------------------------------
/** Narrow layouts: a sidebar is a drawer over the centre column. It starts below the header, so both toggles (the
 * navigation anchors) stay visible and usable; Esc closes it through the manifest's escape chain (rule `drawer`).
 * Built from plain views because NSH's `Sheet` breaks Escape routing for dialogs opened after it (README "Gaps"),
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
    <View class="h-full w-full bg-sidebar" testID="notifications" accessibilityLabel="Notifications">
      <View class="flex-row items-center gap-2 border-b border-border px-4 h-11">
        <Text class={`flex-1 ${TS.title} font-semibold text-foreground`}>Notifications</Text>
        <Btn label="Mark all read" kind="ghost" onPress={() => run("notifications.mark_all_read")} disabled={!availability(commands.find((c) => c.id === "notifications.mark_all_read")!).ok} />
        <IconButton icon="x" label="Close" onPress={() => drawer.set(null)} />
      </View>
      <If when={notifications().length > 0} else={<Empty title="No notifications" body="Toasts and alerts are kept here, newest first." />}>
        <List each={notifications()} keyExtractor={(n) => String(n.id)} class="flex-1" contentStyle={{ padding: 8, gap: 4 }}>
          {(n) => (
            <Button onPress={() => alertDetail.set(n())} accessibilityLabel={`${n().level}: ${n().text}`} testID="notification-open" class={`flex-row gap-2 rounded-none px-2.5 py-2 hover:bg-accent ${FOCUS}`}>
              <Text class={`w-3.5 text-center ${TS.base} text-foreground-secondary`}>{GL[n().level]}</Text>
              <Text style={{ numberOfLines: 3 }} class={`flex-1 min-w-0 ${TS.base} text-foreground`}>{n().text}</Text>
            </Button>
          )}
        </List>
      </If>
    </View>
  );
}
void compat; void currentProject; void unread;

export function RenameSession() {
  return <If when={renaming()}><RenameSessionBody /></If>;
}
function RenameSessionBody() {
  const initial = viewed()?.title || "";
  const text = signal(initial);
  const cancel = () => renaming.set(false);
  const save = () => {
    const title = text().trim();
    if (!title) return;
    cancel();
    if (title !== initial) void renameSession(title);
  };
  return (
    <Modal size="small" label="Rename session" testID="rename-dialog" onCancel={cancel} onOpenChange={(open) => { if (!open) cancel(); }}>
      <View class="gap-3">
        <ModalTitle title="Rename session" />
        <Input autoFocus defaultValue={initial} onChange={({ value }) => text.set(value)} onSubmit={save} submitBehavior="submit"
          accessibilityLabel="Session title" testID="rename-input" class={`rounded-none bg-card px-3 h-10 ${TS.base} text-foreground`} />
        <View class="flex-row justify-end gap-2">
          <Btn label="Cancel" onPress={cancel} />
          <Btn label="Save" kind="primary" onPress={save} disabled={!text().trim()} testID="rename-save" />
        </View>
        <ModalFooter hints="enter save · esc cancel" />
      </View>
    </Modal>
  );
}
