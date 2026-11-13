import { describe, expect, test } from "nsh/test";
import scenario from "../interface/scenarios/file-completion.json";
import { fileCandidates, fileMenu } from "./src/file-completion";

describe("file completion", () => {
  test("ranks and pads Core's hits like the TUI", () => {
    expect(fileMenu(scenario.query, scenario.hits, scenario.pool)).toEqual(scenario.expected);
    expect(fileMenu(scenario.query, scenario.hits, null)).toEqual(scenario.hits);
  });

  test("reuses only ancestor replies in the same draft context", () => {
    const cache = [{ prefix: "see @a", paths: ["other", "abc", "ab/"] }];
    expect(fileCandidates(cache, "see @ab")).toEqual(["abc", "ab/", "other"]);
    expect(fileCandidates(cache, "see @ab/")).toEqual(["other", "abc"]);
    expect(fileCandidates(cache, "other @ab")).toBeNull();
  });
});
