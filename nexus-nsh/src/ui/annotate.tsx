// Drawing on a composer image: the picture, the pen colours and actions, then the keys.
import { Canvas, Image, PanResponder, Text, View, keymap, nativeModule, signal } from "nsh";
import { Button } from "nsh/base";
import type { AnnotateModule } from "nexus/native";
import { Modal, modalBodyHeight, ModalFooter, ModalTitle } from "./modal";
import { FOCUS, T } from "./kit";
import { annotating, toast } from "../store";
import { imageDrafts } from "../images";
import { annotationInks } from "../gen/theme";
import { drawStroke, extend, fit, normalise, withinMessageLimit, type Stroke } from "../annotate-logic";

export function Annotate() {
  return <If when={!!annotating()}>{() => <AnnotateBody />}</If>;
}

function AnnotateBody() {
  const target = annotating()!;
  const draft = () => (imageDrafts()[target.key] ?? []).find((image) => image.id === target.id);
  const source = draft();
  const name = source?.name ?? "";
  const src = source?.data_url ?? "";
  const strokes = signal<Stroke[]>([]);
  const ink = signal(0);
  const natural = signal<{ width: number; height: number } | null>(null);
  const room = signal({ width: 0, height: 0 });
  const close = () => annotating.set(null);
  const rect = () => { const size = natural(); return size && room().width > 0 ? fit(size, room()) : null; };
  const undo = () => strokes.update((all) => all.slice(0, -1));
  const clear = () => strokes.set([]);
  const done = () => {
    const marks = strokes();
    if (!marks.length) { close(); return; }
    try {
      const native = nativeModule<{ Annotate: AnnotateModule }>("nexus/native").Annotate;
      const dataUrl = native.flatten(src, marks);
      const all = imageDrafts()[target.key] ?? [];
      if (!withinMessageLimit(all.map((image) => image.id === target.id ? dataUrl.length : image.data_url.length))) {
        toast("warning", "At most eight images and 8 MiB per message");
        return;
      }
      imageDrafts.update((drafts) => ({
        ...drafts,
        [target.key]: (drafts[target.key] ?? []).map((image) => image.id === target.id ? { ...image, data_url: dataUrl } : image),
      }));
      close();
    } catch (error) {
      toast("error", error instanceof Error ? error.message : String(error));
    }
  };
  annotationInks.forEach((_, index) => keymap.bind(String(index + 1), () => ink.set(index), { when: "always" }));
  keymap.bind("u", undo, { when: "always" });
  keymap.bind("x", clear, { when: "always" });
  keymap.bind("enter", done, { when: "always" });

  const at = (event: { locationX: number; locationY: number }) => {
    const size = rect();
    return normalise(event.locationX, event.locationY, size ?? { width: 1, height: 1 });
  };
  const pen = PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onMoveShouldSetPanResponder: () => true,
    onPanResponderGrant: (event) => {
      strokes.update((all) => [...all, { color: annotationInks[ink()], points: [at(event)] }]);
    },
    onPanResponderMove: (event) => {
      strokes.update((all) => all.length ? [...all.slice(0, -1), extend(all[all.length - 1], at(event))] : all);
    },
  });
  // Keep the picture and pen controls inside the shared modal height cap.
  const pictureHeight = () => Math.min(460, modalBodyHeight(60));

  return (
    <Modal size="large" label={`Mark up ${name}`} testID="annotate" closeOnBackdropPress={false}
      onCancel={close} onOpenChange={(open) => { if (!open) close(); }}>
      <View class="gap-3">
        <ModalTitle title={`Mark up ${name}`} />
        <View class="rounded-(--radius-card) bg-card p-2">
          <View style={{ height: pictureHeight() }} onLayout={(event) => {
            room.set({ width: event.bounds.size.width, height: event.bounds.size.height });
          }}>
            <If when={!natural()}>
              <Image src={src} style={{ width: 1, height: 1, opacity: 0 }} accessibilityLabel={name}
                onLoad={(event) => natural.set(event.size)} />
            </If>
            <If when={!!rect()}>
              {() => (
                <View testID="annotate-picture" {...pen.panHandlers}
                  style={{ position: "absolute", left: rect()!.x, top: rect()!.y, width: rect()!.width, height: rect()!.height, cursor: "crosshair" }}>
                  <Image src={src} style={{ position: "absolute", left: 0, top: 0, width: rect()!.width, height: rect()!.height }} accessibilityLabel={name} />
                  <Canvas testID="annotate-canvas" style={{ position: "absolute", left: 0, top: 0, width: rect()!.width, height: rect()!.height, pointerEvents: "none" }}
                    draw={(cx, size) => { for (const stroke of strokes()) drawStroke(cx, stroke, size.width, size.height); }} />
                </View>
              )}
            </If>
          </View>
        </View>
        <View class="flex-row items-center gap-2">
          <Each of={annotationInks.map((color, index) => ({ color, index }))} key={(entry) => entry.index}>
            {(entry) => (
              <Button onPress={() => ink.set(entry().index)} testID={`annotate-ink-${entry().index + 1}`} accessibilityLabel={`Pen colour ${entry().index + 1}`} selected={ink() === entry().index}
                class={`size-8 items-center justify-center rounded-full ${ink() === entry().index ? "border-2 border-foreground" : ""} ${FOCUS}`}>
                <View class="size-5 rounded-full" style={{ backgroundColor: entry().color }} />
              </Button>
            )}
          </Each>
          <View class="flex-1" />
          <Action label="Undo" enabled={strokes().length > 0} onPress={undo} />
          <Action label="Clear" enabled={strokes().length > 0} onPress={clear} />
          <Action label="Done" enabled onPress={done} />
        </View>
        <ModalFooter hints="1-4 colour · u undo · x clear · enter done · esc cancel" />
      </View>
    </Modal>
  );
}

function Action(p: { label: string; enabled: boolean; onPress: () => void }) {
  return (
    <Button onPress={p.onPress} disabled={!p.enabled} testID={`annotate-${p.label.toLowerCase()}`} accessibilityLabel={p.label}
      class={`min-h-8 justify-center rounded-(--radius) px-3 ${p.enabled ? "hover:bg-accent" : ""} ${FOCUS}`}>
      <Text class={p.enabled ? "text-[13px] leading-5 font-medium text-popover-foreground" : T.meta}>{p.label}</Text>
    </Button>
  );
}
