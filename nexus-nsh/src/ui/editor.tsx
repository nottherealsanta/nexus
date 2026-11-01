import { PanResponder, Scroll, Text, View, createMotion, platform, safeArea, signal } from "nsh";
import { titleBar } from "nsh/desktop";
import { Button, DiffView as CodeDiffView, Editor, Input, type EditorRef } from "nsh/base";
import { Btn, FOCUS, IconButton, T } from "./kit";
import { availability, byId, keyLabel, run, waitItems } from "../commands";
import { fileDirty, fileFindOpen, isNarrow, leftOpen, openFile, panelWide, setDraft, width } from "../store";
import { leftSidebarWidth } from "./sidebar";

const hint = (id: string) => { const c = byId.get(id); return c ? keyLabel(c) : null; };
/** The built-in code editor's font. It measures its text in this family, so set it here. */
const FONT = "Monaspace Argon";
/** The header is 44 pt in the wide layout; the panel starts under it. */
const HEADER = 44;
/** The collapsed left sidebar's rail. */
const RAIL = 48;
/** The width the user dragged the panel to; null means the default share of the space. */
const widthOverride = signal<number | null>(null);

function availableWidth(): number {
  const left = isNarrow() ? 0 : leftOpen() ? leftSidebarWidth() : RAIL;
  return width() - left;
}

/** The panel is at most 55% of the space right of the left sidebar and 760 px, so the timeline keeps 480. */
export const filePanelWidth = () => {
  const space = availableWidth();
  const max = Math.max(360, Math.min(Math.floor(space * 0.55), 760, space - 480));
  return Math.max(360, Math.min(max, widthOverride() ?? max));
};

/** The open project file, in the right-hand panel: read-only, or the code editor once `draft` is set (K10). */
export function FilePanel() {
  const f = () => openFile();
  const editing = () => f()?.draft !== undefined;
  const diff = signal(false);
  const query = signal("");
  const caseSensitive = signal(false);
  const found = signal<{ count: number; current?: number | null; error?: string | null }>({ count: 0 });
  let editor!: EditorRef;
  let startWidth = 0;
  const resize = PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onPanResponderGrant: () => { startWidth = filePanelWidth(); },
    onPanResponderMove: (_event, gesture) => { widthOverride.set(Math.max(360, startWidth - gesture.dx)); },
  });
  const showDiff = () => diff() && editing();
  const status = () => (f()?.saving ? "saving…" : f()?.stale ? "changed on disk · reload" : fileDirty() ? "● modified" : editing() ? "editing" : "");
  const can = (id: string) => availability(byId.get(id)!).ok;
  const loaded = () => editing() || f()?.content !== undefined;
  const matchCount = () => {
    const r = found();
    if (!query()) return "–";
    if (r.error) return r.error;
    if (r.current == null) return `${r.count} ${r.count === 1 ? "match" : "matches"}`;
    return `${r.current + 1} of ${r.count}`;
  };
  // Wide, the panel is a column beside the timeline and appears at once.
  // Narrow, it slides in from the right over the whole body, the composer included.
  const slide = createMotion({ enter: { duration: 220, easing: "easeOut", keyframes: { translateX: [1200, 0] } } });
  const style = () => panelWide()
    ? { position: "absolute" as const, top: HEADER + safeArea().top, right: 0, bottom: 0, width: filePanelWidth() }
    : { position: "absolute" as const, top: 0, right: 0, bottom: 0, left: 0, ...slide.style };
  // Narrow, the panel covers the whole window, traffic lights included: its header makes room
  // for them and drags the window, as the app's own band does.
  const lights = () => (!panelWide() && platform.os === "macos" ? titleBar().left : 0);
  const headerStyle = () => (lights() ? { paddingLeft: lights() + 8 } : {});
  return (
    <View class="absolute flex-col bg-background border-l border-border" style={style()} testID="file-panel">
      <If when={panelWide()}>
        <View {...resize.panHandlers} class="absolute left-0 top-0 bottom-0 z-10 w-1" testID="file-panel-resize" accessibilityLabel="Resize file panel" />
      </If>
      <View class="flex-row items-center gap-2 px-3 min-h-11 border-b border-border bg-sidebar" style={headerStyle()}
        {...(lights() ? { desktop: { windowDrag: true } } : {})}>
        <IconButton icon="arrow-left" label="Back to the session" onPress={() => void run("editor.close")} testID="file-back" />
        <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`flex-1 min-w-0 ${T.mono} text-foreground`} testID="file-path">{f()?.path ?? ""}</Text>
        <If when={!panelWide() && waitItems().length > 0}>
          <Text class="text-[12px] leading-4 font-medium text-warning" testID="file-panel-waiting">{`${waitItems().length} waiting`}</Text>
        </If>
        <If when={!!status()}><Text class={`${T.meta} ${f()?.stale ? "text-warning" : ""}`} testID="file-status">{status()}</Text></If>
        <If when={!editing()}>
          <Btn label="Edit" kind="ghost" onPress={() => void run("editor.edit")} disabled={!can("editor.edit")} reason={availability(byId.get("editor.edit")!).reason} testID="file-edit" />
        </If>
        <If when={editing()}>
          <Btn label={diff() ? "Text" : "Diff"} kind="ghost" onPress={() => diff.set(!diff())} testID="file-diff" />
          <Btn label="Reload" kind="ghost" onPress={() => void run("editor.reload")} testID="file-reload" />
          <Btn label={hint("editor.save") ? `Save ${hint("editor.save")}` : "Save"} kind="primary" onPress={() => void run("editor.save")} disabled={!can("editor.save")} reason={availability(byId.get("editor.save")!).reason} busy={!!f()?.saving} testID="file-save" />
        </If>
      </View>
      <If when={fileFindOpen()}>
        <View class="flex-row items-center gap-2 px-3 min-h-9 border-b border-border bg-sidebar" testID="file-find">
          <Input autoFocus value={query()} onChange={({ value }) => query.set(value)} onSubmit={() => editor?.run("findNext")}
            placeholder="Find" accessibilityLabel="Find in file" testID="file-find-input"
            class="h-7 flex-1 min-w-0 rounded-(--radius) bg-background px-2 font-mono text-xs text-foreground" />
          <Text class={`${T.meta} w-20 text-right`} testID="file-find-count">{matchCount()}</Text>
          <View class="flex-row rounded-(--radius) bg-muted p-0.5">
            <Button onPress={() => caseSensitive.set(!caseSensitive())} selected={caseSensitive()} accessibilityLabel="Match case" testID="file-find-case"
              class={`rounded-[8px] px-2 min-h-6 items-center justify-center hover:bg-accent ${caseSensitive() ? "bg-selection" : ""} ${FOCUS}`}>
              <Text class={`text-[13px] leading-5 ${caseSensitive() ? "font-medium text-foreground" : "text-foreground-secondary"}`}>Aa</Text>
            </Button>
          </View>
          <IconButton icon="chevron-up" label="Previous match" onPress={() => editor?.run("findPrevious")} testID="file-find-prev" />
          <IconButton icon="chevron-down" label="Next match" onPress={() => editor?.run("findNext")} testID="file-find-next" />
          <IconButton icon="x" label="Close find" onPress={() => fileFindOpen.set(false)} testID="file-find-close" />
        </View>
      </If>
      <View class="flex-1 min-h-0" testID="file-view">
        <If when={!!f()?.error} else={
          <If when={!loaded()} else={
            <If when={showDiff()} else={
              <Editor ref={(r) => (editor = r)} value={f()?.draft ?? f()?.content ?? ""} onChange={({ value }) => setDraft(value)} readOnly={!editing()} softWrap
                search={{ query: query(), caseSensitive: caseSensitive() }} onSearchChange={({ result }) => found.set(result)}
                language={f()?.path} fontFamily={FONT} accessibilityLabel={`Editing ${f()?.path ?? "file"}`}
                class="flex-1 min-h-0 bg-background" testID="file-editor" />
            }>
              <CodeDiffView oldValue={f()?.content ?? ""} newValue={f()?.draft ?? ""} language={f()?.path} fontFamily={FONT}
                class="flex-1 min-h-0 bg-background" testID="file-diff-view" />
            </If>
          }>
            <Scroll class="flex-1"><Text style={{ userSelect: "text" }} class="font-mono text-xs text-muted-foreground px-3 py-2">Loading file…</Text></Scroll>
          </If>
        }>
          <Scroll class="flex-1"><Text style={{ userSelect: "text" }} class="font-mono text-xs text-error px-3 py-2">{f()?.error}</Text></Scroll>
        </If>
      </View>
    </View>
  );
}
