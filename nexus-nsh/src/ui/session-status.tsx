import { Text, View } from "nsh";
import { Button } from "nsh/base";
import { run, term } from "../commands";
import { PENDING_SLOW_MS, m, model, pendingFailed, pendingTurns, phone, tick, viewed } from "../store";
import { diag, summaryDiags } from "./timeline";

export function SessionStatus() {
  // A refusal, or no answer from Core after 10 s (plan 37): the reason in the strip, with Retry.
  const refused = () => { m(); const id = viewed()?.id; return id ? pendingFailed()[id] ?? null : null; };
  const slow = () => { m(); tick(); const id = viewed()?.id; const p = id ? pendingTurns()[id] : undefined; return !!p?.key && !p.queued && !p.accepted && Date.now() - p.started >= PENDING_SLOW_MS; };
  const outcome = () => {
    m();
    if (viewed()?.status === "busy" || refused() || slow()) return null;
    const turn = model.selected?.turns.at(-1);
    const scratch = (model.selected?.scratch ?? []).filter((e) => ["session.error", "retry_scheduled", "retry_started", "retry_exhausted", "turn.recovered"].includes(e.type) || (["permission.replied", "question.replied"].includes(e.type) && e.data?.outcome === "cancelled" && e.data?.origin !== "manager")).map(diag).filter((r) => r != null);
    const rows = scratch.length ? scratch.map((row) => ({ row })) : turn ? summaryDiags(turn) : [];
    const latest = rows.at(-1)?.row;
    return latest ? { ...latest, text: latest.text.split("\n")[0], detail: latest.detail && !latest.text.includes(latest.detail) ? latest.detail : undefined, count: rows.length } : null;
  };
  return <>
    <If when={refused() || slow()}>
      <View class="flex-row items-center gap-3 px-5 py-1" testID="session-status">
        <Text style={{ numberOfLines: 1 }} class={refused() ? "flex-1 text-error" : "flex-1 text-warning"}>
          {refused() ? `× Continue refused · ${refused()}` : "! No answer from Core yet"}
        </Text>
        <If when={!refused()}><Button onPress={() => run("session.continue")} testID="pending-retry"><Text class="text-warning">Retry</Text></Button></If>
      </View>
    </If>
    <SessionOutcome outcome={outcome} />
  </>;
}
function SessionOutcome(p: { outcome: () => any }) {
  const outcome = p.outcome;
  return <If when={outcome()}>{() => <View class="flex-row items-center gap-3 px-5 py-1" testID="session-status">
    <Text style={{ numberOfLines: 1 }} class={outcome()!.tone === "error" ? "flex-1 text-error" : "flex-1 text-warning"}>
      {`${outcome()!.tone === "error" ? "×" : "!"} ${outcome()!.text}${outcome()!.detail ? ` · ${outcome()!.detail}` : ""}${outcome()!.count > 1 ? ` (+${outcome()!.count - 1})` : ""}`}
    </Text>
    <If when={term("session.interrupted")}><Button onPress={() => run("session.continue")}><Text class="text-warning">/continue</Text></Button></If>
    <If when={!phone}><Button onPress={() => run("app.open_inspector")}><Text class="text-muted-foreground">Inspect</Text></Button></If>
  </View>}</If>;
}
