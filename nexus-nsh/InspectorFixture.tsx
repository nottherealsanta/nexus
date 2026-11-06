import { mount, View, keymap } from "nsh";
import { InspectorModal } from "./src/ui/inspector";
import { closeInspector, inspectSession } from "./src/inspector";
import "./fonts/Geist-Regular.ttf";
// The inspector opened through the real request path; tests answer its requests with the shared scenario.
function Fixture() {
  inspectSession("ses_inspector");
  keymap.bind("escape", closeInspector, { when: "always" });
  return <View class="flex-1 bg-background"><InspectorModal /></View>;
}
mount(Fixture);
