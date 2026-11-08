
export const textCellPoints = () => 9.6;
export const defaultLeftSidebarWidth = (width: number) => width >= 1280 ? 272 : 224;
export const rightSidebarWidth = (width: number) => width >= 1280 ? 288 : 240;

export function contextGrid(items: string[], width: number, most: number) {
  const unit = textCellPoints();
  const cell = Math.max(1, ...items.map((name) => Array.from(name).length + 6)) * unit;
  const columns = Math.max(1, Math.min(most, Math.floor(Math.max(0, width - 32) / cell)));
  return { columns, cell: Math.min(cell, Math.max(unit, width - 32)) };
}

export function workspaceTabCells(titles: { title: string; session: boolean }[], available: number): number {
  const size = (limit: number) => Math.max(8, ...titles.map((item) => Math.min(limit, Array.from(item.title).length) + (item.session ? 7 : 5)));
  let limit = 28;
  while (limit > 8 && (size(limit) + 1) * titles.length + 4 > available) limit -= 2;
  return size(limit);
}
