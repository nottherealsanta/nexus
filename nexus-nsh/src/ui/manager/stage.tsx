// Manager dropdown and the one-row braille indicator, anchored to the header in both clients.
import { Overlay, Scroll, Text, View, colorScheme, effect, keymap, signal, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { FOCUS, Icon, T, TS } from "../kit";
import { CardBody } from "./cards";
import { WaveMode, brailleField, waveColor, waveOpacity } from "./braille";
import { WORDS, cardCaption, cardTitle } from "../../manager";
import {
  dropdownOpen, dropdownInteractive, closeManagerDropdown, voiceEnabled, audioRequested, callMinimised, card, consent, cardCollapsed,
  detailsScroll, scrollDetails, fieldText, inCall, listening, liveVoice, managerCaption, managerError, micMuted, notice, voiceConnected,
  phase, reducedMotion, connecting, speaking, stopManagerVoice, submitField, transcribing, userCaption, visual,
} from "../../core/manager";
import { isNarrow, live, phone } from "../../store";
import { run } from "../../commands";
const SHADOW = "0 18px 48px rgb(0 0 0 / 0.32)";
/** Everything outside an open Manager panel recedes; the panel itself stays opaque. */
const DIM = "bg-black/40";
/** The privacy notice keeps the lighter card scrim the TUI uses (25%). */
const CONSENT_DIM = "bg-black/25";

/** Whether anything of the Manager is on screen: otherwise no node of it exists. */
export const stageVisible = () => consent() !== null || dropdownOpen();

export const managerHeaderIndicatorVisible = () => dropdownOpen() || inCall() || connecting();

/** The header indicator's state, from the same signals as the call. Waves only while the voice is connected; a disconnected
 *  voice is a still quiet mark and a muted microphone a still flat line. */
const waveMode = (): WaveMode => !voiceConnected() && !connecting() ? "still" : connecting() ? "thinking" : listening() && !micMuted() ? "listening" : speaking() ? "speaking" : phase() !== "idle" || transcribing() ? "thinking" : liveVoice() && micMuted() ? "muted" : "settling";
/** Nothing moves unless the voice is connected; a call left idle in the background is still too. */
const waveMoving = () => (voiceConnected() || connecting()) && (dropdownOpen() || connecting() || (listening() && !micMuted()) || speaking() || phase() !== "idle" || transcribing());

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

/** The braille wave field: the header indicator on a desktop, the title of the phone's Manager tab. */
export function ManagerWave() {
  const animated = () => !reducedMotion() && waveMode() !== "still" && waveMode() !== "muted" && (waveMoving() || liveVoice());
  effect(() => { if (!animated()) return; return holdWave(); });
  // Read on every waveFrame tick (and whenever the call's signals change): `visual` is plain data the call's frame loop
  // updates, so the colour blend and the measured level are current at each 33 ms render. Without the loop (reduced
  // motion, or before it starts) the mode comes straight from the signals and the field is still.
  const wave = () => {
    animated() && waveFrame();
    const scheme = colorScheme() === "light" ? "light" : "dark";
    const mode = waveMode();
    const eased = animated() && visual.running;
    const from = eased ? visual.from : mode, blend = eased ? visual.blend : 1, to = eased ? visual.mode : mode;
    return { mode: to, level: eased ? visual.level : 0.5, color: waveColor(from, to, blend, scheme), opacity: reducedMotion() && mode !== "still" && mode !== "muted" ? 0.7 : waveOpacity(from, to, blend) };
  };
  return <Text class="font-mono text-[13px] leading-[18px]" testID="manager-indicator-wave"
    style={{ color: wave().color, opacity: wave().opacity, numberOfLines: 1 }}>
    {brailleField(animated() ? waveFrame() : 0, wave().mode, wave().level)}
  </Text>;
}

/** Where the header indicator sits in the window: the open panel's scrim leaves this box undimmed (the wave stays at full strength). */
export const indicatorBox = signal<{ x: number; y: number; w: number; h: number } | null>(null);

export function ManagerHeaderIndicator() {
  const hovered = signal(false);
  const stopping = () => liveVoice() && hovered();
  return <If when={managerHeaderIndicatorVisible()}>{() => <View onMouseEnter={() => hovered.set(true)} onMouseLeave={() => hovered.set(false)}
    onLayout={(b: any) => {
      const r = b?.bounds ?? b, o = r?.origin ?? r, z = r?.size ?? r;
      if (z?.width && z?.height && Number.isFinite(o?.x) && Number.isFinite(o?.y)) indicatorBox.set({ x: o.x, y: o.y, w: z.width, h: z.height });
    }}
    style={{ width: 100, height: 20 }}>
    <Button
      onPress={() => { if (liveVoice()) void stopManagerVoice(); else void run("manager.header"); }}
      testID="manager-indicator" accessibilityLabel={liveVoice() ? "Stop Manager voice" : "Manager"} class={`w-full h-full ${FOCUS}`}>
      <If when={stopping()}>{() => <Text class={`${TS.base} font-medium text-foreground`} testID="manager-indicator-stop">Stop</Text>}</If>
      <If when={!stopping()}>{() => <ManagerWave />}</If>
    </Button>
  </View>}</If>;
}

export function ManagerStage() { return <If when={stageVisible()}>{() => <Layer />}</If>; }

function caption(): { text: string; dim: boolean } | null {
  const error = managerError();
  if (error) return { text: [error.message, error.fix].filter(Boolean).join(" "), dim: false };
  if (inCall() && !live()) return { text: WORDS.reconnecting, dim: false };
  if (notice()) return { text: notice(), dim: false };
  // A phone's Manager tab does not show the voice conversation: while voice is on it shows the call's state only.
  const spoken = phone && (audioRequested() || liveVoice());
  if (!spoken && managerCaption()) return { text: managerCaption(), dim: false };
  if (!spoken && userCaption()) return { text: userCaption(), dim: true };
  // Reduced motion has no animation to say what is happening, so the state is a word.
  if (reducedMotion() && listening()) return { text: WORDS.listening, dim: false };
  if (reducedMotion() && (phase() !== "idle" || transcribing())) return { text: WORDS.thinking, dim: false };
  const c = card();
  if (c && (cardCollapsed() || callMinimised())) return { text: cardCaption(c), dim: false };
  return null;
}

/** The Manager's caption, card, typed field and voice controls: the header dropdown on a desktop, the whole Manager tab on a phone. */
/** Talk opens the microphone; while it listens the same button sends; a live call stops. */
const talkLabel = () => listening() ? "Send" : "Talk";

export function ManagerPanel(p: { full?: boolean }) {
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
  return <>
        <Scroll ref={(node: any) => { scroll = node; }} onScroll={(event) => { offset = event.offset.y; limit = Math.max(0, event.contentSize.height - event.size.height); }} class={p.full ? "flex-1 min-h-0" : ""} style={p.full ? {} : { maxHeight: Math.max(100, windowSize().height - 230) }}>
          <View class="gap-3">
            {/* The Manager's model context is not shown here: it is the debug session view (Open Manager session). */}
            <Text class={T.body} style={{ userSelect: "text" }} testID="manager-caption">{connecting() ? "Connecting…" : caption()?.text ?? (speaking() ? "Speaking" : liveVoice() && micMuted() ? WORDS.muted : listening() ? "Listening" : phase() !== "idle" ? "Thinking" : "Ready")}</Text>
            <If when={card() != null}>{() => <View testID="manager-card" class="gap-3"><Text class={T.section}>{cardTitle(card()!)}</Text><CardBody card={() => card()!} interactive maxHeight={Math.max(100, windowSize().height - 320)} /></View>}</If>
          </View>
        </Scroll>
        <Input value={fieldText() ?? ""} autoFocus={!phone && dropdownInteractive()} placeholder={WORDS.placeholder} accessibilityLabel="Tell the Manager" testID="manager-field"
          onFocus={() => dropdownInteractive.set(true)} onChange={({ value }) => fieldText.set(value)} onSubmit={submitField} submitBehavior="submit" class={`h-12 w-full bg-card px-3 ${TS.body} text-popover-foreground`} />
        <VoiceButtons />
  </>;
}

/** Two buttons, each labelled by its action: Connect / Disconnect the voice, and Mute / Unmute the microphone. */
function VoiceButtons() {
  const size = phone ? "min-h-11 flex-1 justify-center" : "px-2 py-2";
  const look = phone ? "bg-raised" : "hover:bg-accent active:bg-selection";
  const button = (p: { id: string; label: () => string; icon: () => any; command: string; disabled?: () => boolean }) =>
    <Button onPress={() => void run(p.command)} disabled={p.disabled?.()} class={`flex-row items-center gap-2 rounded-(--radius) px-3 ${size} ${look} disabled:opacity-50 ${FOCUS}`} testID={p.id} accessibilityLabel={p.label()}>
      <Icon name={p.icon()} size={14} /><Text class={T.body}>{p.label()}</Text>
    </Button>;
  return <View class="flex-row items-center gap-2 flex-wrap">
    {button({ id: "manager-voice-connect", label: () => voiceConnected() ? "Disconnect" : "Connect", icon: () => voiceConnected() ? "stop" : "play", command: "manager.audio_toggle" })}
    {button({ id: "manager-voice-mute", label: () => micMuted() ? "Unmute" : "Mute", icon: () => micMuted() ? "mic-off" : "mic", command: "manager.mute_toggle", disabled: () => !liveVoice() })}
    <If when={phone && voiceConnected() && !liveVoice()}><Button onPress={() => void run("manager.talk")} class={`flex-row items-center gap-2 rounded-(--radius) bg-primary px-4 min-h-11 ${FOCUS}`} testID="manager-talk" accessibilityLabel={talkLabel()}><Icon name={listening() ? "arrow-up" : "mic"} size={16} class="tint-on-accent" /><Text class={`${TS.body} text-on-accent font-semibold`}>{talkLabel()}</Text></Button></If>
    <If when={!phone}><View class="flex-1" /><Button onPress={() => void run("manager.open_session")} class={`flex-row items-center gap-2 rounded-(--radius) px-2 py-2 hover:bg-accent active:bg-selection ${FOCUS}`} testID="manager-open-session"><Icon name="session" size={14} /><Text class={T.body}>Open Manager session</Text></Button></If>
  </View>;
}

/** The phone's Manager tab (AGENTS.md: mobile talks and types to the Manager here; there is no header indicator). */
export function ManagerTab() {
  return <View class="flex-1 min-h-0 bg-popover p-4 gap-3" testID="manager-tab"><ManagerPanel full /></View>;
}

/** The scrim of an open panel: the whole window except the header indicator's box, so the wave is never dimmed. */
function Backdrop() {
  const dim = (style: Record<string, number | string>, testID?: string) => <View class={`absolute ${DIM}`} style={{ pointerEvents: "none", ...style }} accessible={false} testID={testID} />;
  const boxed = () => indicatorBox() !== null && managerHeaderIndicatorVisible();
  return <>
    <If when={!boxed()}>{() => dim({ left: 0, right: 0, top: 0, bottom: 0 }, "manager-backdrop")}</If>
    <If when={boxed()}>{() => {
      const b = indicatorBox()!;
      return <>
        {dim({ left: 0, right: 0, top: 0, height: b.y }, "manager-backdrop")}
        {dim({ left: 0, right: 0, top: b.y + b.h, bottom: 0 })}
        {dim({ left: 0, width: b.x, top: b.y, height: b.h }, "manager-backdrop-hole")}
        {dim({ left: b.x + b.w, right: 0, top: b.y, height: b.h })}
      </>;
    }}</If>
  </>;
}

function Layer() {
  return <Overlay class="inset-0" style={{ pointerEvents: "box-none" }} testID="manager-stage">
    <If when={dropdownOpen() && !phone}>{() => <>
      {/* The whole viewport recedes behind the panel; the panel is opaque above it. */}
      <Backdrop />
      <View class="absolute left-0 right-0 bottom-0" style={{ top: isNarrow() ? 56 : 44 }} onPress={closeManagerDropdown} />
      <View class="absolute right-3 bg-popover p-4 gap-3" style={{ top: isNarrow() ? 56 : 44, width: Math.min(560, windowSize().width - 24), boxShadow: SHADOW }} testID="manager-dropdown">
        <Text class={T.section}>Manager</Text>
        <ManagerPanel />
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
    <View class="bg-popover/95 rounded-(--radius-card) p-6 gap-3" testID="manager-consent" accessibilityLabel="Manager privacy notice"
      style={{ width: p.width, boxShadow: SHADOW }}>
      <Text class={T.section}>Privacy</Text>
      <Text style={{ userSelect: "text" }} class={T.body}>{consent()?.notice ?? ""}</Text>
      <Text class={`${T.meta} pt-1`} testID="manager-consent-hints">enter accept · esc cancel</Text>
    </View>
  );
}
