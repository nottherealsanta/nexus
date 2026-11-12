import { Text, View, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { FOCUS, T, TS } from "../kit";

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
      <View class="rounded-xl bg-background px-3 py-2" testID="waiting-compact">
        <Text style={{ numberOfLines: 4, userSelect: "text" }} class={`${T.mono} text-foreground`}>{p.command}</Text>
      </View>
      <View class="flex-row gap-2">
        <Button onPress={p.onAllowOnce} disabled={off()} testID="allow-once" accessibilityLabel="Allow once"
          class={`flex-1 min-h-12 items-center justify-center rounded-xl bg-primary active:opacity-80 disabled:opacity-50 ${FOCUS}`}>
          <Text class={`${TS.body} font-semibold text-on-accent`}>Allow once</Text>
        </Button>
        <Button onPress={p.onAllowSession} disabled={off()} testID="allow-session" accessibilityLabel="Always allow, for this session"
          class={`flex-1 min-h-12 items-center justify-center rounded-xl border border-border-strong bg-secondary active:bg-selection disabled:opacity-50 ${FOCUS}`}>
          <Text class={`${TS.body} font-semibold text-foreground`}>Always allow</Text>
        </Button>
      </View>
      <Text class={`${T.meta} -mt-1`} testID="allow-session-note">Always allow covers this session only.</Text>
      <View class="flex-row items-center gap-2">
        <Input value={instead()} onChange={({ value }) => instead.set(value)} onSubmit={deny} submitBehavior="submit"
          placeholder="Tell it what to do instead" accessibilityLabel="Tell it what to do instead" testID="deny-note"
          class={`h-12 flex-1 min-w-0 rounded-xl bg-background px-3 text-[16px] font-(family-name:--font-ui) text-foreground placeholder:text-muted-foreground`} />
        <Button onPress={deny} disabled={off()} testID="deny" accessibilityLabel={instead().trim() ? "Deny with your note" : "Deny"}
          class={`min-h-12 px-4 items-center justify-center rounded-xl border border-border-strong bg-secondary active:bg-selection disabled:opacity-50 ${FOCUS}`}>
          <Text class={`${TS.body} font-semibold text-error`}>Deny</Text>
        </Button>
      </View>
    </View>
  );
}
