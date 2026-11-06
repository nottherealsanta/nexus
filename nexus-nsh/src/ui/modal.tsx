// One modal recipe (DESIGN_PLAN D13): a borderless square raised block over the scrim, 24 px
// padding and the shadow `0 12px 40px rgb(0 0 0 / .4)`. Title first,
// key hints last. Rows are 32 px with a selection band. Square corners match the TUI's modal.
import { Text, View, platform, windowSize } from "nsh";
import type { JSX } from "nsh";
import { Dialog } from "nsh/base";
import { T, TS } from "./kit";
import { config, isNarrow } from "../store";

// Behind a modal the page is covered by the theme background, not dimmed (ledger 2026-11-06); `surface.canvas` in the TUI.
export const SCRIM = "bg-background";

/**
 * The terminal draws every Dialog layer inside a `╭─╮` frame (upstream nsh's render.rs `frame_around`
 * boxes each overlay it paints), which the Rust TUI's borderless modal does not have. So in a
 * terminal a modal is an ordinary absolute view of this app: a raised block with the TUI's metrics
 * (2 columns and 1 row of padding, `paint::modal_rect` geometry in whole cells).
 */
export const terminalModal = platform.idiom === "terminal";
export const CELL = { w: 8, h: 16 };
export const termCols = () => Math.floor(windowSize().width / CELL.w);
export const termRows = () => Math.floor(windowSize().height / CELL.h);
const CELLS = { small: 60, medium: 72, large: 88 };
/** `paint::modal_rect`: centred, 2 cells from each edge, at most 70% of the rows. */
export function modalRect(width: number, height: number, cols = termCols(), rows = termRows()) {
  const w = Math.max(1, Math.min(width, cols - 4));
  const h = Math.max(Math.min(8, rows), Math.min(height, Math.floor(rows * 7 / 10), rows - 4));
  return { x: Math.floor((cols - w) / 2), y: Math.floor((rows - h) / 2), w, h };
}
const SHADOW = "0 12px 40px rgb(0 0 0 / 0.4)";
const WIDTH = { small: 500, medium: 600, large: 740 };

/** Room for a scrollable body after padding, title, footer and any extra controls. */
export const modalBodyHeight = (extra = 0) => Math.max(48, Math.floor(windowSize().height * 0.70) - 140 - extra);

export function Modal(p: {
  size?: "small" | "medium" | "large"; testID?: string; label: string; scrim?: string;
  onCancel: () => void; onOpenChange?: (open: boolean) => void; closeOnBackdropPress?: boolean;
  padded?: boolean; tall?: boolean; rows?: number; children?: JSX.Element;
}) {
  if (terminalModal) {
    const close = () => (p.onOpenChange ? p.onOpenChange(false) : p.onCancel());
    const fixed = () => p.rows ? modalRect(CELLS[p.size ?? "medium"], p.rows) : null;
    const width = () => Math.max(1, Math.min(CELLS[p.size ?? "medium"], termCols() - 4)) * CELL.w;
    return (
      <View testID={p.testID ? `${p.testID}-layer` : undefined} class="absolute inset-0 items-center justify-center" style={{ zIndex: 50 }}>
        <View testID={p.testID ? `${p.testID}-backdrop` : undefined} class="absolute inset-0 bg-background" onPress={p.closeOnBackdropPress === false ? undefined : close} />
        <View testID={p.testID ? `${p.testID}-shell` : undefined} class="bg-popover p-3"
          style={fixed() ? { position: "absolute", left: fixed()!.x * CELL.w, top: fixed()!.y * CELL.h, width: fixed()!.w * CELL.w, height: fixed()!.h * CELL.h } : { width: width(), maxHeight: Math.floor(termRows() * 7 / 10) * CELL.h }}>
          <View accessibilityLabel={p.label} testID={p.testID} class="min-h-0 flex-1">{p.children}</View>
        </View>
      </View>
    );
  }
  return (
    <Dialog
      open closeOnEscape={false} onCancel={p.onCancel} closeOnBackdropPress={p.closeOnBackdropPress ?? true}
      onOpenChange={({ open }) => p.onOpenChange?.(open)} animation={config()?.values?.ui?.reduced_motion ? "none" : "fade"} backdropClass={p.scrim ?? SCRIM}
      style={{ pointerEvents: "box-none" }} class="absolute inset-0 px-3 items-center justify-center"
    >
      <View testID={p.testID ? `${p.testID}-shell` : undefined}
        style={{ boxShadow: SHADOW, width: isNarrow() ? "100%" : WIDTH[p.size ?? "medium"], maxWidth: "100%", pointerEvents: "auto" }}
        class={`${p.tall ? "max-h-[90%]" : "max-h-[70%]"} rounded-none bg-popover overflow-hidden ${p.padded === false ? "" : "p-6"}`}>
        <View accessibilityLabel={p.label} testID={p.testID} class="min-h-0">
          {p.children}
        </View>
      </View>
    </Dialog>
  );
}

/** The title uses the full row; dismissal belongs in the footer. */
export function ModalTitle(p: { title: string }) {
  if (terminalModal) return <Text class={`${T.title} pb-4`} style={{ numberOfLines: 1 }}>{p.title}</Text>;
  return (
    <View class="flex-row items-center min-h-8 gap-3">
      <Text class={`flex-1 ${TS.title} font-semibold text-popover-foreground`}>{p.title}</Text>
    </View>
  );
}

/**
 * The last row: key hints on the left, an optional count on the right. A hint
 * given as segments is drawn as the TUI's hint row is: each part on its own,
 * separated by `·`, so every key/verb pair keeps its own weight and name.
 */
export function ModalFooter(p: { hints: string | readonly string[]; right?: string }) {
  const parts = () => typeof p.hints === "string" ? [p.hints] : p.hints;
  if (terminalModal) {
    // The TUI's `hint_row`: a bold key, its muted verb, ` · ` between, the count at the far end.
    const pair = (hint: string) => { const at = hint.indexOf(" "); return at < 0 ? [hint, ""] : [hint.slice(0, at), hint.slice(at)]; };
    return (
      <View class="flex-row items-center mt-4">
        <Text class={`flex-1 ${T.meta}`} style={{ numberOfLines: 1 }}>
          {() => parts().map((hint, i) => <>{i > 0 ? " · " : ""}<Text class="font-semibold text-foreground">{pair(hint)[0]}</Text>{pair(hint)[1]}</>)}
        </Text>
        <If when={!!p.right}><Text class={T.meta}>{p.right!}</Text></If>
      </View>
    );
  }
  return (
    <View class="flex-row items-center gap-3 pt-3">
      <View class={`flex-1 flex-row flex-wrap items-center gap-x-2 gap-y-1 ${T.meta}`}>
        {() => parts().map((hint, i) => (<>
          <If when={i > 0}><Text class={T.meta}>·</Text></If>
          <Text class={T.meta}>{hint}</Text>
        </>))}
      </View>
      <If when={!!p.right}><Text class={T.meta}>{p.right!}</Text></If>
    </View>
  );
}

/** A muted section heading (also used by Settings). */
export function Heading(p: { text: string; first?: boolean }) {
  return <Text class={`${T.section} ${p.first ? "pt-1" : "pt-5"} pb-1.5`}>{p.text}</Text>;
}
