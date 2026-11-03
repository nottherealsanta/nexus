import { Scroll, Text, View, effect } from "nsh";
import { Button } from "nsh/base";
import { FOCUS, T, midTruncate } from "./kit";
import { currentProject, loadProjectMcp, m, model, projectMcp } from "../store";
import { configMeta, configShape, stateLabel } from "../project-mcp";
import type { ProjectMcpRow } from "../project-mcp";

/** The selected project's MCP files, their status and non-secret configuration facts. */
export function ProjectMcpPanel() {
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
  const state = () => { const id = currentProject(); return id ? projectMcp()[id] : undefined; };
  const data = () => state()?.data ?? null;
  const rows = () => (data()?.servers ?? []).filter((row) => row.server !== "(file)");
  const autoStart = () => rows().some((row) => row.enabled !== false && row.state !== "disabled" && row.state !== "invalid" && !!configShape(row));
  effect(() => { const id = currentProject(); if (id) void loadProjectMcp(id); });
  const refresh = () => { const id = currentProject(); if (id) void loadProjectMcp(id, true); };

  return (
    <View class="shrink-0 border-t border-border p-2 gap-1.5" testID="project-mcp" accessibilityLabel="Project MCP servers">
      <View class="flex-row items-center gap-2">
        <Text class="flex-1 text-[13px] leading-5 font-medium text-foreground">Project MCP</Text>
        <Button onPress={refresh} disabled={!!state()?.loading} testID="project-mcp-refresh" accessibilityLabel="Refresh"
          class={`flex-row items-center justify-center rounded-(--radius) px-2 min-h-6 hover:bg-accent disabled:opacity-50 ${FOCUS}`}>
          <Text class="text-[13px] leading-5 font-medium text-muted-foreground">Refresh</Text>
        </Button>
      </View>
      <If when={!!data()?.files?.length}>
        <Each of={data()!.files!} key={(file) => file.name}>
          {(file) => (
            <Text style={{ numberOfLines: 1 }} class={`${T.meta} font-mono`} testID={`project-mcp-file-${file().name.replaceAll("/", "-")}`}>
              {midTruncate(file().exists ? file().file : `${file().file} (not found)`, 46)}
            </Text>
          )}
        </Each>
      </If>
      <If when={!data()?.files?.length && !!project()}>
        <Text style={{ numberOfLines: 1 }} class={`${T.meta} font-mono`}>{midTruncate(data()?.file || project()!.path, 46)}</Text>
      </If>
      <If when={autoStart()}>
        <Text style={{ numberOfLines: 3 }} class="text-xs leading-4 text-warning" testID="project-mcp-autostart">
          Enabled servers start automatically with a new session; their commands run then, without asking.
        </Text>
      </If>
      <If when={!!state()?.error}><Text style={{ numberOfLines: 3 }} class="text-xs leading-4 text-error">{"✕ " + state()!.error}</Text></If>
      <If when={!!data()?.error}><Text style={{ numberOfLines: 3 }} class="text-xs leading-4 text-error">{data()!.error!}</Text></If>
      <If when={!!data()?.warnings?.length}><Text style={{ numberOfLines: 2 }} class="text-xs leading-4 text-warning">{data()!.warnings!.join(" · ")}</Text></If>
      <If when={!!state()?.loading && !data()}><Text class={T.sec}>Reading MCP configuration…</Text></If>
      <If when={!!data() && !data()!.exists}><Text class={T.sec}>No MCP configuration in this project.</Text></If>
      <If when={!!data()?.exists && rows().length === 0}><Text class={T.sec}>No servers in the project MCP files.</Text></If>
      <If when={rows().length > 0}>
        <Scroll class="max-h-[210px]">
          <View class="gap-1">
            <Each of={rows()} key={(row) => row.server}>
              {(entry) => <ProjectMcpServerRow row={entry} />}
            </Each>
          </View>
        </Scroll>
      </If>
    </View>
  );
}

function ProjectMcpServerRow(p: { row: () => ProjectMcpRow }) {
  const row = p.row;
  const shape = () => configShape(row());
  const meta = () => configMeta(row());
  return (
    <View class="gap-1 rounded-(--radius) px-1 py-1 hover:bg-accent" testID={`project-mcp-server-${row().server}`}>
      <View class="flex-row items-center gap-2">
        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground">{row().server}</Text>
        <Text class={T.meta} testID={`project-mcp-state-${row().server}`}>{stateLabel(row())}</Text>
      </View>
      <If when={!!shape()}>
        <Text style={{ numberOfLines: 1 }} class="font-mono text-xs leading-4 text-muted-foreground" testID={`project-mcp-shape-${row().server}`}>{shape()}</Text>
      </If>
      <If when={!!meta()}>
        <Text style={{ numberOfLines: 1 }} class="font-mono text-xs leading-4 text-muted-foreground" testID={`project-mcp-meta-${row().server}`}>{meta()}</Text>
      </If>
      <If when={!!row().warnings?.length}>
        <Text style={{ numberOfLines: 3 }} class="text-xs leading-4 text-warning" testID={`project-mcp-warning-${row().server}`}>{row().warnings!.join(" · ")}</Text>
      </If>
      <If when={!!row().error}><Text style={{ numberOfLines: 2 }} class="text-xs leading-4 text-warning">{row().error!}</Text></If>
    </View>
  );
}
