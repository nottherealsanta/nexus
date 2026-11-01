// The full-size view of a composer or timeline attachment (D13 modal recipe).
import { Image, View } from "nsh";
import { Modal, ModalFooter, ModalTitle } from "./modal";
import { imagePreview } from "../store";

export function ImagePreview() {
  return (
    <If when={!!imagePreview()}>
      {() => {
        const p = imagePreview()!;
        return (
          <Modal width={880} top="top-[8%]" label={`Image preview: ${p.name}`} testID="image-preview"
            onCancel={() => imagePreview.set(null)} onOpenChange={(open) => { if (!open) imagePreview.set(null); }}>
            <View class="gap-3">
              <ModalTitle title={p.name} />
              <View class="items-center justify-center rounded-(--radius-card) bg-card p-2">
                <Image src={p.src} style={{ width: "100%", height: 460, objectFit: "contain" }} accessibilityLabel={p.name} />
              </View>
              <ModalFooter hints="esc close" />
            </View>
          </Modal>
        );
      }}
    </If>
  );
}
