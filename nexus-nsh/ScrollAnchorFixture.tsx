import { List, Text, View, mount, signal } from "nsh";

// Exercise the same NSH virtual-list primitive used by Timeline, with a row
// that can be remeasured while it is just above the viewport.
export default function ScrollAnchorFixture() {
  const grown = signal(false);
  const prepended = signal(false);
  const offset = signal(0);
  const saved = signal(0);
  const position = signal(0);
  const savedPosition = signal(0);
  let marker: any;
  const rows = Array.from({ length: 30 }, (_, i) => i);
  return <View style={{ flex: 1 }}>
    <Text testID="save-offset" onPress={async () => { saved.set(offset()); savedPosition.set((await marker.measure()).pagePosition.y); }}>Save position</Text>
    <Text testID="grow-row" onPress={() => grown.set(true)}>Grow row</Text>
    <Text testID="prepend-rows" onPress={() => prepended.set(true)}>Prepend rows</Text>
    <Text testID="measure" onPress={async () => position.set((await marker.measure()).pagePosition.y)}>Measure position</Text>
    <Text testID="offset-delta">{`delta ${Math.round(offset() - saved())}`}</Text>
    <Text testID="saved-offset">{`saved ${Math.round(saved())}`}</Text>
    <Text testID="position-delta">{`position ${Math.round(position() - savedPosition())}`}</Text>
    <List each={prepended() ? [-2, -1, ...rows] : rows} keyExtractor={(i) => String(i)} estimatedItemSize={60} style={{ flex: 1 }}
      onScroll={(e) => offset.set(e.offset.y)} testID="anchor-list">
      {(i) => <View ref={(node: any) => { if (i() === 3) marker = node; }} style={{ height: i() === 0 && grown() ? 140 : 60 }}><Text>{`Row ${i()}`}</Text></View>}
    </List>
  </View>;
}

mount(ScrollAnchorFixture);
