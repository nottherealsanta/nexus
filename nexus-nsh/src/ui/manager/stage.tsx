// The Manager's stage (MANAGER_PLAN.md 10.1): an orb at the bottom centre, a caption above it, a one-line
// typed field, and at most one card in a semi-transparent modal. Three layers, each present only when it has
// something to do, painted over the app. Nothing here is mounted at rest: no nodes, no frames, no timers.
//
// The Manager modal is its own light layer: a scrim of about 25% and a panel of about 80% with a backdrop
// blur. It is not the app's `Modal`, which dims heavily and captures input.
import { Canvas, Overlay, Scroll, Text, View, colorScheme, createMotion, safeArea, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { FOCUS, T, TS } from "../kit";
import { CardBody, narrowCard } from "./cards";
import { drawOrb, drawStaticOrb } from "./orb";
import { managerColors } from "../../gen/theme";
import {
  Card, ORB_HIT, ORB_SIZE, OrbMode, SCRIM_ALPHA, WORDS, cardCaption, cardTitle, footerHints,
} from "../../manager";
import {
  captionGone, card, consent, cardCollapsed, cardFocused, dismissCard, dockHeight, exchange, fieldText, focusCard, frame, inCall,
  listening, managerCaption, managerError, notice, orbOn, phase, reducedMotion, speaking, submitField, transcribing, userCaption, visual,
} from "../../core/manager";
import { live, phone, phonePage } from "../../store";
import { run } from "../../commands";

const SHADOW = "0 18px 48px rgb(0 0 0 / 0.32)";
const CANVAS = 148;
const light = () => colorScheme() === "light";
const palette = (): Record<OrbMode, string> => {
  const c = managerColors[light() ? "light" : "dark"];
  return { listening: c.listening, thinking: c.thinking, speaking: c.speaking, settling: c.idle };
};

/** Whether anything of the Manager is on screen: otherwise no node of it exists. */
export const stageVisible = () => consent() !== null || exchange() || orbOn() || !!card() || fieldText() !== null;

export function ManagerStage() {
  return <If when={stageVisible()}>{() => <Layer />}</If>;
}

const rgba = (alpha: number) => `rgba(0,0,0,${alpha})`;

/** The caption text and whether it is dim (the user's own words as heard). */
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
  if (c && cardCollapsed()) return { text: cardCaption(c), dim: false };
  return null;
}

function Layer() {
  // The composer's top edge is where the orb sits; views without one use the window's bottom.
  const bottom = () => {
    if (phone && phonePage() !== "conversation") return 84 + safeArea().bottom;
    return (dockHeight() || 72) + 8;
  };
  const cardWidth = () => Math.min(narrowCard() ? 560 : 780, windowSize().width - 24);
  const cardMax = () => Math.max(180, windowSize().height - bottom() - ORB_SIZE - 120);
  return (
    <Overlay class="inset-0" style={{ pointerEvents: "box-none" }} testID="manager-stage">
      <If when={cardFocused()}>
        <View class="absolute inset-0" style={{ backgroundColor: rgba(SCRIM_ALPHA) }} onPress={dismissCard} testID="manager-scrim" accessibilityLabel="Close the card" />
      </If>
      <View class="absolute left-0 right-0 items-center gap-2.5" style={{ bottom: bottom(), pointerEvents: "box-none" }} testID="manager-column">
        <If when={consent() !== null}>{() => <ConsentPanel width={cardWidth()} />}</If>
        <If when={!!card() && !cardCollapsed()}>{() => <CardPanel width={card()?.type === "diagram" ? Math.min(560, cardWidth()) : cardWidth()} maxHeight={cardMax()} />}</If>
        <Caption />
        <Field />
        <If when={orbOn() || captionGone()}><Orb /></If>
      </View>
    </Overlay>
  );
}

/** The privacy notice before a first call: in the modal style, accepted with enter and cancelled with esc. */
function ConsentPanel(p: { width: number }) {
  return (
    <View class="bg-popover/80 rounded-(--radius-card) p-6 gap-3" testID="manager-consent" accessibilityLabel="Manager privacy notice"
      style={{ width: p.width, backdropFilter: "blur(22px) saturate(1.15)", boxShadow: SHADOW }}>
      <Text class={T.section}>Privacy</Text>
      <Text style={{ userSelect: "text" }} class={T.body}>{consent()?.notice ?? ""}</Text>
      <Text class={`${T.meta} pt-1`} testID="manager-hints">enter accept · esc cancel</Text>
    </View>
  );
}

function Caption() {
  const line = () => caption();
  return (
    <If when={!!line()}>
      {() => {
        const rise = reducedMotion() ? undefined : createMotion({ enter: { duration: 180, keyframes: { opacity: [0, 1], translateY: [4, 0] } } });
        const tappable = () => !!card() && cardCollapsed();
        return (
          <Button onPress={() => { if (tappable()) focusCard(); }} disabled={!tappable()} testID="manager-caption" accessibilityLabel={line()?.text}
            class={`bg-popover/75 rounded-none px-4 py-2 transition-opacity duration-300 ${captionGone() ? "opacity-0" : "opacity-100"} ${FOCUS}`}
            style={{ backdropFilter: "blur(16px)", boxShadow: "0 6px 22px rgb(0 0 0 / 0.22)", ...(rise ? rise.style : {}), maxWidth: Math.min(560, windowSize().width - 24) }}>
            <Text style={{ numberOfLines: 2, textAlign: "center" }} class={`${TS.body} ${line()?.dim ? "text-muted-foreground" : "text-popover-foreground"}`}>{line()?.text ?? ""}</Text>
          </Button>
        );
      }}
    </If>
  );
}

function Field() {
  return (
    <If when={fieldText() !== null}>
      {() => (
        <View class="bg-popover/85 rounded-none px-4" style={{ backdropFilter: "blur(16px)", boxShadow: "0 6px 22px rgb(0 0 0 / 0.22)", width: Math.min(520, windowSize().width - 24) }} testID="manager-field-box">
          <Input value={fieldText() ?? ""} autoFocus placeholder={WORDS.placeholder} accessibilityLabel="Tell the Manager" testID="manager-field"
            onChange={({ value }) => { if (fieldText() !== null) fieldText.set(value); }}
            onSubmit={submitField} submitBehavior="submit"
            class={`h-10 w-full bg-transparent ${TS.body} text-popover-foreground`} />
        </View>
      )}
    </If>
  );
}

function CardPanel(p: { width: number; maxHeight: number }) {
  const current = () => card()!;
  const focused = () => cardFocused();
  const rise = reducedMotion() ? undefined : createMotion({ enter: { duration: 220, keyframes: { opacity: [0, 1], translateY: [8, 0] } } });
  const hints = () => footerHints(current(), { focused: focused() });
  return (
    <View
      class="bg-popover/80 rounded-(--radius-card) p-6" testID="manager-card" accessibilityLabel={cardTitle(current())}
      style={{
        width: p.width, maxHeight: p.maxHeight,
        backdropFilter: "blur(22px) saturate(1.15)", boxShadow: SHADOW,
        ...(rise ? rise.style : {}),
      }}>
      <Text class={`${T.section} pb-3`}>{cardTitle(current())}</Text>
      <View class="shrink min-h-0">
        <Scroll class="shrink min-h-0">
          <View><CardBody card={current as () => Card} interactive={focused()} maxHeight={p.maxHeight - 185} /></View>
        </Scroll>
      </View>
      <If when={!!hints()}><Text class={`${T.meta} pt-4`} testID="manager-hints">{hints()}</Text></If>
    </View>
  );
}

/** The orb: a canvas redrawn each animation frame while visible, with a 44 px hit target over it. */
function Orb() {
  const reduced = reducedMotion;
  const staticMode = (): OrbMode => (listening() ? "listening" : speaking() ? "speaking" : phase() !== "idle" || transcribing() ? "thinking" : "settling");
  const label = () => (listening() ? "Send what you said" : "Talk to the Manager");
  return (
    <View style={{ width: ORB_SIZE, height: ORB_SIZE, alignItems: "center", justifyContent: "center" }} testID="manager-orb-box">
      <Canvas
        testID="manager-orb-canvas"
        style={{ position: "absolute", left: (ORB_SIZE - CANVAS) / 2, top: (ORB_SIZE - CANVAS) / 2, width: CANVAS, height: CANVAS, pointerEvents: "none" }}
        draw={(cx, size) => {
          const colors = palette();
          if (reduced()) {
            // A still disc per state; the caption names the state in words.
            void orbOn(); void listening(); void speaking(); void phase(); void transcribing(); void managerError();
            if (orbOn()) drawStaticOrb(cx, size.width, size.height, ORB_SIZE / 2, managerError() ? "settling" : staticMode(), colors, light());
            return;
          }
          frame();
          drawOrb(cx, size.width, size.height, ORB_SIZE / 2, {
            t: (Date.now() - visual.startedAt) / 1000, level: visual.level, presence: visual.presence, mode: visual.mode, from: visual.from,
            blend: visual.blend, still: visual.still, light: light(), colors,
          });
        }}
      />
      <Button onPress={() => void run("manager.talk")} testID="manager-orb" accessibilityLabel={label()} disabled={!orbOn()}
        class={`items-center justify-center rounded-full ${FOCUS}`} style={{ width: ORB_HIT, height: ORB_HIT }} />
    </View>
  );
}

