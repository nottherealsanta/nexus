// The full-size view of a composer or timeline attachment (D13 modal recipe).
import { Image, Text, View } from "nsh";
import { Btn, TS } from "./kit";
import { Modal, modalBodyHeight, ModalFooter } from "./modal";
import { imagePreview } from "../store";

export function ImagePreview() {
  return (
    <If when={!!imagePreview()}>
      {() => {
        const p = imagePreview()!;
        return (
          <Modal size="large" label={`Image preview: ${p.name}`} testID="image-preview"
            onCancel={() => imagePreview.set(null)} onOpenChange={(open) => { if (!open) imagePreview.set(null); }}>
            <View class="gap-3">
              <Text style={{ numberOfLines: 1 }} class={`${TS.title} min-h-8 font-semibold text-popover-foreground`}>{p.name}</Text>
              <View class="items-center justify-center rounded-(--radius-card) bg-card p-2">
                <Image src={p.src} style={{ width: "100%", height: Math.min(460, modalBodyHeight(64)), objectFit: "contain" }} accessibilityLabel={p.name} />
              </View>
              <View class="flex-row items-center gap-3">
                <View class="flex-1"><ModalFooter hints="esc close" /></View>
                <Btn label="Close" kind="ghost" testID="image-preview-close" onPress={() => imagePreview.set(null)} />
              </View>
            </View>
          </Modal>
        );
      }}
    </If>
  );
}
