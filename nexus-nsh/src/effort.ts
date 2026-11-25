// Per-agent, per-model reasoning effort (docs/config.md). Settings and onboarding share these labels and
// writes so an unset override never reads as the provider's default when the agent file sets one.
import { config, patchConfig, picker, toast } from "./store";
import { manifest } from "./gen/manifest";

const STRINGS = (manifest as unknown as { strings?: Record<string, string> }).strings ?? {};

/** A shared manifest string with its `{name}` placeholders filled. */
export function effortText(key: string, values: Record<string, string> = {}): string {
  return (STRINGS[key] ?? key).replace(/\{([a-z_]+)\}/g, (whole, name: string) => values[name] ?? whole);
}

/** The reasoning-effort levels the catalog lists for a model; a model with none has no levels. */
export function catalogEfforts(models: any, ref: string): string[] {
  const item = (models?.models ?? []).find((m: any) => m.ref === ref);
  return (item?.efforts ?? []).map(String);
}

export function agentEfforts(configValue: any, agent: string): Record<string, string> {
  return configValue?.values?.agents?.efforts?.[agent] ?? {};
}

/** The agent file's own `effort`, which applies when neither the session nor `agents.efforts` sets one. */
export function agentFileEffort(agents: any, agent: string): string | null {
  const found = (agents?.agents ?? []).find((a: any) => a.name === agent);
  return typeof found?.effort === "string" && found.effort ? found.effort : null;
}

export function effortOverride(configValue: any, agent: string, ref: string): string | null {
  return agentEfforts(configValue, agent)[ref] ?? null;
}

/** The picker's first entry, and what a model row shows while it has no override. */
export function defaultEffortLabel(agentEffort: string | null): string {
  return agentEffort ? effortText("effort.agent_default", { effort: agentEffort }) : effortText("effort.model_default");
}

export function effortLabel(override: string | null, agentEffort: string | null): string {
  return override ?? defaultEffortLabel(agentEffort);
}

/** A pool or fallback row's text: `effort` plus the override or effective default. */
export function effortRowLabel(override: string | null, agentEffort: string | null): string {
  return effortText("effort.row", { value: effortLabel(override, agentEffort) });
}

/** Write or clear one model's entry, leaving the agent's other models and every other agent alone. */
export async function writeAgentEffort(agent: string, ref: string, effort: string | null) {
  const all: Record<string, Record<string, string>> = { ...(config()?.values?.agents?.efforts ?? {}) };
  const mine = { ...(all[agent] ?? {}) };
  if (effort) mine[ref] = effort; else delete mine[ref];
  if (Object.keys(mine).length) all[agent] = mine; else delete all[agent];
  await patchConfig("agents.efforts", all);
}

/** The default first (clears only this model's override), then the levels the catalog supports. */
export function effortPickerItems(agent: string, ref: string, agentEffort: string | null, levels: string[]) {
  return [
    {
      id: "__default__", label: defaultEffortLabel(agentEffort),
      run: async () => { picker.set(null); await writeAgentEffort(agent, ref, null); },
    },
    ...levels.map((level) => ({
      id: `effort:${level}`, label: level,
      run: async () => { picker.set(null); await writeAgentEffort(agent, ref, level); },
    })),
  ];
}

export function openEffortPicker(agent: string, ref: string, agentEffort: string | null, levels: string[]) {
  // A model without catalog levels has no control, unless a stale override can still be cleared.
  if (!levels.length && !effortOverride(config(), agent, ref)) {
    toast("warning", effortText("effort.no_levels", { model: ref }));
    return;
  }
  picker.set({ title: "Reasoning effort", items: effortPickerItems(agent, ref, agentEffort, levels) });
}
