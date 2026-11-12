import { Text, View, signal } from "nsh";
import { Input } from "nsh/base";
import { Btn, Icon, T, TS } from "../kit";
import { archivedFilter, currentProject, m, model, scopeAll } from "../../store";
import { orderedProjects } from "../../project-rail";
import { projectDot } from "../../mobile";
import { BottomSheet, SheetRow } from "./sheet";
import { archiveById, chooseProject, projectSheet, renameById, rowSheet } from "./state";
import { resumeArchived } from "../../commands";

const draft = signal("");

function RowSheet(p: { id: string; renaming: boolean }) {
  const s = () => { m(); return model.sessions.get(p.id); };
  const close = () => rowSheet.set(null);
  if (p.renaming) draft.set(s()?.title ?? "");
  const save = () => void renameById(p.id, draft());
  return (
    <BottomSheet label="Session actions" testID="row-sheet" onClose={close}>
      <Text style={{ numberOfLines: 1 }} class={`${T.title} px-2 pb-1`} testID="row-sheet-title">{s()?.title || "Untitled"}</Text>
      <If when={p.renaming} else={<>
        <SheetRow label="Rename" testID="row-rename" onPress={() => { draft.set(s()?.title ?? ""); rowSheet.set({ id: p.id, renaming: true }); }} leading={<Icon name="pencil" size={18} class="tint-foreground-secondary" />} />
        <If when={!s()?.archived} else={<SheetRow label="Unarchive" testID="row-unarchive" onPress={() => { close(); resumeArchived(p.id); }} leading={<Icon name="archive" size={18} class="tint-foreground-secondary" />} />}>
          <SheetRow label="Archive" testID="row-archive" onPress={() => void archiveById(p.id)} leading={<Icon name="archive" size={18} class="tint-foreground-secondary" />} />
        </If>
        <SheetRow label="Cancel" testID="row-cancel" onPress={close} />
      </>}>
        <Input autoFocus value={draft()} onChange={({ value }) => draft.set(value)} onSubmit={save} submitBehavior="submit"
          accessibilityLabel="Session title" testID="row-rename-input"
          class={`h-12 rounded-(--radius) bg-raised px-3 ${TS.body} text-foreground`} />
        <View class="flex-row gap-2 pt-2">
          <Btn label="Cancel" onPress={close} class="flex-1 min-h-12" testID="row-rename-cancel" />
          <Btn label="Save" kind="primary" onPress={save} disabled={!draft().trim()} class="flex-1 min-h-12" testID="row-rename-save" />
        </View>
      </If>
    </BottomSheet>
  );
}

function ProjectSheet() {
  const close = () => projectSheet.set(false);
  const projects = () => { m(); return orderedProjects(model.projects.values()); };
  const mark = (on: boolean) => <View class="w-5 items-center">{on ? <Icon name="check" size={16} class="tint-primary" /> : null}</View>;
  return (
    <BottomSheet label="Switch project" testID="project-sheet" onClose={close}>
      <Text class={`${T.section} px-2 pb-1`}>Project</Text>
      <SheetRow label="All projects" testID="project-all" selected={scopeAll()} onPress={() => chooseProject(null)} leading={mark(scopeAll())} />
      <Each of={projects()} key={(x) => x.id}>
        {(x) => <SheetRow label={x().name} testID={`project-${x().id}`} selected={!scopeAll() && currentProject() === x().id} onPress={() => chooseProject(x().id)}
          leading={<View class="w-5 items-center">{!scopeAll() && currentProject() === x().id ? <Icon name="check" size={16} class="tint-primary" /> : <View class={`size-2.5 rounded-full ${projectDot(x().id)}`} />}</View>} />}
      </Each>
      <View class="h-px bg-border my-1" />
      <SheetRow label="Show archived" testID="project-archived" detail={archivedFilter() ? "On" : "Off"} onPress={() => archivedFilter.update((v) => !v)} leading={<Icon name="archive" size={18} class="tint-foreground-secondary" />} />
      <Text class={`${T.meta} px-2 pt-1`}>The project you pick also receives new sessions.</Text>
    </BottomSheet>
  );
}

/** The phone shell's sheets: the row menu and the project switcher. */
export function PhoneSheets() {
  return <>
    <If when={rowSheet()}>{(r) => <RowSheet id={r().id} renaming={r().renaming} />}</If>
    <If when={projectSheet()}><ProjectSheet /></If>
  </>;
}
