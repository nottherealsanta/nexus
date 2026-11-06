import { mount, View, Text, keymap } from "nsh";
import { InspectorModal } from "./src/ui/inspector";
import { closeInspector, inspectSession, scrollTop } from "./src/inspector";
import "./fonts/Geist-Regular.ttf";
// The inspector opened through the real request path; tests answer its requests with the shared scenario.
function Fixture() {
  inspectSession("ses_inspector");
  keymap.bind("escape", closeInspector, { when: "always" });
  return <View class="flex-1 bg-background"><InspectorModal /><Text testID="scroll-top" class="absolute left-0 top-0 text-xs">{`scroll ${scrollTop()}`}</Text></View>;
}
mount(Fixture);
