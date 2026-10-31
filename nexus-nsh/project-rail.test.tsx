import { describe, expect, test } from "nsh/test";
import { projectActivity, projectLabels, railProjects } from "./src/project-rail";
import type { Project, Session } from "./src/core/api";

const project = (name: string, created: number): Project => ({ id: name, name, created, updated: created, path: "/" + name, revision: 1 });
const session = (updated: number, status = "idle"): Session => ({ id: "s", project_id: "Nexus", directory: "/", title: "", agent: "", parent_id: null, revision: 1, created: 1, updated, status });

describe("project rail", () => {
  test("assigns initials in creation order, skipping vowels and used consonants", () => {
    const projects = [project("Nexus", 1), project("Notes", 2), project("Nimbus", 3), project("Nova", 4), project("Atlas", 5), project("Nats", 6), project("Noooo", 7)];
    expect([...projectLabels([...projects].reverse()).values()]).toEqual(["N", "NT", "NM", "NV", "A", "NS", "N2"]);
    expect(railProjects([...projects].reverse()).map((p) => p.id)).toEqual(projects.map((p) => p.id));
  });

  test("activity expires after one hour but running sessions remain active", () => {
    const now = 10_000_000;
    expect(projectActivity("Nexus", [session(6401)], new Map(), now).active).toBe(true);
    expect(projectActivity("Nexus", [session(6400)], new Map(), now).active).toBe(false);
    expect(projectActivity("Nexus", [session(1)], new Map([["s", "turn"]]), now).running).toBe(1);
    expect(projectActivity("Nexus", [session(1, "busy")], new Map(), now).active).toBe(true);
    expect(projectActivity("Other", [session(9999)], new Map(), now).active).toBe(false);
    expect(projectActivity("Nexus", [], new Map(), now).active).toBe(false);
  });
});
