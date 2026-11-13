import { describe, expect, test } from "nsh/test";
import {
  activate, closeGroup, closeTab, collapsed, dividers, dropTab, dropZone, emptyLayout, findGroup, focus, focusedGroup, groupRects, groupsOf, join, MIN_H, MIN_W,
  moveTab, moveToward, neighbour, normalize, openTab, openToSide, parse, requiredSize, resize, restore, serialize, split, splitFits, splitWith, stepTab, tabKey,
  type Layout, type Tab,
} from "./src/layout";

const BIG = { w: 1600, h: 900 };
const file = (id: string): Tab => ({ kind: "file", id });
const session = (id: string): Tab => ({ kind: "session", id });
const term = (id: string): Tab => ({ kind: "terminal", id });
const ids = (l: Layout) => groupsOf(l.root).map((g) => g.id);
const keys = (l: Layout, id: string) => findGroup(l, id)!.tabs.map(tabKey);
function ok(r: { ok: boolean }): Layout { expect(r.ok).toBe(true); return (r as any).layout; }
const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
const near = (a: number, b: number) => expect(Math.abs(a - b) < 0.001).toBe(true);

/** One group with two tabs split right: g1 [a, b] | g2 [b]. */
function two(): Layout {
  let l = openTab(emptyLayout(), file("a"));
  l = openTab(l, file("b"));
  return ok(split(l, "right", file("b"), BIG));
}

describe("layout tree", () => {
  test("a fresh layout is one empty focused group", () => {
    const l = emptyLayout();
    expect(ids(l)).toEqual(["g1"]);
    expect(l.focus).toBe("g1");
    expect(focusedGroup(l).active).toBeNull();
  });

  test("open appends and activates; reopening activates without duplicating", () => {
    let l = openTab(emptyLayout(), file("a"));
    l = openTab(l, file("b"));
    expect(keys(l, "g1")).toEqual(["file:a", "file:b"]);
    expect(findGroup(l, "g1")!.active).toBe("file:b");
    l = openTab(l, file("a"));
    expect(keys(l, "g1")).toEqual(["file:a", "file:b"]);
    expect(findGroup(l, "g1")!.active).toBe("file:a");
  });

  test("split right puts the tab in a new focused group beside the old one", () => {
    const l = two();
    expect(ids(l)).toEqual(["g1", "g2"]);
    expect(l.focus).toBe("g2");
    expect(keys(l, "g1")).toEqual(["file:a", "file:b"]);
    expect(keys(l, "g2")).toEqual(["file:b"]);
    const r = groupRects(l, BIG);
    expect(r.get("g1")).toEqual({ x: 0, y: 0, w: 800, h: 900 });
    expect(r.get("g2")).toEqual({ x: 800, y: 0, w: 800, h: 900 });
  });

  test("split down stacks, and splits of the same direction stay flat", () => {
    let l = two();
    l = ok(split(l, "right", file("c"), BIG));
    expect(l.root.type).toBe("split");
    expect((l.root as any).children).toHaveLength(3);
    near(sum((l.root as any).ratios), 1);
    l = ok(split(l, "down", file("d"), BIG));
    const below = groupRects(l, BIG).get("g4")!;
    const above = groupRects(l, BIG).get("g3")!;
    expect(below.y).toBe(450);
    expect(above.h).toBe(450);
    expect(below.x).toBe(above.x);
  });

  test("a terminal split is just a new tab in the new group (the caller creates the terminal)", () => {
    const l = openTab(emptyLayout(), term("t1"));
    const s = ok(split(l, "down", term("t2"), BIG));
    expect(keys(s, "g1")).toEqual(["terminal:t1"]);
    expect(keys(s, "g2")).toEqual(["terminal:t2"]);
  });

  test("splitting is refused under the minimum pane size, and the layout is unchanged", () => {
    const l = openTab(emptyLayout(), file("a"));
    const narrow = { w: MIN_W * 2 - 1, h: 900 };
    const r = split(l, "right", file("a"), narrow);
    expect(r).toEqual({ ok: false, reason: "too_small" });
    expect(splitFits(l, "g1", "right", narrow)).toBe(false);
    expect(splitFits(l, "g1", "bottom", narrow)).toBe(true);
    const short = { w: 1600, h: MIN_H * 2 - 1 };
    expect(split(l, "down", file("a"), short)).toEqual({ ok: false, reason: "too_small" });
    expect(split(l, "right", file("a"), { w: MIN_W * 2, h: 900 }).ok).toBe(true);
  });

  test("drop-zone splits take each side, left and top put the new group first", () => {
    const l = openTab(emptyLayout(), file("a"));
    const left = ok(splitWith(l, "g1", "left", [file("n")], BIG));
    expect(groupRects(left, BIG).get("g2")!.x).toBe(0);
    expect(groupRects(left, BIG).get("g1")!.x).toBe(800);
    const top = ok(splitWith(l, "g1", "top", [file("n")], BIG));
    expect(groupRects(top, BIG).get("g2")!.y).toBe(0);
    expect(splitWith(l, "nope", "left", [], BIG)).toEqual({ ok: false, reason: "missing" });
  });

  test("close tab picks the neighbour, and the last tab removes the group with its sibling taking the space", () => {
    let l = openTab(openTab(openTab(emptyLayout(), file("a")), file("b")), file("c"));
    l = activate(l, "g1", "file:b");
    l = closeTab(l, "g1", "file:b");
    expect(findGroup(l, "g1")!.active).toBe("file:c");
    const s = two();
    const closed = closeTab(closeTab(s, "g2", "file:b"), "g1", "file:a");
    expect(ids(closed)).toEqual(["g1"]);
    expect(closeTab(s, "g2", "file:b").root.type).toBe("group");
    expect(groupRects(closeTab(s, "g2", "file:b"), BIG).get("g1")).toEqual({ x: 0, y: 0, w: 1600, h: 900 });
  });

  test("the last group is kept empty rather than removed", () => {
    const l = closeTab(openTab(emptyLayout(), file("a")), "g1", "file:a");
    expect(ids(l)).toEqual(["g1"]);
    expect(findGroup(l, "g1")!.tabs).toEqual([]);
    expect(findGroup(l, "g1")!.active).toBeNull();
  });

  test("closing a group in the middle gives its space to the previous sibling and focus follows", () => {
    let l = two();
    l = ok(split(l, "right", file("c"), BIG)); // g1 | g2 | g3 with ratios .5 .25 .25
    l = focus(l, "g2");
    const c = closeGroup(l, "g2");
    expect(ids(c)).toEqual(["g1", "g3"]);
    expect(c.focus).toBe("g1");
    const r = groupRects(c, BIG);
    near(r.get("g1")!.w + r.get("g3")!.w, 1600);
    near(r.get("g1")!.w, 1200);
  });

  test("closing the only child of a nested split collapses the nesting", () => {
    let l = two();
    l = ok(split(l, "down", file("c"), BIG)); // g1 | (g2 / g3)
    expect(l.root.type).toBe("split");
    const c = closeGroup(l, "g3");
    expect(ids(c)).toEqual(["g1", "g2"]);
    expect((c.root as any).children.every((n: any) => n.type === "group")).toBe(true);
  });

  test("a duplicated tab survives closing one copy", () => {
    const s = two();
    const c = closeTab(s, "g1", "file:b");
    expect(keys(c, "g1")).toEqual(["file:a"]);
    expect(keys(c, "g2")).toEqual(["file:b"]);
  });

  test("dropTab removes a tab from every group and collapses emptied ones", () => {
    const s = two();
    const d = dropTab(s, "file:b");
    expect(ids(d)).toEqual(["g1"]);
    expect(keys(d, "g1")).toEqual(["file:a"]);
  });
});

describe("moving tabs", () => {
  test("reorder inside a group", () => {
    let l = openTab(openTab(openTab(emptyLayout(), file("a")), file("b")), file("c"));
    l = moveTab(l, "g1", "file:a", "g1", 2);
    expect(keys(l, "g1")).toEqual(["file:b", "file:c", "file:a"]);
    expect(findGroup(l, "g1")!.active).toBe("file:a");
  });

  test("move to another group appends at an index, focuses the target and removes an emptied source", () => {
    let l = openTab(emptyLayout(), file("a"));
    l = ok(splitWith(l, "g1", "right", [file("x"), file("y")], BIG));
    l = moveTab(l, "g1", "file:a", "g2", 1);
    expect(keys(l, "g2")).toEqual(["file:x", "file:a", "file:y"]);
    expect(findGroup(l, "g2")!.active).toBe("file:a");
    expect(l.focus).toBe("g2");
    expect(ids(l)).toEqual(["g2"]);
  });

  test("copy (Alt) leaves the source tab in place; moving a tab the target holds just activates it", () => {
    const s = two();
    const copied = moveTab(s, "g1", "file:a", "g2", undefined, true);
    expect(keys(copied, "g1")).toEqual(["file:a", "file:b"]);
    expect(keys(copied, "g2")).toEqual(["file:b", "file:a"]);
    const dup = moveTab(s, "g1", "file:b", "g2");
    expect(keys(dup, "g2")).toEqual(["file:b"]);
    expect(keys(dup, "g1")).toEqual(["file:a"]);
  });

  test("move toward a direction follows geometry and refuses where there is no group", () => {
    const s = two();
    const l = focus(s, "g1");
    const moved = ok(moveToward(l, "right", BIG));
    expect(keys(moved, "g2")).toEqual(["file:b", "file:b"].filter((_, i) => i === 0));
    expect(moveToward(l, "left", BIG)).toEqual({ ok: false, reason: "no_neighbour" });
    expect(moveToward(l, "up", BIG).ok).toBe(false);
  });
});

describe("join", () => {
  test("join right appends the tabs to the neighbour, keeps the moved active tab active and closes the split", () => {
    let l = openTab(openTab(emptyLayout(), file("a")), file("b"));
    l = ok(splitWith(l, "g1", "right", [file("x")], BIG));
    l = focus(l, "g1");
    l = activate(l, "g1", "file:a");
    const j = ok(join(l, "right", BIG));
    expect(ids(j)).toEqual(["g2"]);
    expect(keys(j, "g2")).toEqual(["file:x", "file:a", "file:b"]);
    expect(findGroup(j, "g2")!.active).toBe("file:a");
    expect(j.focus).toBe("g2");
    expect(j.root.type).toBe("group");
  });

  test("join down merges into the group below; a shared tab is not duplicated", () => {
    const l0 = openTab(emptyLayout(), file("a"));
    const l1 = ok(splitWith(l0, "g1", "bottom", [file("a"), file("z")], BIG));
    const j = ok(join(focus(l1, "g1"), "down", BIG));
    expect(keys(j, "g2")).toEqual(["file:a", "file:z"]);
  });

  test("join is unavailable with no neighbour that way", () => {
    const s = two();
    expect(join(focus(s, "g2"), "right", BIG)).toEqual({ ok: false, reason: "no_neighbour" });
    expect(join(focus(s, "g1"), "down", BIG).ok).toBe(false);
  });

  test("join reaches a neighbour that is not a sibling, and the freed space goes to its neighbour", () => {
    let l = openTab(emptyLayout(), file("a"));
    l = ok(splitWith(l, "g1", "right", [file("b")], BIG)); // g1 | g2
    l = ok(splitWith(l, "g2", "bottom", [file("c")], BIG)); // g1 | (g2 / g3)
    const j = ok(join(focus(l, "g1"), "right", BIG));
    expect(ids(j)).toEqual(["g2", "g3"]);
    expect(j.root.type).toBe("split");
    expect((j.root as any).dir).toBe("column");
    expect(keys(j, "g2")).toEqual(["file:b", "file:a"]);
  });
});

describe("focus by geometry", () => {
  test("a 2 x 2 grid", () => {
    let l = openTab(emptyLayout(), file("a"));
    l = ok(splitWith(l, "g1", "right", [file("b")], BIG)); // g1 | g2
    l = ok(splitWith(l, "g1", "bottom", [file("c")], BIG)); // (g1/g3) | g2
    l = ok(splitWith(l, "g2", "bottom", [file("d")], BIG)); // (g1/g3) | (g2/g4)
    expect(neighbour(l, "g1", "right", BIG)).toBe("g2");
    expect(neighbour(l, "g1", "down", BIG)).toBe("g3");
    expect(neighbour(l, "g4", "left", BIG)).toBe("g3");
    expect(neighbour(l, "g4", "up", BIG)).toBe("g2");
    expect(neighbour(l, "g1", "left", BIG)).toBeNull();
    expect(neighbour(l, "g1", "up", BIG)).toBeNull();
  });

  test("a tall group beside two short ones picks the one it overlaps most", () => {
    let l = openTab(emptyLayout(), file("a"));
    l = ok(splitWith(l, "g1", "right", [file("b")], BIG)); // g1 | g2
    l = ok(splitWith(l, "g2", "bottom", [file("c")], BIG)); // g1 | (g2 / g3)
    expect(neighbour(l, "g1", "right", BIG)).toBe("g2");
    expect(neighbour(l, "g3", "left", BIG)).toBe("g1");
  });

  test("next and previous tab wrap inside the group", () => {
    let l = openTab(openTab(emptyLayout(), file("a")), file("b"));
    l = stepTab(l, 1);
    expect(findGroup(l, "g1")!.active).toBe("file:a");
    l = stepTab(l, -1);
    expect(findGroup(l, "g1")!.active).toBe("file:b");
  });
});

describe("ratios and minimum sizes", () => {
  test("dragging a divider moves the boundary", () => {
    const l = two();
    const d = dividers(l.root, { x: 0, y: 0, ...{ w: BIG.w, h: BIG.h } });
    expect(d).toHaveLength(1);
    const moved = resize(l, d[0].path, d[0].index, 200, BIG);
    expect(groupRects(moved, BIG).get("g1")!.w).toBe(1000);
    expect(groupRects(moved, BIG).get("g2")!.x).toBe(1000);
  });

  test("clamping keeps both panes at the minimum", () => {
    const l = two();
    const far = resize(l, [], 0, 5000, BIG);
    near(groupRects(far, BIG).get("g2")!.w, MIN_W);
    const back = resize(l, [], 0, -5000, BIG);
    near(groupRects(back, BIG).get("g1")!.w, MIN_W);
  });

  test("clamping counts the minimums of everything nested beside it", () => {
    let l = openTab(emptyLayout(), file("a"));
    l = ok(splitWith(l, "g1", "right", [file("b")], BIG)); // g1 | g2
    l = ok(splitWith(l, "g2", "right", [file("c")], BIG)); // g1 | g2 | g3 (flat)
    const nested = ok(splitWith(l, "g2", "bottom", [file("d")], BIG));
    const far = resize(nested, [], 0, 5000, BIG);
    const r = groupRects(far, BIG);
    // The divider moves only the pair it separates: g1 grows until the column beside it is at its minimum.
    near(r.get("g2")!.w, MIN_W);
    near(r.get("g1")!.w, 1600 - MIN_W - 400);
    near(r.get("g3")!.w, 400);
  });

  test("ratios always sum to one", () => {
    let l = two();
    l = resize(l, [], 0, 123, BIG);
    near(sum((l.root as any).ratios), 1);
  });

  test("narrow widths collapse to the focused group, and the layout is kept", () => {
    const l = two();
    expect(requiredSize(l)).toEqual({ w: 2 * MIN_W, h: MIN_H });
    expect(collapsed(l, { w: 2 * MIN_W - 1, h: 900 })).toBe(true);
    expect(collapsed(l, { w: 2 * MIN_W, h: 900 })).toBe(false);
    expect(collapsed(openTab(emptyLayout(), file("a")), { w: 10, h: 10 })).toBe(false);
    expect(ids(l)).toEqual(["g1", "g2"]);
  });

  test("open to the side reuses the group on the right, or creates it", () => {
    const one = openTab(emptyLayout(), file("a"));
    const made = ok(openToSide(one, file("n"), BIG));
    expect(keys(made, "g2")).toEqual(["file:n"]);
    const reused = ok(openToSide(focus(made, "g1"), file("m"), BIG));
    expect(keys(reused, "g2")).toEqual(["file:n", "file:m"]);
    expect(openToSide(one, file("n"), { w: 300, h: 900 }).ok).toBe(false);
  });
});

describe("drop zones", () => {
  const r = { x: 100, y: 50, w: 400, h: 200 };
  test("edges split, the middle moves", () => {
    expect(dropZone(r, 110, 150)).toBe("left");
    expect(dropZone(r, 490, 150)).toBe("right");
    expect(dropZone(r, 300, 55)).toBe("top");
    expect(dropZone(r, 300, 245)).toBe("bottom");
    expect(dropZone(r, 300, 150)).toBe("centre");
  });
});

describe("persistence and restore", () => {
  test("a layout round-trips", () => {
    let l = two();
    l = ok(split(l, "down", session("s1"), BIG));
    const back = parse(serialize(l))!;
    expect(back).toEqual(l);
  });

  test("malformed saves are ignored", () => {
    expect(parse(null)).toBeNull();
    expect(parse("not json")).toBeNull();
    expect(parse(JSON.stringify({ v: 2, root: {} }))).toBeNull();
    expect(parse(JSON.stringify({ v: 1, root: { type: "split", dir: "diagonal", children: [] } }))).toBeNull();
    const dup = { v: 1, root: { type: "split", dir: "row", children: [{ type: "group", id: "g1", tabs: [], active: null }, { type: "group", id: "g1", tabs: [], active: null }], ratios: [0.5, 0.5] }, focus: "g1", seq: 1 };
    expect(parse(JSON.stringify(dup))).toBeNull();
  });

  test("unknown tab kinds are dropped on read, bad ratios repaired", () => {
    const text = JSON.stringify({ v: 1, root: { type: "split", dir: "row", children: [{ type: "group", id: "g1", tabs: [{ kind: "file", id: "a" }, { kind: "alien", id: "z" }], active: "alien:z" }, { type: "group", id: "g2", tabs: [], active: null }], ratios: [0, -1] }, focus: "gone", seq: 1 });
    const l = parse(text)!;
    expect(keys(l, "g1")).toEqual(["file:a"]);
    expect(findGroup(l, "g1")!.active).toBe("file:a");
    expect(l.focus).toBe("g1");
    expect(l.seq).toBe(2);
    expect((l.root as any).ratios).toEqual([0.5, 0.5]);
  });

  test("restore drops tabs whose resource is gone and collapses empty groups", () => {
    let l = two();
    l = ok(split(l, "down", term("t1"), BIG));
    const alive = (t: Tab) => t.kind !== "terminal" && t.id !== "a";
    const r = restore(l, alive);
    expect(ids(r)).toEqual(["g1", "g2"]);
    expect(keys(r, "g1")).toEqual(["file:b"]);
    expect(findGroup(r, "g1")!.active).toBe("file:b");
    expect(r.focus).toBe("g1");
    expect(r.seq).toBe(l.seq);
  });

  test("restore of a layout with nothing alive is one empty group", () => {
    const r = restore(two(), () => false);
    expect(ids(r)).toEqual(["g1"]);
    expect(findGroup(r, "g1")!.tabs).toEqual([]);
  });

  test("new group ids never reuse a restored one", () => {
    const l = parse(serialize(two()))!;
    const s = ok(split(l, "down", file("q"), BIG));
    expect(new Set(ids(s)).size).toBe(ids(s).length);
  });

  test("normalize merges nested splits of one direction", () => {
    const g = (id: string) => ({ type: "group" as const, id, tabs: [], active: null });
    const n = normalize({ type: "split", dir: "row", ratios: [0.5, 0.5], children: [{ type: "split", dir: "row", ratios: [0.5, 0.5], children: [g("a"), g("b")] }, g("c")] });
    expect((n as any).children).toHaveLength(3);
    expect((n as any).ratios).toEqual([0.25, 0.25, 0.5]);
  });
});
