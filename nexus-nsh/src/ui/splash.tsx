import { Text, View, signal } from "nsh";

/** The five startup milestones, shared with the TUI (`nexus-tui/src/splash.rs`). */
export const SPLASH_STEPS = ["daemon", "workspace", "providers", "session", "ready"] as const;
/** The `_/` N mark from the TUI, one string per row. */
export const SPLASH_LOGO = [
  "    _/      _/",
  "   _/_/    _/",
  "  _/  _/  _/",
  " _/    _/_/",
  "_/      _/",
] as const;
export const SPLASH_SEGMENTS = 30;

/** Milestones reached, 0..5. The bar follows these, never the clock. */
export const splashStage = signal(0);
/** The splash is fading out: the app is live and input can reach it. */
const splashLeaving = signal(false);
/** The splash is over for this launch. */
export const splashDone = signal(false);
/** Bumped by start/dismiss so a fade timer from an earlier splash cannot hide a new one. */
let generation = 0;

/** Record a milestone; a later one never moves the bar backwards. */
export function splashReach(step: number) {
  if (splashDone()) return;
  splashStage.update((s) => Math.max(s, Math.min(SPLASH_STEPS.length, step)));
}

/** Live: fade the splash into the first frame. */
export function splashFinish() {
  if (splashDone() || splashLeaving()) return;
  splashReach(SPLASH_STEPS.length);
  splashLeaving.set(true);
  const at = generation;
  setTimeout(() => { if (at === generation) splashDone.set(true); }, 180);
}

/** A failed start: show the error at once instead of covering it. */
export function splashDismiss() {
  generation++;
  splashLeaving.set(false);
  splashDone.set(true);
}

/** Show the splash again for a new Core address (the Connect screen was open). */
export function splashStart() {
  generation++;
  splashLeaving.set(false);
  splashDone.set(false);
  splashStage.set(0);
}

/**
 * The launch splash: the logo, a 30-segment milestone bar and the next step.
 * Mirrors the TUI's centred block; the animation is the bar's fill.
 */
export function Splash() {
  const filled = () => Math.round((splashStage() / SPLASH_STEPS.length) * SPLASH_SEGMENTS);
  const pct = () => Math.round((splashStage() / SPLASH_STEPS.length) * 100);
  const step = () => SPLASH_STEPS[Math.min(splashStage(), SPLASH_STEPS.length - 1)];
  return (
    <If when={!splashDone()}>
      <View
        testID="splash"
        class={`absolute inset-0 items-center justify-center bg-background transition-opacity motion-reduce:transition-none duration-150 ${splashLeaving() ? "opacity-0" : "opacity-100"}`}
        style={{ pointerEvents: splashLeaving() ? "none" : "auto" }}
      >
        <View class="items-center gap-6" accessibilityLabel={`Starting Nexus Core, ${pct()}%`}>
          <View class="items-center" accessible={false}>
            <Each of={SPLASH_LOGO}>
              {(row) => <Text style={{ numberOfLines: 1 }} class="font-mono text-[15px] leading-[20px] font-semibold text-foreground">{row()}</Text>}
            </Each>
          </View>
          <View class="items-center gap-2">
            <View class="flex-row items-center gap-3">
              <View class="flex-row items-center gap-[2px]" accessible={false}>
                <Each of={[...Array(SPLASH_SEGMENTS).keys()]}>
                  {(i) => <View class={`h-2 w-1.5 rounded-none transition-colors motion-reduce:transition-none duration-200 ${i() < filled() ? "bg-foreground" : "bg-border"}`} />}
                </Each>
              </View>
              <Text class="font-mono text-[13px] leading-5 text-muted-foreground tabular-nums">{String(pct()).padStart(3, " ")}%</Text>
            </View>
            <Text class="font-mono text-[13px] leading-5 text-muted-foreground">{step()}</Text>
          </View>
        </View>
      </View>
    </If>
  );
}
