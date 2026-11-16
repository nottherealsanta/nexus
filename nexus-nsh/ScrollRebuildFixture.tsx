import { List, Text, View, mount, signal } from "nsh";
import { listSlots } from "./src/ui/list-slots";

// Two changing rows surround unchanged, differently sized rows, as running children do in a timeline.
export default function ScrollRebuildFixture() {
  const generation = signal(0);
  const fixed = Array.from({ length: 60 }, (_, i) => ({ key: String(i), id: i, generation: 0 }));
  const slots = listSlots<typeof fixed[number]>();
  const rows = () => slots(fixed.map((row) => row.id === 0 || row.id === 59 ? { ...row, generation: generation() } : row));
  return <View style={{ flex: 1 }}>
    <Text testID="rebuild" onPress={() => generation.update((n) => n + 1)}>Update children</Text>
    <List each={rows()} keyExtractor={(row) => row.key} estimatedItemSize={72} style={{ flex: 1 }} testID="rebuild-list">
      {(slot) => <View style={{ height: slot().value().id % 2 === 0 ? 160 : 40 }}><Text>{`Row ${slot().value().id} generation ${slot().value().generation}`}</Text></View>}
    </List>
  </View>;
}

mount(ScrollRebuildFixture);
