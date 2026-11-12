// The session switcher: the project's sessions (or every project's), opened with
// ctrl/cmd+k or by pressing the project already open on the rail. It mirrors the
// TUI palette: a state glyph leads each row, the title follows and the age (or
// the matched message) sits at the right end.
import { Scroll, Text, View, keymap, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { ApiError, request } from "../core/transport";
import { attention as attentionGlyph, statusGlyphs } from "../gen/theme";
import { escape } from "../commands";
import { currentProject, m, model, scopeAll, selectSession, switcherOpen, switcherQuery, tick } from "../store";
import { FOCUS, Spinner, T, TONE, relTime } from "./kit";
import { Modal, modalBodyHeight, ModalFooter, ModalTitle } from "./modal";
import { attention } from "./sidebar";

/** The TUI-style fuzzy filter: the query's characters appear in order. */
function fuzzyMatch(query: string, text: string) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  let at = 0;
  for (const ch of text.toLowerCase()) if (ch === q[at]) at++;
  return at === q.length;
}

/** The session tab's state glyph, in the TUI switcher's order. */
function switchState(id: string) {
  m();
  const session = model.sessions.get(id);
  if (attention(id) === "waiting") {
    return { glyph: attentionGlyph.waiting.unicode, tone: TONE[attentionGlyph.waiting.token], label: "waiting", spin: false };
  }
  if (model.running.has(id) || session?.status === "busy") {
    return { glyph: statusGlyphs.running.unicode, tone: TONE[statusGlyphs.running.token], label: "running", spin: true };
  }
  if (model.failedSessions.has(id) || session?.status === "failed") {
    return { glyph: "\u25cf", tone: "text-error", label: "failed", spin: false };
  }
  if (model.unreadSessions.has(id)) {
    return { glyph: attentionGlyph.unread.unicode, tone: TONE[attentionGlyph.unread.token], label: "unread", spin: false };
  }
  return { glyph: "\u00b7", tone: "text-muted-foreground", label: "idle", spin: false };
}

type Hit = { session_id: string; snippet: string };
type Row = { id: string; title: string; hint: string; message: boolean };

function truncate(text: string, limit: number) {
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}\u2026`;
}

export function SessionSwitcher() {
  return <If when={switcherOpen()}>{() => <SessionSwitcherBody />}</If>;
}

function SessionSwitcherBody() {
  const query = signal(switcherQuery());
  const idx = signal(0);
  const hits = signal<{ q: string; rows: Hit[] } | null>(null);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let seq = 0;
  const close = () => switcherOpen.set(false);

  const setQuery = (value: string) => {
    query.set(value);
    idx.set(0);
    clearTimeout(timer);
    const q = value.trim();
    if (q.length < 2) {
      seq++;
      hits.set(null);
      return;
    }
    const mine = ++seq;
    timer = setTimeout(async () => {
      const project = scopeAll() ? null : currentProject();
      const qs = `q=${encodeURIComponent(q)}&limit=30${project ? `&project=${encodeURIComponent(project)}` : ""}`;
      try {
        const r = await request<{ hits: Hit[] }>("GET", `/api/search?${qs}`);
        if (mine === seq) hits.set({ q, rows: r.hits ?? [] });
      } catch (e) {
        // Titles are already on screen; a failed message search stays quiet
        // unless it is the only thing that could have matched.
        if (mine === seq && e instanceof ApiError) hits.set({ q, rows: [] });
      }
    }, 150);
  };
  const toggleScope = () => {
    scopeAll.update((v) => !v);
    setQuery(query());
  };
  // `/search <text>` prefills the field; ask Core for message matches too, not just title matches.
  if (switcherQuery().trim()) setTimeout(() => setQuery(switcherQuery()), 0);

  const sessions = () => {
    m();
    const all = model.sessionsRecent().filter((s) => !s.parent_id);
    const project = currentProject();
    return all.filter((s) => scopeAll() || !project || s.project_id === project);
  };
  const rows = (): Row[] => {
    m();
    const q = query().trim();
    const out: Row[] = sessions()
      .filter((s) => fuzzyMatch(q, s.title || "Untitled"))
      .map((s) => ({ id: s.id, title: s.title || "Untitled", hint: relTime(s.updated, (tick(), Date.now())), message: false }));
    const h = hits();
    if (!h || h.q !== q) return out;
    const seen = new Set(out.map((row) => row.id));
    for (const hit of h.rows) {
      if (seen.has(hit.session_id)) continue;
      const s = model.sessions.get(hit.session_id);
      if (!s || s.parent_id) continue;
      seen.add(hit.session_id);
      out.push({ id: s.id, title: s.title || "Untitled", hint: truncate(hit.snippet.replace(/[\[\]]/g, ""), 32), message: true });
    }
    return out;
  };
  const clamp = (n: number) => Math.max(0, Math.min(rows().length - 1, n));
  const choose = (row = rows()[clamp(idx())]) => {
    if (!row) return;
    close();
    void selectSession(row.id);
  };

  try { keymap.bind("down", () => idx.set(clamp(idx() + 1)), { when: "always" }); } catch {}
  try { keymap.bind("up", () => idx.set(clamp(idx() - 1)), { when: "always" }); } catch {}
  try { keymap.bind("tab", toggleScope, { when: "always" }); } catch {}

  return (
    <Modal size="medium" label="Session switcher" testID="session-switcher"
      onCancel={() => escape()} onOpenChange={(open) => { if (!open) close(); }}>
      <View role="menu" class="gap-2">
        <ModalTitle title={`Search sessions \u00b7 ${scopeAll() ? "Global" : "Project"}`} />
        <View class="border-b border-border">
          <Input autoFocus value={query()} placeholder="type to find a session" accessibilityLabel="Search sessions"
            testID="session-switcher-input" onChange={({ value }) => setQuery(value)} onSubmit={() => choose()} submitBehavior="submit"
            class="h-10 text-[13px] text-foreground placeholder:text-muted-foreground" />
        </View>
        <If when={rows().length > 0} else={
          <View class="py-3">
            <Text class={T.sec}>{query().trim() ? `No session matches \u201c${query()}\u201d.` : "No sessions yet."}</Text>
          </View>
        }>
          <Scroll style={{ maxHeight: Math.min(380, modalBodyHeight(40)) }} testID="session-switcher-list">
            <View class="gap-0.5">
              <Each of={rows()} key={(row) => row.id}>
                {(row, i) => {
                  const state = () => switchState(row().id);
                  return (
                    <Button onPress={() => choose(row())} selected={idx() === clamp(i())}
                      accessibilityLabel={`${row().title}, ${state().label}`} testID={`switch-session-${row().id}`}
                      class={`group/row relative rounded-(--radius) px-3 py-0.5 min-h-8 justify-center hover:bg-accent ${idx() === clamp(i()) ? "bg-selection" : ""} ${FOCUS}`}>
                      <View class="flex-row items-center gap-2">
                        <If when={state().spin} else={<Text testID={`switch-glyph-${row().id}`} accessibilityLabel={state().label}
                          class={`w-3.5 shrink-0 text-center text-[13px] leading-5 ${state().tone}`}>{state().glyph}</Text>}>
                          <Spinner testID={`switch-glyph-${row().id}`} class={`w-3.5 shrink-0 text-center text-[13px] leading-5 ${state().tone}`} label={state().label} />
                        </If>
                        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 text-[13px] leading-5 ${row().message ? "text-muted-foreground" : "text-popover-foreground"}`}>{row().title}</Text>
                        <If when={!!row().hint}>
                          <Text style={{ numberOfLines: 1 }} class={`${T.meta} shrink-0 max-w-[190px]`}>{row().hint}</Text>
                        </If>
                      </View>
                    </Button>
                  );
                }}
              </Each>
            </View>
          </Scroll>
        </If>
        <ModalFooter hints="tab scope · ↑↓ move · enter run · esc close"
          right={rows().length ? `${clamp(idx()) + 1} of ${rows().length}` : undefined} />
      </View>
    </Modal>
  );
}
