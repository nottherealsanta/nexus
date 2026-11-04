// The Manager's current hidden context (docs/manager.md): a read-only, scrollable
// debug modal opened by the header button. Core formats the text, so it matches the
// TUI word for word. It is not a timeline or a chat, and it sits above the call stage.
import { Scroll, Text, View, windowSize } from "nsh";
import { managerContext } from "../../core/manager";
import { Modal, ModalFooter, ModalTitle } from "../modal";
import { TS } from "../kit";

export function ManagerContextModal() {
  return (
    <If when={managerContext()}>{() => (
      <Modal width={760} top="top-[8%]" label="Manager context" testID="manager-context"
        onCancel={() => managerContext.set(null)} onOpenChange={(open) => { if (!open) managerContext.set(null); }}>
        <View class="gap-3">
          <ModalTitle title={managerContext()?.title ?? "Manager context"} esc={false} />
          <Scroll style={{ maxHeight: Math.max(240, Math.round(windowSize().height * 0.62)) }}>
            <Text style={{ userSelect: "text" }} class={`${TS.mono} text-foreground-secondary`} testID="manager-context-text">
              {managerContext()?.text ?? ""}
            </Text>
          </Scroll>
          <ModalFooter hints="esc close" />
        </View>
      </Modal>
    )}</If>
  );
}
