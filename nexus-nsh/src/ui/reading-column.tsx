// One reading column for the timeline and the new-session starting context (TUI D04/I9):
// every card shares the column's left edge, and prose/activity use the same inner inset.
import { View } from "nsh";
import type { JSX } from "nsh";
import { width } from "../store";

/** A fixed compact gutter, like the Rust TUI, leaves the available width to the conversation. */
export function timelineColumnClass(_w: number): string {
  return "px-3";
}

/** The inset in pixels, for tests and the docs; the class above is what lays it out. */
export function timelineColumnPx(_w: number): number {
  return 12;
}

/** The same inset the timeline uses, for the new-session starting context. */
export const columnClass = () => timelineColumnClass(width());

/** A card's inner inset; assistant prose and activity rows start at this same text edge. */
export const CARD_INSET = "px-2";
/** The one text edge: prose, cards and full rows all start 8 px inside the column. */
export const TEXT_INSET = "px-2";
/** Prose and activity rows sit one card inset in from the column edge. */
export const TEXT_INDENT = "pl-2";
/** User bands and activity affordances have a little extra breathing room. */
export const TURN_INSET = "pl-4";
export const USER_BAND_INSET = "pl-4 pr-2";
/** The activity row's chevron slot starts on the shared turn text edge. */
export const ACTIVITY_CHEVRON = "w-4 shrink-0 -ml-1";

export function TimelineColumn(p: { class?: string; style?: Record<string, string | number>; testID?: string; children?: JSX.Element; onLayout?: (b: any) => void }) {
  return (
    <View class={`w-full ${columnClass()} ${p.class ?? ""}`} style={p.style} testID={p.testID} onLayout={p.onLayout}>
      {p.children}
    </View>
  );
}
