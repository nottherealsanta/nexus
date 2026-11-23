// The phone's tool-group summary (plans/05 section 5.1, user 2026-11-23): one compact, always-collapsed and
// non-expandable row per group, in the timeline's original order. It says what the group is doing and how much it
// holds, never an individual tool's command, path, output or diff. A waiting group's approval card is separate, above
// the composer, so it stays usable.
import { Text, View } from "nsh";
import { Spinner } from "../kit";
import { activityCountsText, phoneActivityStatus, type GroupCount } from "../../mobile";

type Status = ReturnType<typeof phoneActivityStatus>;

const BADGE: Record<Status["status"], string> = {
  waiting: "bg-warning/15",
  running: "bg-info/15",
  done: "bg-success/15",
};
const GLYPH: Record<Status["status"], string> = { waiting: "◆", running: "◐", done: "✓" };
const WORD: Record<Status["tone"], string> = { warning: "text-warning", info: "text-info", success: "text-success" };

export function PhoneActivityRow(p: {
  counts: () => readonly GroupCount[];
  running: () => boolean;
  waiting: () => boolean;
  denied: () => number;
  diff: () => { add: number; del: number } | undefined;
}) {
  const summary = () => activityCountsText(p.counts());
  const status = () => phoneActivityStatus({ running: p.running(), waiting: p.waiting() });
  const label = () => {
    const denied = p.denied();
    return `Tool activity: ${summary()}. ${status().word}${denied ? `, ${denied} denied` : ""}.`;
  };
  return (
    <View class="px-1 py-1" testID="phone-activity">
      <View accessible accessibilityLabel={label()} class="flex-row items-center gap-2.5 min-h-11 rounded-[12px] bg-raised px-3 py-2">
        <View accessible={false} class={`size-6 shrink-0 items-center justify-center rounded-full ${BADGE[status().status]}`}>
          <If when={status().status === "running"} else={<Text class={`text-[13px] leading-5 ${WORD[status().tone]}`}>{GLYPH[status().status]}</Text>}>
            {/* The kit spinner holds its still glyph under reduced motion. */}
            <Spinner class={`text-[13px] leading-5 ${WORD[status().tone]}`} label={status().word} />
          </If>
        </View>
        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 font-(family-name:--font-ui) text-[14px] leading-5 font-medium text-foreground-secondary" testID="phone-activity-summary">{summary()}</Text>
        <If when={!!p.diff()}>
          {() => <Text class="shrink-0 text-[12px] leading-5 font-(family-name:--font-ui) tabular-nums"><Text class="text-success">{`+${p.diff()!.add}`}</Text>{" "}<Text class="text-error">{`−${p.diff()!.del}`}</Text></Text>}
        </If>
        <Text style={{ numberOfLines: 1 }} testID={`phone-activity-${status().status}`}
          class={`shrink-0 font-(family-name:--font-ui) text-[12px] leading-5 font-semibold ${status().status === "done" && p.denied() > 0 ? "text-error" : WORD[status().tone]}`}>
          {status().status === "done" && p.denied() > 0 ? `${p.denied()} denied` : status().word}
        </Text>
      </View>
    </View>
  );
}
