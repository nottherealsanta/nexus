// Diffs for edit, write and patch tool details (DESIGN_PLAN D09, I4): split at window width 1100 px
// and up, unified below. Built from the tool detail lines, so the detail cap and its announced
// "lines hidden" marker still apply: the view only lays out the lines it is given.
import { Text, View } from "nsh";
import type { ToolDetailLine } from "./timeline";
import { width } from "../store";

export const SPLIT_MIN_WIDTH = 1100;
const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)/;
const MONO = "font-mono text-[13px] leading-5";

export type DiffItem = { kind: "hunk" | "ctx" | "add" | "del"; o?: number; n?: number; text: string };

/** Detail lines as diff items. Hunk headers carry the line numbers; a write's file header is dropped. */
export function diffItems(lines: ToolDetailLine[]): DiffItem[] {
  const out: DiffItem[] = [];
  let o: number | undefined, n: number | undefined;
  for (const line of lines) {
    const t = line.text;
    if (t.includes("lines hidden")) continue;
    if (line.tone === "info") {
      if (!t.startsWith("@@")) continue; // a write's "+ path" header: the row's head already names the file
      const h = t.match(HUNK);
      o = h ? Number(h[1]) : undefined;
      n = h ? Number(h[2]) : undefined;
      out.push({ kind: "hunk", text: t });
      continue;
    }
    if (line.tone === "plain" && (t.startsWith("--- ") || t.startsWith("+++ ") || t.startsWith("\\"))) continue;
    if (line.tone === "add") {
      out.push({ kind: "add", n, text: t.slice(1) });
      if (n !== undefined) n++;
    } else if (line.tone === "del") {
      out.push({ kind: "del", o, text: t.slice(1) });
      if (o !== undefined) o++;
    } else {
      out.push({ kind: "ctx", o, n, text: t.startsWith(" ") ? t.slice(1) : t });
      if (o !== undefined) o++;
      if (n !== undefined) n++;
    }
  }
  return out;
}

/** Split rows: a run of removals and additions is paired line by line; the missing side stays empty. */
export type SplitRow =
  | { kind: "hunk"; text: string }
  | { kind: "ctx"; o?: number; n?: number; text: string }
  | { kind: "pair"; left?: DiffItem; right?: DiffItem };

export function splitRows(items: DiffItem[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let i = 0;
  while (i < items.length) {
    const it = items[i];
    if (it.kind === "hunk") { rows.push({ kind: "hunk", text: it.text }); i++; continue; }
    if (it.kind === "ctx") { rows.push({ kind: "ctx", o: it.o, n: it.n, text: it.text }); i++; continue; }
    const dels: DiffItem[] = [], adds: DiffItem[] = [];
    while (i < items.length && items[i].kind === "del") dels.push(items[i++]);
    while (i < items.length && items[i].kind === "add") adds.push(items[i++]);
    for (let k = 0; k < Math.max(dels.length, adds.length); k++) rows.push({ kind: "pair", left: dels[k], right: adds[k] });
  }
  return rows;
}

const num = (n?: number) => (n === undefined ? "" : String(n));

/** The diff of a tool detail. `lines` is reactive (the row's body). */
export function DiffView(p: { lines: () => ToolDetailLine[] }) {
  return (
    <View class="w-full min-w-0" testID="diff">
      {() => (width() >= SPLIT_MIN_WIDTH ? <SplitView lines={p.lines} /> : <UnifiedView lines={p.lines} />)}
    </View>
  );
}

function Hunk(p: { text: string }) {
  return (
    <View class="h-6 justify-center px-3">
      <Text numberOfLines={1} class="font-mono text-xs leading-4 text-muted-foreground">{`⋯ ${p.text}`}</Text>
    </View>
  );
}

function SplitView(p: { lines: () => ToolDetailLine[] }) {
  const rows = () => splitRows(diffItems(p.lines()));
  return (
    <View class="w-full min-w-0">
      <Each of={rows()} key={(_, i) => `${i}`}>
        {(row) => {
          const r = row();
          if (r.kind === "hunk") return <Hunk text={r.text} />;
          if (r.kind === "ctx") {
            return (
              <View class="flex-row min-h-5">
                <SplitCell num={num(r.o)} text={r.text} />
                <View class="w-px bg-border-strong" />
                <SplitCell num={num(r.n)} text={r.text} />
              </View>
            );
          }
          return (
            <View class="flex-row min-h-5">
              {r.left
                ? <SplitCell num={num(r.left.o)} text={r.left.text} tint="bg-diff-del-bg" numClass="text-diff-del" />
                : <Empty />}
              <View class="w-px bg-border-strong" />
              {r.right
                ? <SplitCell num={num(r.right.n)} text={r.right.text} tint="bg-diff-add-bg" numClass="text-diff-add" />
                : <Empty />}
            </View>
          );
        }}
      </Each>
    </View>
  );
}

function SplitCell(p: { num: string; text: string; tint?: string; numClass?: string }) {
  return (
    <View class={`flex-1 min-w-0 flex-row ${p.tint ?? ""}`}>
      <Text class={`w-12 shrink-0 text-right pr-2 font-mono text-xs leading-5 text-muted-foreground ${p.numClass ?? ""}`}>{p.num}</Text>
      <Text numberOfLines={1} class={`flex-1 min-w-0 pl-2 pr-2 ${MONO} text-foreground`}>{p.text}</Text>
    </View>
  );
}

/** The missing side of an add or a remove: a flat empty fill (NSH views cannot draw the hatch). */
function Empty() {
  return <View class="flex-1 min-w-0 bg-border-strong opacity-20" />;
}

function UnifiedView(p: { lines: () => ToolDetailLine[] }) {
  const items = () => diffItems(p.lines());
  return (
    <View class="w-full min-w-0">
      <Each of={items()} key={(_, i) => `${i}`}>
        {(item) => {
          const it = item();
          if (it.kind === "hunk") return <Hunk text={it.text} />;
          const tint = it.kind === "add" ? "bg-diff-add-bg" : it.kind === "del" ? "bg-diff-del-bg" : "";
          const sign = it.kind === "add" ? "+" : it.kind === "del" ? "-" : " ";
          const signClass = it.kind === "add" ? "text-diff-add" : it.kind === "del" ? "text-diff-del" : "text-muted-foreground";
          const textClass = it.kind === "ctx" ? "text-muted-foreground" : "text-foreground";
          const oldNo = it.kind === "add" ? undefined : it.o;
          const newNo = it.kind === "del" ? undefined : it.n;
          return (
            <View class={`flex-row min-h-5 ${tint}`}>
              <Text class="w-12 shrink-0 text-right pr-2 font-mono text-xs leading-5 text-muted-foreground">{num(oldNo)}</Text>
              <Text class="w-12 shrink-0 text-right pr-2 font-mono text-xs leading-5 text-muted-foreground">{num(newNo)}</Text>
              <Text class={`w-5 text-center ${MONO} ${signClass}`}>{sign}</Text>
              <Text numberOfLines={1} class={`flex-1 min-w-0 pr-2 ${MONO} ${textClass}`}>{it.text}</Text>
            </View>
          );
        }}
      </Each>
    </View>
  );
}
