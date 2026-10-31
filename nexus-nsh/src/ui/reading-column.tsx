// One reading column for the timeline and the new-session starting context (TUI D04/I9):
// every card shares the column's left edge, and prose/activity use the same inner inset.
import { View } from "nsh";
import type { JSX } from "nsh";
import { NARROW, width } from "../store";

/** Horizontal insets at a window width: 24 px narrow, then wider than the old fixed 40 px. */
export function timelineColumnClass(w: number): string {
  if (w < NARROW) return "px-6";
  if (w < 1200) return "px-12";
  if (w < 1600) return "px-16";
  return "px-20";
}

/** The inset in pixels, for tests and the docs; the class above is what lays it out. */
export function timelineColumnPx(w: number): number {
  if (w < NARROW) return 24;
  if (w < 1200) return 48;
  if (w < 1600) return 64;
  return 80;
}

/** The same inset the timeline uses, for the new-session starting context. */
export const columnClass = () => timelineColumnClass(width());

/** A card's inner inset; assistant prose and activity rows start at this same text edge. */
export const CARD_INSET = "px-4";
/** Prose and activity rows sit one card inset in from the column edge. */
export const TEXT_INDENT = "pl-4";
/** Activity rows keep a 4 px hover pad while their text lands on the prose edge. */
export const ACTIVITY_INDENT = "pl-3";
/** The activity row's hover pad, so `pl-3` plus this lands on the `pl-4` prose edge. */
export const ACTIVITY_CONTROL_INSET = "px-1";

export function TimelineColumn(p: { class?: string; testID?: string; children?: JSX.Element; onLayout?: (b: any) => void }) {
  return (
    <View class={`w-full ${timelineColumnClass(width())} ${p.class ?? ""}`} testID={p.testID} onLayout={p.onLayout}>
      {p.children}
    </View>
  );
}
