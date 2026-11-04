import { Text, View, mount, signal } from "nsh";
import { Button } from "nsh/base";
import scenario from "../interface/scenarios/skill-picker.json";
import { ContextDetailModal, ContextSections, inventorySections } from "./src/ui/newsession";
import type { InventoryRow } from "./src/skills-mcp";
import { setApiBase } from "./src/core/transport";

export default function SkillPickerFixture() {
  setApiBase("http://nexus.test");
  const count = signal(13);
  const rows = signal<InventoryRow[]>(scenario.names.map((name) => ({
    kind: "skill", name, description: scenario.description, on: true, locked: false, meta: "",
  })));
  const sections = () => inventorySections(rows().slice(0, count()));
  return <View class="flex-1 bg-background p-6">
    <Button testID="boundary" onPress={() => count.set(count() === 13 ? 12 : 13)}><Text>12 / 13 skills</Text></Button>
    <View>
      <ContextSections sections={sections()} total={1200} estimated onInventoryToggle={(_, name) => rows.update((items) => items.map((item) => item.name === name ? { ...item, on: !item.on } : item))} />
    </View>
    <ContextDetailModal />
  </View>;
}

mount(SkillPickerFixture);
