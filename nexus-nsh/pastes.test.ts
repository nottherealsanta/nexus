import { describe, expect, test } from "nsh/test";
import { isBigPaste, makePaste, normalisePaste, pasteLines, pasteTokens } from "./src/pastes";

describe("collapsed pastes", () => {
  test("the TUI's size rule decides what collapses", () => {
    expect(isBigPaste("one\ntwo")).toBe(false);
    expect(isBigPaste("one\ntwo\nthree")).toBe(true);
    expect(isBigPaste("x".repeat(199))).toBe(false);
    expect(isBigPaste("x".repeat(200))).toBe(true);
    expect(isBigPaste("x\r\ny\r\nz")).toBe(true);
  });

  test("markers count tokens and lines the way the TUI does", () => {
    expect(pasteTokens("x".repeat(199))).toBe(49);
    expect(pasteTokens("x")).toBe(1);
    expect(pasteLines("one\ntwo\n")).toBe(2);
    expect(pasteLines("")).toBe(1);
    expect(normalisePaste("a\r\nb\rc\td\u{1b}")).toBe("a\nb\nc    d");
  });

  test("repeated pastes get distinct, non-colliding markers", () => {
    const text = "a\nb\nc";
    const first = makePaste(text, [], "typed");
    const second = makePaste(text, [first], "typed");
    const third = makePaste(text, [first, second], `typed ${first.marker}`);
    expect(first.marker).toBe("[Pasted ~1 tokens]");
    expect(second.marker).toBe("[Pasted ~1 tokens #2]");
    expect(third.marker).toBe("[Pasted ~1 tokens #3]");
  });
});
