// Settings › MCP: the servers (global and the project's), adding a remote one, and one server's detail modal
// (sign in, headers, switch off, restart, remove). The TUI's `app/mcp.rs` draws the same page and modal with
// the same wording; keep them in step.
//
// Core does the work (docs/mcp.md#managing-servers). This shows `GET /api/mcp` and the project's
// `GET /api/project/{id}/mcp` rows and sends the writes. Secret values never come back from Core: a header is
// typed once as `Name: value` and only its name is shown afterwards.

import { Text, View, openUrl, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, FOCUS, T, TS } from "./kit";
import { Modal, ModalFooter, ModalTitle } from "./modal";
import { confirmDialog, currentProject, live, toast } from "../store";
import { request } from "../core/transport";

export interface McpRow {
  server: string;
  scope: "global" | "project";
  project_id?: string;
  state: string;
  auth?: "signed_in" | "required" | "none";
  transport?: string;
  url?: string;
  command?: string;
  arg_count?: number;
  oauth?: boolean;
  enabled?: boolean;
  tools?: number;
  error?: string | null;
  header_names?: string[];
  file?: string;
}

export interface McpKey { project: string | null; name: string }
interface Login { status: string; message?: string | null; verification_uri?: string | null }

export const mcpServers = signal<{ servers: McpRow[]; fileError: string | null } | null>(null);
export const mcpDetail = signal<McpKey | null>(null);
/** The last `mcp.login` state per server (`scope:name`): a pending sign-in shows its link here. */
export const mcpLogins = signal<Record<string, Login>>({});
const opened = new Map<string, string>();
const adding = signal<{ project: string | null } | null>(null);
const addressText = signal("");
const headerFor = signal<McpKey | null>(null);
const headerText = signal("");
const busy = signal(false);

const same = (key: McpKey) => (row: McpRow) => row.server === key.name && (row.scope === "project") === (key.project !== null);
const loginKey = (key: McpKey) => `${key.project ?? "global"}:${key.name}`;
const keyOf = (row: McpRow): McpKey => ({ project: row.scope === "project" ? row.project_id ?? null : null, name: row.server });
const base = (project: string | null) => (project ? `/api/project/${project}/mcp` : "/api/mcp");
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export async function loadMcp() {
  const project = currentProject();
  try {
    const global = await request<{ servers: McpRow[] }>("GET", "/api/mcp");
    const servers: McpRow[] = (global.servers ?? []).map((row) => ({ ...row, scope: "global" as const }));
    let fileError: string | null = null;
    if (project) {
      const scoped = await request<{ servers: McpRow[]; error?: string | null }>("GET", `/api/project/${project}/mcp`);
      fileError = scoped.error ?? null;
      for (const row of scoped.servers ?? []) servers.push({ ...row, scope: "project" as const });
    }
    mcpServers.set({ servers: servers.filter((row) => row.server !== "(file)"), fileError });
  } catch (e) { toast("error", `MCP servers could not be read: ${message(e)}`); }
}

/** The value on the section's entry, as the TUI's `page_summary("mcp")`. */
export function mcpSummary(): string {
  const list = mcpServers();
  if (!list) return "loading…";
  const rows = list.servers.length, waiting = list.servers.filter((row) => row.auth === "required").length;
  if (!rows) return "none yet";
  const base_ = `${rows} server${rows === 1 ? "" : "s"}`;
  return waiting ? `${base_} · ${waiting} to sign in` : base_;
}

export function stateLabel(row: McpRow): string {
  const tools = row.tools ?? 0;
  if (row.state === "connected") return tools === 1 ? "connected · 1 tool" : tools > 0 ? `connected · ${tools} tools` : "connected";
  if (row.state === "failed" && row.auth === "required") return "sign-in required";
  if (row.state === "idle" && row.auth === "none") return "not signed in";
  return row.state.replaceAll("_", " ");
}

const remote = (row: McpRow) => row.transport === "http" || row.transport === "sse";

/** `mcp.login` and `mcp.connected`… events: keep the list and each sign-in current. */
export function mcpEvent(type: string, data: Record<string, unknown>) {
  if (type === "mcp.login") {
    const key: McpKey = { project: data.scope === "project" ? String(data.project_id ?? "") : null, name: String(data.server ?? "") };
    const status = String(data.status ?? "");
    mcpLogins.update((all) => {
      const { [loginKey(key)]: _drop, ...rest } = all;
      return status === "signed_out" ? rest : { ...rest, [loginKey(key)]: data as unknown as Login };
    });
    if (status === "signed_in") toast("success", `Signed in to ${key.name}`);
    if (status === "failed") toast("error", String(data.message ?? "Sign-in failed"));
    const url = typeof data.verification_uri === "string" ? data.verification_uri : null;
    if (status === "pending" && url) {
      // The link is the point: open it for the user, who asked for the sign-in.
      if (opened.get(loginKey(key)) !== url) { opened.set(loginKey(key), url); void openUrl(url).catch(() => toast("info", "Open the sign-in link in your browser.")); }
    } else if (status !== "pending") opened.delete(loginKey(key));
    if (status !== "pending") void loadMcp();
    return;
  }
  void loadMcp();
}

async function write(project: string | null, method: string, path: string, body?: unknown, done?: string): Promise<any> {
  busy.set(true);
  try {
    const reply = await request<any>(method, `${base(project)}${path}`, body === undefined ? {} : { body });
    if (done) toast("success", done);
    await loadMcp();
    return reply;
  } catch (e) { toast("error", message(e)); await loadMcp(); return null; }
  finally { busy.set(false); }
}

async function addServer() {
  const target = adding(), address = addressText().trim();
  if (!target) return;
  if (!/^https?:\/\//.test(address)) { toast("warning", "The address must start with https://"); return; }
  const row = await write(target.project, "POST", "", { entry: { url: address } });
  if (!row) return;
  adding.set(null); addressText.set("");
  toast("success", `Added ${row.server}`);
  mcpDetail.set({ project: target.project, name: row.server });
}

async function addHeader() {
  const key = headerFor(), text = headerText();
  if (!key) return;
  const at = text.indexOf(":");
  const name = text.slice(0, at).trim(), value = text.slice(at + 1).trim();
  if (at < 0 || !name || !value) { toast("warning", "Write the header as Name: value"); return; }
  if (await write(key.project, "PATCH", `/${key.name}`, { entry: { headers: { [name]: value } } }, `Header ${name} set`)) { headerFor.set(null); headerText.set(""); }
}

async function signIn(key: McpKey) {
  mcpLogins.update((all) => ({ ...all, [loginKey(key)]: { status: "pending" } }));
  const state = await write(key.project, "POST", `/${key.name}/login`, {});
  if (state?.status) mcpLogins.update((all) => ({ ...all, [loginKey(key)]: state }));
  else mcpLogins.update((all) => { const { [loginKey(key)]: _drop, ...rest } = all; return rest; });
}

async function signOut(key: McpKey) {
  mcpLogins.update((all) => { const { [loginKey(key)]: _drop, ...rest } = all; return rest; });
  await write(key.project, "DELETE", `/${key.name}/login`);
}

function remove(key: McpKey) {
  confirmDialog.set({
    title: `Remove ${key.name}?`, destructive: true, confirm: "Remove",
    body: `${key.project ? "It is deleted from this project's MCP file." : "It is deleted from your MCP settings."} Its sign-in is forgotten. Running sessions keep what they started with.`,
    run: async () => { await write(key.project, "DELETE", `/${key.name}`, undefined, "Server removed"); if (mcpDetail()?.name === key.name) mcpDetail.set(null); },
  });
}

function ServerRow(p: { row: McpRow }) {
  const row = p.row;
  const tone = row.state === "connected" ? "text-foreground-secondary" : row.state === "failed" || row.state === "invalid" ? "text-warning" : "text-muted-foreground";
  return <Button onPress={() => { headerFor.set(null); mcpDetail.set(keyOf(row)); }} testID={`mcp-open-${row.scope}-${row.server}`} accessibilityLabel={`${row.server}: ${stateLabel(row)}`}
    class={`flex-row items-center gap-3 rounded-(--radius) px-3 py-2 hover:bg-accent ${FOCUS}`}>
    <View class="flex-1"><Text class={T.base}>{row.server}</Text></View>
    <Text class={`${TS.base} ${tone}`}>{`${stateLabel(row)} · open`}</Text>
  </Button>;
}

/** The MCP page: add, then every server with its state. */
export function McpPage(p: { children?: any; firstRef?: (node: any) => void }) {
  const rows = (scope: string) => (mcpServers()?.servers ?? []).filter((row) => row.scope === scope);
  const project = () => currentProject();
  return <View class="gap-1" testID="mcp-page">
    <View class="flex-row items-center gap-2 px-3 pb-1">
      <Btn ref={p.firstRef} label="Add a remote server" kind="primary" disabled={!live()} onPress={() => { addressText.set(""); adding.set({ project: null }); }} testID="mcp-add" />
      <If when={!!project()}><Btn label="Add to this project" disabled={!live()} onPress={() => { addressText.set(""); adding.set({ project: project() }); }} testID="mcp-add-project" /></If>
    </View>
    <If when={!mcpServers()}><Text class={`px-3 ${T.meta}`}>Loading servers…</Text></If>
    <If when={!!mcpServers()?.fileError}><Text class={`px-3 ${TS.base} text-error`}>{`Project MCP files: ${mcpServers()!.fileError}`}</Text></If>
    <If when={rows("global").length > 0}>
      <Text class={`px-3 pt-2 ${T.label}`}>Every project</Text>
      <Each of={rows("global")} key={(row) => `g:${row.server}`}>{(row) => <ServerRow row={row()} />}</Each>
    </If>
    <If when={rows("project").length > 0}>
      <Text class={`px-3 pt-2 ${T.label}`}>This project</Text>
      <Each of={rows("project")} key={(row) => `p:${row.server}`}>{(row) => <ServerRow row={row()} />}</Each>
    </If>
    <If when={!!mcpServers() && mcpServers()!.servers.length === 0}>
      <Text class={`px-3 ${T.meta}`}>No MCP servers yet. A remote one needs only its address; local ones are added with `nexus config import-mcp`.</Text>
    </If>
    <Text class={`px-3 pt-2 ${T.label}`}>New sessions</Text>
    {p.children}
  </View>;
}

/** One server's actions over Settings: what it is, then one action per row. */
export function McpModals() {
  const key = () => mcpDetail();
  const row = () => { const k = key(); return k ? (mcpServers()?.servers ?? []).find(same(k)) : undefined; };
  const login = () => { const k = key(); return k ? mcpLogins()[loginKey(k)] : undefined; };
  const pending = () => login()?.status === "pending";
  const failed = () => login()?.status === "failed";
  const close = () => { mcpDetail.set(null); headerFor.set(null); headerText.set(""); };
  const open = (url: string) => void openUrl(url).catch(() => toast("info", "Open this link in your browser."));
  const enabled = () => row()?.enabled !== false;
  return <>
    <If when={!!adding()}>{() => <Modal size="small" label="Add MCP server" testID="mcp-add-modal" onCancel={() => adding.set(null)} onOpenChange={(o) => { if (!o) adding.set(null); }}>
      <View class="gap-3">
        <ModalTitle title="Add MCP server" />
        <Text class={T.meta}>{adding()!.project ? "MCP server address, added to this project (https://…). It is written to .mcp.json, which may be committed." : "MCP server address (https://…)"}</Text>
        <Input autoFocus accessibilityLabel="MCP server address" placeholder="https://mcp.example.com/mcp" onChange={({ value }) => addressText.set(value)} onSubmit={() => void addServer()}
          class={`h-9 rounded-(--radius) bg-background px-3 ${TS.base} text-foreground`} testID="mcp-address" />
        <View class="flex-row items-center justify-between pt-1">
          <ModalFooter hints="enter add · esc cancel" />
          <View class="flex-row gap-2"><Btn label="Cancel" kind="ghost" onPress={() => adding.set(null)} /><Btn label="Add" kind="primary" busy={busy()} disabled={!live()} onPress={() => void addServer()} testID="mcp-add-confirm" /></View>
        </View>
      </View>
    </Modal>}</If>
    <If when={!!key() && !!row()}>{() => <Modal size="medium" label={`${key()!.name} server`} testID="mcp-detail" onCancel={close} onOpenChange={(o) => { if (!o) close(); }}>
      <View class="gap-3">
        <ModalTitle title={key()!.name} />
        <Text class={T.sec}>{`${stateLabel(row()!)} · ${key()!.project ? "this project" : "every project"}`}</Text>
        <If when={!!row()!.url}><Text style={{ userSelect: "text" }} class={T.meta}>{row()!.url!}</Text></If>
        <If when={!row()!.url && !!row()!.command}><Text class={T.meta}>{`${row()!.command} · ${row()!.arg_count ?? 0} arguments`}</Text></If>
        <If when={!!row()!.error}><Text class={`${TS.base} text-warning`} testID="mcp-detail-error">{row()!.error!}</Text></If>
        <If when={!!row()!.header_names?.length}><Text class={T.meta}>{`headers ${row()!.header_names!.join(", ")} (values never shown)`}</Text></If>
        <If when={!!row()!.file}><Text class={T.meta}>{`in ${row()!.file}`}</Text></If>
        <If when={remote(row()!) && row()!.oauth === true}>{() => <View class="gap-2">
          <If when={pending()}>{() => <View class="gap-2">
            <If when={!!login()?.verification_uri} else={<Text class={T.meta}>Starting sign-in…</Text>}>
              <View class="flex-row items-center gap-2">
                <Button onPress={() => open(login()!.verification_uri!)} accessibilityLabel={`Open ${login()!.verification_uri}`} testID="mcp-detail-link"
                  class={`flex-1 rounded-(--radius) px-2 min-h-9 items-start justify-center hover:bg-accent ${FOCUS}`}>
                  <Text style={{ userSelect: "text", numberOfLines: 2 }} class={`${TS.base} text-info underline`}>{login()!.verification_uri!}</Text>
                </Button>
                <Btn label="Open" kind="ghost" icon="web" onPress={() => open(login()!.verification_uri!)} testID="mcp-detail-open" />
              </View>
            </If>
            <Text class={T.meta}>Waiting for authorization…</Text>
            <View class="flex-row"><Btn label="Cancel sign-in" onPress={() => void signOut(key()!)} testID="mcp-detail-cancel" /></View>
          </View>}</If>
          <If when={!pending()}>{() => <View class="gap-2">
            <If when={failed()}><Text class={`${TS.base} text-error`}>{login()?.message ?? "Sign-in failed"}</Text></If>
            <View class="flex-row items-center gap-2">
              <Btn label={row()!.auth === "signed_in" ? "Sign in again" : "Sign in"} kind={row()!.auth === "signed_in" ? "default" : "primary"} disabled={!live()} onPress={() => void signIn(key()!)} testID="mcp-detail-sign-in" />
              <If when={row()!.auth === "signed_in"}><Btn label="Sign out" kind="ghost" disabled={!live()} onPress={() => void signOut(key()!)} testID="mcp-detail-sign-out" /></If>
            </View>
          </View>}</If>
        </View>}</If>
        <If when={remote(row()!)}>{() => <View class="gap-2">
          <If when={!!headerFor()} else={<View class="flex-row"><Btn label="Add a header" disabled={!live()} onPress={() => { headerText.set(""); headerFor.set(key()!); }} testID="mcp-detail-header" /></View>}>
            <Input masked autoFocus accessibilityLabel={`Header for ${key()!.name}`} placeholder="Name: value" onChange={({ value }) => headerText.set(value)} onSubmit={() => void addHeader()}
              class={`h-9 rounded-(--radius) bg-background px-3 ${TS.base} text-foreground`} testID="mcp-header-input" />
            <View class="flex-row items-center gap-2"><Btn label="Save header" kind="primary" busy={busy()} onPress={() => void addHeader()} testID="mcp-header-save" /><Btn label="Cancel" onPress={() => { headerFor.set(null); headerText.set(""); }} /></View>
          </If>
          <If when={!!key()!.project}><Text class={T.meta}>This file may be committed: write a secret as ${"${env:NAME}"}, not the value.</Text></If>
        </View>}</If>
        <View class="flex-row items-center gap-2">
          <Btn label={enabled() ? "Switch off" : "Switch on"} disabled={!live()} onPress={() => void write(key()!.project, "PATCH", `/${key()!.name}`, { entry: { enabled: enabled() ? false : null } })} testID="mcp-detail-toggle" />
          <Btn label="Restart" disabled={!live()} onPress={() => void write(key()!.project, "POST", `/${key()!.name}/restart`, {}, "Server restarted")} testID="mcp-detail-restart" />
          <Btn label="Remove" kind="danger" disabled={!live()} onPress={() => remove(key()!)} testID="mcp-detail-remove" />
        </View>
        <Text class={T.meta}>New sessions use the change; running sessions keep the servers they started with.</Text>
        <View class="flex-row items-center justify-between pt-1">
          <ModalFooter hints="esc close" />
          <Btn label="Close" kind="ghost" onPress={close} testID="mcp-detail-close" />
        </View>
      </View>
    </Modal>}</If>
  </>;
}
