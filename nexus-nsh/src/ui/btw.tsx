import { Scroll, Text, View, effect } from "nsh";
import { Button, TextView } from "nsh/base";
import { btw, btwParts, closeBtw, toggleBtwContext, toggleBtwTranscript } from "../btw";
import { bindOpenKeys } from "../commands";
import { phone } from "../store";
import { PROSE, T } from "./kit";
import { Modal, ModalFooter, ModalTitle, modalBodyHeight } from "./modal";

export function BtwModal() {
  const parts = () => btwParts(btw()!);
  effect(() => { if (btw()) return bindOpenKeys({ i: toggleBtwContext, c: toggleBtwTranscript }); });
  return <If when={btw()}>{() => <Modal size="large" label="Side question" testID="btw-modal" onCancel={closeBtw} onOpenChange={(open) => { if (!open) closeBtw(); }}>
    <View class="gap-3">
      <ModalTitle title={`Side question · ${btw()!.question}`} />
      <Scroll style={{ height: Math.min(480, modalBodyHeight()) }} testID="btw-progress">
        <View class="gap-3">
          <Button onPress={toggleBtwContext} testID="btw-inherited" class="self-start"><Text class={T.base}>{parts().inheritedLine}</Text></Button>
          <If when={!!parts().inheritedBody}><TextView selectable links="open" markdown={parts().inheritedBody} classes={PROSE} /></If>
          <TextView selectable links="open" markdown={parts().middle} classes={PROSE} />
          <Button onPress={toggleBtwTranscript} testID="btw-context" class="self-start"><Text class={T.base}>{parts().contextLine}</Text></Button>
          <If when={!!parts().contextBody}><TextView selectable links="open" markdown={parts().contextBody} classes={PROSE} /></If>
          <Text class={T.base}>{parts().tail}</Text>
        </View>
      </Scroll>
      <ModalFooter hints={[...(phone ? [] : ["↑↓ scroll"]), btw()!.running ? "esc close and cancel" : "esc close"]} />
    </View>
  </Modal>}</If>;
}
