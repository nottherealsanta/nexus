import { Text, View } from "nsh";
import { Button } from "nsh/base";
import { run, term } from "../commands";
import { m, model, phone, viewed } from "../store";
import { diag, summaryDiags } from "./timeline";

export function SessionStatus() {
  const outcome = () => {
    m();
    if (viewed()?.status === "busy") return null;
    const turn = model.selected?.turns.at(-1);
    const scratch = (model.selected?.scratch ?? []).filter((e) => ["session.error", "retry_scheduled", "retry_started", "retry_exhausted", "turn.recovered"].includes(e.type) || (["permission.replied", "question.replied"].includes(e.type) && e.data?.outcome === "cancelled" && e.data?.origin !== "manager")).map(diag).filter((r) => r != null);
    const rows = scratch.length ? scratch.map((row) => ({ row })) : turn ? summaryDiags(turn) : [];
    const latest = rows.at(-1)?.row;
    return latest ? { ...latest, text: latest.text.split("\n")[0], detail: latest.detail && !latest.text.includes(latest.detail) ? latest.detail : undefined, count: rows.length } : null;
  };
  return <If when={outcome()}>{() => <View class="flex-row items-center gap-3 px-5 py-1" testID="session-status">
    <Text style={{ numberOfLines: 1 }} class={outcome()!.tone === "error" ? "flex-1 text-error" : "flex-1 text-warning"}>
      {`${outcome()!.tone === "error" ? "×" : "!"} ${outcome()!.text}${outcome()!.detail ? ` · ${outcome()!.detail}` : ""}${outcome()!.count > 1 ? ` (+${outcome()!.count - 1})` : ""}`}
    </Text>
    <If when={term("session.interrupted")}><Button onPress={() => run("session.continue")}><Text class="text-warning">/continue</Text></Button></If>
    <If when={!phone}><Button onPress={() => run("app.open_inspector")}><Text class="text-muted-foreground">Inspect</Text></Button></If>
  </View>}</If>;
}
