import { signal } from "nsh";

/** Keep list item identities stable while allowing each row's content to change. */
export function listSlots<T extends { key: string }>() {
  const makeSlot = (row: T) => ({ key: row.key, current: row, value: signal<T>(row) });
  type Slot = ReturnType<typeof makeSlot>;
  let slots = new Map<string, Slot>();
  return (rows: T[]) => {
    const next = new Map<string, Slot>();
    const result = rows.map((row) => {
      const slot = slots.get(row.key) ?? makeSlot(row);
      if (slot.current !== row) { slot.current = row; slot.value.set(row); }
      next.set(row.key, slot);
      return slot;
    });
    slots = next;
    return result;
  };
}
