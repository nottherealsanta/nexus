import { Scroll, Text, View, Clipboard, effect, keymap, untracked, windowSize } from "nsh";
import { Button, Input } from "nsh/base";
import { Modal, ModalFooter, ModalTitle } from "./modal";
import { Btn, FOCUS, T } from "./kit";
import { commands, toKeystroke } from "../commands";
import {
  cacheLines, clearFilters, closeInspector, copyText, cycleFilter, dataOf, filtersLine, focusers, idOf, indexOf, inspectorCommand, inspector, inspectorViews, kindOptions, loadFull,
  newer, older, omittedLines, pageOf, panBy, refreshInspector, rowsOf, select, setFilter, setTarget, setView, statusLine, statusOptions, supportsType, timingWindow, toggleExpand,
  toggleInfo, toggleLive, rowNodes, scrollNode, scrollTop, treeWindow, windowRange, zoomBy, fit, bytesText,
} from "../inspector";
import type { Row } from "../inspector";

const DETAIL_LINES = 120;
const tone = (lane: Row) => lane.end_unknown ? "bg-muted-foreground" : /failed|error|interrupted/.test(lane.status ?? "") ? "bg-error" : lane.running ? "bg-warning" : lane.kind === "model" ? "bg-info" : lane.kind === "tool" ? "bg-success" : "bg-muted-foreground";
const duration = (lane: Row) => lane.end_unknown ? "end unknown" : `${((lane.end ?? 0) - (lane.start ?? 0)).toFixed(2)}s${lane.running ? " live" : ""}`;
const TONE = { live: "text-success", paused: "text-warning", offline: "text-error" } as const;

export function InspectorModal() {
  return <If when={!!inspector()}>{() => <InspectorBody />}</If>;
}
function InspectorBody() {
  const state = () => inspector()!;
  const page = () => pageOf(state());
  const data = () => dataOf(state());
  const rows = () => rowsOf(data(), state().view);
  const index = () => indexOf(state());
  const small = () => windowSize().width < 600;
  const labelWidth = () => small() ? 112 : 176;
  const view = () => state().view;
  const win = () => timingWindow(rows(), page().zoom, page().pan);
  const stamp = (seconds: number) => `+${seconds.toFixed(small() ? 1 : 2)}s`;
  const status = () => statusLine(state());
  const visible = () => { const [a, b] = windowRange(index(), rows().length); return { a, b, rows: rows().slice(a, b) }; };
  const selectedRow = () => rows()[index()];
  const hasTtfb = () => rows().some((r) => typeof r.ttfb === "number");
  const tree = () => data()?.tree ?? [];
  /** The modal is at most 90% of the window; what is not fixed chrome goes to the scrolling body. */
  const bodyHeight = () => Math.max(120, Math.min(600, Math.floor(windowSize().height * 0.9) - 48 - (small() ? 500 : 330) - (small() ? 0 : (view() === 0 ? 100 : 0) + (tree().length > 1 ? 36 : 0))));
  const noun = () => view() === 0 ? "timings" : "records";

  // Keys live only while the inspector is open; they never fire while a field is being edited.
  effect(() => {
    const offs: (() => void)[] = [];
    for (const c of commands) {
      const key = c.key_context === "inspector" ? c.keys?.nsh : undefined;
      if (!key) continue;
      try { offs.push(keymap.bind(toKeystroke(key), () => inspectorCommand(c.id), { when: "notEditing" })); } catch { /* the platform keeps this keystroke */ }
    }
    return () => offs.forEach((off) => off());
  });
  // Scroll position belongs to the (session, view) page: it survives a refresh and a trip to another view.
  let scroll: any;
  effect(() => {
    const s = inspector(); if (!s) return;
    const key = `${s.target}|${s.view}`; const fetched = pageOf(s).fetched;
    void key; void fetched;
    untracked(() => { setTimeout(() => { try { scroll?.scrollTo({ y: pageOf(inspector()!).top, animated: false }); } catch { /* not scrollable yet */ } }, 0); });
  });

  return <Modal size="large" tall label="Session inspector" testID="inspector" onCancel={closeInspector} onOpenChange={(open) => { if (!open) closeInspector(); }}>
    <View class="gap-2">
      <ModalTitle title="Session inspector" />
      <Text class={T.meta} testID="inspect-session">{state().target === state().session ? state().session : `${state().session} → inspecting ${tree().find((n) => n.id === state().target)?.title ?? state().target} (${state().target})`}</Text>
      <View class="flex-row flex-wrap gap-2">
        <Each of={inspectorViews} key={(v) => v}>{(v, i) => <If when={i() === view()} else={<Btn label={`${i() + 1} ${v()}`} onPress={() => setView(i())} testID={`inspect-${v()}`} />}><Btn label={`${i() + 1} ${v()}`} kind="primary" onPress={() => setView(i())} testID={`inspect-${v()}`} /></If>}</Each>
      </View>
      <Text class={`${T.base} ${TONE[status().tone]}`} testID="inspect-status">{status().text}</Text>
      {() => !small() && <>
      <Text class={T.meta} testID="inspect-filters">{filtersLine(state())}</Text>
      <View class="flex-row flex-wrap items-center gap-2">
        <View class="flex-1 border-b border-border" style={{ minWidth: 160 }}>
          <Input ref={(n: any) => { focusers.q = () => { try { n?.focus(); } catch { /* not focusable */ } }; }} value={page().filters.q} placeholder="search" accessibilityLabel="Search" testID="inspect-q" submitBehavior="submit"
            onSubmit={({ text }: { text: string }) => setFilter("q", text)} class="h-8 text-foreground placeholder:text-muted-foreground" />
        </View>
        <If when={kindOptions(view()).length > 0}><Btn label={`Kind: ${page().filters.kind || "any"}`} onPress={() => cycleFilter("kind")} testID="inspect-kind" /></If>
        <If when={statusOptions(view()).length > 0}><Btn label={`Status: ${page().filters.status || "any"}`} onPress={() => cycleFilter("status")} testID="inspect-status-filter" /></If>
        <If when={supportsType(view())}>
          <View class="border-b border-border" style={{ minWidth: 140 }}>
            <Input ref={(n: any) => { focusers.types = () => { try { n?.focus(); } catch { /* not focusable */ } }; }} value={page().filters.types} placeholder="event types" accessibilityLabel="Event types" testID="inspect-types" submitBehavior="submit"
              onSubmit={({ text }: { text: string }) => setFilter("types", text)} class="h-8 text-foreground placeholder:text-muted-foreground" />
          </View>
        </If>
        <Btn label="Clear filters" onPress={clearFilters} testID="inspect-clear" />
      </View>
      <If when={tree().length > 1}>
        <View class="flex-row flex-wrap items-center gap-x-1" testID="inspect-tree">
          <Text class={T.meta}>{`Sessions (${tree().length}) [ ]`}</Text>
          <If when={treeWindow(tree(), state().target).before > 0}><Text class={T.meta}>{`· … ${treeWindow(tree(), state().target).before} before`}</Text></If>
          <Each of={treeWindow(tree(), state().target).nodes} key={(n) => n.id}>{(n) => <><Text class={T.meta}>·</Text><Button onPress={() => setTarget(n().id)} accessibilityLabel={`${n().title ?? n().id} ${n().status}`} testID={`inspect-session-${n().id}`} class={FOCUS}><Text class={`${T.base} ${n().id === state().target ? "font-medium text-foreground" : "text-muted-foreground"}`}>{`${n().id === state().target ? "›" : ""}${n().title ?? n().id} ${n().status}`}</Text></Button></>}</Each>
          <If when={treeWindow(tree(), state().target).after > 0}><Text class={T.meta}>{`· … ${treeWindow(tree(), state().target).after} after`}</Text></If>
        </View>
      </If>
      <View class="gap-1">
          <If when={view() === 0}>
            <View class="flex-row flex-wrap items-center gap-2">
              <Btn label="Zoom in" onPress={() => zoomBy(true)} testID="inspect-zoom-in" />
              <Btn label="Zoom out" onPress={() => zoomBy(false)} />
              <Btn label="Earlier" onPress={() => panBy(false)} />
              <Btn label="Later" onPress={() => panBy(true)} />
              <Btn label="Fit" onPress={fit} testID="inspect-fit" />
              <Text class={T.meta} testID="inspect-axis">{`${page().zoom}× · view ${stamp(win().start - win().origin)} to ${stamp(win().start + win().span - win().origin)} of ${win().total.toFixed(2)}s`}</Text>
            </View>
            <Text class={T.meta}>{`model · tool · turn / attempt · live · failed · ? end unknown${hasTtfb() ? " · | first byte" : ""}`}</Text>
          </If>
          <If when={view() === 0 && rows().length > 0}>
              <View class="flex-row gap-2"><View style={{ width: labelWidth() }} /><View class="flex-1 gap-0.5" testID="inspect-ruler">
                <View class="flex-row justify-between">
                  <Text class={T.meta}>{stamp(win().start - win().origin)}</Text>
                  <If when={!small()}><Text class={T.meta}>{stamp(win().start + win().span / 4 - win().origin)}</Text></If>
                  <Text class={T.meta}>{stamp(win().start + win().span / 2 - win().origin)}</Text>
                  <If when={!small()}><Text class={T.meta}>{stamp(win().start + win().span * 3 / 4 - win().origin)}</Text></If>
                  <Text class={T.meta}>{stamp(win().start + win().span - win().origin)}</Text>
                </View>
                <View class="h-2 relative border-b border-border">
                  <Each of={[0, 25, 50, 75, 100]} key={(p) => p}>{(p) => <View class="absolute top-0 bottom-0 w-px bg-border" style={{ left: `${p()}%` }} />}</Each>
                </View>
              </View></View>
          </If>
      </View>
      </>}
      <Scroll ref={(n: any) => { scroll = n; scrollNode.current = n; }} onScroll={(e: any) => { const s = inspector(); if (s) pageOf(s).top = e.offset.y; scrollTop.set(Math.round(e.offset.y)); }} style={{ height: bodyHeight() }} testID="inspector-body">
        <View class="gap-1">
          {() => small() && <>
      <Text class={T.meta} testID="inspect-filters">{filtersLine(state())}</Text>
      <View class="flex-row flex-wrap items-center gap-2">
        <View class="flex-1 border-b border-border" style={{ minWidth: 160 }}>
          <Input ref={(n: any) => { focusers.q = () => { try { n?.focus(); } catch { /* not focusable */ } }; }} value={page().filters.q} placeholder="search" accessibilityLabel="Search" testID="inspect-q" submitBehavior="submit"
            onSubmit={({ text }: { text: string }) => setFilter("q", text)} class="h-8 text-foreground placeholder:text-muted-foreground" />
        </View>
        <If when={kindOptions(view()).length > 0}><Btn label={`Kind: ${page().filters.kind || "any"}`} onPress={() => cycleFilter("kind")} testID="inspect-kind" /></If>
        <If when={statusOptions(view()).length > 0}><Btn label={`Status: ${page().filters.status || "any"}`} onPress={() => cycleFilter("status")} testID="inspect-status-filter" /></If>
        <If when={supportsType(view())}>
          <View class="border-b border-border" style={{ minWidth: 140 }}>
            <Input ref={(n: any) => { focusers.types = () => { try { n?.focus(); } catch { /* not focusable */ } }; }} value={page().filters.types} placeholder="event types" accessibilityLabel="Event types" testID="inspect-types" submitBehavior="submit"
              onSubmit={({ text }: { text: string }) => setFilter("types", text)} class="h-8 text-foreground placeholder:text-muted-foreground" />
          </View>
        </If>
        <Btn label="Clear filters" onPress={clearFilters} testID="inspect-clear" />
      </View>
      <If when={tree().length > 1}>
        <View class="flex-row flex-wrap items-center gap-2" testID="inspect-tree">
          <Text class={T.meta}>{`Sessions (${tree().length}) [ ]`}</Text>
          <If when={treeWindow(tree(), state().target).before > 0}><Text class={T.meta}>{`… ${treeWindow(tree(), state().target).before} before`}</Text></If>
          <Each of={treeWindow(tree(), state().target).nodes} key={(n) => n.id}>{(n) => <If when={n().id === state().target} else={<Btn label={`${n().title ?? n().id} ${n().status}`} onPress={() => setTarget(n().id)} testID={`inspect-session-${n().id}`} />}><Btn label={`›${n().title ?? n().id} ${n().status}`} kind="primary" onPress={() => setTarget(n().id)} testID={`inspect-session-${n().id}`} /></If>}</Each>
          <If when={treeWindow(tree(), state().target).after > 0}><Text class={T.meta}>{`… ${treeWindow(tree(), state().target).after} after`}</Text></If>
        </View>
      </If>
      <View class="gap-1">
          <If when={view() === 0}>
            <View class="flex-row flex-wrap items-center gap-2">
              <Btn label="Zoom in" onPress={() => zoomBy(true)} testID="inspect-zoom-in" />
              <Btn label="Zoom out" onPress={() => zoomBy(false)} />
              <Btn label="Earlier" onPress={() => panBy(false)} />
              <Btn label="Later" onPress={() => panBy(true)} />
              <Btn label="Fit" onPress={fit} testID="inspect-fit" />
              <Text class={T.meta} testID="inspect-axis">{`${page().zoom}× · view ${stamp(win().start - win().origin)} to ${stamp(win().start + win().span - win().origin)} of ${win().total.toFixed(2)}s`}</Text>
            </View>
            <Text class={T.meta}>{`model · tool · turn / attempt · live · failed · ? end unknown${hasTtfb() ? " · | first byte" : ""}`}</Text>
          </If>
          <If when={view() === 0 && rows().length > 0}>
              <View class="flex-row gap-2"><View style={{ width: labelWidth() }} /><View class="flex-1 gap-0.5" testID="inspect-ruler">
                <View class="flex-row justify-between">
                  <Text class={T.meta}>{stamp(win().start - win().origin)}</Text>
                  <If when={!small()}><Text class={T.meta}>{stamp(win().start + win().span / 4 - win().origin)}</Text></If>
                  <Text class={T.meta}>{stamp(win().start + win().span / 2 - win().origin)}</Text>
                  <If when={!small()}><Text class={T.meta}>{stamp(win().start + win().span * 3 / 4 - win().origin)}</Text></If>
                  <Text class={T.meta}>{stamp(win().start + win().span - win().origin)}</Text>
                </View>
                <View class="h-2 relative border-b border-border">
                  <Each of={[0, 25, 50, 75, 100]} key={(p) => p}>{(p) => <View class="absolute top-0 bottom-0 w-px bg-border" style={{ left: `${p()}%` }} />}</Each>
                </View>
              </View></View>
          </If>
      </View>
          </>}
          <Each of={cacheLines(data())} key={(l) => l}>{(l) => <Text class={l().startsWith(" ") ? T.meta : T.sec}>{l()}</Text>}</Each>
          <Each of={omittedLines(data())} key={(l) => l}>{(l) => <Text class="text-warning" style={{ fontSize: 13 }}>{l()}</Text>}</Each>
          <Each of={(data()?.notes ?? []).filter((n) => state().info || !n.startsWith("Cache"))} key={(n) => n}>{(n) => <Text class={T.meta}>{n()}</Text>}</Each>
          <If when={state().loading && !data()}><Text class={T.sec}>Loading inspector…</Text></If>
          <If when={!!state().error}><Text class="text-error">{`Error · ${state().error}`}</Text></If>
          <Text class={T.meta}>{page().stack.length === 0 ? (data()?.more ? "Newest records · n older" : "Newest records") : data()?.more ? "Older records · p newer · n older" : "Older records · p newer · oldest retained"}</Text>
          <If when={rows().length > 0} else={<Text class={T.sec}>{(() => { const f = page().filters; const any = !!(f.q || f.kind || f.status || f.types); return view() === 0 ? (any ? "No timings match the filters." : "No retained execution timings.") : any ? "No records match the filters." : data()?.text ?? ""; })()}</Text>}>
            <If when={visible().a > 0}><Text class={T.meta}>{`… ${visible().a} rows above`}</Text></If>
            <Each of={visible().rows} key={(r) => idOf(r)}>{(r, i) => {
              const id = () => idOf(r());
              const open = () => !!page().expanded[id()];
              const picked = () => page().selected === id() || (page().selected == null && visible().a + i() === 0);
              const header = () => view() === 0 && (i() === 0 || visible().rows[i() - 1]?.session_id !== r().session_id);
              const left = () => Math.max(0, Math.min(1, ((r().start ?? 0) - win().start) / win().span));
              const right = () => Math.max(0, Math.min(1, ((r().end ?? 0) - win().start) / win().span));
              const shown = () => (r().end ?? 0) >= win().start && (r().start ?? 0) <= win().start + win().span;
              return <View class="gap-1">
                <If when={header()}><Text class={`${T.label} pt-2`} testID={`inspect-group-${r().session_id}`}>{`${r().session ?? r().session_id} · ${rows().filter((x) => x.session_id === r().session_id).length} operation${rows().filter((x) => x.session_id === r().session_id).length === 1 ? "" : "s"}`}</Text></If>
                <Button ref={(n: any) => { if (n) rowNodes.set(id(), n); else rowNodes.delete(id()); }} onPress={() => { if (page().selected === id()) toggleExpand(id()); else { select(id()); toggleExpand(id()); } }} accessibilityLabel={view() === 0 ? `${r().session}, ${r().kind}, ${r().label}, ${r().status}, ${duration(r())}` : r().summary ?? id()} testID={view() === 0 ? `lane-${id()}` : `row-${id()}`}
                  class={`flex-row items-center min-h-9 gap-2 ${picked() ? "bg-selection" : ""} ${FOCUS}`}>
                  <If when={view() === 0} else={<Text class={`${T.base} flex-1 ${picked() ? "font-medium" : ""}`}>{`${open() ? "▾" : "▸"} ${r().summary ?? ""}`}</Text>}>
                    <View style={{ width: labelWidth() }}><Text class="text-xs text-foreground" style={{ numberOfLines: 1 }}>{`${open() ? "▾" : "▸"} ${r().kind}`}</Text><Text class={T.meta} style={{ numberOfLines: 1 }}>{r().label ?? ""}</Text></View>
                    <View class="flex-1 h-6 bg-card relative overflow-hidden">
                      <If when={shown()}><View style={{ left: `${left() * 100}%`, width: r().end_unknown ? 6 : `${Math.max(0.4, (right() - left()) * 100)}%` }} class={`absolute h-3 top-1 ${tone(r())}`} /></If>
                      <If when={typeof r().ttfb === "number"}><View testID={`ttfb-${id()}`} style={{ left: `${Math.max(0, Math.min(1, ((r().start ?? 0) + (r().ttfb ?? 0) - win().start) / win().span)) * 100}%`, width: 2 }} class="absolute h-6 top-0 bg-foreground" /></If>
                      <Text class="absolute right-1 text-xs text-foreground">{duration(r())}</Text>
                    </View>
                  </If>
                </Button>
                <If when={open()}><Detail row={r()} /></If>
              </View>;
            }}</Each>
            <If when={visible().b < rows().length}><Text class={T.meta}>{`… ${rows().length - visible().b} rows below`}</Text></If>
          </If>
        </View>
      </Scroll>
      <View class="flex-row flex-wrap gap-2">
        <Btn label="Refresh" onPress={() => void refreshInspector()} testID="inspect-refresh" />
        <Btn label={state().paused ? "Resume live" : "Pause live"} onPress={toggleLive} testID="inspect-live" />
        <If when={page().stack.length > 0}><Btn label="Newer" onPress={newer} testID="inspect-newer" /></If>
        <If when={!!data()?.next_before}><Btn label="Older" onPress={older} testID="inspect-older" /></If>
        <If when={!!selectedRow()?.detail_truncated && !page().full[selectedRow() ? idOf(selectedRow()!) : ""]}><Btn label="Load full record" onPress={() => void loadFull()} testID="inspect-full" /></If>
        <Btn label="Cache info" onPress={toggleInfo} testID="inspect-info" />
        <Btn label={selectedRow() ? "Copy row" : "Copy view"} onPress={() => { void Clipboard.writeText(copyText()); }} testID="inspect-copy" />
      </View>
      <ModalFooter hints={["1-6 view", "j/k select", "enter expand", "/ search", "esc close"]} right={`${rows().length} ${noun()}`} />
    </View>
  </Modal>;
}

/** An expanded row: links, the exact JSON (first 120 lines), and the clipped/full state. */
function Detail(p: { row: Row }) {
  const s = () => inspector()!;
  const id = () => idOf(p.row);
  const full = () => pageOf(s()).full[id()];
  const text = () => full() ?? p.row.detail ?? "";
  const lines = () => text().split("\n");
  const links = () => Object.entries(p.row.links ?? {}).sort(([a], [b]) => (a < b ? -1 : 1)).filter(([, v]) => typeof v === "string").map(([k, v]) => `${k} ${v}`);
  return <View class="pl-6 gap-1" testID={`detail-${id()}`}>
    <If when={links().length > 0}><Text class={T.meta}>{`Links · ${links().join(" · ")}`}</Text></If>
    <Text class="font-mono text-xs text-foreground" style={{ userSelect: "text" }}>{lines().slice(0, DETAIL_LINES).join("\n")}</Text>
    <If when={lines().length > DETAIL_LINES}><Text class={T.meta}>{`… ${lines().length - DETAIL_LINES} more lines (copy takes all)`}</Text></If>
    <If when={full() != null} else={<If when={!!p.row.detail_truncated}>
      <If when={s().loadingFull === id()} else={<Text class="text-warning" style={{ fontSize: 13 }}>{`Clipped · showing the first part of ${bytesText(p.row.detail_bytes ?? 0)} · e loads the full record`}</Text>}><Text class={T.meta}>Loading full record…</Text></If>
    </If>}><Text class={T.meta}>{`Full record loaded · ${p.row.detail_bytes ?? text().length} bytes`}</Text></If>
  </View>;
}
