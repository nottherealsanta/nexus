import { describe, expect, test } from "nsh/test";
import { closedFenceSources, toneClass } from "./src/mermaid-logic";

describe("mermaid fences", () => {
  test("returns closed sources and hides a fence still streaming", () => {
    const closed = closedFenceSources("Intro\n```mermaid\nflowchart TD\n  Client --> Core --> Store\n```\nAfter");
    expect(closed).toEqual(["flowchart TD\n  Client --> Core --> Store"]);

    expect(closedFenceSources("Intro\n```mermaid\nflowchart TD\n  Client -->")).toEqual([]);
    expect(closedFenceSources("```mermaid\n```\n```mermaid\nstill open")).toEqual([""]);
  });

  test("accepts tildes and longer runs, and ignores other languages", () => {
    expect(closedFenceSources("~~~mermaid\npie title A\n  \"x\" : 1\n~~~")).toEqual([
      "pie title A\n  \"x\" : 1",
    ]);
    expect(closedFenceSources("````mermaid\ngraph TD\n````")).toEqual(["graph TD"]);
    expect(closedFenceSources("```ts\nflowchart TD\n```")).toEqual([]);
    // A shorter run does not close a longer opening fence.
    expect(closedFenceSources("````mermaid\ngraph TD\n```")).toEqual([]);
  });

  test("maps each tone to a readable role", () => {
    expect(toneClass("title")).toContain("font-semibold");
    expect(toneClass("heading")).toContain("text-foreground");
    expect(toneClass("bar")).toBe("text-primary");
    expect(toneClass("quiet")).toBe("text-muted-foreground");
    expect(toneClass("unknown")).toBe("text-foreground");
  });
});
