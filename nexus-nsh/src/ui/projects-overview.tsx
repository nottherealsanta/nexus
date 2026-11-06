// PLAN 15.4: the projects overview, drawn from the same rows as the TUI's (`App::overview_rows`).
import { Scroll, Text, View, keymap } from "nsh";
import { Button } from "nsh/base";
import { Modal, modalBodyHeight, ModalFooter, ModalTitle } from "./modal";
import { FOCUS, FocusEdge, T, TS } from "./kit";
import { attention } from "./sidebar";
import { flowEnd, setProjectStatus } from "../commands";
import { m, model, projectsOverview, selectProject, selectSession } from "../store";
import { PROJECT_STATUS, PROJECT_STATUS_LABEL, overviewRows, projectStatus, sessionStateWord } from "../project-rail";
import type { OverviewRow } from "../project-rail";
import { escape } from "../commands";

const STATE_TONE: Record<string, string> = {
  "running": "text-info", "needs input": "text-multi", "failed": "text-error", "finished": "text-success",
  "archived": "text-muted-foreground",
};

export function ProjectsOverviewHost() {
  return <If when={projectsOverview() !== null}>{() => <ProjectsOverviewBody />}</If>;
}

function ProjectsOverviewBody() {
  const rows = (): OverviewRow[] => { m(); return overviewRows(model.projects.values(), model.sessions.values(), new Set(projectsOverview()?.archived ?? [])); };
  const cursor = () => Math.min(projectsOverview()?.cursor ?? 0, Math.max(0, rows().length - 1));
  const set = (i: number) => projectsOverview.update((o) => (o ? { ...o, cursor: i } : o));
  const projectOf = (row: OverviewRow | undefined): string | null => {
    if (!row) return null;
    if (row.kind === "project") return row.id;
    return row.kind === "group" ? null : row.project;
  };
  const move = (delta: number) => {
    const all = rows();
    let next = cursor() + delta;
    while (next >= 0 && next < all.length && all[next].kind === "group") next += Math.sign(delta);
    if (next >= 0 && next < all.length) set(next);
  };
  const toggle = (project: string | null) => {
    if (!project) return;
    projectsOverview.update((o) => (o ? { ...o, archived: o.archived.includes(project) ? o.archived.filter((x) => x !== project) : [...o.archived, project] } : o));
  };
  const status = (value: "current" | "passive" | "inactive") => { const id = projectOf(rows()[cursor()]); if (id) void setProjectStatus(id, value); };
  const activate = (row: OverviewRow | undefined = rows()[cursor()]) => {
    if (!row) return;
    if (row.kind === "session") { projectsOverview.set(null); void selectSession(row.id); flowEnd(); }
    else if (row.kind === "project" || row.kind === "empty") { projectsOverview.set(null); selectProject(projectOf(row)!); flowEnd(); }
    else if (row.kind === "toggle") toggle(row.project);
  };
  const bind = (key: string, fn: () => void) => { try { keymap.bind(key, fn, { when: "always" }); } catch { /* the platform keeps this keystroke */ } };
  bind("up", () => move(-1)); bind("down", () => move(1)); bind("k", () => move(-1)); bind("j", () => move(1));
  bind("home", () => set(Math.max(0, rows().findIndex((r) => r.kind !== "group"))));
  bind("end", () => { const all = rows(); for (let i = all.length - 1; i >= 0; i--) if (all[i].kind !== "group") { set(i); return; } });
  bind("enter", () => activate()); bind("space", () => activate());
  bind("a", () => toggle(projectOf(rows()[cursor()])));
  bind("1", () => status(PROJECT_STATUS.current)); bind("2", () => status(PROJECT_STATUS.passive)); bind("3", () => status(PROJECT_STATUS.inactive));
  bind("r", () => status(PROJECT_STATUS.current));
  bind("right", () => projectsOverview.set(null)); bind("l", () => projectsOverview.set(null));

  const project = (id: string) => model.projects.get(id);
  const stateOf = (id: string) => {
    const s = model.sessions.get(id);
    return s ? sessionStateWord(s, attention(id) === "waiting", model.running.has(id), model.unreadSessions.has(id), model.failedSessions.has(id)) : "idle";
  };
  const line = (row: OverviewRow) => {
    switch (row.kind) {
      case "group": return <Text class={`${TS.base} font-semibold text-muted-foreground`}>{row.title}</Text>;
      case "project": {
        const p = project(row.id);
        const st = p ? projectStatus(p) : PROJECT_STATUS.current;
        return (
          <View class="flex-row items-center">
            <Text class={`${TS.base} ${st === PROJECT_STATUS.passive ? "text-muted-foreground" : "font-semibold text-foreground"}`}>{p?.name ?? row.id}</Text>
            <Text class={`${TS.base} text-muted-foreground`}>{`\u00a0\u00a0· ${PROJECT_STATUS_LABEL[st]}`}</Text>
            <If when={st === PROJECT_STATUS.inactive}><Text class={`${TS.base} text-foreground-secondary`}>{"\u00a0\u00a0· r restore"}</Text></If>
          </View>
        );
      }
      case "empty": return <Text class={`${TS.base} text-muted-foreground`}>{"\u00a0\u00a0└\u00a0No active sessions"}</Text>;
      case "session": {
        const state = row.archived ? "archived" : stateOf(row.id);
        return (
          <View class="flex-row items-center">
            <Text class={`${TS.base} text-muted-foreground`}>{row.last ? "\u00a0\u00a0└\u00a0" : "\u00a0\u00a0├\u00a0"}</Text>
            <Text style={{ numberOfLines: 1 }} class={`${TS.base} ${row.archived ? "text-muted-foreground" : "font-semibold text-foreground"}`}>{model.sessions.get(row.id)?.title || row.id}</Text>
            <Text class={`${TS.base} ${STATE_TONE[state] ?? "text-foreground-secondary"}`}>{` · ${state}`}</Text>
          </View>
        );
      }
      case "toggle": return <Text class={`${TS.base} text-muted-foreground`}>{`\u00a0\u00a0${row.expanded ? "├" : "└"}\u00a0${row.expanded ? "▾" : "▸"}\u00a0${row.count} archived`}</Text>;
    }
  };
  return (
    <Modal size="large" label="Projects" onCancel={() => escape()} onOpenChange={(open) => { if (!open) projectsOverview.set(null); }}>
      <View testID="projects-overview" accessibilityLabel="Projects" role="menu" class="gap-2">
        <ModalTitle title="Projects" />
        <If when={rows().length > 0} else={<View class="py-3"><Text class={T.sec}>No projects yet.</Text></View>}>
          <Scroll style={{ maxHeight: modalBodyHeight(40) }}>
            <View class="gap-0.5">
              <Each of={rows()} key={(r) => r.kind === "group" ? "group" : r.kind === "project" ? `p:${r.id}` : r.kind === "empty" ? `e:${r.project}` : r.kind === "toggle" ? `t:${r.project}` : `s:${r.id}:${r.archived}`}>
                {(row, i) => (
                  <Button onPress={() => { set(i()); activate(row()); }} selected={cursor() === i()} disabled={row().kind === "group"} testID={`overview-row-${i()}`}
                    accessibilityLabel={`${row().kind} ${row().kind === "session" ? (model.sessions.get((row() as { id: string }).id)?.title ?? "") : ""}`}
                    class={`relative rounded-none px-3 min-h-8 justify-center hover:bg-accent ${cursor() === i() ? "bg-selection" : ""} ${FOCUS}`}>
                    <If when={cursor() === i()}><FocusEdge testID={`overview-cursor-${i()}`} /></If>
                    {line(row())}
                  </Button>
                )}
              </Each>
            </View>
          </Scroll>
        </If>
        <ModalFooter hints={["↑↓ select", "a archived", "1/2/3 current/passive/inactive", "r restore", "enter open", "esc back"]} />
      </View>
    </Modal>
  );
}
