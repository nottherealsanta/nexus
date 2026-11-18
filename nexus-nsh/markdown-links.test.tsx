import { describe, expect, test } from "nsh/test";
import { markdownFilePath } from "./src/markdown-links";

describe("Markdown file links", () => {
  test("resolve relative and absolute workspace paths, but not outside paths or web URLs", () => {
    expect(markdownFilePath("../src/main.rs#L4", "/work/demo", "docs/readme.md")).toBe("src/main.rs");
    expect(markdownFilePath("/work/demo/docs/guide.md", "/work/demo")).toBe("docs/guide.md");
    expect(markdownFilePath("../../outside", "/work/demo", "docs/readme.md")).toBe(null);
    expect(markdownFilePath("https://example.test", "/work/demo")).toBe(null);
  });
});
