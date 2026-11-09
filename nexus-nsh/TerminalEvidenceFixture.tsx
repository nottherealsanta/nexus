import { mount, View } from "nsh";
import { Composer } from "./src/ui/composer";
import { Confirm } from "./src/ui/overlays";
import { centre, connection, currentProject, draft } from "./src/store";
import { previewTerminalEvidence } from "./src/terminal-evidence-preview";
import { terminalEvidence } from "./src/terminal-evidence";
import "./fonts/Geist-Regular.ttf";

function Fixture() {
  centre.set("new_session");
  currentProject.set("fixture");
  connection.set({kind:"live"});
  draft.set({"new:fixture":"Explain this"});
  previewTerminalEvidence("new:fixture", terminalEvidence("t1", "Build", "selected output", {
    project_id:"", workspace:"/tmp", stream_seq:null,
  }));
  return <View class="flex-1 bg-background justify-end"><Composer /><Confirm /></View>;
}
mount(Fixture);
