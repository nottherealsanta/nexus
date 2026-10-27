// Small building blocks on NSH base components, styled only with theme tokens.
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
  mono: "font-mono text-[13px] leading-5 text-foreground-secondary",
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
      tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{p.reason ? `${p.label}: ${p.reason}` : p.label}</Text>}
      class={`items-center justify-center rounded-(--radius) hover:bg-accent selected:bg-selection active:bg-selection disabled:opacity-50 ${isNarrow() ? "size-11" : "size-8"} ${FOCUS} ${p.class ?? ""}`}
    >
      <Icon name={p.icon} class={p.tint ?? "tint-foreground-secondary"} />
    </Button>
  );
}

/** Text button: primary (accent fill), default (raised, hairline) or danger. */
export function Btn(p: { label: string; onPress?: () => void; kind?: "primary" | "default" | "danger" | "ghost"; disabled?: boolean; reason?: string; testID?: string; busy?: boolean; icon?: IconName }) {
  const k = p.kind ?? "default";
  const skin = k === "primary" ? "bg-primary border-primary" : k === "danger" ? "bg-error border-error" : k === "ghost" ? "border-transparent hover:bg-accent" : "bg-secondary border-border hover:bg-accent";
  const fg = k === "primary" || k === "danger" ? "text-on-accent" : "text-foreground";
  return (
    <Button
      onPress={p.onPress} disabled={p.disabled || p.busy} testID={p.testID} accessibilityLabel={p.reason && p.disabled ? `${p.label}, unavailable: ${p.reason}` : p.label}
      class={`flex-row items-center justify-center gap-1.5 rounded-(--radius) border px-3 ${isNarrow() ? "min-h-11" : "min-h-8"} disabled:opacity-50 active:opacity-80 ${skin} ${FOCUS}`}
    >
      <If when={p.icon}>{() => <Icon name={p.icon!} class={k === "primary" || k === "danger" ? "tint-on-accent" : "tint-foreground-secondary"} />}</If>
      <Text class={`text-[13px] leading-5 font-medium ${fg}`}>{p.busy ? "Working…" : p.label}</Text>
    </Button>
  );
}

/** Status mark: glyph + word, colour from the same token table as the TUI. */
const TONE: Record<string, string> = {
  "text.primary": "text-foreground", "status.success": "text-success", "status.error": "text-error", "interactive.accent": "text-primary",
  "status.warning": "text-warning", "text.muted": "text-muted-foreground", "status.info": "text-info",
};
export function Status(p: { kind: keyof typeof statusGlyphs; word?: boolean; class?: string }) {
  const g = statusGlyphs[p.kind];
  return (
    <View class="flex-row items-center gap-1" accessible accessibilityLabel={g.label}>
      <Text class={`w-3.5 text-center text-[13px] leading-5 ${TONE[g.token]} ${p.class ?? ""}`}>{g.unicode}</Text>
      <If when={p.word}><Text class={`text-xs leading-4 ${TONE[g.token]}`}>{g.label}</Text></If>
    </View>
  );
}
export function AttentionMark(p: { kind: keyof typeof attention }) {
  const g = attention[p.kind];
  return <Text class={`w-3.5 text-center text-[13px] leading-5 ${TONE[g.token]}`} accessibilityLabel={p.kind}>{g.unicode}</Text>;
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
export const abbrev = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k` : String(n));

/** Middle truncation with an ellipsis (paths, ids): the ends carry the meaning. */
export function midTruncate(t: string, max: number): string {
  if (t.length <= max) return t;
  const keep = max - 1, tail = Math.ceil(keep * 0.6), head = keep - tail;
  return `${t.slice(0, head)}…${t.slice(t.length - tail)}`;
}
