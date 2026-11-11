// The new-session view and the starting-context sections (DESIGN_PLAN D10, I9). The sections are one
// renderer for both places: the new-session view (a client-side preview) and the timeline's context
// row (the frozen snapshot). Every preview line comes from text the model receives (the system
// prompt); an agent's description is never sent, so it is never shown here.
import { Scroll, Text, View, effect, signal } from "nsh";
import { Button, TextView } from "nsh/base";
import { Btn, FOCUS, Icon, PROSE, T, TS } from "./kit";
import { run, toggleDraftInventory } from "../commands";
import {
  type ContextEntry, contextDetail, agentCatalog, currentProject, info, pendingFirstPrompt, rememberedModel, draftPlanContext, loadComposerData, loadDraftContext, loadStartingInventory, m, model, modelCatalog, sessionDraft, userHome,
} from "../store";
import { request } from "../core/transport";
import { Modal, modalBodyHeight, ModalFooter, ModalTitle } from "./modal";
import { sessionInventoryRows } from "../skills-mcp";
import type { InventoryKind, InventoryRow, McpEntry, SkillEntry } from "../skills-mcp";
import { TimelineColumn } from "./reading-column";
import { contextGrid } from "./geometry";
import "../../fonts/MonaspaceArgon-Regular.otf";
import "../../fonts/MonaspaceArgon-Medium.otf";
import "../../fonts/MonaspaceArgon-Bold.otf";

// --- the shared section model -----------------------------------------------------------------
export type CtxBody =
  | { kind: "grid"; items: { name: string; on: boolean }[] }
  | { kind: "inventory"; items: InventoryRow[] }
  | { kind: "none" };
export interface CtxSection { key: string; title: string; tokens?: number; count?: number; meta: string; body: CtxBody; detail: string; markdown?: boolean; entries?: ContextEntry[] }

/** `17580` reads `17,580`. */
export function thousands(n: number): string {
  const d = String(Math.max(0, Math.round(n)));
  let out = "";
  for (let i = 0; i < d.length; i++) { if (i > 0 && (d.length - i) % 3 === 0) out += ","; out += d[i]; }
  return out;
}
/** The first meaningful line of a system prompt: not blank, not a heading and not an HTML comment. */
export function firstPromptLine(prompt: string): string {
  return prompt.split("\n").map((l) => l.trim()).find((l) => l && !l.startsWith("#") && !l.startsWith("<!--")) ?? "";
}
export function share(total: number, window?: number): string | undefined {
  if (!window || window <= 0) return undefined;
  const pct = (total / window) * 100;
  return pct > 0 && pct < 0.1 ? "<0.1%" : `${pct.toFixed(1)}%`;
}
export const contextTokens = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}K` : thousands(n);
const utf8 = (s: string) => { let n = 0; for (const c of s) { const p = c.codePointAt(0)!; n += p < 0x80 ? 1 : p < 0x800 ? 2 : p < 0x10000 ? 3 : 4; } return n; };

/** Tokens the generated system prompt costs before the agent body and the instruction file; the figures are the client's estimate until Core answers `POST /api/draft/context` (PLAN K4). */
export const PROMPT_BASE_TOKENS = 900;

/** The sections the next session will start with, estimated on the client. */
export function previewSections(i: {
  agent: string; prompt: string; off?: string[];
  disabled_tools?: string[];
  file?: { path: string; bytes: number; line: string } | null;
  /** Core's answer for this draft: its counts replace the estimates. */
  core?: { used: number; parts: { instructions: number; tools: number; instruction_file: number }; tools: string[]; skills: SkillEntry[]; mcp: McpEntry[] } | null;
}): { sections: CtxSection[]; total: number } {
  // The lists are Core's plan for this draft, never a client guess: empty until it answers.
  const tools = [...(i.core?.tools ?? [])];
  const off = i.off ?? [];
  const fileTokens = i.core?.parts.instruction_file ?? (i.file ? Math.ceil(i.file.bytes / 4) : 0);
  const promptTokens = i.core?.parts.instructions ?? PROMPT_BASE_TOKENS + Math.ceil(Array.from(i.prompt).length / 4) + fileTokens;
  const toolTokens = i.core?.parts.tools;
  const sections: CtxSection[] = [
    { key: "system", title: "System prompt", tokens: promptTokens, meta: "", body: { kind: "none" }, markdown: true, detail: `Agent prompt preview (the full starting context is available after the session starts):\n\n${i.prompt}` },
    i.file
      ? { key: "instructions", title: "AGENTS.md", tokens: fileTokens, meta: "", body: { kind: "none" }, markdown: true, detail: `Project instruction preview: ${i.file.path}\n\n${i.file.line}` }
      : { key: "instructions", title: "AGENTS.md", meta: "unavailable or not read", body: { kind: "none" }, detail: "The project instruction file is not available in this preview; the starting context will be known when the session starts." },
    {
      key: "tools", title: "Tools", tokens: toolTokens, count: tools.length, meta: tools.some((t) => off.includes(t)) ? `${tools.filter((t) => off.includes(t)).length} off` : "",
      entries: tools.map((t) => ({ key: t, name: t, description: "", full: "The schema is recorded when the session starts." })),
      body: { kind: "grid", items: tools.map((t) => ({ name: t, on: !off.includes(t) })) }, detail: `Estimated tool inventory; schemas are available after the session starts.\n\n${tools.map((t) => `${t}${off.includes(t) ? " (off)" : ""}`).join("\n")}`,
    },
  ];
  sections.push(...inventorySections(
    sessionInventoryRows(i.core?.skills ?? [], i.core?.mcp ?? [], i.disabled_tools ?? i.off ?? []),
    i.core ? undefined : "loading…",
  ));
  // The instruction file is part of the system prompt: the total counts the prompt and the tool schemas.
  return { sections, total: i.core?.used ?? promptTokens + (toolTokens ?? 0) };
}

function itemDetail(item: InventoryRow): string {
  const lines = [`${item.name}: ${item.on ? "on" : "off"}`];
  if (item.description) lines.push(item.description);
  if (item.tools !== undefined && item.tools !== null) lines.push(`${item.tools} tools`);
  if (item.error) lines.push(`unavailable: ${item.error}`);
  if (item.warnings?.length) lines.push(`warning: ${item.warnings.join(" · ")}`);
  if (item.locked) lines.push("new session required to enable");
  return lines.join("\n");
}

/** A skill's full text and a server's facts come from Core when the entry is opened. */
function entryOf(item: InventoryRow): ContextEntry {
  const name = encodeURIComponent(item.name);
  const project = currentProject() ?? undefined;
  if (item.kind === "skill") {
    return {
      key: item.name, name: item.name, description: item.description ?? "", markdown: true,
      load: async () => {
        const skill = await request<any>("GET", `/api/skill/${name}`, { query: { project } });
        const resources: string[] = skill.children ?? skill.files ?? [];
        const content = String(skill.content ?? skill.body ?? "This skill has no body.");
        const markdown = content.replace(/^(---\r?\n[\s\S]*?\r?\n---)(\r?\n|$)/, "```yaml\n$1\n```\n");
        return `## Description\n\n${skill.description ?? item.description ?? ""}\n\n## Supporting resources\n\n${resources.length ? resources.map((name) => `- \`${name.replaceAll("`", "\\`")}\``).join("\n") : "No other files or folders."}\n\n## SKILL.md\n\n${markdown}`;
      },
    };
  }
  return {
    key: item.name, name: item.name,
    description: [item.meta, item.tools === null || item.tools === undefined ? "" : `${item.tools} tools`, item.error ?? "", ...(item.warnings ?? [])].filter(Boolean).join(" · "),
    load: async () => {
      const row = await request<any>("GET", `/api/mcp/${name}`).catch(() => request<any>("GET", `/api/project/${project}/mcp/${name}`)).catch(() => null);
      return row ? `${itemDetail(item)}\n\n${JSON.stringify(row, null, 2)}` : itemDetail(item);
    },
  };
}

export function inventorySections(items: InventoryRow[], loading?: string, error?: string): CtxSection[] {
  return (["skill", "mcp"] as const).map((kind) => {
    const rows = items.filter((item) => item.kind === kind);
    const title = kind === "skill" ? "Skills" : "MCP";
    const noun = kind === "skill" ? "skills" : "servers";
    const off = rows.filter((item) => !item.on).length;
    const duplicates = new Set(rows.flatMap((item) => item.warnings?.filter((warning) => warning.includes("is defined in both")) ?? [])).size;
    const meta = loading ?? error ?? (duplicates
      ? `${duplicates} duplicate name${duplicates === 1 ? "" : "s"}; .mcp.json wins`
      : rows.length === 0 ? "none" : off ? `${off} off` : "");
    return {
      key: kind === "skill" ? "skills" : "mcp", title, count: loading || error ? undefined : rows.length, meta,
      entries: rows.map((item) => entryOf(item)),
      body: rows.length ? { kind: "inventory", items: rows } : { kind: "none" },
      detail: rows.length ? rows.map(itemDetail).join("\n\n") : error ?? (loading ? "Loading…" : `No ${noun} found.`),
    };
  });
}

/** A session's `disabled_tools` (Core sends an array or a JSON string). */
export function disabledOf(session: { disabled_tools?: unknown } | null | undefined): string[] {
  const v = session?.disabled_tools;
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === "string");
  if (typeof v === "string") { try { const p = JSON.parse(v); return Array.isArray(p) ? p.filter((x): x is string => typeof x === "string") : []; } catch { return []; } }
  return [];
}

/** The sections of a frozen starting context (`GET …/snapshot`), as the timeline shows them. */
/** A ZMX child's starting context: what Nexus knows about the CLI run, never a complete prompt (the CLI adds context Nexus cannot see). */
export function zmxSections(w: { label: string; disclosure?: string; permission_mode?: string; directory?: string; model?: string; not_applied?: string[]; session_id?: string }): { sections: CtxSection[]; total: number } {
  const facts = [w.model ? `model ${w.model}` : "", w.permission_mode ? `permission ${w.permission_mode}` : "", w.directory ? `cwd ${w.directory}` : ""].filter(Boolean).join(" · ");
  const section = (key: string, title: string, meta: string, detail: string): CtxSection => ({ key, title, meta, body: { kind: "none" }, detail });
  return {
    total: 0,
    sections: [
      section("zmx", w.label, "context incomplete", w.disclosure ?? "The CLI adds context Nexus cannot see."),
      section("zmx-run", "Run", facts || "not started", facts || "The CLI has not started yet."),
      section("zmx-settings", "Settings", "CLI inherited", "The CLI's own settings, memory, MCP servers and plugins apply."),
      section("zmx-not-applied", "Not applied", (w.not_applied ?? []).join(", "), "Nexus permission rules, disabled tools and step limits do not reach inside the CLI, which ran its own tools."),
    ],
  };
}

export function startingSections(s: any, off: string[], inventory?: { skills?: SkillEntry[]; mcp?: McpEntry[] }): { sections: CtxSection[]; total: number } {
  const sys = String(s.system_prompt ?? "");
  const file = s.instruction_file ?? {};
  const names: string[] = Array.isArray(s.tools) ? s.tools.filter((x: unknown): x is string => typeof x === "string") : [];
  const schemas = s.tool_definitions && typeof s.tool_definitions === "object" ? s.tool_definitions : {};
  const schemaText = Object.keys(schemas).length ? JSON.stringify(schemas) : "";
  const sysTokens = Math.ceil(Array.from(sys).length / 4);
  const toolTokens = schemaText ? Math.ceil(Array.from(schemaText).length / 4) : 0;
  const fileBytes = typeof file.bytes === "number" ? file.bytes : undefined;
  const status = file.status === "loaded" ? "loaded" : file.status === "missing" ? "not found" : String(file.status ?? "not read").replaceAll("_", " ");
  const path = String(file.path ?? "");
  const base = path.split("/").filter(Boolean).at(-1) ?? "AGENTS.md";
  const sections: CtxSection[] = [
    { key: "system", title: "System prompt", tokens: sysTokens, meta: "", body: { kind: "none" }, markdown: true, detail: sys || "No system prompt was recorded." },
    {
      key: "instructions", title: base, tokens: fileBytes === undefined ? undefined : Math.ceil(fileBytes / 4),
      meta: [status, path].filter(Boolean).join(" · "), body: { kind: "none" }, markdown: typeof file.content === "string", detail: typeof file.content === "string" ? file.content : JSON.stringify(file, null, 2),
    },
    {
      key: "tools", title: "Tools", tokens: toolTokens || undefined, count: names.length, meta: names.some((n) => off.includes(n)) ? `${names.filter((n) => off.includes(n)).length} off` : "",
      entries: names.map((n) => ({
        key: n, name: n,
        description: typeof schemas[n]?.description === "string" ? firstPromptLine(schemas[n].description) : "",
        full: schemas[n] ? JSON.stringify(schemas[n], null, 2) : "No schema was recorded for this tool.",
      })),
      body: { kind: "grid", items: names.map((n) => ({ name: n, on: !off.includes(n) })) }, detail: schemaText ? `Stored tool definitions (definitions may change during a session):\n\n${JSON.stringify(schemas, null, 2)}` : names.join("\n") || "No tool definitions were recorded.",
    },
  ];
  sections.push(...inventorySections(sessionInventoryRows(inventory?.skills ?? s.skills ?? [], inventory?.mcp ?? s.mcp ?? [], off)));
  if (s.workspace?.active) sections.push({ key: "workspace", title: "Workspace tools", meta: "active", body: { kind: "none" }, detail: JSON.stringify(s.workspace, null, 2) });
  return { sections, total: sysTokens + toolTokens };
}

// --- the renderer -----------------------------------------------------------------------------
const ESTIMATED = "estimated: characters ÷ 4 of Core's starting context";

/** Up to `most` columns, filled downward: five items in two columns read three and two. */
export function inColumns<T>(items: T[], most: number): T[][] {
  const count = Math.max(1, Math.min(most, items.length));
  const height = Math.ceil(items.length / count);
  return Array.from({ length: count }, (_, x) => items.slice(x * height, (x + 1) * height)).filter((c) => c.length);
}

/** One cell's nominal width; long names ellipsize inside it. */
const HEAD = TS.base;

/** A switch cell, as the TUI draws it: the marker switches and the name opens its information. */
function Cell(p: { name: string; on: boolean; locked?: boolean; status?: string; error?: string | null; label: string; testID: string; onToggle: () => void; onInfo: () => void }) {
  return (
    <View class="w-full min-w-0">
      <View class="flex-row items-center min-w-0 h-6">
        <Button role="switch" selected={p.on} onPress={p.onToggle} accessibilityLabel={p.label} testID={p.testID} class={`shrink-0 w-5 h-6 items-center justify-center rounded-(--radius) hover:bg-accent ${FOCUS}`}>
          <Text class={`text-[10px] leading-5 ${p.on ? "text-muted-foreground" : "text-muted-foreground opacity-60"}`}>{p.on ? "●" : "○"}</Text>
        </Button>
        <Button onPress={p.onInfo} accessibilityLabel={`${p.name} details`} testID={`${p.testID}-info`} class={`shrink min-w-0 flex-row items-center gap-2 rounded-(--radius) px-1 h-6 hover:bg-accent ${FOCUS} ${p.locked ? "opacity-60" : ""}`}>
          <Text style={{ numberOfLines: 1 }} class={`shrink min-w-0 ${TS.mono} ${p.on ? "text-foreground-secondary" : "text-muted-foreground line-through opacity-60"}`}>{p.name}</Text>
          <If when={!!p.status}><Text style={{ numberOfLines: 1 }} class={T.meta}>{p.status!}</Text></If>
        </Button>
      </View>
      <If when={!!p.error}><Text style={{ numberOfLines: 2 }} class={`pl-6 ${TS.base} text-warning`}>{p.error!}</Text></If>
    </View>
  );
}

function Section(p: { s: CtxSection; estimated: boolean; width: number; onOpen: () => void; onItem: (name: string) => void; onInventoryToggle?: (kind: InventoryKind, name: string, locked: boolean) => void }) {
  const s = p.s;
  const offNote = /\boff$/.test(s.meta);
  // Tools take up to four columns and skills and servers two, as many as fit.
  const names = () => s.body.kind === "grid" ? s.body.items.map((item) => item.name) : s.body.kind === "inventory" ? s.body.items.map((item) => [item.name, item.meta, item.tools == null ? "" : `${item.tools} tools`, item.locked ? "new session" : ""].filter(Boolean).join(" · ")) : [];
  const grid = (most: number) => contextGrid(names(), p.width, most);
  const shownMeta = !offNote && !!s.meta && s.tokens === undefined;
  return (
    <View testID={`ctx-${s.key}`}>
      <Button onPress={p.onOpen} testID={`ctx-${s.key}-open`} accessibilityLabel={`View ${s.title} details`}
        tooltip={p.estimated && s.tokens !== undefined ? <Text class={`${TS.base} text-popover-foreground bg-popover rounded-(--radius) px-2 py-1`}>{ESTIMATED}</Text> : undefined}
        class={`self-stretch flex-row items-center gap-1.5 min-h-6 rounded-(--radius) px-1 -mx-1 hover:bg-accent ${FOCUS}`}>
        <Text class={`shrink-0 ${HEAD} font-semibold text-foreground-secondary`}>{s.title}</Text>
        <If when={s.count !== undefined}><Text class={`shrink-0 ${HEAD} text-muted-foreground tabular-nums`}>{`[${s.count}]`}</Text></If>
        <If when={offNote}><Text class={`shrink-0 ${HEAD} text-muted-foreground`}>{`(${s.meta})`}</Text></If>
        <If when={s.tokens !== undefined}><Text class={`shrink-0 ${HEAD} text-foreground-secondary tabular-nums`}>{`${p.estimated ? "~" : ""}${contextTokens(s.tokens ?? 0)}`}</Text></If>
        <If when={shownMeta}><Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`shrink min-w-0 ml-1 ${HEAD} text-muted-foreground`}>{s.meta}</Text></If>
      </Button>
      <If when={s.body.kind === "grid"}>
        <View class="flex-row pl-1" testID={`ctx-${s.key}-grid`}>
          <Each of={inColumns((s.body as { items: { name: string; on: boolean }[] }).items, grid(4).columns)} key={(_, i) => `${i}`}>
            {(column) => (
              <View class="min-w-0 shrink-0" style={{ width: grid(4).cell }}>
                <Each of={column()} key={(c) => c.name}>
                  {(c) => <Cell name={c().name} on={c().on} label={`${c().name} ${c().on ? "on" : "off"}`} testID={`ctx-chip-${c().name}`}
                    onToggle={() => p.onInventoryToggle?.("tool", c().name, false)} onInfo={() => p.onItem(c().name)} />}
                </Each>
              </View>
            )}
          </Each>
        </View>
      </If>
      <If when={s.body.kind === "inventory" && !(s.key === "skills" && (s.count ?? 0) > 12)}>
        <View class="flex-row pl-1" testID={`ctx-${s.key}-inventory`}>
          <Each of={inColumns((s.body as { items: InventoryRow[] }).items, grid(2).columns)} key={(_, i) => `${i}`}>
            {(column) => (
              <View class="min-w-0 shrink-0" style={{ width: grid(2).cell }}>
                <Each of={column()} key={(item) => item.name}>
                  {(item) => <Cell name={item().name} on={item().on} locked={item().locked} error={item().error}
                    status={[item().meta, item().tools === null || item().tools === undefined ? "" : `${item().tools} tools`, item().locked ? "new session" : ""].filter(Boolean).join(" · ")}
                    label={`${item().name} ${item().on ? "on" : "off"}${item().locked ? ", new session" : ""}`} testID={`ctx-${item().kind}-row-${item().name.replace(/[^A-Za-z0-9_-]/g, "-")}`}
                    onToggle={() => p.onInventoryToggle?.(item().kind, item().name, item().locked)} onInfo={() => p.onItem(item().name)} />}
                </Each>
              </View>
            )}
          </Each>
        </View>
      </If>
    </View>
  );
}

/** One entry of the context modal: name and description; opening it shows its schema or its full text. */
function EntryRow(p: { entry: () => ContextEntry; focus: boolean }) {
  const e = p.entry;
  const open = signal(p.focus);
  const loaded = signal<string | undefined>(e().full);
  const load = () => {
    const fn = e().load;
    if (!fn || loaded() !== undefined) return;
    fn().then((text) => loaded.set(text)).catch((error) => loaded.set(`Could not load this: ${error instanceof Error ? error.message : String(error)}`));
  };
  if (p.focus) load();
  const id = e().key.replace(/[^A-Za-z0-9_-]/g, "-");
  return (
    <View testID={`context-entry-${id}`}>
      <Button onPress={() => { const next = !open(); open.set(next); if (next) load(); }} accessibilityLabel={`${e().name}, ${open() ? "collapse" : "expand"}`} testID={`context-entry-${id}-toggle`}
        class={`self-stretch flex-row items-start gap-2 rounded-(--radius) px-1 py-1 hover:bg-accent ${FOCUS}`}>
        <View class="w-4 h-5 shrink-0 items-center justify-center"><Icon name={open() ? "chevron-down" : "chevron-right"} size={14} class="tint-muted-foreground" /></View>
        <View class="flex-1 min-w-0">
          <Text class={`${TS.mono} text-foreground`}>{e().name}</Text>
          <If when={!!e().description}><Text style={{ numberOfLines: 2 }} class={T.sec}>{e().description}</Text></If>
        </View>
      </Button>
      <If when={open()}>
        <View class="ml-6 mr-1 mb-2" testID={`context-entry-${id}-full`}>
          <If when={loaded() !== undefined} else={<Text class={T.meta}>Loading…</Text>}>
            {() => <If when={!!e().markdown} else={<Text style={{ userSelect: "text" }} class={`${TS.mono} text-foreground-secondary`}>{loaded()!}</Text>}>
              <TextView selectable links="open" markdown={loaded()!} classes={PROSE} />
            </If>}
          </If>
        </View>
      </If>
    </View>
  );
}

function SkillPicker(p: { entries: ContextEntry[]; rows: InventoryRow[]; focus?: string; toggle?: (kind: InventoryKind, name: string, locked: boolean) => void }) {
  const selected = signal<ContextEntry | null>(null);
  const focused = signal(p.focus ?? p.entries[0]?.key);
  const focusedAction = signal<"name" | "toggle">("name");
  const names = new Map<string, { focus: () => void }>();
  const text = signal("Loading…");
  const close = () => {
    const entry = selected();
    if (!entry) return;
    selected.set(null);
    setTimeout(() => names.get(entry.key)?.focus(), 0);
  };
  const open = (entry: ContextEntry) => {
    selected.set(entry);
    text.set("Loading…");
    (entry.load ? entry.load() : Promise.resolve(entry.full ?? "Nothing more recorded.")).then((value) => {
      if (selected() === entry) text.set(value);
    }).catch((error) => { if (selected() === entry) text.set(`Could not load this: ${String(error)}`); });
  };
  const initial = p.entries.find((entry) => entry.key === p.focus);
  if (initial) setTimeout(() => open(initial), 0);
  else setTimeout(() => names.get(p.entries[0]?.key)?.focus(), 0);
  return <>
    <View class="flex-row gap-3">
      <Each of={inColumns(p.entries, 2)} key={(_, index) => String(index)}>{(column) => <View class="flex-1 min-w-0 gap-3">
        <Each of={column()} key={(entry) => entry.key}>{(entry) => {
          const row = () => p.rows.find((row) => row.name === entry().key);
          return <View class={`p-3 gap-2 min-w-0 ${focused() === entry().key ? "bg-skill-card-focus" : "bg-skill-card"}`} testID={`skill-card-${entry().key}`}>
            <View role="button" focusable ref={(node: any) => names.set(entry().key, node)} onFocus={() => { focused.set(entry().key); focusedAction.set("name"); }} onPress={() => open(entry())} accessibilityLabel={`${entry().name} details`} testID={`skill-name-${entry().key}`} class="self-stretch min-w-0 focus-visible:outline-none">
              <Text class={`${TS.mono} font-semibold text-foreground ${focused() === entry().key && focusedAction() === "name" ? "underline" : ""}`}>{entry().name}</Text>
            </View>
            <Text style={{ numberOfLines: 2 }} class={T.sec}>{entry().description}</Text>
            <View role="switch" focusable accessibilityState={{ selected: row()?.on ?? false }} onFocus={() => { focused.set(entry().key); focusedAction.set("toggle"); }} onPress={() => p.toggle?.("skill", entry().key, row()?.locked ?? true)} accessibilityLabel={`${entry().name} ${row()?.on ? "on" : "off"}`} testID={`skill-toggle-${entry().key}`} class="self-start px-2 py-1 hover:bg-accent focus-visible:outline-none">
              <Text class={`${T.sec} ${focused() === entry().key && focusedAction() === "toggle" ? "underline" : ""}`}>{row()?.locked ? "Locked · new session" : row()?.on ? "● Enabled" : "○ Disabled"}</Text>
            </View>
          </View>;
        }}</Each>
      </View>}</Each>
    </View>
    <If when={selected()}>{() => <Modal size="large" label={`${selected()!.name} details`} testID="skill-detail" onCancel={close} onOpenChange={(open) => { if (!open) close(); }}>
      <View class="gap-3">
        <ModalTitle title={selected()!.name} />
        <Scroll focusable ref={(node: any) => { setTimeout(() => node?.focus(), 0); }} style={{ height: Math.min(480, modalBodyHeight()) }} testID="skill-detail-scroll"><TextView selectable links="open" markdown={text()} classes={PROSE} /></Scroll>
        <ModalFooter hints="esc back to skills" />
      </View>
    </Modal>}</If>
  </>;
}

/** A single detail dialog remains mounted even when the context section is folded. */
export function ContextDetailModal() {
  const detail = contextDetail;
  const body = () => detail()!;
  return <If when={detail()}>{() => <Modal size="large" label={`${detail()!.title} details`} testID="context-detail" onCancel={() => detail.set(null)} onOpenChange={(open) => { if (!open) detail.set(null); }}>
    <View class="gap-3">
      <ModalTitle title={body().title} />
      <Scroll style={{ height: Math.min(480, modalBodyHeight()) }} testID="context-picker-scroll">
        <If when={!!body().entries} else={
          <If when={!!body().markdown} else={<Text testID="context-detail-text" style={{ userSelect: "text" }} class={`${TS.mono} text-foreground-secondary`}>{body().detail}</Text>}>
            <View testID="context-detail-markdown"><TextView selectable links="open" markdown={body().detail} classes={PROSE} /></View>
          </If>
        }>
          <If when={body().skillRows} else={<View class="gap-0.5">
            <If when={(body().entries ?? []).length === 0}><Text class={T.sec}>{body().detail}</Text></If>
            <Each of={body().entries ?? []} key={(entry) => entry.key}>{(entry) => <EntryRow entry={entry} focus={body().focus === entry().key} />}</Each>
          </View>}>
            <SkillPicker entries={body().entries ?? []} rows={body().skillRows!.rows} toggle={body().skillRows!.toggle} focus={body().focus} />
          </If>
        </If>
      </Scroll>
      <ModalFooter hints={body().skillRows ? "tab focus · enter activate · esc close" : "esc close"} />
    </View>
  </Modal>}</If>;
}

/** The sections and the total as text (no bar). */
export function ContextSections(p: { sections: CtxSection[]; total: number; estimated: boolean; window?: number; reconstructed?: boolean; onInventoryToggle?: (kind: InventoryKind, name: string, locked: boolean) => void }) {
  const pct = () => share(p.total, p.window);
  const detail = contextDetail;
  const cardWidth = signal(0);
  effect(() => {
    const active = detail();
    const section = p.sections.find((section) => section.key === "skills");
    if (active?.skillRows && section?.body.kind === "inventory" && JSON.stringify(active.skillRows.rows) !== JSON.stringify(section.body.items)) {
      detail.set({ ...active, skillRows: { ...active.skillRows, rows: section.body.items } });
    }
  });
    return (
    <View class="w-full bg-card px-4 py-6 gap-4" testID="context-sections"
      onLayout={(b: any) => cardWidth.set(b.bounds?.size?.width ?? b.size?.width ?? 0)}>
      <If when={!!p.reconstructed}><Text class={`${TS.base} text-warning`}>reconstructed</Text></If>
      <Each of={p.sections} key={(s) => `${s.key}|${s.meta}|${s.count ?? ""}|${s.tokens ?? ""}|${JSON.stringify(s.body)}`}>{(s) => {
        const open = (name?: string) => detail.set({ ...s(), focus: name, skillRows: s().key === "skills" && s().body.kind === "inventory" ? { rows: (s().body as { items: InventoryRow[] }).items, toggle: p.onInventoryToggle } : undefined });
        return <Section s={s()} estimated={p.estimated} width={cardWidth()} onOpen={() => open()} onItem={open} onInventoryToggle={p.onInventoryToggle} />;
      }}</Each>
      <View class="flex-row items-center gap-2" testID="context-total">
        <Text class={`${HEAD} font-semibold text-foreground-secondary`}>total</Text>
        <Text class={`${HEAD} text-foreground-secondary tabular-nums`}>{`${p.estimated ? "~" : ""}${thousands(p.total)} tok`}</Text>
        <If when={!!pct()}><Text class={`${HEAD} text-muted-foreground`}>{`· ${pct()} of the window`}</Text></If>
      </View>
    </View>
  );
}

/** The starting context in the timeline and before a session starts: one card, one width. */
export function StartingContext(p: { sections: CtxSection[]; total: number; estimated: boolean; window?: number; reconstructed?: boolean; onInventoryToggle?: (kind: InventoryKind, name: string, locked: boolean) => void }) {
  return (
    <View class="w-full my-4" testID="context-section">
      <ContextSections
        sections={p.sections} total={p.total} estimated={p.estimated} window={p.window} reconstructed={p.reconstructed}
        onInventoryToggle={p.onInventoryToggle}
      />
    </View>
  );
}

// --- the view ---------------------------------------------------------------------------------
const prompts = signal<Record<string, string>>({});
const files = signal<Record<string, { bytes: number; line: string } | null>>({});
const asked = new Set<string>();

function ensure(project: string, agent: string) {
  const pk = `p|${project}|${agent}`;
  if (!asked.has(pk)) {
    asked.add(pk);
    request<any>("GET", `/api/agent/${agent}`, { query: { project } })
      .then((a) => prompts.update((all) => ({ ...all, [`${project}|${agent}`]: String(a?.prompt ?? "") })))
      .catch(() => asked.delete(pk));
  }
  const fk = `f|${project}`;
  if (!asked.has(fk)) {
    asked.add(fk);
    request<any>("GET", `/api/project/${project}/file`, { query: { path: "AGENTS.md" } })
      .then((r) => { const text = String(r?.content ?? ""); files.update((all) => ({ ...all, [project]: { bytes: utf8(text), line: firstPromptLine(text) } })); })
      .catch(() => files.update((all) => ({ ...all, [project]: null }))); // unavailable in the preview
  }
}

export function NewSessionView() {
  const project = () => { m(); const id = currentProject(); return id ? model.projects.get(id) ?? null : null; };
  const agentName = () => sessionDraft().agent ?? "build";
  const agent = () => agentCatalog().agents.find((a) => a.name === agentName());
  effect(() => {
    const p = project();
    if (!p) return;
    loadComposerData(p.id);
    loadStartingInventory(p.id);
    ensure(p.id, agentName());
  });
  // `project()` changes on every model bump; the draft key decides when Core is asked again.
  let opened = true;
  effect(() => { const p = project(); if (p) { void loadDraftContext(p.id, opened); opened = false; } });
  const modelRef = () => sessionDraft().model || agent()?.model || rememberedModel(currentProject(), agentName());
  const modelEntry = () => modelCatalog().find((e) => e.ref === modelRef());
  const window = () => { const c = Number(modelEntry()?.context); return Number.isFinite(c) && c > 0 ? c : undefined; };
  const fullPath = () => {
    const p = project(); if (!p) return "";
    const stateHome = info()?.home ?? "";
    const home = (userHome() || (/\/\.nexus(10)?$/.test(stateHome) ? stateHome.replace(/\/\.nexus(10)?$/, "") : "")).replace(/\/$/, "");
    return home && (p.path === home || p.path.startsWith(`${home}/`)) ? `~${p.path.slice(home.length)}` : p.path;
  };
  const preview = () => {
    const p = project()!;
    return previewSections({
      core: draftPlanContext(p.id),
      agent: agentName(), prompt: prompts()[`${p.id}|${agentName()}`] ?? "",
      disabled_tools: sessionDraft().disabled_tools,
      file: files()[p.id] ? { path: `${fullPath().replace(/\/$/, "")}/AGENTS.md`, ...files()[p.id]! } : null,
    });
  };
  return (
    <View class="flex-1 min-h-0" testID="new-session">
      <If when={project()} else={
        <View class="flex-1 items-center justify-center p-6">
          <View class="w-full max-w-[560px] gap-2">
            <Text class={T.title}>Welcome to Nexus</Text>
            <Text class={T.sec}>Add a project directory to begin. Sessions run inside a project and keep everything the model sees in one timeline.</Text>
            <View class="flex-row pt-1"><Btn label="Add project" icon="plus" kind="primary" onPress={() => run("project.add")} testID="welcome-add-project" /></View>
          </View>
        </View>
      }>
        {() => (
          <Scroll class="flex-1 min-h-0">
            <TimelineColumn class="items-center py-3" testID="new-session-column">
              <StartingContext sections={preview().sections} total={preview().total} estimated window={window()} onInventoryToggle={(kind, name, locked) => toggleDraftInventory(kind, name, locked)} />
              <If when={pendingFirstPrompt()}>
                {() => <View class="w-full px-4 py-3 gap-2" testID="first-prompt-sending">
                  <Text class={T.sec}>You · Sending…</Text>
                  <Text class={`${TS.body} text-foreground`}>{pendingFirstPrompt()!.text}</Text>
                  <If when={pendingFirstPrompt()!.images > 0}><Text class={T.sec}>{`[${pendingFirstPrompt()!.images} image${pendingFirstPrompt()!.images === 1 ? "" : "s"}]`}</Text></If>
                </View>}
              </If>
            </TimelineColumn>
          </Scroll>
        )}
      </If>
    </View>
  );
}
