import { Scroll, Text, View } from "nsh";
import { Input } from "nsh/base";
import { Btn, T } from "./kit";
import { availability, byId, keyLabel, run } from "../commands";
import { fileDirty, openFile, setDraft } from "../store";

const hint = (id: string) => { const c = byId.get(id); return c ? keyLabel(c) : null; };

/** The open project file: read-only text, or the built-in editor once `draft` is set (K10). */
export function FileView() {
  const f = () => openFile();
  const editing = () => f()?.draft !== undefined;
  const status = () => (f()?.saving ? "saving…" : f()?.stale ? "changed on disk · reload" : fileDirty() ? "● modified" : editing() ? "editing" : "");
  const can = (id: string) => availability(byId.get(id)!).ok;
  return (
    <View class="flex-1 min-h-0" testID="file-view">
      <View class="flex-row items-center gap-2 px-3 min-h-10 border-b border-border">
        <Btn label="Back" kind="ghost" onPress={() => void run("editor.close")} testID="file-back" />
        <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class="flex-1 min-w-0 font-mono text-[13px] leading-5 text-foreground" testID="file-path">{f()?.path ?? ""}</Text>
        <If when={!!status()}><Text class={`${T.meta} ${f()?.stale ? "text-warning" : ""}`} testID="file-status">{status()}</Text></If>
        <If when={!editing()}>
          <Btn label="Edit" onPress={() => void run("editor.edit")} disabled={!can("editor.edit")} reason={availability(byId.get("editor.edit")!).reason} testID="file-edit" />
        </If>
        <If when={editing()}>
          <Btn label="Reload" onPress={() => void run("editor.reload")} testID="file-reload" />
          <Btn label={hint("editor.save") ? `Save ${hint("editor.save")}` : "Save"} kind="primary" onPress={() => void run("editor.save")} disabled={!can("editor.save")} reason={availability(byId.get("editor.save")!).reason} busy={!!f()?.saving} testID="file-save" />
        </If>
      </View>
      <If when={editing()} else={
        <Scroll class="flex-1"><Text style={{ userSelect: "text" }} class="font-mono text-xs text-foreground px-3 py-2">{f()?.error ?? f()?.content ?? "Loading file…"}</Text></Scroll>
      }>
        <Input multiline value={f()?.draft ?? ""} onChange={({ value }) => setDraft(value)} testID="file-editor" accessibilityLabel={`Editing ${f()?.path ?? "file"}`}
          class="flex-1 min-h-0 bg-background px-3 py-2 font-mono text-xs text-foreground" />
      </If>
    </View>
  );
}
