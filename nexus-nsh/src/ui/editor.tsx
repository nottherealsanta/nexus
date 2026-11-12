import { Scroll, Text, View, platform, signal, effect } from "nsh";
import { titleBar } from "nsh/desktop";
import { Button, Dialog, DiffView as CodeDiffView, Editor, Input, TextView, type EditorRef } from "nsh/base";
import { Btn, FOCUS, IconButton, PROSE, T } from "./kit";
import { availability, byId, escape, keyLabel, run, waitItems } from "../commands";
import { config, fileDirty, fileFindOpen, fileSource, isNarrow, markdownFile, openFile, setDraft, width } from "../store";
import { SCRIM } from "./modal";
import { CodeFence, MermaidFence } from "./timeline";
import { closedFenceSources } from "../mermaid";

const hint = (id: string) => { const c = byId.get(id); return c ? keyLabel(c) : null; };
/** The built-in code editor's font. It measures its text in this family, so set it here. */
const FONT = "Monaspace Argon";
/** The modal is the window's full height, this wide at most, and keeps this margin either side. */
const MODAL_MAX = 1100;
const MODAL_MARGIN = 32;
/** One editable project-file modal; Markdown preview and diff retain the same draft. */
export function FilePanel() {
  const f = () => openFile();
  const editing = () => f()?.draft !== undefined;
  const diff = signal(false);
  const query = signal("");
  const replacement = signal("");
  const caseSensitive = signal(false);
  const found = signal<{ count: number; current?: number | null; error?: string | null }>({ count: 0 });
  let editor!: EditorRef;
  const showDiff = () => diff() && editing();
  const preview = () => markdownFile() && !fileSource();
  effect(() => { if (!fileFindOpen() && !preview() && !showDiff()) editor?.focus(); });
  const status = () => (f()?.saving ? "saving…" : f()?.stale ? "changed on disk · reload" : fileDirty() ? "● modified" : editing() ? "saved" : "");
  const can = (id: string) => availability(byId.get(id)!).ok;
  const loaded = () => editing() || f()?.content !== undefined;
  const matchCount = () => {
    const r = found();
    if (!query()) return "";
    if (r.error) return r.error;
    if (r.current == null) return `${r.count} ${r.count === 1 ? "match" : "matches"}`;
    return `${r.current + 1} of ${r.count}`;
  };
  // Below this the modal takes the whole window, traffic lights included: its header makes room
  // for them and drags the window, as the app's own band does.
  const full = () => isNarrow();
  const modalWidth = () => Math.min(MODAL_MAX, width() - 2 * MODAL_MARGIN);
  // Full height, the header shares the top band with the traffic lights when
  // they reach into it: it makes room for them and drags the window.
  const lights = () => {
    if (platform.os !== "macos") return 0;
    const left = full() ? 0 : (width() - modalWidth()) / 2;
    return Math.max(0, titleBar().left - left);
  };
  const headerStyle = () => (lights() ? { paddingLeft: lights() + 8 } : {});
  return (
    <Dialog open closeOnEscape={false} onCancel={() => escape()} onOpenChange={({ open }) => { if (!open) void run("editor.close"); }}
      animation={config()?.values?.ui?.reduced_motion ? "none" : "fade"} backdropClass={SCRIM} testID="file-panel-shell"
      style={full() ? {} : { width: modalWidth(), boxShadow: "0 12px 40px rgb(0 0 0 / 0.4)" }}
      class={full() ? "absolute inset-0 bg-background" : "absolute left-0 right-0 top-0 bottom-0 mx-auto border-x border-border bg-background overflow-hidden"}>
    <View class="flex-1 min-h-0 flex-col" testID="file-panel">
      <View class="flex-row items-center gap-2 px-3 min-h-11 border-b border-border bg-sidebar" style={headerStyle()}
        {...(lights() ? { desktop: { windowDrag: true } } : {})}>
        <IconButton icon="arrow-left" label="Back to the session" onPress={() => void run("editor.close")} testID="file-back" />
        <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`flex-1 min-w-0 ${T.mono} text-foreground`} testID="file-path">{f()?.path ?? ""}</Text>
        <If when={waitItems().length > 0}>
          <Text class="text-[12px] leading-4 font-medium text-warning" testID="file-panel-waiting">{`${waitItems().length} waiting`}</Text>
        </If>
        <If when={!!status()}><Text class={`${T.meta} ${f()?.stale ? "text-warning" : ""}`} testID="file-status">{status()}</Text></If>
        <If when={markdownFile()}>
          <Btn label={fileSource() ? "Preview" : "Source"} kind="ghost" onPress={() => void run("editor.preview")} testID="file-preview" />
        </If>
        <If when={editing()}>
          <Btn label={diff() ? "Text" : "Diff"} kind="ghost" onPress={() => diff.set(!diff())} testID="file-diff" />
          <Btn label="Reload" kind="ghost" onPress={() => void run("editor.reload")} testID="file-reload" />
          <Btn label={hint("editor.save") ? `Save ${hint("editor.save")}` : "Save"} kind="primary" onPress={() => void run("editor.save")} disabled={!can("editor.save")} reason={availability(byId.get("editor.save")!).reason} busy={!!f()?.saving} testID="file-save" />
        </If>
      </View>
      <If when={fileFindOpen() && !preview()}>
        <View class="px-3 py-1 gap-1 border-b border-border bg-sidebar" testID="file-find">
        <View class="flex-row items-center gap-2 min-h-8">
          <Input autoFocus value={query()} onChange={({ value }) => query.set(value)} onSubmit={() => editor?.run("findNext")}
            placeholder="Find" accessibilityLabel="Find in file" testID="file-find-input"
            class="h-7 flex-1 min-w-0 rounded-(--radius) bg-background px-2 font-mono text-[12.5px] text-foreground" />
          <Text class={`${T.meta} w-20 text-right`} testID="file-find-count">{matchCount()}</Text>
          <View class="flex-row rounded-(--radius) bg-muted p-0.5">
            <Button onPress={() => caseSensitive.set(!caseSensitive())} selected={caseSensitive()} accessibilityLabel="Match case" testID="file-find-case"
              class={`rounded-none px-2 min-h-6 items-center justify-center hover:bg-accent ${caseSensitive() ? "bg-selection" : ""} ${FOCUS}`}>
              <Text class={`text-[13px] leading-5 ${caseSensitive() ? "font-medium text-foreground" : "text-foreground-secondary"}`}>Aa</Text>
            </Button>
          </View>
          <IconButton icon="chevron-up" label="Previous match" onPress={() => editor?.run("findPrevious")} testID="file-find-prev" />
          <IconButton icon="chevron-down" label="Next match" onPress={() => editor?.run("findNext")} testID="file-find-next" />
          <IconButton icon="x" label="Close find" onPress={() => fileFindOpen.set(false)} testID="file-find-close" />
        </View>
        <View class="flex-row items-center gap-2 min-h-8">
          <Input value={replacement()} onChange={({ value }) => replacement.set(value)} placeholder="Replace" accessibilityLabel="Replace in file" testID="file-replace-input"
            class="h-7 flex-1 min-w-0 rounded-(--radius) bg-background px-2 font-mono text-[12.5px] text-foreground" />
          <Btn label="Replace" kind="ghost" disabled={!query() || !editing() || !found().count} onPress={() => editor?.replace(replacement())} testID="file-replace-next" />
          <Btn label="All" kind="ghost" disabled={!query() || !editing() || !found().count} onPress={() => void editor?.replaceAll(replacement())} testID="file-replace-all" />
        </View>
        </View>
      </If>
      <View class="flex-1 min-h-0 pr-3" testID="file-view">
        <If when={!!f()?.error} else={
          <If when={!loaded()} else={
            <If when={preview()} else={
              <If when={showDiff()} else={
                <Editor ref={(r) => { editor = r; if (!fileFindOpen()) r.focus(); }} value={f()?.draft ?? f()?.content ?? ""} onChange={({ value }) => setDraft(value)} readOnly={!editing()} softWrap
                  search={{ query: query(), caseSensitive: caseSensitive() }} onSearchChange={({ result }) => found.set(result)}
                  language={f()?.path} fontFamily={FONT} accessibilityLabel={`Editing ${f()?.path ?? "file"}`}
                  class="flex-1 min-h-0 bg-background" testID="file-editor" />
              }>
                <CodeDiffView oldValue={f()?.content ?? ""} newValue={f()?.draft ?? ""} language={f()?.path} fontFamily={FONT}
                  class="flex-1 min-h-0 bg-background" testID="file-diff-view" />
              </If>
            }>
              <Scroll class="flex-1"><View class="px-6 py-4" testID="file-markdown">
                <TextView selectable links="open" markdown={f()?.draft ?? f()?.content ?? ""} classes={PROSE}
                  renderCodeBlock={(block) => block.language === "mermaid" && closedFenceSources(f()?.draft ?? f()?.content ?? "").includes(block.code.trimEnd())
                    ? <MermaidFence block={block} />
                    : <CodeFence block={block} />} />
              </View></Scroll>
            </If>
          }>
            <Scroll class="flex-1"><Text class={`${T.sec} px-4 py-3`}>Loading file…</Text></Scroll>
          </If>
        }>
          <Scroll class="flex-1"><Text style={{ userSelect: "text" }} class="text-[13px] leading-5 text-error px-4 py-3">{f()?.error}</Text></Scroll>
        </If>
      </View>
    </View>
    </Dialog>
  );
}
