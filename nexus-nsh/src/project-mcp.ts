// Project `.mcp.json` state as `GET /api/project/{id}/mcp` serves it. Enabled, valid
// entries start with a new session; reading this state never starts one. Rows carry
// only the non-secret facts `config.describe()` reports, so the UI never invents an
// exact command line: it shows the command name, argument count, cwd and env/header names.

export interface ProjectMcpRow {
  server: string;
  state: string;
  error?: string | null;
  tools?: number;
  enabled?: boolean;
  source?: string;
  project_id?: string;
  file?: string;
  transport?: string;
  command?: string;
  arg_count?: number;
  cwd?: string | null;
  env_names?: string[];
  url?: string;
  header_names?: string[];
  include?: string[];
  exclude?: string[];
  connect_timeout?: number;
  call_timeout?: number;
  ignored_keys?: string[];
  warnings?: string[];
  [key: string]: unknown;
}

export interface ProjectMcpSummary {
  project_id: string;
  file: string;
  exists: boolean;
  revision: string;
  error?: string | null;
  warnings?: string[];
  servers: ProjectMcpRow[];
}

const list = (items: string[] | undefined) => items?.length ? items.join(", ") : "none";

/** A one-line summary of the entry's transport and command; it never claims to be the full command. */
export function configShape(row: ProjectMcpRow): string {
  if (!row.transport) return "";
  if (row.transport !== "stdio") return `${row.transport} · ${row.url || "URL not reported"}`;
  const count = row.arg_count ?? 0;
  return `stdio · ${row.command || "command"} · ${count} argument${count === 1 ? "" : "s"}`;
}

/** The rest of the non-secret facts worth showing beside the shape; values are never included. */
export function configMeta(row: ProjectMcpRow): string {
  if (!row.transport) return "";
  if (row.transport !== "stdio") return `headers ${list(row.header_names)} (values never shown)`;
  const cwd = row.cwd || "project directory";
  return `${cwd} · env ${list(row.env_names)} (values never shown)`;
}

export function stateLabel(row: ProjectMcpRow): string {
  switch (row.state) {
    case "idle": return row.enabled === false ? "disabled" : "idle";
    case "connecting": return "connecting";
    case "connected": return row.tools === 1 ? "connected · 1 tool" : row.tools ? `connected · ${row.tools} tools` : "connected";
    case "failed": return "failed";
    case "disabled": return "disabled";
    case "invalid": return "invalid";
    default: return row.state.replaceAll("_", " ");
  }
}
