// The new-session view and the starting-context sections (DESIGN_PLAN D10, I9). The sections are one
// renderer for both places: the new-session view (a client-side preview) and the timeline's context
// row (the frozen snapshot). Every preview line comes from text the model receives (the system
// prompt); an agent's description is never sent, so it is never shown here.
import { Scroll, Text, View, effect, signal, windowSize } from "nsh";
import { Button } from "nsh/base";
import { Btn, FOCUS, T } from "./kit";
import { run, toggleDraftInventory } from "../commands";
import {
  contextDetail, agentCatalog, currentProject, info, rememberedModel, draftContext, draftContextKey, loadComposerData, loadDraftContext, loadStartingInventory, m, model, modelCatalog, sessionDraft, userHome,
} from "../store";
import { request } from "../core/transport";
import { Modal } from "./modal";
import { sessionInventoryRows } from "../skills-mcp";
import type { InventoryKind, InventoryRow, McpEntry, SkillEntry } from "../skills-mcp";
import "../../fonts/MonaspaceArgon-Regular.otf";
import "../../fonts/MonaspaceArgon-Medium.otf";
import "../../fonts/MonaspaceArgon-Bold.otf";

// --- the shared section model -----------------------------------------------------------------
export type CtxBody =
  | { kind: "preview"; text: string }
  | { kind: "grid"; items: { name: string; on: boolean }[] }
  | { kind: "inventory"; items: InventoryRow[] }
  | { kind: "none" };
export interface CtxSection { key: string; title: string; tokens?: number; meta: string; body: CtxBody; detail: string }

/** `17580` reads `17,580`. */
export function thousands(n: number): string {
  const d = String(Math.max(0, Math.round(n)));
  let out = "";
  for (let i = 0; i < d.length; i++) { if (i > 0 && (d.length - i) % 3 === 0) out += ","; out += d[i]; }
  return out;
}
/** `17.6k` for the folded line; plain digits below a thousand. */
export const compactTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n)));
/** The folded line after its fold marker: `Context · 4 items · ~17.6k tokens`. */
export function foldedMeta(items: number, total: number, estimated: boolean): string {
  return `· ${items} items · ${estimated ? "~" : ""}${compactTokens(total)} tokens`;
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
/** A grid of four columns and three rows; a longer inventory ends in `+N more`. */
export const GRID_CAP = 12;
export function gridCells(items: { name: string; on: boolean }[]): { name: string; on: boolean; more?: boolean }[] {
  if (items.length <= GRID_CAP) return items;
  return [...items.slice(0, GRID_CAP - 1), { name: `+${items.length - (GRID_CAP - 1)} more`, on: true, more: true }];
}
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
    { key: "system", title: "System prompt", tokens: promptTokens, meta: i.agent, body: { kind: "none" }, detail: `Agent prompt preview (the full starting context is available after the session starts):\n\n${i.prompt}` },
    i.file
      ? { key: "instructions", title: "AGENTS.md", tokens: fileTokens, meta: `in the system prompt · ${i.file.path}`, body: { kind: "preview", text: i.file.line }, detail: `Project instruction preview: ${i.file.path}\n\n${i.file.line}` }
      : { key: "instructions", title: "AGENTS.md", meta: "not available in this preview", body: { kind: "none" }, detail: "The project instruction file is not available in this preview; the starting context will be known when the session starts." },
    {
      key: "tools", title: "Tools", tokens: toolTokens, meta: `${tools.length} tools · ${tools.filter((t) => !off.includes(t)).length} on`,
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
  if (item.locked) lines.push("new session required to enable");
  return lines.join("\n");
}

export function inventorySections(items: InventoryRow[], loading?: string, error?: string): CtxSection[] {
  return (["skill", "mcp"] as const).map((kind) => {
    const rows = items.filter((item) => item.kind === kind);
    const title = kind === "skill" ? "Skills" : "MCP";
    const noun = kind === "skill" ? "skills" : "servers";
    const meta = loading ?? error ?? `${rows.length} ${noun} · ${rows.filter((item) => item.on).length} on`;
    return {
      key: kind === "skill" ? "skills" : "mcp", title, meta,
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
export function startingSections(s: any, off: string[], inventory?: { skills?: SkillEntry[]; mcp?: McpEntry[] }): { sections: CtxSection[]; total: number } {
  const sys = String(s.system_prompt ?? "");
  const file = s.instruction_file ?? {};
  const names: string[] = Array.isArray(s.tools) ? s.tools.filter((x: unknown): x is string => typeof x === "string") : [];
  const schemas = s.tool_definitions && typeof s.tool_definitions === "object" ? s.tool_definitions : {};
  const schemaText = Object.keys(schemas).length ? JSON.stringify(schemas) : "";
  const sysTokens = Math.ceil(Array.from(sys).length / 4);
  const toolTokens = schemaText ? Math.ceil(Array.from(schemaText).length / 4) : 0;
  const fileBytes = typeof file.bytes === "number" ? file.bytes : undefined;
  const lines = sys ? sys.split("\n").length : 0;
  const status = file.status === "loaded" ? "loaded" : file.status === "missing" ? "not found" : String(file.status ?? "not read").replaceAll("_", " ");
  const path = String(file.path ?? "");
  const base = path.split("/").filter(Boolean).at(-1) ?? "AGENTS.md";
  const sections: CtxSection[] = [
    { key: "system", title: "System prompt", tokens: sysTokens, meta: `${s.agent?.name ?? "?"} · ${lines} line${lines === 1 ? "" : "s"}`, body: { kind: "none" }, detail: sys || "No system prompt was recorded." },
    {
      key: "instructions", title: base, tokens: fileBytes === undefined ? undefined : Math.ceil(fileBytes / 4),
      meta: [status, fileBytes ? "in the system prompt" : "", path].filter(Boolean).join(" · "), body: { kind: "none" }, detail: typeof file.content === "string" ? file.content : JSON.stringify(file, null, 2),
    },
    {
      key: "tools", title: "Tools", tokens: toolTokens || undefined, meta: `${names.length} tools · ${names.filter((n) => !off.includes(n)).length} on`,
      body: { kind: "grid", items: names.map((n) => ({ name: n, on: !off.includes(n) })) }, detail: schemaText ? `Stored tool definitions (definitions may change during a session):\n\n${JSON.stringify(schemas, null, 2)}` : names.join("\n") || "No tool definitions were recorded.",
    },
  ];
  sections.push(...inventorySections(sessionInventoryRows(inventory?.skills ?? s.skills ?? [], inventory?.mcp ?? s.mcp ?? [], off)));
  if (s.workspace?.active) sections.push({ key: "workspace", title: "Workspace tools", meta: "active", body: { kind: "none" }, detail: JSON.stringify(s.workspace, null, 2) });
  return { sections, total: sysTokens + toolTokens };
}

// --- the renderer -----------------------------------------------------------------------------
const ESTIMATED = "estimated: characters ÷ 4 of Core's starting context";

/** A count that is an estimate reads `~N tok` and says so on hover. */
function Count(p: { text: string; estimated: boolean; class: string }) {
  return (
    <If when={p.estimated} else={<Text class={p.class}>{p.text}</Text>}>
      <Button accessibilityLabel={`${p.text}, ${ESTIMATED}`} testID="ctx-estimate"
        tooltip={<Text class="text-xs text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{ESTIMATED}</Text>}
        class="rounded-(--radius)"><Text class={p.class}>{p.text}</Text></Button>
    </If>
  );
}

function Switch(p: { on: boolean }) {
  return (
    <View class={`w-[18px] h-[10px] shrink-0 rounded-full justify-center ${p.on ? "bg-primary" : "bg-border-strong"}`}>
      <View class={`size-[6px] rounded-full bg-background ${p.on ? "ml-[10px]" : "ml-[2px]"}`} />
    </View>
  );
}

function Section(p: { s: CtxSection; estimated: boolean; onOpen: () => void; onInventoryToggle?: (kind: InventoryKind, name: string, locked: boolean) => void }) {
  const s = p.s;
  return (
    <View class="py-1 gap-1" testID={`ctx-${s.key}`}>
      <View class="flex-row items-center gap-2 min-h-5 rounded-(--radius) hover:bg-accent">
        <Text class={`${T.meta} font-mono text-muted-foreground`}>◆</Text>
        <Button onPress={p.onOpen} testID={`ctx-${s.key}-open`} accessibilityLabel={`View ${s.title} details`} class={`rounded-(--radius) ${FOCUS}`}><Text class="font-mono text-[13px] leading-5 font-semibold text-foreground">{s.title}</Text></Button>
        <If when={s.tokens !== undefined}>
          <Count text={`${p.estimated ? "~" : ""}${thousands(s.tokens ?? 0)} tok`} estimated={p.estimated} class={`${T.sec} font-mono tabular-nums`} />
        </If>
        <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 ${T.meta} font-mono`}>{`· ${s.meta}`}</Text>
      </View>
      <If when={s.body.kind === "preview" && (s.body as { text: string }).text !== ""}>
        <Text style={{ numberOfLines: 1 }} class={`${T.meta} font-mono pl-5`} testID={`ctx-${s.key}-preview`}>{(s.body as { text: string }).text}</Text>
      </If>
      <If when={s.body.kind === "grid"}>
        <View class="flex-row flex-wrap pl-5" testID={`ctx-${s.key}-grid`}>
          <Each of={gridCells((s.body as { items: { name: string; on: boolean }[] }).items)} key={(c) => c.name}>
            {(c) => (
              <View class="w-1/4 pr-1.5 pb-1.5">
                <If when={!c().more} else={<View class="h-6 justify-center px-2"><Text class={`${T.meta} font-mono`}>{c().name}</Text></View>}>
                  <View class={`h-6 flex-row items-center gap-1.5 px-2 ${c().on ? "" : "opacity-60"}`} testID={`ctx-chip-${c().name}`} accessibilityLabel={`${c().name} ${c().on ? "on" : "off"}`}>
                    <Switch on={c().on} />
                    <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 font-mono text-xs leading-4 ${c().on ? "text-foreground-secondary" : "text-muted-foreground line-through"}`}>{c().name}</Text>
                  </View>
                </If>
              </View>
            )}
          </Each>
        </View>
      </If>
      <If when={s.body.kind === "inventory"}>
        <View class="gap-1 pl-5" testID={`ctx-${s.key}-inventory`}>
          <Each of={(s.body as { items: InventoryRow[] }).items} key={(item) => item.name}>
            {(item) => {
              const status = () => [item().meta, item().tools === null || item().tools === undefined ? "" : `${item().tools} tools`].filter(Boolean).join(" · ");
              const idName = item().name.replace(/[^A-Za-z0-9_-]/g, "-");
              return (
                <Button role="switch" selected={item().on} onPress={() => p.onInventoryToggle?.(item().kind, item().name, item().locked)}
                  accessibilityLabel={`${item().name} ${item().on ? "on" : "off"}${item().locked ? ", new session" : ""}`} testID={`ctx-${item().kind}-row-${idName}`}
                  class={`w-full flex-row items-start gap-2 rounded-(--radius) px-2 py-1 hover:bg-accent ${FOCUS} ${item().locked ? "opacity-60" : ""}`}>
                  <View class="pt-1"><Switch on={item().on} /></View>
                  <View class="flex-1 min-w-0 gap-0.5">
                    <View class="flex-row items-center gap-2">
                      <Text style={{ numberOfLines: 1 }} class={`flex-1 min-w-0 font-mono text-xs leading-4 ${item().on ? "text-foreground-secondary" : "text-muted-foreground line-through"}`}>{item().name}</Text>
                      <If when={item().locked}><Text class="font-mono text-[10px] leading-4 text-muted-foreground">new session</Text></If>
                    </View>
                    <If when={!!item().description}><Text style={{ numberOfLines: 2 }} class={`${T.meta} font-mono`}>{item().description}</Text></If>
                    <If when={!!status()}><Text class={`${T.meta} font-mono`}>{status()}</Text></If>
                    <If when={!!item().error}><Text style={{ numberOfLines: 2 }} class="font-mono text-xs leading-4 text-warning">{item().error}</Text></If>
                  </View>
                </Button>
              );
            }}
          </Each>
        </View>
      </If>
    </View>
  );
}

/** A single detail dialog remains mounted even when the context section is folded. */
export function ContextDetailModal() {
  const detail = contextDetail;
  return <If when={detail()}>{() => <Modal width={760} label={`${detail()!.title} details`} testID="context-detail" onCancel={() => detail.set(null)} onOpenChange={(open) => { if (!open) detail.set(null); }}><View class="flex-row items-center min-h-8 gap-3"><Text class="flex-1 font-mono text-[15px] leading-5 font-semibold text-popover-foreground">{detail()!.title}</Text><Text class={`${T.meta} font-mono rounded-(--radius) bg-muted px-1.5`}>esc</Text></View><Scroll style={{ height: Math.min(480, Math.round(windowSize().height * 0.55)) }} contentClass="pt-3"><Text testID="context-detail-text" style={{ userSelect: "text" }} class="font-mono text-[13px] leading-5 text-foreground">{detail()!.detail}</Text></Scroll></Modal>}</If>;
}

/** The sections and the total as text (no bar). */
export function ContextSections(p: { sections: CtxSection[]; total: number; estimated: boolean; window?: number; onInventoryToggle?: (kind: InventoryKind, name: string, locked: boolean) => void }) {
  const pct = () => share(p.total, p.window);
  const detail = contextDetail;
  return (
    <View class="rounded-(--radius-card) bg-card px-3.5 py-2" testID="context-sections">
      <Each of={p.sections} key={(s) => `${s.key}|${s.meta}|${s.tokens ?? ""}|${JSON.stringify(s.body)}`}>{(s) => <Section s={s()} estimated={p.estimated} onOpen={() => detail.set(s())} onInventoryToggle={p.onInventoryToggle} />}</Each>
      <View class="flex-row items-center gap-2 pt-1" testID="context-total">
        <Text class="font-mono text-[13px] leading-5 font-semibold text-foreground">Context total</Text>
        <Count text={`${p.estimated ? "~" : ""}${thousands(p.total)} tokens`} estimated={p.estimated} class="font-mono text-[13px] leading-5 text-foreground tabular-nums" />
        <If when={!!pct()}><Text class={`${T.meta} font-mono`}>{`· ${pct()} of the window`}</Text></If>
      </View>
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
    const answer = draftContext();
    return previewSections({
      core: answer?.key === draftContextKey(p.id) ? answer.data : null,
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
            <Text class="text-[18px] leading-7 font-semibold text-foreground">Welcome to Nexus</Text>
            <Text class={T.sec}>Add a project directory to begin. Sessions run inside a project and keep everything the model sees in one timeline.</Text>
            <View class="flex-row pt-1"><Btn label="Add project" icon="plus" kind="primary" onPress={() => run("project.add")} testID="welcome-add-project" /></View>
          </View>
        </View>
      }>
        {() => (
          <Scroll class="flex-1 min-h-0">
            <View class="w-full items-center px-4 py-6">
              <View class="w-full max-w-[760px]">
                <ContextSections sections={preview().sections} total={preview().total} estimated window={window()} onInventoryToggle={(kind, name, locked) => toggleDraftInventory(kind, name, locked)} />
              </View>
            </View>
          </Scroll>
        )}
      </If>
    </View>
  );
}
