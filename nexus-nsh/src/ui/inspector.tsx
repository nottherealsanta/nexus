import { Scroll, Text, View, Clipboard, windowSize } from "nsh";
import { Button } from "nsh/base";
import { Modal, ModalFooter, ModalTitle, modalBodyHeight } from "./modal";
import { Btn, FOCUS, T } from "./kit";
import { closeInspector, inspectSession, inspector, inspectorInfo, inspectorLane, inspectorPan, inspectorViews, inspectorZoom, timingWindow } from "../inspector";
import type { Lane } from "../inspector";

const number = (n: number | null | undefined) => n == null ? "unknown" : n.toLocaleString(undefined, { maximumFractionDigits: 1 });
const tone = (lane: Lane) => /failed|error|interrupted/.test(lane.status) ? "bg-error" : lane.running ? "bg-warning" : lane.kind === "model" ? "bg-info" : lane.kind === "tool" ? "bg-success" : "bg-muted-foreground";
const duration = (lane: Lane) => lane.end_unknown ? "end unknown" : `${(lane.end - lane.start).toFixed(2)}s${lane.running ? " · live" : ""}`;

export function InspectorModal() {
  return <If when={!!inspector()}>{() => <InspectorBody />}</If>;
}
function InspectorBody() {
  const state = () => inspector()!;
  const data = () => state().data;
  const cache = () => data()?.cache;
  const lanes = () => data()?.lanes ?? [];
  const window = () => timingWindow(lanes(), inspectorZoom(), inspectorPan());
  const selected = () => lanes().find((l) => l.id === inspectorLane());
  const small = () => windowSize().width < 600;
  const labelWidth = () => small() ? 112 : 176;
  const stamp = (seconds: number) => `+${seconds.toFixed(small() ? 1 : 2)}s`;
  const load = (view = state().view, page = state().page) => void inspectSession(state().session, view, page);
  return <Modal size="large" tall label="Session inspector" testID="inspector" onCancel={closeInspector} onOpenChange={(open) => { if (!open) closeInspector(); }}>
    <View class="gap-2">
      <ModalTitle title="Session inspector" />
      <Text class={T.meta}>{state().session}</Text>
      <View class="flex-row flex-wrap gap-2">
        <Each of={inspectorViews} key={(v) => v}>{(v) => <Btn label={v()} onPress={() => load(v(), 0)} testID={`inspect-${v()}`} />}</Each>
      </View>
      <Text class={T.sec}>{`${state().view} · Input ${number(cache()?.input)} · Cached ${number(cache()?.cached)} · Uncached ${number(cache()?.uncached)} · Hit ${cache()?.hit_percent == null ? "unknown" : `${number(cache()?.hit_percent)}%`} · Writes unknown`}</Text>
      <If when={state().loading}><Text class={T.sec}>Loading inspector…</Text></If>
      <If when={!!state().error}><Text class="text-error">{state().error}</Text></If>
      <Scroll style={{ height: Math.min(520, modalBodyHeight(small() ? 240 : 140)) }} testID="inspector-body">
        <View class="gap-2">
          <Each of={(data()?.notes ?? []).filter((n) => inspectorInfo() || !n.startsWith("Cache"))} key={(n) => n}>{(n) => <Text class={T.meta}>{n()}</Text>}</Each>
          <If when={state().view === "Timing"} else={<Text class="font-mono text-xs text-foreground">{data()?.text ?? ""}</Text>}>
            <View class="flex-row flex-wrap gap-2">
              <Btn label="Zoom in" onPress={() => inspectorZoom.set(Math.min(64, inspectorZoom() * 2))} />
              <Btn label="Zoom out" onPress={() => inspectorZoom.set(Math.max(1, inspectorZoom() / 2))} />
              <Btn label="Earlier" onPress={() => inspectorPan.set(Math.max(0, inspectorPan() - 0.1))} />
              <Btn label="Later" onPress={() => inspectorPan.set(Math.min(1, inspectorPan() + 0.1))} />
              <Text class={T.meta}>{`${inspectorZoom()}× · model / tool / turn / attempt · yellow live · red failed`}</Text>
            </View>
            <If when={lanes().length > 0} else={<Text class={T.sec}>No retained execution timings.</Text>}>
              <View class="flex-row gap-2"><Text style={{ width: labelWidth() }} class="text-xs text-muted-foreground">Session / operation</Text><View class="flex-1 flex-row justify-between"><Text class={T.meta}>{stamp(window().start - window().origin)}</Text><If when={!small()}><Text class={T.meta}>{stamp(window().start + window().span / 2 - window().origin)}</Text></If><Text class={T.meta}>{stamp(window().start + window().span - window().origin)}</Text></View></View>
              <Each of={lanes()} key={(l) => l.id}>{(l) => {
                const left = () => Math.max(0, Math.min(1, (l().start - window().start) / window().span));
                const right = () => Math.max(0, Math.min(1, (l().end - window().start) / window().span));
                const visible = () => l().end >= window().start && l().start <= window().start + window().span;
                return <Button onPress={() => inspectorLane.set(inspectorLane() === l().id ? null : l().id)} accessibilityLabel={`${l().session}, ${l().kind}, ${l().label}, ${l().status}, ${duration(l())}`} testID={`lane-${l().id}`} class={`flex-row items-center min-h-9 gap-2 ${inspectorLane() === l().id ? "bg-selection" : ""} ${FOCUS}`}>
                  <View style={{ width: labelWidth() }}><Text class="text-xs text-foreground" style={{ numberOfLines: 1 }}>{`${l().session} · ${l().kind}`}</Text><Text class={T.meta} style={{ numberOfLines: 1 }}>{l().label}</Text></View>
                  <View class="flex-1 h-6 bg-card relative overflow-hidden">
                    <View class="absolute left-1/2 top-0 bottom-0 border-l border-border" />
                    <If when={visible()}><View style={{ left: `${left() * 100}%`, width: `${Math.max(0.4, (right() - left()) * 100)}%` }} class={`absolute h-3 top-1 ${tone(l())}`} /></If>
                    <Text class="absolute right-1 text-xs text-foreground">{duration(l())}</Text>
                  </View>
                </Button>;
              }}</Each>
              <If when={!!selected()}><Text class="font-mono text-xs text-foreground">{selected()?.detail ?? ""}</Text></If>
            </If>
          </If>
        </View>
      </Scroll>
      <View class="flex-row flex-wrap gap-3">
        <Btn label="Refresh" onPress={() => load()} testID="inspect-refresh" />
        <Btn label="Cache info" onPress={() => inspectorInfo.set(!inspectorInfo())} />
        <Btn label={selected() ? "Copy details" : "Copy view"} onPress={() => { void Clipboard.writeText(selected()?.detail ?? (state().view === "Timing" ? JSON.stringify(data(), null, 2) : data()?.text ?? "")); }} />
        <If when={state().page > 0}><Btn label="Newer" onPress={() => load(state().view, state().page - 1)} /></If>
        <If when={!!data()?.more}><Btn label="Older" onPress={() => load(state().view, state().page + 1)} testID="inspect-older" /></If>
        <Text class={T.meta}>{`Page ${state().page + 1}`}</Text>
      </View>
      <ModalFooter hints={["esc close", "select a timing bar for details"]} />
    </View>
  </Modal>;
}
