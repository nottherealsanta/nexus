import { effect, Text } from "nsh";
import { Button } from "nsh/base";
import type { CoreEvent, TurnSummary } from "../core/api";
import { contextDetail, isNarrow, m, model, streamRev } from "../store";
import { FOCUS, T, TS } from "./kit";
import { fmtDuration } from "./timeline";

type RetryNotice = { key: string; time: number; text: string; detail?: string; error?: boolean };

/** Live retry events are replaced by their saved attempt summaries when a turn closes. */
export function retryNotices(turns: TurnSummary[], scratch: CoreEvent[]): RetryNotice[] {
  const notices: RetryNotice[] = [];
  for (const turn of turns) {
    if (turn.status === "running") continue;
    for (const request of turn.requests ?? []) {
      const attempts = request.attempts ?? [];
      attempts.forEach((attempt, i) => {
        const next = attempts[i + 1];
        const time = attempt.finished ?? attempt.started ?? request.started ?? turn.started;
        if (next) {
          const delay = typeof next.delay === "number" ? ` in ${fmtDuration(next.delay)}` : "";
          notices.push({ key: `retry-${request.id}-${attempt.n}`, time, text: `provider retry attempt ${next.n}${delay}`, detail: attempt.error ?? undefined });
        } else if (i > 0 && attempt.status === "failed") {
          notices.push({ key: `retry-${request.id}-end`, time, text: "provider retries exhausted", detail: attempt.error ?? undefined, error: true });
        }
      });
    }
  }
  for (const event of scratch) {
    const data = event.data ?? {};
    const base = { key: `retry-event-${event.id}`, time: event.time };
    if (event.type === "retry_scheduled") notices.push({ ...base, text: `provider retry ${data.attempt ?? ""}/${data.max_attempts ?? ""} in ${typeof data.delay === "number" ? fmtDuration(data.delay) : "?"}`, detail: data.reason });
    else if (event.type === "retry_started") notices.push({ ...base, text: `provider retry started (attempt ${data.attempt ?? ""})` });
    else if (event.type === "retry_exhausted") notices.push({ ...base, text: "provider retries exhausted", detail: data.reason, error: true });
  }
  return notices.sort((a, b) => a.time - b.time);
}

export function activeRetry(turns: TurnSummary[], scratch: CoreEvent[], pending: boolean, running: boolean): RetryNotice | null {
  if (pending) return retryNotices([], scratch).at(-1) ?? null;
  if (running) return null;
  const last = turns.at(-1);
  if (last?.status !== "failed") return null;
  const request = last.requests?.at(-1);
  const attempts = request?.attempts ?? [];
  if (request?.status !== "failed" || attempts.length < 2 || attempts.at(-1)?.status !== "failed") return null;
  return retryNotices([last], []).at(-1) ?? null;
}

export function RetryNotices() {
  const notices = () => { m(); const selected = model.selected; return selected ? retryNotices(selected.turns, selected.scratch) : []; };
  const latest = () => { m(); streamRev(); const selected = model.selected; return selected ? activeRetry(selected.turns, selected.scratch, !!selected.retryPending, model.running.has(selected.sessionId)) : null; };
  const history = () => notices().map((n) => `${n.text}${n.detail ? ` · ${n.detail}` : ""}`).join("\n\n");
  let detailSession: string | null = null;
  const showDetails = () => { detailSession = model.selected?.sessionId ?? null; contextDetail.set({ title: "Provider retry history", detail: history() }); };
  effect(() => {
    const detail = contextDetail();
    if (!detailSession || detail?.title !== "Provider retry history") return;
    const text = history();
    if (model.selected?.sessionId !== detailSession) { detailSession = null; contextDetail.set(null); }
    else if (detail.detail !== text) contextDetail.set({ ...detail, detail: text });
  });
  return <If when={!!latest()}>
      <Button onPress={showDetails} testID="retry-notices" accessibilityLabel={`${latest()?.text ?? "Provider retry"}, show retry history`}
        class={`flex-row items-center gap-2 border-b border-border ${isNarrow() ? "min-h-11" : "min-h-10"} hover:bg-accent ${FOCUS}`}>
        <Text class={`${TS.base} ${latest()?.error ? "text-error" : "text-warning"}`}>▪</Text>
        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${T.sec}`}><If when={!latest()?.key.startsWith("retry-event-")}>Previous · </If><Text class={latest()?.error ? "text-error" : "text-foreground-secondary"}>{latest()?.text}</Text>{latest()?.detail ? ` · ${latest()!.detail}` : ""}</Text>
        <Text class={`${T.meta} shrink-0`}>Details</Text>
      </Button>
  </If>;
}
