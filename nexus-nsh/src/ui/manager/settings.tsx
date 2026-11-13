// The Manager section of Settings (docs/manager-design.md 10.6): on or off, mode, model, how much it speaks up,
// and where words and sessions go. Core keys `[manager] enabled | mode | model | proactive`.
import type { JSX } from "nsh";
import { Text, View, effect, signal } from "nsh";
import { Input } from "nsh/base";
import { Btn, Seg, TS } from "../kit";
import { config, patchConfig } from "../../store";

export const MANAGER_ROWS = ["manager.enabled", "manager.mode", "manager.model", "manager.proactive", "manager.privacy"];

const values = () => config()?.values?.manager ?? {};

function ModelRef() {
  const value = signal(String(values().model ?? "copilot/gpt-6-luna"));
  const revision = signal<any>(config()?.revision);
  effect(() => {
    const current = config();
    if (current && current.revision !== revision()) { revision.set(current.revision); value.set(String(current.values?.manager?.model ?? "copilot/gpt-6-luna")); }
  });
  const save = () => { const ref = value().trim(); if (ref) void patchConfig("manager.model", ref); };
  return (
    <View class="flex-row items-center gap-2">
      <Input value={value()} placeholder="provider/model" accessibilityLabel="Manager model, provider/model" testID="manager-model"
        onChange={({ value: text }) => value.set(text)} onSubmit={save} submitBehavior="submit"
        class={`h-8 w-56 rounded-(--radius) bg-background px-2 ${TS.base} text-foreground`} />
      <Btn label="Save" onPress={save} testID="manager-model-save" />
    </View>
  );
}

/** The control for one Manager settings row, or null for a row that is not the Manager's. */
export function managerControl(id: string, label: string, options?: string[]): JSX.Element | null {
  switch (id) {
    case "manager.enabled":
      return <Seg label={label} options={["off", "on"]} value={values().enabled ? "on" : "off"} onPick={(v) => void patchConfig("manager.enabled", v === "on")} />;
    case "manager.mode":
      return <Seg label={label} options={options ?? ["pipeline", "live"]} value={String(values().mode ?? "pipeline")} onPick={(v) => void patchConfig("manager.mode", v)} />;
    case "manager.model": return <ModelRef />;
    case "manager.proactive":
      return <Seg label={label} options={options ?? ["off", "attention", "all"]} value={String(values().proactive ?? "attention")} onPick={(v) => void patchConfig("manager.proactive", v)} />;
    case "manager.privacy": return <Text class="hidden" />;
    default: return null;
  }
}
