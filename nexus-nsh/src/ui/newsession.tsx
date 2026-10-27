import { Text, View } from "nsh";
import { Btn, T, midTruncate } from "./kit";
import { run } from "../commands";
import { currentProject, m, model, info } from "../store";
import { signal } from "nsh";
import { request } from "../core/transport";

const defaults = signal<{ model?: string; agents?: any[] } | null>(null);
let asked = false;

export function NewSessionView() {
  if (!asked) {
    asked = true;
    request<any>("GET", "/api/model/default").then((r) => defaults.update((d) => ({ ...d, model: r.model }))).catch(() => {});
    request<any>("GET", "/api/agent").then((r) => defaults.update((d) => ({ ...d, agents: r.agents }))).catch(() => {});
  }
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
  return (
    <View class="flex-1 min-h-0 items-center justify-center p-6" testID="new-session">
      <View class="w-full max-w-[560px] gap-4">
        <If when={project()} else={
          <View class="gap-2">
            <Text class="text-[18px] leading-7 font-semibold text-foreground">Welcome to Nexus</Text>
            <Text class={T.sec}>Add a project directory to begin. Sessions run inside a project and keep everything the model sees in one timeline.</Text>
            <View class="flex-row pt-1"><Btn label="Add project" icon="plus" kind="primary" onPress={() => run("project.add")} testID="welcome-add-project" /></View>
          </View>
        }>
          {(p) => (
            <View class="gap-4">
              <View class="gap-1">
                <Text class="text-[18px] leading-7 font-semibold text-foreground">New session</Text>
                <Text class={T.sec}>{`Starts in ${p().name}. Send a message to begin.`}</Text>
              </View>
              <View class="rounded-(--radius-card) border border-border bg-card px-4 py-1">
                <Line k="Directory" v={midTruncate(p().path, 38)} mono />
                <Line k="Agent" v="build (project default)" />
                <Line k="Model" v={defaults()?.model ?? "default"} mono />
                <View class="py-2"><Text class={T.meta}>Starting-context inspection (system prompt, AGENTS.md, tools, token count) arrives with Core's session draft endpoint (Step 12a).</Text></View>
              </View>
            </View>
          )}
        </If>
      </View>
    </View>
  );
}
function Line(p: { k: string; v: string; mono?: boolean }) {
  return (
    <View class="flex-row gap-3 py-2 border-b border-border">
      <Text class={`w-[88px] ${T.meta} pt-0.5`}>{p.k}</Text>
      <Text selectable class={`flex-1 min-w-0 ${p.mono ? "font-mono text-xs leading-5" : "text-[13px] leading-5"} text-foreground`}>{p.v}</Text>
    </View>
  );
}
void info;
