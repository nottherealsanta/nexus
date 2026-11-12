import { Text, View, effect, signal } from "nsh";
import { config } from "../store";

/** The five startup milestones, shared with the TUI (`nexus-tui/src/splash.rs`). Tracked only to know when `ready` is reached. */
export const SPLASH_STEPS = ["daemon", "workspace", "providers", "session", "ready"] as const;
/** The `_/` N mark from the TUI, one string per row. */
export const SPLASH_LOGO = [
  "    _/      _/",
  "   _/_/    _/",
  "  _/  _/  _/",
  " _/    _/_/",
  "_/      _/",
] as const;
/** Milliseconds of waiting before one quiet "Starting Nexus Core…" line shows (TUI: `SLOW_FRAMES`). */
export const SPLASH_SLOW_MS = 5000;
/** Milliseconds per column step of the light sweep across the logo. */
const SWEEP_MS = 32;
const LOGO_WIDTH = Math.max(...SPLASH_LOGO.map((r) => r.length));
/** Column of the light sweep `ms` into the wait, as in the TUI: a function of the clock only. */
export function splashSweep(ms: number) {
  return Math.floor(ms / SWEEP_MS) % (LOGO_WIDTH + 16) - 8;
}

/** Milestones reached, 0..5. Nothing shows them; they only tell when `ready` is reached. */
export const splashStage = signal(0);
/** The splash is fading out: the app is live and input can reach it. */
const splashLeaving = signal(false);
/** The splash is over for this launch. */
export const splashDone = signal(false);
/** Bumped by start/dismiss so a fade timer from an earlier splash cannot hide a new one. */
let generation = 0;

/** Record a milestone; a later one never moves backwards. */
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
 * The launch splash: the logo, animated from the first frame (a light sweeps
 * across it while Core starts) and, after 5 s, one quiet line. No progress bar,
 * percentage or step name. Mirrors the TUI's centred block.
 */
export function Splash() {
  const waited = signal(0);
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  effect(() => {
    if (splashDone() || splashLeaving()) return;
    const t0 = Date.now();
    waited.set(0);
    if (reduced()) {
      const id = setTimeout(() => waited.set(Date.now() - t0), SPLASH_SLOW_MS);
      return () => clearTimeout(id);
    }
    const id = setInterval(() => waited.set(Date.now() - t0), 33);
    return () => clearInterval(id);
  });
  const lit = (col: number) => !reduced() && Math.abs(col - splashSweep(waited())) <= 1;
  return (
    <If when={!splashDone()}>
      <View
        testID="splash"
        class={`absolute inset-0 items-center justify-center bg-background transition-opacity motion-reduce:transition-none duration-150 ${splashLeaving() ? "opacity-0" : "opacity-100"}`}
        style={{ pointerEvents: splashLeaving() ? "none" : "auto" }}
      >
        <View class="items-center gap-6" accessibilityLabel="Starting Nexus Core">
          <View class="items-center" accessible={false}>
            <Each of={SPLASH_LOGO}>
              {(row) => (
                <View class="flex-row">
                  <Each of={[...row().padEnd(LOGO_WIDTH, " ")].map((ch, col) => ({ ch, col }))}>
                    {(c) => <Text style={{ numberOfLines: 1 }} class={`font-mono text-[15px] leading-[20px] font-semibold ${lit(c().col) ? "text-foreground" : "text-muted-foreground"}`}>{c().ch === " " ? " " : c().ch}</Text>}
                  </Each>
                </View>
              )}
            </Each>
          </View>
          <Text testID="splash-slow" class={`font-mono text-[13px] leading-5 text-muted-foreground ${waited() >= SPLASH_SLOW_MS ? "opacity-100" : "opacity-0"}`}>Starting Nexus Core…</Text>
        </View>
      </View>
    </If>
  );
}
