import type { JSX } from "nsh";
import { Text, View, keyboardFrame, safeArea } from "nsh";
import { Button } from "nsh/base";
import { FOCUS, TS, T } from "../kit";

/**
 * A bottom sheet over the phone shell: a scrim that closes it, and a raised panel with a grab bar, safe-area padding
 * and keyboard avoidance. Plain views (not NSH's `Sheet`), so Escape routing and the shell's other layers are unchanged.
 */
export function BottomSheet(p: { label: string; testID: string; onClose: () => void; children?: JSX.Element }) {
  return (
    <View class="absolute inset-0 justify-end" testID={p.testID} role="none" style={{ zIndex: 50 }}>
      <View onPress={p.onClose} accessibilityLabel="Close" focusable={false} class="absolute inset-0 bg-black/55" testID={`${p.testID}-scrim`} />
      <View accessibilityLabel={p.label} class="rounded-t-(--radius-card) bg-popover px-4 pt-2 gap-1"
        style={{ paddingBottom: Math.max(safeArea().bottom, 12) + keyboardFrame().height, boxShadow: "0 -12px 40px rgb(0 0 0 / 0.4)" }}>
        <View class="items-center pb-2"><View class="h-1 w-9 rounded-full bg-border-strong" accessible={false} /></View>
        {p.children}
      </View>
    </View>
  );
}

/** A 44 px menu row of a sheet: an optional leading glyph slot, the label, and a tone for destructive actions. */
export function SheetRow(p: { label: string; onPress: () => void; testID: string; tone?: "danger"; detail?: string; selected?: boolean; leading?: JSX.Element }) {
  return (
    <Button onPress={p.onPress} testID={p.testID} accessibilityLabel={p.label} selected={p.selected}
      class={`flex-row items-center gap-3 rounded-(--radius) px-2 min-h-12 active:bg-selection ${FOCUS}`}>
      {p.leading}
      <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${TS.body} ${p.tone === "danger" ? "text-error" : "text-foreground"}`}>{p.label}</Text>
      {p.detail ? <Text class={T.meta}>{p.detail}</Text> : null}
    </Button>
  );
}
