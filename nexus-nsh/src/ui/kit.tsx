// Small building blocks on NSH base components, styled only with theme tokens.
import type { JSX } from "nsh";
import { Image, Text, View } from "nsh";
import { Button } from "nsh/base";
import { icons, IconName } from "../icons";
import { statusGlyphs, attention } from "../gen/theme";
import { isNarrow } from "../store";

// Type levels (INTERFACE_PLAN.md "Scale"): title 15/20, body 14/22, secondary 13/20, meta 12/16.
export const T = {
  title: "text-[15px] leading-5 font-semibold text-foreground",
  body: "text-[14px] leading-[22px] text-foreground",
  sec: "text-[13px] leading-5 text-foreground-secondary",
  meta: "text-xs leading-4 text-muted-foreground tabular-nums",
  mono: "font-mono text-[12.5px] leading-5 text-foreground-secondary",
};
export const FOCUS = "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring";

export function Icon(p: { name: IconName; size?: number; class?: string }) {
  const s = p.size ?? 16;
  return <Image src={icons[p.name]} style={{ width: s, height: s }} class={`shrink-0 ${p.class ?? "tint-foreground-secondary"}`} accessible={false} />;
}

export function IconButton(p: { icon: IconName; label: string; onPress?: () => void; disabled?: boolean; selected?: boolean; testID?: string; class?: string; tint?: string; reason?: string }) {
  return (
    <Button
      onPress={p.onPress} disabled={p.disabled} selected={p.selected} accessibilityLabel={p.label} testID={p.testID}
      tooltip={<Text class="text-xs text-popover-foreground bg-popover rounded-(--radius) px-2 py-1">{p.reason ? `${p.label}: ${p.reason}` : p.label}</Text>}
      class={`items-center justify-center rounded-(--radius) hover:bg-accent disabled:opacity-50 ${isNarrow() ? "size-11" : "size-8"} ${FOCUS} ${p.class ?? ""}`}
    >
      <Icon name={p.icon} class={p.tint ?? "tint-foreground-secondary"} />
    </Button>
  );
}

/** Text button: primary (accent fill), default (raised, hairline) or danger. */
export function Btn(p: { initial?: boolean; label: string; onPress?: () => void; kind?: "primary" | "default" | "danger" | "ghost"; disabled?: boolean; reason?: string; testID?: string; busy?: boolean; icon?: IconName; class?: string }) {
  const k = p.kind ?? "default";
  const skin = k === "primary" ? "bg-primary" : k === "danger" ? "bg-secondary hover:bg-accent" : k === "ghost" ? "hover:bg-accent" : "bg-secondary hover:bg-accent";
  const fg = k === "primary" ? "text-on-accent" : k === "danger" ? "text-error" : "text-foreground";
  return (
    <Button
      onPress={p.onPress} disabled={p.disabled || p.busy} testID={p.testID} accessibilityLabel={p.reason && p.disabled ? `${p.label}, unavailable: ${p.reason}` : p.label}
      class={`flex-row items-center justify-center gap-1.5 rounded-(--radius) px-3 ${isNarrow() ? "min-h-11" : "min-h-8"} disabled:opacity-50 active:opacity-80 ${skin} ${p.initial ? "border-ring" : ""} ${p.class ?? ""} ${FOCUS}`}
    >
      <If when={p.icon}>{() => <Icon name={p.icon!} class={k === "primary" ? "tint-on-accent" : k === "danger" ? "tint-error" : "tint-foreground-secondary"} />}</If>
      <Text class={`text-[13px] leading-5 font-medium ${fg}`}>{p.busy ? "Working…" : p.label}</Text>
    </Button>
  );
}

/** Status mark: glyph + word, colour from the same token table as the TUI. */
export const TONE: Record<string, string> = {
  "text.primary": "text-foreground", "status.success": "text-success", "status.error": "text-error", "interactive.accent": "text-primary",
  "status.warning": "text-warning", "text.muted": "text-muted-foreground", "status.info": "text-info",
};
/** TextView part classes: a calm hierarchy on the 14/22 base (headings only a step above, semibold, tight to what they introduce). */
export const PROSE = {
  root: "text-[14px] leading-[22px] text-foreground",
  heading1: "text-[17px] leading-6 font-semibold",
  heading2: "text-[15px] leading-6 font-semibold",
  heading3: "text-[14px] leading-[22px] font-semibold",
  heading4: "text-[14px] leading-[22px] font-semibold",
  heading5: "text-[14px] leading-[22px] font-semibold text-foreground-secondary",
  heading6: "text-[14px] leading-[22px] font-semibold text-foreground-secondary",
  listMarker: "text-[14px] leading-[22px] text-foreground-secondary",
  table: "border-0",
  tableScroll: "border-0",
  tableRow: "border-0",
  tableCell: "border-0 text-[13px] leading-5",
  tableHeaderCell: "text-muted-foreground font-medium",
  tableHead: "bg-card rounded-(--radius)",
  code: "font-(family-name:--font-code) text-[12.5px] rounded-[6px] bg-card text-foreground",
};

/** The TUI's soft shimmer: a light passes through the word, letter by letter. */
export function Shimmer(p: { text: string; reduced: boolean; testID?: string }) {
  return <View class="flex-row" accessibilityLabel={p.text} testID={p.testID ?? "working-shimmer"}>
    <Each of={[...p.text]}>{(c, i) => <Text
      class="text-[13px] leading-5 font-medium text-foreground"
      style={p.reduced ? {} : { animationName: "pulse", animationDuration: 1800, animationDelay: i() * 110, animationIterationCount: "infinite" }}
    >{c()}</Text>}</Each>
  </View>;
}

export function Status(p: { kind: keyof typeof statusGlyphs; word?: boolean; class?: string; text?: string }) {
  const g = statusGlyphs[p.kind];
  const label = p.text ?? g.label;
  return (
    <View class="flex-row items-center gap-1" accessible accessibilityLabel={label}>
      <Text class={`w-3.5 text-center text-[13px] leading-5 ${TONE[g.token]} ${p.class ?? ""}`}>{g.unicode}</Text>
      <If when={p.word}><Text class={`text-xs leading-4 ${TONE[g.token]}`}>{label}</Text></If>
    </View>
  );
}
export function AttentionMark(p: { kind: keyof typeof attention }) {
  const g = attention[p.kind];
  return <Text class={`w-3.5 text-center text-[13px] leading-5 ${TONE[g.token]}`} accessibilityLabel={p.kind}>{g.unicode}</Text>;
}

/**
 * The status rail on a card that carries status: 3 px wide, fully rounded, inset 6 px from the
 * left and 12 px from the top and bottom. The parent must be `relative`. Only the square user
 * band keeps its flush rail.
 */
export function StatusRail(p: { color?: string; testID?: string }) {
  return <View class={`absolute left-[6px] top-3 bottom-3 w-[3px] rounded-full ${p.color ?? "bg-primary"}`} testID={p.testID} accessible={false} />;
}

/** The one segmented control: a muted well, an 8 px option, and the selected option as a soft pill. */
export function Seg(p: { options: string[]; value: string; onPick?: (v: string) => void; disabled?: boolean; label: string; format?: (v: string) => string }) {
  return (
    <View class="flex-row rounded-(--radius) bg-muted p-0.5" role="radiogroup" accessibilityLabel={p.label}>
      <Each of={p.options} key={(o) => o}>
        {(o) => (
          <Button role="radio" selected={p.value === o()} disabled={p.disabled} onPress={() => p.onPick?.(o())} accessibilityLabel={`${p.label}: ${(p.format ?? identity)(o())}`}
            class={`rounded-[8px] px-3 ${isNarrow() ? "min-h-10" : "min-h-7"} items-center justify-center hover:bg-accent disabled:opacity-50 ${p.value === o() ? "bg-selection" : ""} ${FOCUS}`}>
            <Text class={`text-[13px] leading-5 ${p.value === o() ? "text-foreground font-medium" : "text-foreground-secondary"}`}>{(p.format ?? identity)(o())}</Text>
          </Button>
        )}
      </Each>
    </View>
  );
}
const identity = (v: string) => v;

/** The one empty state: a medium title, an optional secondary line and an optional action, left-aligned. */
export function Empty(p: { title: string; body?: string; action?: JSX.Element; testID?: string }) {
  return (
    <View class="items-start gap-2 p-4" testID={p.testID}>
      <Text class="text-[13px] leading-5 font-medium text-foreground">{p.title}</Text>
      <If when={p.body}><Text class={T.sec}>{p.body}</Text></If>
      {p.action}
    </View>
  );
}

export function Rule() { return <View class="h-px bg-border" />; }

export function relTime(sec: number, now = Date.now()): string {
  const d = Math.max(0, now / 1000 - sec);
  if (d < 45) return "now";
  if (d < 3600) return `${Math.round(d / 60)}m`;
  if (d < 86400) return `${Math.round(d / 3600)}h`;
  if (d < 86400 * 14) return `${Math.round(d / 86400)}d`;
  return `${Math.round(d / 604800)}w`;
}
export const abbrev = (n: number) => n >= 1_000_000
  ? `${(n / 1_000_000).toFixed(1)}M`
  : n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);

/** Middle truncation with an ellipsis (paths, ids): the ends carry the meaning. */
export function midTruncate(t: string, max: number): string {
  if (t.length <= max) return t;
  const keep = max - 1, tail = Math.ceil(keep * 0.6), head = keep - tail;
  return `${t.slice(0, head)}…${t.slice(t.length - tail)}`;
}

/** A path with the user's home shortened to `~`, shared by the location strip and the project rows. */
export function shortPath(path: string, home: string): string {
  const h = (home ?? "").replace(/\/$/, "");
  return h && (path === h || path.startsWith(`${h}/`)) ? `~${path.slice(h.length)}` : path;
}
