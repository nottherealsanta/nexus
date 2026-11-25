// The phone's tool-group summary (plans/05 section 5.1, user 2026-11-23): one compact, always-collapsed and
// non-expandable row per group, in the timeline's original order. It says what the group is doing and how much it
// holds, never an individual tool's command, path, output or diff. A waiting group's approval card is separate, above
// the composer, so it stays usable.
import { Text, View } from "nsh";
import { Icon, Spinner } from "../kit";
import { activityCountsText, phoneActivityStatus, phoneChildStatus, type GroupCount } from "../../mobile";

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
    <View class="py-0.5" testID="phone-activity">
      {/* No card (user, 2026-11-24): the status badge sits in the session page's left gutter, the words on the text edge. */}
      <View accessible accessibilityLabel={label()} class="flex-row items-center gap-2 min-h-8 -ml-7 py-1">
        <View class="w-7 shrink-0 items-center" accessible={false}><View class={`size-5 items-center justify-center rounded-full ${BADGE[status().status]}`}>
          <If when={status().status === "running"} else={<Text class={`text-[12px] leading-4 ${WORD[status().tone]}`}>{GLYPH[status().status]}</Text>}>
            {/* The kit spinner holds its still glyph under reduced motion. */}
            <Spinner class={`text-[12px] leading-4 ${WORD[status().tone]}`} label={status().word} />
          </If>
        </View></View>
        <Text style={{ numberOfLines: 1 }} class="-ml-2 flex-1 min-w-0 font-(family-name:--font-ui) text-[14px] leading-5 text-foreground-secondary" testID="phone-activity-summary">{summary()}</Text>
        <If when={!!p.diff()}>
          {() => <Text class="shrink-0 text-[11px] leading-4 font-(family-name:--font-ui) tabular-nums"><Text class="text-success">{`+${p.diff()!.add}`}</Text>{" "}<Text class="text-error">{`−${p.diff()!.del}`}</Text></Text>}
        </If>
        <Text style={{ numberOfLines: 1 }} testID={`phone-activity-${status().status}`}
          class={`shrink-0 font-(family-name:--font-ui) text-[11px] leading-4 font-semibold ${status().status === "done" && p.denied() > 0 ? "text-error" : WORD[status().tone]}`}>
          {status().status === "done" && p.denied() > 0 ? `${p.denied()} denied` : status().word}
        </Text>
      </View>
    </View>
  );
}

const CHILD_TONE: Record<ReturnType<typeof phoneChildStatus>["status"], string> = {
  waiting: "text-warning", running: "text-info", failed: "text-error", done: "text-success", idle: "text-muted-foreground",
};
const CHILD_BADGE: Record<ReturnType<typeof phoneChildStatus>["status"], string> = {
  waiting: "bg-warning/15", running: "bg-info/15", failed: "bg-error/15", done: "bg-success/15", idle: "bg-phone-control",
};
const CHILD_GLYPH = { waiting: "◆", failed: "!", done: "✓", idle: "•" } as const;

/**
 * A subagent on the phone (user, 2026-11-24): one tappable card in timeline order that opens the child session. It
 * names the agent and its task, shows a live status (working, needs you, failed, done), the model and how long it has
 * run, and ends in a chevron. A background start says so. No tool detail from inside the child is shown here.
 */
export function PhoneChildRow(p: {
  agent: () => string; title: () => string; state: () => "running" | "done" | "idle"; outcome: () => string;
  meta: () => string; background: () => boolean; childId: () => string | undefined; onOpen: () => void;
}) {
  const st = () => phoneChildStatus({ state: p.state(), outcome: p.outcome() });
  const name = () => p.agent().charAt(0).toUpperCase() + p.agent().slice(1);
  const label = () => `Subagent ${name()}: ${p.title() || "untitled"}. ${st().word}${p.meta() ? `, ${p.meta()}` : ""}${p.childId() ? ". Opens its session" : ""}`;
  return (
    <View class="py-1" testID="phone-child">
      {/* No card (user, 2026-11-24): the badge sits in the left gutter, the agent, task and meta on the text edge. */}
      <View role="button" focusable onPress={() => { if (p.childId()) p.onOpen(); }} accessibilityLabel={label()} testID="child-card"
        class="flex-row items-start min-h-12 -ml-7 py-1 active:opacity-60">
        <View accessible={false} class="w-7 shrink-0 items-center pt-0.5"><View class={`size-5 items-center justify-center rounded-full ${CHILD_BADGE[st().status]}`}>
          <If when={st().status === "running"} else={<Text class={`text-[12px] leading-4 font-semibold ${CHILD_TONE[st().status]}`}>{CHILD_GLYPH[st().status as keyof typeof CHILD_GLYPH]}</Text>}>
            <Spinner class={`text-[12px] leading-4 ${CHILD_TONE.running}`} label="Working" />
          </If>
        </View></View>
        <View class="flex-1 min-w-0">
          <View class="flex-row items-center gap-1.5 min-w-0">
            <Text style={{ numberOfLines: 1 }} class="shrink-0 font-(family-name:--font-ui) text-[12px] leading-4 font-semibold tracking-[0.3px] text-muted-foreground" testID="phone-child-agent">{name().toUpperCase()}</Text>
            <If when={p.background()}><Text class="shrink-0 font-(family-name:--font-ui) text-[12px] leading-4 text-muted-foreground">· background</Text></If>
            <View class="flex-1" />
            <Text style={{ numberOfLines: 1 }} class={`shrink-0 font-(family-name:--font-ui) text-[12px] leading-4 font-semibold ${CHILD_TONE[st().status]}`} testID={`phone-child-${st().status}`}>{st().word}</Text>
          </View>
          <Text style={{ numberOfLines: 1 }} class="font-(family-name:--font-ui) text-[15px] leading-[21px] font-medium text-foreground" testID="phone-child-title">{p.title() || "Subagent"}</Text>
          <If when={!!p.meta() || (st().status === "failed" && !!p.outcome())}>
            <Text style={{ numberOfLines: 1 }} class="font-(family-name:--font-ui) text-[12px] leading-4 text-muted-foreground tabular-nums" testID="phone-child-meta">{st().status === "failed" ? p.outcome() : p.meta()}</Text>
          </If>
        </View>
        <If when={!!p.childId()}><View class="self-center pl-1"><Icon name="chevron-right" size={16} class="tint-muted-foreground" /></View></If>
      </View>
    </View>
  );
}
