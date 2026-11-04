// The Manager's stage (docs/manager-design.md 10.1): in call mode a focused call takes the stage — a large orb
// centred, the caption under it, the hint and End under that, and at most one card below. The app behind is dimmed
// to about 30%. Esc ends the whole call; `/manager` opens a quiet call in the bottom-right corner, with the
// caption under it and the app at full strength and with its keys back. Nothing here is mounted at rest: no
// nodes, no frames, no timers.
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
  CALL_SCRIM_ALPHA, Card, ORB_HIT, ORB_LARGE, ORB_LARGE_CANVAS, ORB_SIZE, OrbMode, SCRIM_ALPHA, WORDS,
  cardCaption, cardTitle, footerHints, managerHint, rowCount,
} from "../../manager";
import {
  callFocused, callMinimised, captionGone, card, consent, cardCollapsed, cardFocused, dismissCard, dockHeight,
  exchange, fieldText, focusCard, frame, inCall, listening, managerCaption, managerError, minimised, notice, orbOn,
  phase, reducedMotion, speaking, submitField, transcribing, userCaption, visual,
} from "../../core/manager";
import { isNarrow, live, phone, phonePage } from "../../store";
import { byId, keyLabel, run } from "../../commands";

const SHADOW = "0 18px 48px rgb(0 0 0 / 0.32)";
const CANVAS = 148;
const light = () => colorScheme() === "light";
const palette = (): Record<OrbMode, string> => {
  const c = managerColors[light() ? "light" : "dark"];
  return { listening: c.listening, thinking: c.thinking, speaking: c.speaking, settling: c.idle };
};

/** Whether anything of the Manager is on screen: otherwise no node of it exists. */
export const stageVisible = () => consent() !== null || exchange() || orbOn() || !!card() || fieldText() !== null || inCall();

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
  if (c && (cardCollapsed() || callMinimised())) return { text: cardCaption(c), dim: false };
  return null;
}

function Layer() {
  // The composer's top edge is where a minimised orb sits; views without one use the window's bottom.
  const bottom = () => {
    if (phone && phonePage() !== "conversation") return 84 + safeArea().bottom;
    return (dockHeight() || 72) + 8;
  };
  const cardWidth = () => Math.min(narrowCard() ? 560 : 780, windowSize().width - 24);
  const focusedTop = () => Math.max(24, windowSize().height * 0.24);
  const cardMax = () => Math.max(150, windowSize().height - focusedTop() - ORB_LARGE - 190);
  return (
    <Overlay class="inset-0" style={{ pointerEvents: "box-none" }} testID="manager-stage">
      <If when={callFocused()}>
        {/* The dim is visual only: the header's Manager button stays a global
            control above the call, so it must receive clicks through it. */}
        <View class="absolute inset-0" style={{ backgroundColor: rgba(CALL_SCRIM_ALPHA), pointerEvents: "none" }} testID="manager-dim"
          accessibilityLabel="Manager call" />
        <View class="absolute left-0 right-0 bottom-0" style={{ top: isNarrow() ? 56 : 44 }}
          onPress={() => { if (cardFocused()) dismissCard(); }} />
      </If>
      <If when={callFocused()}>
        {() => (
          <View class="absolute left-0 right-0 items-center gap-2.5" style={{ top: focusedTop(), pointerEvents: "box-none" }} testID="manager-column">
            <Orb size="focused" />
            <Field />
            <Caption align="center" />
            <Hint align="center" />
            <EndButton />
            <If when={!!card() && !cardCollapsed()}>
              {() => <CardPanel width={card()?.type === "diagram" ? Math.min(560, cardWidth()) : cardWidth()} maxHeight={cardMax()} />}
            </If>
          </View>
        )}
      </If>
      <If when={callMinimised()}>
        {() => (
          <View class="absolute right-3 items-end gap-1" style={{ bottom: bottom(), pointerEvents: "box-none" }} testID="manager-column">
            <Orb size="minimised" />
            <Caption align="end" />
            <Hint align="end" />
            <EndButton small />
          </View>
        )}
      </If>
      <If when={consent() !== null}>
        {() => (
          <View class="absolute inset-0 items-center justify-center" style={{ pointerEvents: "box-none" }}>
            <ConsentPanel width={cardWidth()} />
          </View>
        )}
      </If>
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
      <Text class={`${T.meta} pt-1`} testID="manager-consent-hints">enter accept · esc cancel</Text>
    </View>
  );
}

function Caption(p: { align: "center" | "end" }) {
  const line = () => caption();
  return (
    <If when={!!line()}>
      {() => {
        const rise = reducedMotion() ? undefined : createMotion({ enter: { duration: 180, keyframes: { opacity: [0, 1], translateY: [4, 0] } } });
        const tappable = () => !!card() && (cardCollapsed() || callMinimised());
        return (
          <Button onPress={() => { if (tappable()) focusCard(); }} disabled={!tappable()} testID="manager-caption" accessibilityLabel={line()?.text}
            class={`bg-popover/75 rounded-none px-4 py-2 transition-opacity duration-300 ${captionGone() ? "opacity-0" : "opacity-100"} ${FOCUS}`}
            style={{ backdropFilter: "blur(16px)", boxShadow: "0 6px 22px rgb(0 0 0 / 0.22)", ...(rise ? rise.style : {}), maxWidth: Math.min(p.align === "end" ? 360 : 560, windowSize().width - 24) }}>
            <Text style={{ numberOfLines: 2, textAlign: p.align === "end" ? "right" : "center" }} class={`${TS.body} ${line()?.dim ? "text-muted-foreground" : "text-popover-foreground"}`}>{line()?.text ?? ""}</Text>
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

/** The visible key label for a command, or its manifest key. */
const keyOf = (id: string) => {
  const c = byId.get(id);
  return (c && keyLabel(c)) ?? c?.keys?.nsh ?? "";
};

function Hint(p: { align: "center" | "end" }) {
  const text = () => managerHint({
    consent: consent() !== null,
    minimised: callMinimised(),
    field: fieldText() !== null,
    listening: listening(),
    card: cardFocused() || (callMinimised() && !!card()),
    cardMax: rowCount(card()),
    speaking: speaking(),
    thinking: phase() !== "idle" || transcribing(),
  }, keyOf);
  return (
    <If when={!!text()}>
      <Text class={`${T.meta} px-2`} testID="manager-hints"
        style={{ numberOfLines: 1, textAlign: p.align === "end" ? "right" : "center", maxWidth: Math.min(p.align === "end" ? 560 : 620, windowSize().width - 24) }}>{text()}</Text>
    </If>
  );
}

/** The explicit End under the orb: never the same action as minimise, dismiss or stop speaking. */
function EndButton(p: { small?: boolean }) {
  return (
    <Button onPress={() => void run("manager.stop")} testID="manager-end" accessibilityLabel="End the Manager call"
      class={`bg-popover/80 rounded-none px-3 ${p.small ? "py-0.5" : "py-1"} ${FOCUS}`}
      style={{ backdropFilter: "blur(16px)", boxShadow: "0 6px 22px rgb(0 0 0 / 0.22)" }}>
      <Text class={`${TS.body} text-popover-foreground`}>End</Text>
    </Button>
  );
}

function CardPanel(p: { width: number; maxHeight: number }) {
  const current = () => card()!;
  const focused = () => cardFocused();
  const rise = reducedMotion() ? undefined : createMotion({ enter: { duration: 220, keyframes: { opacity: [0, 1], translateY: [8, 0] } } });
  const hints = () => footerHints(current(), { focused: focused(), dismiss: keyOf("manager.card_dismiss") });
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
      <If when={!!hints()}><Text class={`${T.meta} pt-4`} testID="manager-card-hints">{hints()}</Text></If>
    </View>
  );
}

/** The orb: a canvas redrawn each animation frame while visible, with a hit target over it. */
function Orb(p: { size: "focused" | "minimised" }) {
  const reduced = reducedMotion;
  const box = p.size === "focused" ? ORB_LARGE : ORB_SIZE;
  const canvas = p.size === "focused" ? ORB_LARGE_CANVAS : CANVAS;
  const staticMode = (): OrbMode => (listening() ? "listening" : speaking() ? "speaking" : phase() !== "idle" || transcribing() ? "thinking" : "settling");
  const label = () => (listening() ? "Send what you said" : "Talk to the Manager");
  const animating = () => orbOn() || listening() || speaking() || phase() !== "idle" || transcribing();
  return (
    <View style={{ width: box, height: box, alignItems: "center", justifyContent: "center" }} testID="manager-orb-box">
      <Canvas
        testID="manager-orb-canvas"
        style={{ position: "absolute", left: (box - canvas) / 2, top: (box - canvas) / 2, width: canvas, height: canvas, pointerEvents: "none" }}
        draw={(cx, size) => {
          const colors = palette();
          // The reads keep the canvas in step with the stage's signals.
          void inCall(); void minimised(); void card();
          if (reduced()) {
            // A still disc per state; the caption names the state in words.
            void orbOn(); void listening(); void speaking(); void phase(); void transcribing(); void managerError();
            if (inCall()) drawStaticOrb(cx, size.width, size.height, box / 2, managerError() ? "settling" : staticMode(), colors, light());
            return;
          }
          if (animating()) frame();
          drawOrb(cx, size.width, size.height, box / 2, {
            t: animating() ? (Date.now() - visual.startedAt) / 1000 : 0,
            level: animating() ? visual.level : 0,
            presence: animating() || inCall() ? 1 : visual.presence,
            mode: animating() ? visual.mode : "settling", from: animating() ? visual.from : "settling",
            blend: animating() ? visual.blend : 1, still: visual.still, light: light(), colors,
          });
        }}
      />
      <Button onPress={() => void run("manager.talk")} testID="manager-orb" accessibilityLabel={label()} disabled={!inCall()}
        class={`items-center justify-center rounded-full ${FOCUS}`} style={{ width: p.size === "focused" ? box : ORB_HIT, height: p.size === "focused" ? box : ORB_HIT }} />
    </View>
  );
}
