import { Scroll, Text, View, signal, effect } from "nsh";
import { Button, DiffView as CodeDiffView, Editor, Input, TextView, type EditorRef } from "nsh/base";
import { Btn, FOCUS, IconButton, PROSE, T } from "./kit";
import { availability, byId, keyLabel } from "../commands";
import { bufferDirty, currentFileId, fileBuffers, fileFindOpen, fileSourceOf, gitDiffs, isMarkdownPath, reloadFile, saveFile, setDraft, setFileSource } from "../store";
import { CodeFence, MermaidFence } from "./timeline";
import { closedFenceSources } from "../mermaid";
import { focusedGroupId } from "../layout-state";

const hint = (id: string) => { const c = byId.get(id); return c ? keyLabel(c) : null; };
/** The built-in code editor's font. It measures its text in this family, so set it here. */
const FONT = "Monaspace Argon";
/** One file tab's body (plan 45): the editor over the file's shared buffer, with the Markdown / Mermaid preview toggle. */
export function FilePane(p: { id: string; group: string }) {
  const f = () => fileBuffers()[p.id];
  // The same file can be open in two groups: only the focused group's pane is the one the file commands act on.
  const current = () => currentFileId() === p.id && focusedGroupId() === p.group;
  const editing = () => f()?.draft !== undefined;
  const diff = signal(false);
  const query = signal("");
  const replacement = signal("");
  const caseSensitive = signal(false);
  const found = signal<{ count: number; current?: number | null; error?: string | null }>({ count: 0 });
  let editor!: EditorRef;
  const showDiff = () => diff() && editing();
  const markdown = () => isMarkdownPath(f()?.path);
  const preview = () => markdown() && !fileSourceOf(p.id);
  const findOpen = () => current() && fileFindOpen();
  effect(() => { if (current() && !fileFindOpen() && !preview() && !showDiff()) editor?.focus(); });
  const status = () => (f()?.saving ? "saving…" : f()?.stale ? "changed on disk · reload" : bufferDirty(p.id) ? "● modified" : editing() ? "saved" : "");
  const can = (id: string) => availability(byId.get(id)!).ok;
  const loaded = () => editing() || f()?.content !== undefined;
  const matchCount = () => {
    const r = found();
    if (!query()) return "";
    if (r.error) return r.error;
    if (r.current == null) return `${r.count} ${r.count === 1 ? "match" : "matches"}`;
    return `${r.current + 1} of ${r.count}`;
  };
  return (
    <View class="flex-1 min-h-0 flex-col" testID="file-panel">
      <View class="flex-row items-center gap-2 px-3 min-h-9 border-b border-border bg-sidebar">
        <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`flex-1 min-w-0 ${T.mono} text-foreground`} testID="file-path">{f()?.path ?? ""}</Text>
        <If when={!!status()}><Text class={`${T.meta} ${f()?.stale ? "text-warning" : ""}`} testID="file-status">{status()}</Text></If>
        <If when={markdown()}>
          <Btn label={fileSourceOf(p.id) ? "Preview" : "Source"} kind="ghost" onPress={() => { setFileSource(p.id, !fileSourceOf(p.id)); if (current()) fileFindOpen.set(false); }} testID="file-preview" />
        </If>
        <If when={editing()}>
          <Btn label={diff() ? "Text" : "Diff"} kind="ghost" onPress={() => diff.set(!diff())} testID="file-diff" />
          <Btn label="Reload" kind="ghost" onPress={() => void reloadFile(p.id)} testID="file-reload" />
          <Btn label={hint("editor.save") ? `Save ${hint("editor.save")}` : "Save"} kind="primary" onPress={() => void saveFile(p.id)} disabled={!bufferDirty(p.id) || !!f()?.saving} reason={availability(byId.get("editor.save")!).reason} busy={!!f()?.saving} testID="file-save" />
        </If>
      </View>
      <If when={findOpen() && !preview()}>
        <View class="px-3 py-1 gap-1 border-b border-border bg-sidebar" testID="file-find">
        <View class="flex-row items-center gap-2 min-h-8">
          <Input autoFocus value={query()} onChange={({ value }) => query.set(value)} onSubmit={() => editor?.run("findNext")}
            placeholder="Find" accessibilityLabel="Find in file" testID="file-find-input"
            class="h-7 flex-1 min-w-0 rounded-(--radius) bg-background px-2 font-(family-name:--font-ui) text-[13px] text-foreground" />
          <Text class={`${T.meta} w-20 text-right`} testID="file-find-count">{matchCount()}</Text>
          <View class="flex-row rounded-(--radius) bg-muted p-0.5">
            <Button onPress={() => caseSensitive.set(!caseSensitive())} selected={caseSensitive()} accessibilityLabel="Match case" testID="file-find-case"
              class={`rounded-(--radius) px-2 min-h-6 items-center justify-center hover:bg-accent ${caseSensitive() ? "bg-selection" : ""} ${FOCUS}`}>
              <Text class={`text-[13px] leading-5 ${caseSensitive() ? "font-medium text-foreground" : "text-foreground-secondary"}`}>Aa</Text>
            </Button>
          </View>
          <IconButton icon="chevron-up" label="Previous match" onPress={() => editor?.run("findPrevious")} testID="file-find-prev" />
          <IconButton icon="chevron-down" label="Next match" onPress={() => editor?.run("findNext")} testID="file-find-next" />
          <IconButton icon="x" label="Close find" onPress={() => fileFindOpen.set(false)} testID="file-find-close" />
        </View>
        <View class="flex-row items-center gap-2 min-h-8">
          <Input value={replacement()} onChange={({ value }) => replacement.set(value)} placeholder="Replace" accessibilityLabel="Replace in file" testID="file-replace-input"
            class="h-7 flex-1 min-w-0 rounded-(--radius) bg-background px-2 font-(family-name:--font-ui) text-[13px] text-foreground" />
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
                <Editor ref={(r) => { editor = r; if (current() && !fileFindOpen()) r.focus(); }} value={f()?.draft ?? f()?.content ?? ""} onChange={({ value }) => setDraft(value, p.id)} readOnly={!editing()} softWrap
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
  );
}

/** A Git change's diff tab. */
export function DiffPane(p: { id: string }) {
  const d = () => gitDiffs()[p.id];
  return (
    <View class="flex-1 min-h-0 flex-col" testID="diff-panel">
      <View class="flex-row items-center gap-2 px-3 min-h-9 border-b border-border bg-sidebar">
        <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`flex-1 min-w-0 ${T.mono} text-foreground`} testID="diff-path">{`${d()?.group ?? ""}: ${d()?.path ?? ""}`}</Text>
        <If when={!!d()?.clipped}><Text class={T.meta}>Diff clipped</Text></If>
      </View>
      <Scroll class="flex-1"><Text style={{ userSelect: "text" }} class={`${T.mono} text-foreground px-4 py-3`} testID="diff-text">{d()?.error ?? d()?.text ?? "Loading diff…"}</Text></Scroll>
    </View>
  );
}
