// One modal recipe (DESIGN_PLAN D13): a borderless square raised block over the scrim, 24 px
// padding and the shadow `0 12px 40px rgb(0 0 0 / .4)`. Title first, `esc` on its right,
// key hints last. Rows are 32 px with a selection band. Square corners match the TUI's modal.
import { Text, View } from "nsh";
import type { JSX } from "nsh";
import { Dialog } from "nsh/base";
import { T, TS } from "./kit";
import { config, isNarrow } from "../store";

export const SCRIM = "bg-black/65";
const SHADOW = "0 12px 40px rgb(0 0 0 / 0.4)";

export function Modal(p: {
  width: number; top?: string; testID?: string; label: string; scrim?: string;
  onCancel: () => void; onOpenChange?: (open: boolean) => void; closeOnBackdropPress?: boolean;
  padded?: boolean; children?: JSX.Element;
}) {
  return (
    <Dialog
      open closeOnEscape={false} onCancel={p.onCancel} closeOnBackdropPress={p.closeOnBackdropPress ?? true}
      onOpenChange={({ open }) => p.onOpenChange?.(open)} animation={config()?.values?.ui?.reduced_motion ? "none" : "fade"} backdropClass={p.scrim ?? SCRIM}
      style={isNarrow() ? { boxShadow: SHADOW } : { boxShadow: SHADOW, width: p.width }} testID={p.testID ? `${p.testID}-shell` : undefined}
      class={`absolute left-0 right-0 ${p.top ?? "top-[12%]"} ${isNarrow() ? "mx-3" : "mx-auto"} max-h-[76%] rounded-none bg-popover overflow-hidden ${p.padded === false ? "" : "p-6"}`}
    >
      <View accessibilityLabel={p.label} testID={p.testID}>
        {p.children}
      </View>
    </Dialog>
  );
}

/** The title row: bold title, `esc` right-aligned and muted. */
export function ModalTitle(p: { title: string; esc?: boolean }) {
  return (
    <View class="flex-row items-center min-h-8 gap-3">
      <Text class={`flex-1 ${TS.title} font-semibold text-popover-foreground`}>{p.title}</Text>
      <If when={p.esc !== false}><Text class={`${T.meta} rounded-none bg-muted px-1.5`}>esc</Text></If>
    </View>
  );
}

/** The last row: key hints on the left, an optional count on the right. */
export function ModalFooter(p: { hints: string; right?: string }) {
  return (
    <View class="flex-row items-center gap-3 pt-3">
      <Text class={`flex-1 ${T.meta}`}>{p.hints}</Text>
      <If when={!!p.right}><Text class={T.meta}>{p.right!}</Text></If>
    </View>
  );
}

/** A muted section heading (also used by Settings). */
export function Heading(p: { text: string; first?: boolean }) {
  return <Text class={`${T.section} ${p.first ? "pt-1" : "pt-5"} pb-1.5`}>{p.text}</Text>;
}
