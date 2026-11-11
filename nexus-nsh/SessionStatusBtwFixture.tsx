import { keymap, mount, Text, View } from "nsh";
import { Button } from "nsh/base";
import scenario from "../interface/scenarios/session-status-btw.json";
import { btw, receiveBtw, startBtw } from "./src/btw";
import { bindKeys } from "./src/commands";
import { setApiBase } from "./src/core/transport";
import { bump, connection, model } from "./src/store";
import { BtwModal } from "./src/ui/btw";
import { SessionStatus } from "./src/ui/session-status";
import { Timeline } from "./src/ui/timeline";

export default function SessionStatusBtwFixture() {
  setApiBase("http://nexus.test");
  model.sessions.set("ses_parent", { id: "ses_parent", title: "Check the session", project_id: "prj_1", directory: "/work/demo", status: "idle", model: "mock/demo", agent: "build", permission: "allow", revision: 1, created: 1, updated: 2 } as any);
  model.select("ses_parent");
  model.selected!.messages = scenario.messages as any;
  model.selected!.turns = [scenario.turn] as any;
  model.selected!.loaded = true;
  connection.set({ kind: "live" } as any);
  bindKeys();
  const feed = () => { for (const e of scenario.events) receiveBtw({ ...e, session_id: btw()?.session } as any); };
  keymap.bind("f8", feed);
  return <View class="flex-1 bg-background">
    <View class="flex-row gap-4 p-4">
      <Button testID="busy" onPress={() => { const s = model.sessions.get("ses_parent")!; s.status = s.status === "busy" ? "idle" : "busy"; bump(); }}><Text>Toggle busy</Text></Button>
      <Button testID="start-btw" onPress={() => void startBtw("ses_parent", "Which port?")}><Text>/btw Which port?</Text></Button>
      <Button testID="stream-btw" onPress={feed}><Text>Stream answer</Text></Button>
    </View>
    <Timeline />
    <SessionStatus />
    <View class="h-28 bg-card p-5"><Text>Message…</Text></View>
    <BtwModal />
  </View>;
}
mount(SessionStatusBtwFixture);
