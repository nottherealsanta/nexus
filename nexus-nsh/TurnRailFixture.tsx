import { Text, View, mount } from "nsh";
import { focusedSidebarRow, scrolledUp, setTurnRail } from "./src/store";
import { Timeline } from "./src/ui/timeline";
import { railMessages, seedRail } from "./turn-rail-fixture-state";

function TurnRailFixture() {
  seedRail(railMessages);
  return (
    <View class="flex-1" testID="fixture-root">
      <Text testID="fixture-rail-off" onPress={() => setTurnRail(false)}>rail off</Text>
      <Text testID="fixture-sidebar-focus" onPress={() => focusedSidebarRow.set({ kind: "project", id: "prj_rail" })}>focus sidebar</Text>
      <Text testID="fixture-sidebar-blur" onPress={() => focusedSidebarRow.set(null)}>blur sidebar</Text>
      <Text testID="fixture-scrolled">{scrolledUp() ? "scrolled" : "end"}</Text>
      <View class="flex-1"><Timeline /></View>
    </View>
  );
}

mount(TurnRailFixture);
