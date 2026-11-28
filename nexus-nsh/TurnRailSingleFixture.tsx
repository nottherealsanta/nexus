import { Text, View, mount } from "nsh";
import { Timeline } from "./src/ui/timeline";
import { railSingleMessages, seedRail } from "./turn-rail-fixture-state";

function TurnRailSingleFixture() {
  seedRail(railSingleMessages as any);
  return (
    <View class="flex-1" testID="fixture-root">
      <View class="flex-1"><Timeline /></View>
    </View>
  );
}

mount(TurnRailSingleFixture);
