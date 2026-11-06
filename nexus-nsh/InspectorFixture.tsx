import { mount, View, keymap } from "nsh";
import { InspectorModal } from "./src/ui/inspector";
import { inspector, closeInspector } from "./src/inspector";
import scenario from "../interface/scenarios/inspector.json";
import "./fonts/Geist-Regular.ttf";
function Fixture() {
  inspector.set({ session: scenario.session_id, view: "Timing", page: 0, loading: false, data: scenario });
  keymap.bind("escape", closeInspector, { when: "always" });
  return <View class="flex-1 bg-background"><InspectorModal /></View>;
}
mount(Fixture);
