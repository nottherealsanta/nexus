// Plan 48: the context header as a draft (switches, the worktree row) and fixed (state only).
import { Text, View, mount, signal } from "nsh";
import { Button } from "nsh/base";
import { ContextDetailModal, ContextSections, inventorySections, previewSections, worktreeStatus } from "./src/ui/newsession";
import toolsScenario from "../interface/scenarios/tools-modal.json";
import type { InventoryRow } from "./src/skills-mcp";
import { toggleDraftWorktree } from "./src/commands";
import { contextDetail, currentProject, draftContext, draftContextKey, draftContextPlanKey, model, sessionDraft } from "./src/store";
import { setApiBase } from "./src/core/transport";

export default function ContextModesFixture() {
  setApiBase("http://nexus.test");
  model.select(null);
  currentProject.set("prj_1");
  sessionDraft.set({});
  const skills = [{ name: "review", description: "Review a change", state: "on" }];
  const mcp = [{ server: "docs", state: "on", tools: 3 }];
  const core = {
    used: 1200, window: null, parts: { instructions: 1000, tools: 200, instruction_file: 0 }, tools: toolsScenario.tools,
    skills, mcp, worktree: { available: true, changes: 2 },
  };
  draftContext.set({ key: draftContextKey("prj_1"), planKey: draftContextPlanKey("prj_1"), data: core });
  const fixed = signal(false);
  const git = signal(true);
  const rows: InventoryRow[] = [
    { kind: "skill", name: "review", description: "Review a change", on: true, locked: false },
    { kind: "mcp", name: "docs", on: false, locked: false, tools: 3 },
  ];
  const sections = () => [previewSections({ agent: "build", prompt: "", core, disabled_tools: sessionDraft().disabled_tools }).sections[2], ...inventorySections(rows)];
  return <View class="flex-1 bg-background p-6">
    <Button testID="fix" onPress={() => fixed.set(!fixed())}><Text>fixed</Text></Button>
    <Button testID="no-git" onPress={() => git.set(false)}><Text>no git</Text></Button>
    <Button testID="live-skills" onPress={() => contextDetail.set({ title: "Skills", detail: "", live: "skills" })}><Text>/skills</Text></Button>
    <Button testID="live-mcp" onPress={() => contextDetail.set({ title: "MCP", detail: "", live: "mcp" })}><Text>/mcp</Text></Button>
    <ContextSections sections={sections()} total={1200} estimated fixed={fixed()}
      onInventoryToggle={() => {}}
      worktree={git() ? { on: !!sessionDraft().worktree, changes: 2, onToggle: toggleDraftWorktree } : undefined}
      worktreeStatus={fixed() ? worktreeStatus({ worktree_request: { state: "not_entered" } }) ?? undefined : undefined} />
    <ContextDetailModal />
  </View>;
}

mount(ContextModesFixture);
