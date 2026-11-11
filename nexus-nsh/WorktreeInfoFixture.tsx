// Isolated actual WorktreeInfo for browser pointer/clipboard regression tests.
import { mount, Text, View } from "nsh";
import { Button } from "nsh/base";
import { bindKeys } from "./src/commands";
import { bump, currentProject, model, sessionWorktrees, toasts, worktreeInfoOpen } from "./src/store";
import { WorktreeInfo } from "./src/ui/overlays";

const directory = `/work/${"unbroken_component_".repeat(60)}final-tail`;
function Fixture() {
  model.projects.set("prj_1", { id: "prj_1", path: "/work/demo", name: "demo" } as any);
  model.sessions.set("ses_1", { id: "ses_1", project_id: "prj_1", directory, title: "Long path", status: "idle" } as any);
  model.select("ses_1");
  currentProject.set("prj_1");
  sessionWorktrees.set({ ses_1: { main: "/work/demo", session_directory: directory, worktrees: [
    { path: "/work/demo", branch: "main", main: true },
    { path: directory, branch: "feature/session-controls", main: false },
  ] } });
  bump();
  bindKeys();
  return <View class="flex-1 bg-background"><Button testID="open-worktree" onPress={() => worktreeInfoOpen.set(true)}><Text>Open worktree</Text></Button><WorktreeInfo /><Text testID="toast-text">{toasts().map(t => t.text).join(", ")}</Text></View>;
}
mount(Fixture);
