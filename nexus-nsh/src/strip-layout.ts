// The subagent strip's tab sizing, as the Rust TUI draws it (render.rs `draw_subagents`): one cell width
// for every tab (10..30 columns, from the widest title plus a child's agent), shrunk and then thinned
// until the row fits. Tabs farthest from the viewed child go first; dropped ancestors leave a "…".
type Item = { id: string; title: string; agent?: string };

const cols = (text: string) => Array.from(text).length;

export function stripLayout<T extends Item>(m: { ancestors: T[]; children: T[]; viewed: string; room: number }) {
  let ancestors = m.ancestors.slice();
  let children = m.children.slice();
  const titleW = (s: Item) => Math.min(28, cols(s.title || "Untitled"));
  const widest = Math.max(10, ...ancestors.map(titleW), ...children.map((s) => 4 + cols(s.agent ?? "") + titleW(s)));
  let cell = Math.min(30, Math.max(10, widest + 2));
  const total = (n: number) => n * (cell + 1) + 2;
  const room = Math.max(0, m.room - 3);
  let droppedLeft = false;
  while (total(ancestors.length + children.length) + (droppedLeft ? 2 : 0) > room) {
    if (ancestors.length > 1) { ancestors.shift(); droppedLeft = true; }
    else if (children.length > 1) {
      const at = Math.max(0, children.findIndex((s) => s.id === m.viewed));
      if (at + 1 < children.length) children.pop(); else children.shift();
    } else if (cell > 8) cell -= 1;
    else break;
  }
  return { ancestors, children, cell, droppedLeft };
}
