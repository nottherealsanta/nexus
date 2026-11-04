// One modal recipe (DESIGN_PLAN D13): a borderless square raised block over the scrim, 24 px
// padding and the shadow `0 12px 40px rgb(0 0 0 / .4)`. Title first,
// key hints last. Rows are 32 px with a selection band. Square corners match the TUI's modal.
import { Text, View, windowSize } from "nsh";
import type { JSX } from "nsh";
import { Dialog } from "nsh/base";
import { T, TS } from "./kit";
import { config, isNarrow } from "../store";

export const SCRIM = "bg-black/65";
const SHADOW = "0 12px 40px rgb(0 0 0 / 0.4)";
const WIDTH = { small: 500, medium: 600, large: 740 };

/** Room for a scrollable body after padding, title, footer and any extra controls. */
export const modalBodyHeight = (extra = 0) => Math.max(48, Math.floor(windowSize().height * 0.70) - 140 - extra);

export function Modal(p: {
  size?: "small" | "medium" | "large"; testID?: string; label: string; scrim?: string;
  onCancel: () => void; onOpenChange?: (open: boolean) => void; closeOnBackdropPress?: boolean;
  padded?: boolean; children?: JSX.Element;
}) {
  return (
    <Dialog
      open closeOnEscape={false} onCancel={p.onCancel} closeOnBackdropPress={p.closeOnBackdropPress ?? true}
      onOpenChange={({ open }) => p.onOpenChange?.(open)} animation={config()?.values?.ui?.reduced_motion ? "none" : "fade"} backdropClass={p.scrim ?? SCRIM}
      style={{ pointerEvents: "box-none" }} class="absolute inset-0 px-3 items-center justify-center"
    >
      <View testID={p.testID ? `${p.testID}-shell` : undefined}
        style={{ boxShadow: SHADOW, width: isNarrow() ? "100%" : WIDTH[p.size ?? "medium"], maxWidth: "100%", pointerEvents: "auto" }}
        class={`max-h-[70%] rounded-none bg-popover overflow-hidden ${p.padded === false ? "" : "p-6"}`}>
        <View accessibilityLabel={p.label} testID={p.testID} class="min-h-0">
          {p.children}
        </View>
      </View>
    </Dialog>
  );
}

/** The title uses the full row; dismissal belongs in the footer. */
export function ModalTitle(p: { title: string }) {
  return (
    <View class="flex-row items-center min-h-8 gap-3">
      <Text class={`flex-1 ${TS.title} font-semibold text-popover-foreground`}>{p.title}</Text>
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
