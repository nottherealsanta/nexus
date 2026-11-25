import { Text, View } from "nsh";
import { Button } from "nsh/base";
import { FOCUS } from "../kit";

/**
 * The phone's approval choices (plans/05 sections 5.2 and 5.5): the command in a compact block, then `Deny`,
 * `Always allow` and `Allow once` as three pills in one row, `Allow once` the primary. There is no separate note field:
 * the composer below the card says "Or tell it what to do instead…", and sending from it (or pressing Deny with text in
 * it) denies with that text as the note. 48 px targets. "Always allow" is the desktop's "Allow for session".
 */
export function PhoneApprovalChoices(p: {
  id: string; command: string; busy: boolean; live: boolean;
  onAllowOnce: () => void; onAllowSession: () => void; onDeny: () => void;
}) {
  const off = () => p.busy || !p.live;
  const pill = `flex-1 min-h-12 items-center justify-center rounded-full disabled:opacity-50 ${FOCUS}`;
  const label = "font-(family-name:--font-ui) text-[15px] leading-5";
  return (
    <View class="gap-3" testID="phone-approval">
      <View class="rounded-[14px] bg-background px-3 py-2.5" testID="waiting-compact">
        <Text style={{ numberOfLines: 4, userSelect: "text" }} class="font-mono text-[14px] leading-5 text-foreground">{p.command}</Text>
      </View>
      <View class="flex-row gap-2">
        <Button onPress={p.onDeny} disabled={off()} testID="deny" accessibilityLabel="Deny"
          class={`${pill} bg-diff-del-bg active:opacity-80`}>
          <Text class={`${label} font-semibold text-error`}>Deny</Text>
        </Button>
        <Button onPress={p.onAllowSession} disabled={off()} testID="allow-session" accessibilityLabel="Always allow, for this session"
          class={`${pill} bg-phone-control active:opacity-70`}>
          <Text style={{ numberOfLines: 1 }} class={`${label} font-medium text-foreground`}>Always allow</Text>
        </Button>
        <Button onPress={p.onAllowOnce} disabled={off()} testID="allow-once" accessibilityLabel="Allow once"
          class={`${pill} bg-foreground active:opacity-80`}>
          <Text style={{ numberOfLines: 1 }} class={`${label} font-semibold text-background`}>Allow once</Text>
        </Button>
      </View>
      <Text class="font-(family-name:--font-ui) text-[12px] leading-4 text-muted-foreground text-center" testID="allow-session-note">Always allow covers this session only.</Text>
    </View>
  );
}
