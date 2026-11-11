import { Scroll, Text, View, effect } from "nsh";
import { Button, TextView } from "nsh/base";
import { btw, btwDetail, closeBtw, toggleBtwContext, toggleBtwTranscript } from "../btw";
import { bindOpenKeys } from "../commands";
import { PROSE } from "./kit";
import { Modal, ModalFooter, ModalTitle, modalBodyHeight } from "./modal";

export function BtwModal() {
  effect(() => { if (btw()) return bindOpenKeys({ i: toggleBtwContext, c: toggleBtwTranscript }); });
  return <If when={btw()}>{() => <Modal size="large" label="Side question" testID="btw-modal" onCancel={closeBtw} onOpenChange={(open) => { if (!open) closeBtw(); }}>
    <View class="gap-3">
      <ModalTitle title={`Side question · ${btw()!.question}`} />
      <Button onPress={toggleBtwContext} testID="btw-inherited"><Text class="text-muted-foreground">{btw()!.inherited_open ? "Collapse inherited context" : "Expand inherited context"}</Text></Button>
      <Button onPress={toggleBtwTranscript} testID="btw-context"><Text class="text-muted-foreground">{btw()!.context_open ? "Collapse context" : "Expand context"}</Text></Button>
      <Scroll style={{ height: Math.min(480, modalBodyHeight()) }} testID="btw-progress">
        <TextView selectable links="open" markdown={btwDetail(btw()!)} classes={PROSE} />
      </Scroll>
      <ModalFooter hints={btw()!.running ? "esc close and cancel" : "esc close"} />
    </View>
  </Modal>}</If>;
}
