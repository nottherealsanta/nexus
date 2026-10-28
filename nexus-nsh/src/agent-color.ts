import { agentPins, agentSlots } from "./gen/theme";

// Core restricts agent names to lowercase ASCII letters, digits, '-' and '_'.
// Match the TUI's FNV-1a and linear probe over unpinned slots.
export function agentSlot(name: string): string {
  const key = name.toLowerCase();
  const pins: Record<string, string> = agentPins;
  if (pins[key]) return pins[key];
  let hash = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(key)) hash = Math.imul(hash ^ byte, 0x01000193) >>> 0;
  const pinned = new Set(Object.values(pins));
  const start = hash % agentSlots.length;
  for (let i = 0; i < agentSlots.length; i++) {
    const slot = agentSlots[(start + i) % agentSlots.length];
    if (!pinned.has(slot)) return slot;
  }
  return agentSlots[start];
}
const classes: Record<string, string> = {
  amber: "text-agent-amber", blue: "text-agent-blue", violet: "text-agent-violet", green: "text-agent-green",
  teal: "text-agent-teal", rose: "text-agent-rose", sky: "text-agent-sky", gold: "text-agent-gold",
};
export const agentColor = (name: string) => classes[agentSlot(name)];
