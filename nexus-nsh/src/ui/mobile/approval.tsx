import { Text, View, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { FOCUS } from "../kit";

/** The "tell it what to do instead" text of the approval in view (a phone shows one card at a time). */
const instead = signal("");

/**
 * The phone's approval choices (plans/05 section 5.2): the command in a compact block, `Allow once` and `Always allow`
 * as two big buttons, then a free line "Tell it what to do instead" beside `Deny`. 44 px minimum targets (48 px here).
 * "Always allow" is the desktop's "Allow for session": it grants the same call for the rest of the session.
 */
export function PhoneApprovalChoices(p: {
  id: string; command: string; busy: boolean; live: boolean;
  onAllowOnce: () => void; onAllowSession: () => void; onDeny: (note: string) => void;
}) {
  const off = () => p.busy || !p.live;
  const deny = () => { const note = instead().trim(); instead.set(""); p.onDeny(note); };
  return (
    <View class="gap-3" testID="phone-approval">
      <View class="rounded-[12px] bg-background px-3.5 py-3" testID="waiting-compact">
        <Text style={{ numberOfLines: 4, userSelect: "text" }} class="font-mono text-[13px] leading-5 text-foreground">{p.command}</Text>
      </View>
      <View class="flex-row gap-2">
        <Button onPress={p.onAllowOnce} disabled={off()} testID="allow-once" accessibilityLabel="Allow once"
          class={`flex-1 min-h-12 items-center justify-center rounded-[14px] bg-primary active:opacity-80 disabled:opacity-50 ${FOCUS}`}>
          <Text class="font-(family-name:--font-ui) text-[16px] leading-5 font-semibold text-on-accent">Allow once</Text>
        </Button>
        <Button onPress={p.onAllowSession} disabled={off()} testID="allow-session" accessibilityLabel="Always allow, for this session"
          class={`flex-1 min-h-12 items-center justify-center rounded-[14px] bg-secondary active:bg-selection disabled:opacity-50 ${FOCUS}`}>
          <Text class="font-(family-name:--font-ui) text-[16px] leading-5 font-medium text-foreground">Always allow</Text>
        </Button>
      </View>
      <Text class="font-(family-name:--font-ui) text-[13px] leading-[18px] text-muted-foreground -mt-1" testID="allow-session-note">Always allow covers this session only.</Text>
      <View class="flex-row items-center gap-2">
        <Input value={instead()} onChange={({ value }) => instead.set(value)} onSubmit={deny} submitBehavior="submit"
          placeholder="Tell it what to do instead" accessibilityLabel="Tell it what to do instead" testID="deny-note"
          class="h-12 flex-1 min-w-0 rounded-[14px] bg-background px-3.5 text-[16px] font-(family-name:--font-ui) text-foreground placeholder:text-muted-foreground" />
        <Button onPress={deny} disabled={off()} testID="deny" accessibilityLabel={instead().trim() ? "Deny with your note" : "Deny"}
          class={`min-h-12 px-4 items-center justify-center rounded-[14px] bg-diff-del-bg active:opacity-80 disabled:opacity-50 ${FOCUS}`}>
          <Text class="font-(family-name:--font-ui) text-[16px] leading-5 font-medium text-error">Deny</Text>
        </Button>
      </View>
    </View>
  );
}
