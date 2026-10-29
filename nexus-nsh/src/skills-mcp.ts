/** Pure helpers shared by the skills/MCP starting-context controls and /learn. */
export type InventoryKind = "skill" | "mcp";
export type InventoryRow = {
  kind: InventoryKind;
  name: string;
  on: boolean;
  locked: boolean;
  description?: string;
  meta?: string;
  error?: string | null;
  tools?: number | null;
  hint?: string;
};

export type SkillEntry = { name: string; description?: string; scope?: string; state?: string; locked?: boolean };
export type McpEntry = { server: string; tools?: number | null; state?: string; locked?: boolean; error?: string | null; enabled?: boolean };

export function disabledKey(kind: InventoryKind, name: string): string {
  return `${kind}:${name}`;
}

/** Return a fresh disabled_tools list, preserving unrelated tools and stable ordering. */
export function setDisabledTool(disabled: readonly string[], key: string, off: boolean): string[] {
  const kept = [...disabled];
  const at = kept.indexOf(key);
  if (off && at < 0) kept.push(key);
  if (!off && at >= 0) return kept.filter((entry) => entry !== key);
  return kept;
}

export function toggleDisabledTool(disabled: readonly string[], kind: InventoryKind, name: string): string[] {
  const key = disabledKey(kind, name);
  return setDisabledTool(disabled, key, !disabled.includes(key));
}

export function shouldInstallRequestedDirectly(requested: readonly string[], found: readonly { name: string; installed?: boolean }[]): boolean {
  if (!requested.length) return false;
  const byName = new Map(found.map((skill) => [skill.name, skill]));
  return requested.every((name) => {
    const skill = byName.get(name);
    return !!skill && !skill.installed;
  });
}

export function defaultSkillSelection(found: readonly { name: string; installed?: boolean }[]): string[] {
  return found.filter((skill) => !skill.installed).map((skill) => skill.name);
}

export function selectedSkillCounts(found: readonly { name: string; installed?: boolean }[], selected: readonly string[]) {
  const chosen = new Set(selected);
  const items = found.filter((skill) => chosen.has(skill.name));
  return {
    newCount: items.filter((skill) => !skill.installed).length,
    replacedCount: items.filter((skill) => !!skill.installed).length,
    replacedNames: items.filter((skill) => !!skill.installed).map((skill) => skill.name),
  };
}

/** Skills and MCP rows in a frozen running session. An unavailable MCP server is off. */
export function sessionInventoryRows(
  skills: readonly SkillEntry[] = [],
  servers: readonly McpEntry[] = [],
  disabled: readonly string[] = [],
): InventoryRow[] {
  return [
    ...skills.map((skill): InventoryRow => {
      const key = disabledKey("skill", skill.name);
      const locked = !!skill.locked;
      return {
        kind: "skill", name: skill.name,
        on: skill.state === "on" && !disabled.includes(key),
        locked,
        description: skill.description,
        meta: skill.scope,
        hint: locked ? "new session" : undefined,
      };
    }),
    ...servers.map((server): InventoryRow => {
      const key = disabledKey("mcp", server.server);
      const locked = !!server.locked;
      return {
        kind: "mcp", name: server.server,
        on: server.state === "on" && !disabled.includes(key),
        locked,
        tools: server.tools,
        error: server.state === "unavailable" ? server.error : null,
        meta: server.state === "unavailable" ? "unavailable" : undefined,
        hint: locked ? "new session" : undefined,
      };
    }),
  ];
}

/** Rows for the new-session draft: catalog entries start on unless disabled in the draft. */
export function draftInventoryRows(
  skills: readonly SkillEntry[] = [],
  servers: readonly McpEntry[] = [],
  disabled: readonly string[] = [],
): InventoryRow[] {
  return [
    ...skills.map((skill): InventoryRow => ({
      kind: "skill", name: skill.name,
      on: !disabled.includes(disabledKey("skill", skill.name)),
      locked: false, description: skill.description, meta: skill.scope,
    })),
    ...servers.filter((server) => server.enabled === true && server.state !== "invalid").map((server): InventoryRow => ({
      kind: "mcp", name: server.server,
      on: !disabled.includes(disabledKey("mcp", server.server)),
      locked: false, tools: server.tools,
      error: server.state === "unavailable" ? server.error : null,
      meta: server.state === "unavailable" ? "unavailable" : undefined,
    })),
  ];
}

export type GlobalToast = { level: "info" | "warning"; text: string };
export type GlobalToastState = {
  lastSkillsChanged: number;
  lastMcpChanged: number;
  skillInstallPending: boolean;
  muteSkillsUntil: number;
};
export const emptyGlobalToastState = (): GlobalToastState => ({
  lastSkillsChanged: -Infinity,
  lastMcpChanged: -Infinity,
  skillInstallPending: false,
  muteSkillsUntil: 0,
});

/** Global Core events are live-only, but still flow through the normal cursor reducer. */
export function globalEventToast(type: string, data: any, state: GlobalToastState, now = Date.now()): GlobalToast | null {
  if (type === "skills.changed") {
    const repeated = now - state.lastSkillsChanged < 3000;
    state.lastSkillsChanged = now;
    if (repeated || state.skillInstallPending || state.muteSkillsUntil > now) return null;
    return { level: "info", text: "Skills changed. New sessions will use them." };
  }
  if (type === "mcp.changed") {
    const repeated = now - state.lastMcpChanged < 3000;
    state.lastMcpChanged = now;
    if (repeated) return null;
    return { level: "info", text: "MCP servers changed. New sessions will use them." };
  }
  if (type === "mcp.failed") {
    return { level: "warning", text: `MCP server ${String(data?.server ?? "?")} failed: ${String(data?.error ?? "unknown error")}` };
  }
  return null;
}
