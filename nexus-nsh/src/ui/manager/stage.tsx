// Manager dropdown and the one-row braille indicator, anchored to the header in both clients.
import { Overlay, Scroll, Text, View, effect, keymap, signal, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { FOCUS, T, TS } from "../kit";
import { CardBody } from "./cards";
import { brailleField, waveBrightness } from "./braille";
import { OrbMode, WORDS, cardCaption, cardTitle } from "../../manager";
import {
  dropdownOpen, dropdownInteractive, closeManagerDropdown, voiceEnabled, audioRequested, callMinimised, card, consent, cardCollapsed,
  detailsScroll, scrollDetails, fieldText, inCall, listening, liveVoice, managerCaption, managerError, notice,
  phase, reducedMotion, modelPreview, connecting, speaking, stopManagerVoice, submitField, transcribing, userCaption, visual,
} from "../../core/manager";
import { isNarrow, live } from "../../store";
import { run } from "../../commands";
const SHADOW = "0 18px 48px rgb(0 0 0 / 0.32)";
/** Everything outside an open Manager panel recedes; the panel itself stays opaque. */
const DIM = "bg-black/40";
/** The privacy notice keeps the lighter card scrim the TUI uses (25%). */
const CONSENT_DIM = "bg-black/25";

/** Whether anything of the Manager is on screen: otherwise no node of it exists. */
export const stageVisible = () => consent() !== null || dropdownOpen();

export const managerHeaderIndicatorVisible = () => dropdownOpen() || inCall() || connecting();

/** The header indicator's state, from the same signals as the call. */
const waveMode = (): OrbMode => connecting() ? "thinking" : listening() ? "listening" : speaking() ? "speaking" : phase() !== "idle" || transcribing() ? "thinking" : "settling";
/** A call left idle in the background is still; the dropdown being open means the user is looking. */
const waveMoving = () => dropdownOpen() || connecting() || listening() || speaking() || phase() !== "idle" || transcribing();

const waveFrame = signal(0);
let waveTimer: ReturnType<typeof setInterval> | undefined;
let waveHolders = 0;
function holdWave() {
  waveHolders++;
  if (!waveTimer) {
    const start = Date.now();
    waveTimer = setInterval(() => waveFrame.set((Date.now() - start) / 1000), 33);
  }
  return () => {
    waveHolders--;
    if (waveHolders === 0 && waveTimer) { clearInterval(waveTimer); waveTimer = undefined; waveFrame.set(0); }
  };
}

export function ManagerHeaderIndicator() {
  const animated = () => !reducedMotion() && (waveMoving() || liveVoice());
  const hovered = signal(false);
  effect(() => { if (!animated()) return; return holdWave(); });
  const stopping = () => liveVoice() && hovered();
  return <If when={managerHeaderIndicatorVisible()}>{() => <View onMouseEnter={() => hovered.set(true)} onMouseLeave={() => hovered.set(false)}
    style={{ width: 100, height: 20 }}>
    <Button
      onPress={() => { if (liveVoice()) void stopManagerVoice(); else void run("manager.header"); }}
      testID="manager-indicator" accessibilityLabel={liveVoice() ? "Stop Manager voice" : "Manager"} class={`w-full h-full ${FOCUS}`}>
      <If when={stopping()}>{() => <Text class={`${TS.base} font-medium text-foreground`} testID="manager-indicator-stop">Stop</Text>}</If>
      <If when={!stopping()}>{() => <Text class="font-mono text-[13px] leading-[18px] text-muted-foreground" testID="manager-indicator-wave"
        style={{ opacity: reducedMotion() ? 0.7 : waveBrightness(waveMode()), numberOfLines: 1 }}>
        {brailleField(animated() ? waveFrame() : 0, waveMode(), visual.level)}
      </Text>}</If>
    </Button>
  </View>}</If>;
}

export function ManagerStage() { return <If when={stageVisible()}>{() => <Layer />}</If>; }

function caption(): { text: string; dim: boolean } | null {
  const error = managerError();
  if (error) return { text: [error.message, error.fix].filter(Boolean).join(" "), dim: false };
  if (inCall() && !live()) return { text: WORDS.reconnecting, dim: false };
  if (notice()) return { text: notice(), dim: false };
  if (managerCaption()) return { text: managerCaption(), dim: false };
  if (userCaption()) return { text: userCaption(), dim: true };
  // Reduced motion has no animation to say what is happening, so the state is a word.
  if (reducedMotion() && listening()) return { text: WORDS.listening, dim: false };
  if (reducedMotion() && (phase() !== "idle" || transcribing())) return { text: WORDS.thinking, dim: false };
  const c = card();
  if (c && (cardCollapsed() || callMinimised())) return { text: cardCaption(c), dim: false };
  return null;
}

function Layer() {
  let scroll: any = null, offset = 0, limit = Infinity, lastRequest = detailsScroll();
  effect(() => {
    const next = detailsScroll(), delta = next - lastRequest; lastRequest = next;
    if (delta) { offset = Math.min(limit, Math.max(0, offset + delta)); scroll?.scrollTo({ y: offset, animated: false }); }
  });
  effect(() => {
    if (!dropdownOpen() || !dropdownInteractive()) return;
    const up = keymap.bind("pageup", () => scrollDetails(-200), { when: "always" });
    const down = keymap.bind("pagedown", () => scrollDetails(200), { when: "always" });
    return () => { up(); down(); };
  });
  return <Overlay class="inset-0" style={{ pointerEvents: "box-none" }} testID="manager-stage">
    <If when={dropdownOpen()}>{() => <>
      {/* The whole viewport recedes behind the panel; the panel is opaque above it. */}
      <View class={`absolute inset-0 ${DIM}`} style={{ pointerEvents: "none" }} accessible={false} testID="manager-backdrop" />
      <View class="absolute left-0 right-0 bottom-0" style={{ top: isNarrow() ? 56 : 44 }} onPress={closeManagerDropdown} />
      <View class="absolute right-3 bg-popover p-4 gap-3" style={{ top: isNarrow() ? 56 : 44, width: Math.min(560, windowSize().width - 24), boxShadow: SHADOW }} testID="manager-dropdown">
        <Text class={T.section}>Manager</Text>
        <Scroll ref={(node: any) => { scroll = node; }} onScroll={(event) => { offset = event.offset.y; limit = Math.max(0, event.contentSize.height - event.size.height); }} style={{ maxHeight: Math.max(100, windowSize().height - 230) }}>
          <View class="gap-3">
            <Text class={T.body} style={{ userSelect: "text" }} testID="manager-caption">{connecting() ? "Connecting…" : caption()?.text ?? (speaking() ? "Speaking" : listening() ? "Listening" : phase() !== "idle" ? "Thinking" : "Ready")}</Text>
            <If when={!!modelPreview()?.lines?.length}>{() => <Text class={T.meta} style={{ userSelect: "text" }}>{`Model context · excerpt\n${modelPreview()?.lines.join("\n")}`}</Text>}</If>
            <If when={modelPreview()?.tool != null}>{() => <Text class={T.meta}>{`Tool · ${modelPreview()?.tool?.name} · ${modelPreview()?.tool?.status}`}</Text>}</If>
            <If when={card() != null}>{() => <View testID="manager-card" class="gap-3"><Text class={T.section}>{cardTitle(card()!)}</Text><CardBody card={() => card()!} interactive maxHeight={Math.max(100, windowSize().height - 320)} /></View>}</If>
          </View>
        </Scroll>
        <Input value={fieldText() ?? ""} autoFocus={dropdownInteractive()} placeholder={WORDS.placeholder} accessibilityLabel="Tell the Manager" testID="manager-field"
          onFocus={() => dropdownInteractive.set(true)} onChange={({ value }) => fieldText.set(value)} onSubmit={submitField} submitBehavior="submit" class={`h-12 w-full bg-card px-3 ${TS.body} text-popover-foreground`} />
        <View class="flex-row justify-between items-center gap-3">
        <Button onPress={() => void run("manager.audio_toggle")} class={`py-2 ${FOCUS}`} testID="manager-voice-toggle" accessibilityLabel={voiceEnabled() ? "Voice on" : "Voice off"}><Text class={T.body}>{voiceEnabled() ? "Voice: on" : "Voice: off"}</Text></Button>
        <Button onPress={() => void run("manager.open_session")}  class={`items-start py-2 ${FOCUS}`} testID="manager-open-session"><Text class={T.body}>Open Manager session</Text></Button>
        </View>
      </View>
    </>}</If>
    <If when={consent() !== null}>{() => <>
      <View class={`absolute inset-0 ${CONSENT_DIM}`} style={{ pointerEvents: "none" }} accessible={false} testID="manager-consent-backdrop" />
      <View class="absolute inset-0 items-center justify-center"><ConsentPanel width={Math.min(560, windowSize().width - 24)} /></View>
    </>}</If>
  </Overlay>;
}

/** The privacy notice before a first call: in the modal style, accepted with enter and cancelled with esc. */
function ConsentPanel(p: { width: number }) {
  return (
    <View class="bg-popover/80 rounded-(--radius-card) p-6 gap-3" testID="manager-consent" accessibilityLabel="Manager privacy notice"
      style={{ width: p.width, backdropFilter: "blur(22px) saturate(1.15)", boxShadow: SHADOW }}>
      <Text class={T.section}>Privacy</Text>
      <Text style={{ userSelect: "text" }} class={T.body}>{consent()?.notice ?? ""}</Text>
      <Text class={`${T.meta} pt-1`} testID="manager-consent-hints">enter accept · esc cancel</Text>
    </View>
  );
}
