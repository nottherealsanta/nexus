// Small building blocks on NSH base components, styled only with theme tokens.
import type { JSX } from "nsh";
import { Image, Text, View, effect, signal } from "nsh";
import { Button } from "nsh/base";
import { icons, IconName } from "../icons";
import { statusGlyphs, attention, spinner as spinnerFrames } from "../gen/theme";
import { config, isNarrow } from "../store";

// Type sizes: one shared 16/24 for titles, prose, interface text and code. Weight
// and colour carry the hierarchy; the size does not.
export const TS = {
  title: "text-[16px] leading-6",
  body: "text-[16px] leading-6",
  base: "text-[16px] leading-6",
  mono: "font-mono text-[16px] leading-6",
};
// Colour roles on top of the shared sizes: a modal or settings pane picks a role, not a size.
export const T = {
  title: `${TS.title} font-semibold text-foreground`,
  body: `${TS.body} text-foreground`,
  base: `${TS.base} text-foreground`,
  label: `${TS.base} font-medium text-foreground`,
  sec: `${TS.base} text-foreground-secondary`,
  meta: `${TS.base} text-muted-foreground tabular-nums`,
  section: `${TS.base} font-medium text-muted-foreground`,
  mono: `${TS.mono} text-foreground-secondary`,
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
      tooltip={<Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{p.reason ? `${p.label}: ${p.reason}` : p.label}</Text>}
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
      <Text class={`${TS.base} font-medium ${fg}`}>{p.busy ? "Working…" : p.label}</Text>
    </Button>
  );
}

/** Status mark: glyph + word, colour from the same token table as the TUI. */
export const TONE: Record<string, string> = {
  "text.primary": "text-foreground", "status.success": "text-success", "status.error": "text-error", "interactive.accent": "text-primary",
  "status.warning": "text-warning", "text.muted": "text-muted-foreground", "status.info": "text-info",
};
/** TextView part classes: a calm hierarchy on the shared 16/24 base, told apart by weight and colour. */
export const PROSE = {
  root: `${TS.body} text-foreground`,
  heading1: `${TS.title} font-semibold`,
  heading2: `${TS.title} font-semibold`,
  heading3: `${TS.body} font-semibold`,
  heading4: `${TS.body} font-semibold`,
  heading5: `${TS.body} font-semibold text-foreground-secondary`,
  heading6: `${TS.body} font-semibold text-foreground-secondary`,
  listMarker: `${TS.body} text-foreground-secondary`,
  table: "border-0 rounded-none",
  tableScroll: "border-0 rounded-none",
  tableRow: "border-0",
  tableCell: `border-0 ${TS.base}`,
  tableHeaderCell: "text-muted-foreground font-medium",
  tableHead: "bg-card rounded-none",
  code: `font-(family-name:--font-code) ${TS.base} rounded-none bg-card text-foreground`,
};

/** The TUI's soft shimmer: a light passes through the word, letter by letter. */
export function Shimmer(p: { text: string; reduced: boolean; testID?: string }) {
  return <View class="flex-row" accessibilityLabel={p.text} testID={p.testID ?? "working-shimmer"}>
    <Each of={[...p.text]}>{(c, i) => <Text
      class={`${TS.base} font-medium text-foreground`}
      style={p.reduced ? {} : { animationName: "pulse", animationDuration: 1800, animationDelay: i() * 110, animationIterationCount: "infinite" }}
    >{c()}</Text>}</Each>
  </View>;
}

/** The spinner phase, advanced for as long as at least one running mark is mounted. */
const spinnerFrame = signal(0);
let spinnerHolders = 0;
let spinnerTimer: ReturnType<typeof setInterval> | undefined;
function holdSpinner() {
  spinnerHolders++;
  if (!spinnerTimer) {
    const start = Date.now();
    spinnerTimer = setInterval(() => spinnerFrame.set(Math.floor((Date.now() - start) / 125)), 125);
  }
  return () => {
    spinnerHolders--;
    if (spinnerHolders === 0 && spinnerTimer) {
      clearInterval(spinnerTimer);
      spinnerTimer = undefined;
      spinnerFrame.set(0);
    }
  };
}

/** The running mark: the TUI's braille spinner, or its static running glyph under reduced motion. */
export function Spinner(p: { class?: string; label?: string; testID?: string }) {
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  effect(() => {
    if (reduced()) return;
    return holdSpinner();
  });
  const glyph = () => reduced() ? statusGlyphs.running.unicode : spinnerFrames[spinnerFrame() % spinnerFrames.length];
  return <Text class={p.class} testID={p.testID} accessibilityLabel={p.label ?? statusGlyphs.running.label}>{glyph()}</Text>;
}

export function Status(p: { kind: keyof typeof statusGlyphs; word?: boolean; class?: string; text?: string }) {
  const g = statusGlyphs[p.kind];
  const label = p.text ?? g.label;
  return (
    <View class="flex-row items-center gap-1" accessible accessibilityLabel={label}>
      <If when={p.kind === "running"} else={<Text class={`w-3.5 text-center ${TS.base} ${TONE[g.token]} ${p.class ?? ""}`}>{g.unicode}</Text>}>
        <Spinner class={`w-3.5 text-center ${TS.base} ${TONE[g.token]} ${p.class ?? ""}`} label={label} />
      </If>
      <If when={p.word}><Text class={`${TS.base} ${TONE[g.token]}`}>{label}</Text></If>
    </View>
  );
}
export function AttentionMark(p: { kind: keyof typeof attention }) {
  const g = attention[p.kind];
  return <Text class={`w-3.5 text-center ${TS.base} ${TONE[g.token]}`} accessibilityLabel={p.kind}>{g.unicode}</Text>;
}

/**
 * The status rail on a card that carries status: 3 px wide, fully rounded, inset 6 px from the
 * left and 12 px from the top and bottom. The parent must be `relative`. Only the square user
 * band keeps its flush rail.
 */
export function StatusRail(p: { color?: string; testID?: string }) {
  return <View class={`absolute left-[6px] top-3 bottom-3 w-[3px] rounded-full ${p.color ?? "bg-primary"}`} testID={p.testID} accessible={false} />;
}

/** The one segmented control: a square muted well with square options and the selected one on the selection band. */
export function Seg(p: { options: string[]; value: string; onPick?: (v: string) => void; disabled?: boolean; label: string; format?: (v: string) => string }) {
  return (
    <View class="flex-row rounded-none bg-muted p-0.5" role="radiogroup" accessibilityLabel={p.label}>
      <Each of={p.options} key={(o) => o}>
        {(o) => (
          <Button role="radio" selected={p.value === o()} disabled={p.disabled} onPress={() => p.onPick?.(o())} accessibilityLabel={`${p.label}: ${(p.format ?? identity)(o())}`}
            class={`rounded-none px-3 ${isNarrow() ? "min-h-10" : "min-h-7"} items-center justify-center hover:bg-accent disabled:opacity-50 ${p.value === o() ? "bg-selection" : ""} ${FOCUS}`}>
            <Text class={`${TS.base} ${p.value === o() ? "text-foreground font-medium" : "text-foreground-secondary"}`}>{(p.format ?? identity)(o())}</Text>
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
      <Text class={T.label}>{p.title}</Text>
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
