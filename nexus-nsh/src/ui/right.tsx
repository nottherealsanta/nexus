import type { JSX } from "nsh";
import { Scroll, Text, View } from "nsh";
import { Btn, IconButton, Status, T, relTime, midTruncate } from "./kit";
import { manifest } from "../gen/manifest";
import { run } from "../commands";
import { m, model, viewed } from "../store";

const sections = manifest.navigation.right_sidebar.section as unknown as { id: string; label: string; order: number; unavailable_reason?: string }[];

function Field(p: { k: string; v?: string | null; mono?: boolean; children?: JSX.Element }) {
  return (
    <View class="flex-row items-start gap-3 py-1">
      <Text class={`w-[76px] shrink-0 ${T.meta} pt-0.5`}>{p.k}</Text>
      <View class="flex-1 min-w-0">
        {p.children ?? <Text selectable class={`${p.mono ? "font-mono text-xs leading-5" : "text-[13px] leading-5"} text-foreground`}>{p.v || "—"}</Text>}
      </View>
    </View>
  );
}
function Section(p: { title: string; children?: JSX.Element }) {
  return (
    <View class="px-4 py-3 border-b border-border">
      <Text class="text-[13px] leading-5 font-semibold text-foreground pb-1.5">{p.title}</Text>
      {p.children}
    </View>
  );
}
const date = (s?: number) => (s ? new Date(s * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");

export function RightSidebar(p: { drawer?: boolean }) {
  const s = () => viewed();
  return (
    <View class={`bg-card ${p.drawer ? "flex-1" : "w-[288px] border-l border-border"}`} testID="right-sidebar" accessibilityLabel="Right sidebar">
      <Scroll class="flex-1" contentClass="">
        <Section title={sections[0].label}>
          <If when={s()} else={<Text class={T.sec}>Select a session to see its details.</Text>}>
            {() => {
              const ses = () => { m(); return s()!; };
              const msgs = () => { m(); return model.selected?.messages ?? []; };
              const turns = () => msgs().filter((x) => x.role === "user" && !/^\[(Nexus|Notification)/.test(x.parts[0]?.text ?? "")).length;
              const calls = () => msgs().reduce((n, x) => n + x.parts.filter((q) => q.type === "tool_call").length, 0);
              const kids = () => [...model.sessions.values()].filter((x) => x.parent_id === ses().id);
              return (
                <View>
                  <Field k="Title" v={ses().title || "Untitled"} />
                  <Field k="Id"><View class="flex-row items-center gap-1"><Text selectable class="flex-1 min-w-0 font-mono text-xs leading-5 text-foreground">{midTruncate(ses().id, 18)}</Text><IconButton icon="copy" label="Copy session id" onPress={() => run("session.copy_id")} class="size-7" /></View></Field>
                  <Field k="Status"><View class="flex-row"><Status kind={ses().status === "busy" ? "running" : ses().archived ? "cancelled" : "done"} word /></View></Field>
                  <Field k="Agent" v={ses().agent} />
                  <Field k="Model" v={`${ses().model || "default"}${ses().model_source ? ` (${ses().model_source})` : ""}`} mono />
                  <Field k="Effort" v={(ses().effort as string) || "default"} />
                  <Field k="Directory" v={midTruncate(ses().directory, 30)} mono />
                  <Field k="Started" v={date(ses().created)} />
                  <Field k="Last activity" v={`${date(ses().updated)} · ${relTime(ses().updated)}`} />
                  <Field k="Turns" v={`${turns()} · ${msgs().length} messages · ${calls()} tool calls`} />
                  <Field k="Tools" v={(ses().tools as string[] | undefined)?.join(", ") || "loading…"} />
                  <If when={(ses().disabled_tools as string[] | undefined)?.length}><Field k="Disabled" v={(ses().disabled_tools as string[]).join(", ")} /></If>
                  <Field k="Children" v={kids().length ? `${kids().length} (${kids().filter((k) => k.status === "busy").length} running)` : "none"} />
                  <Text class={`${T.meta} pt-2`}>Tokens, context use, cost and kernels need Core usage totals (not in the API yet).</Text>
                </View>
              );
            }}
          </If>
        </Section>
        <Section title={sections[1].label}>
          <View class="flex-row items-start gap-2">
            <Text class="text-[13px] leading-5 text-muted-foreground w-3.5 text-center">⊖</Text>
            <View class="flex-1 gap-1">
              <Text class="text-[13px] leading-5 text-foreground-secondary font-medium">Unavailable</Text>
              <Text class={T.sec}>{sections[1].unavailable_reason}</Text>
            </View>
          </View>
        </Section>
      </Scroll>
    </View>
  );
}
void Btn;
