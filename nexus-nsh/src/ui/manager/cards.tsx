// The seven cards the Manager can show (docs/manager-design.md 10.4): board, sessions, approval, question,
// diagram, excerpt and note. A small catalog drawn natively; no free layout. Rows come from the live
// store when the client has the session, from the payload otherwise. Each card has a narrow form.
import { Scroll, Text, View, windowSize } from "nsh";
import { Button, TextView } from "nsh/base";
import { FOCUS, PROSE, T, TS } from "../kit";
import { MermaidFence } from "../timeline";
import { attention as attentionGlyph, statusGlyphs } from "../../gen/theme";
import { Card, COLUMNS, Column, Row, WORDS, boardColumns, liveRow, more, rowLine, stateWord, summaryRepeats } from "../../manager";
import { liveSession, pickRow } from "../../core/manager";
import { m } from "../../store";

type Of<T extends Card["type"]> = Extract<Card, { type: T }>;

/** True below a phone-to-tablet width: the board stacks and padding tightens. */
export const narrowCard = () => windowSize().width < 640;

function glyphOf(state: string): { glyph: string; tone: string } {
  switch (state) {
    case "needs_approval": case "needs_answer": case "waiting": return { glyph: attentionGlyph.waiting.unicode, tone: "text-warning" };
    case "failed": case "blocked": return { glyph: statusGlyphs.error.unicode, tone: "text-error" };
    case "working": case "running": return { glyph: statusGlyphs.running.unicode, tone: "text-info" };
    default: return { glyph: "·", tone: "text-muted-foreground" };
  }
}

/** One numbered, clickable session row: `2 migration`, its title and state beneath, and why it matched. */
function SessionRow(p: { row: Row; interactive: boolean }) {
  const row = () => { m(); return liveRow(p.row, liveSession(p.row.session_id)); };
  const g = () => glyphOf(row().state);
  return (
    <Button onPress={() => pickRow(p.row.n)} disabled={!p.interactive} testID={`manager-row-${p.row.n}`}
      accessibilityLabel={`${p.row.n} ${p.row.handle}, ${stateWord(row().state)}`}
      class={`-mx-2 flex-row items-start gap-3 rounded-(--radius) px-2 py-1.5 hover:bg-accent ${FOCUS}`}>
      <Text class={`w-3.5 pt-px text-center ${TS.base} ${g().tone}`}>{g().glyph}</Text>
      <View class="flex-1 min-w-0">
        <Text style={{ numberOfLines: 1 }} class={`${TS.base} font-medium text-foreground`}>{`${p.row.n} ${p.row.handle}`}</Text>
        <Text style={{ numberOfLines: 1 }} class={T.meta}>{rowLine(row())}</Text>
      </View>
    </Button>
  );
}

function BoardColumn(p: { label: string; rows: Row[]; more: number; interactive: boolean; first?: boolean }) {
  return (
    <View class={`${narrowCard() ? "w-full" : "flex-1 min-w-0"} gap-0.5`}>
      <Text class={`${T.section} pb-1`}>{p.label}</Text>
      <If when={p.rows.length === 0}><Text class={T.meta}>—</Text></If>
      <Each of={p.rows} key={(row) => row.n}>{(row) => <SessionRow row={row()} interactive={p.interactive} />}</Each>
      <If when={p.more > 0}><Text class={`${T.meta} pt-1`}>{more(p.more)}</Text></If>
    </View>
  );
}

function Board(p: { card: () => Of<"board">; interactive: boolean }) {
  const columns = () => { m(); return boardColumns(p.card(), (row) => liveSession(row.session_id)); };
  return (
    <View class={narrowCard() ? "gap-4" : "flex-row gap-4"} testID="manager-board">
      <Each of={COLUMNS} key={(c) => c.key}>
        {(c) => <BoardColumn label={c().label} rows={columns()[c().key as Column]} more={p.card().more?.[c().key as Column] ?? 0} interactive={p.interactive} />}
      </Each>
    </View>
  );
}

function Sessions(p: { card: () => Of<"sessions">; interactive: boolean }) {
  return (
    <View class="gap-0.5" testID="manager-sessions">
      <Each of={p.card().rows} key={(row) => row.n}>{(row) => <SessionRow row={row()} interactive={p.interactive} />}</Each>
      <If when={(p.card().more ?? 0) > 0}><Text class={`${T.meta} pt-1`}>{more(p.card().more ?? 0)}</Text></If>
    </View>
  );
}

/** The exact command or diff a session wants, with the session's name. Answered by voice, not by click. */
function Approval(p: { card: () => Of<"approval"> }) {
  return (
    <View class="gap-3" testID="manager-approval">
      <View class="gap-0.5">
        <Text class={`${TS.title} font-semibold text-foreground`}>{p.card().handle}</Text>
        <If when={!summaryRepeats(p.card().summary, p.card().detail)}><Text class={T.sec}>{p.card().summary}</Text></If>
      </View>
      <View class="rounded-(--radius) bg-card p-3 max-h-56">
        <Scroll>
          <View>
            {p.card().detail.split("\n").map((line) => <Text style={{ userSelect: "text" }} class={`font-(family-name:--font-code) text-[12.5px] leading-5 ${lineTone(line)}`}>{line || " "}</Text>)}
          </View>
        </Scroll>
        <If when={!!p.card().clipped}><Text class={`${T.meta} pt-1.5`}>{WORDS.cut}</Text></If>
      </View>
    </View>
  );
}

const lineTone = (line: string) => line.startsWith("+") && !line.startsWith("+++") ? "text-success" : line.startsWith("-") && !line.startsWith("---") ? "text-error" : "text-foreground";

function Question(p: { card: () => Of<"question"> }) {
  return (
    <View class="gap-3" testID="manager-question">
      <View class="gap-0.5">
        <Text class={`${TS.title} font-semibold text-foreground`}>{p.card().handle}</Text>
        <Text style={{ userSelect: "text" }} class={T.body}>{p.card().question}</Text>
      </View>
      <View class="gap-1">
        <Each of={p.card().options} key={(o, i) => `${i}:${o}`}>
          {(option) => (
            <View class="flex-row items-start gap-2 rounded-(--radius) bg-card px-3 py-1.5">
              <Text class={`${TS.base} text-muted-foreground`}>·</Text>
              <Text class={`flex-1 ${TS.base} text-foreground`}>{option()}</Text>
            </View>
          )}
        </Each>
      </View>
    </View>
  );
}

/** The existing Mermaid path, fallback included: the box art, or the source in a code block. */
function Diagram(p: { card: () => Of<"diagram">; maxHeight: number }) {
  return (
    <View testID="manager-diagram" class="items-center">
      <MermaidFence block={{ language: "mermaid", code: p.card().source } as any} maxHeight={p.maxHeight} centered />
      <If when={!!p.card().clipped}><Text class={`${T.meta} pt-1.5`}>{WORDS.cut}</Text></If>
    </View>
  );
}

/** A passage from a session's answer: quoted, labelled with its source, never the Manager's own voice. */
function Excerpt(p: { card: () => Of<"excerpt"> }) {
  return (
    <View class="gap-2" testID="manager-excerpt">
      <Text class={T.meta}>{`From ${p.card().handle}`}</Text>
      <View class="flex-row gap-3">
        <View class="w-0.5 rounded-full bg-border-strong" />
        <View class="flex-1 min-w-0">
          <TextView selectable links="open" markdown={p.card().markdown} classes={{ ...PROSE, root: `${TS.body} text-foreground-secondary` }} />
          <If when={!!p.card().clipped}><Text class={`${T.meta} pt-1`}>{WORDS.cut}</Text></If>
        </View>
      </View>
    </View>
  );
}

function Note(p: { card: () => Of<"note"> }) {
  return (
    <View testID="manager-note">
      <TextView selectable links="open" markdown={p.card().markdown} classes={PROSE} />
      <If when={!!p.card().clipped}><Text class={`${T.meta} pt-1`}>{WORDS.cut}</Text></If>
    </View>
  );
}

/** The card body for any type. `interactive` is false when the card is unfocused (it takes no clicks on rows). */
export function CardBody(p: { card: () => Card; interactive: boolean; maxHeight: number }) {
  const as = <K extends Card["type"]>() => p.card as unknown as () => Of<K>;
  return (
    <>
      <If when={p.card().type === "board"}><Board card={as<"board">()} interactive={p.interactive} /></If>
      <If when={p.card().type === "sessions"}><Sessions card={as<"sessions">()} interactive={p.interactive} /></If>
      <If when={p.card().type === "approval"}><Approval card={as<"approval">()} /></If>
      <If when={p.card().type === "question"}><Question card={as<"question">()} /></If>
      <If when={p.card().type === "diagram"}><Diagram card={as<"diagram">()} maxHeight={p.maxHeight} /></If>
      <If when={p.card().type === "excerpt"}><Excerpt card={as<"excerpt">()} /></If>
      <If when={p.card().type === "note"}><Note card={as<"note">()} /></If>
    </>
  );
}

