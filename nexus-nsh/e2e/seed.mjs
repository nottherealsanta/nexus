#!/usr/bin/env node
// Seeds a scripted Core (e2e/scripted_core.py) through its public HTTP API: a workspace with every
// timeline and rail scenario -- a multi-step turn with thoughts, tools, a patch, a failure, a child
// and an image; a live "think" turn left running; sessions waiting for an approval and a question;
// a failed tool; and empty projects with colliding initials for the rail.
//   node e2e/seed.mjs <NEXUS10_HOME> <workspace-dir-a> <workspace-dir-b>
import fs from "node:fs";
import path from "node:path";

const [home, dirA, dirB] = process.argv.slice(2);
const d = JSON.parse(fs.readFileSync(path.join(home, "daemon.json"), "utf8"));
const call = async (method, p, body) => {
  const r = await fetch(d.url + p, { method, headers: { authorization: `Bearer ${d.token}`, "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  if (!r.ok) throw new Error(`${method} ${p}: ${r.status} ${t}`);
  return t ? JSON.parse(t) : null;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const idle = async (id) => { for (let i = 0; i < 300; i++) { const s = await call("GET", `/api/session/${id}`); if (s.status !== "busy") return; await sleep(100); } throw new Error("still busy " + id); };
const waitApproval = async (id) => { for (let i = 0; i < 300; i++) { const a = await call("GET", `/api/session/${id}/permission`); if (a.length) return a[0]; await sleep(100); } throw new Error("no approval"); };
const waitQuestion = async (id) => { for (let i = 0; i < 300; i++) { const a = await call("GET", `/api/session/${id}/question`); if (a.length) return a[0]; await sleep(100); } throw new Error("no question"); };
const title = (id, t) => call("PATCH", `/api/session/${id}`, { title: t });
const project = (dir, name) => { fs.mkdirSync(dir, { recursive: true }); return call("POST", "/api/project", { path: dir, name }); };
const session = async (projectId, prompt, titleText, extra = {}) => {
  const s = await call("POST", "/api/session", { project: projectId, ...extra });
  await call("POST", `/api/session/${s.id}/prompt`, { text: prompt });
  if (titleText) await title(s.id, titleText);
  return s;
};

// The scenario reads and patches real files in its workspace.
fs.mkdirSync(dirA, { recursive: true });
fs.writeFileSync(path.join(dirA, "README.md"), "# Nexus demo\n\nA small workspace for the visual harness.\n");
fs.writeFileSync(path.join(dirA, "notes.txt"), "old notes, not needed any more\n");
fs.copyFileSync(new URL("../../nexus-tui/fixtures/clipboard.png", import.meta.url), path.join(dirA, "clipboard.png"));
const source = path.join(dirA, "nexus-nsh/src/ui");
fs.mkdirSync(source, { recursive: true });
fs.writeFileSync(path.join(source, "timeline.tsx"), [
  "// seeded timeline source for the visual harness",
  ...Array.from({ length: 180 }, (_, i) => `const row${i} = groupActivity(${i}); // footer and Thinking rows`),
].join("\n") + "\n");

const base = path.dirname(dirA);
const A = await project(dirA, "Nexus");
const B = await project(dirB, "Notes");
const C = await project(path.join(base, "nimbus"), "Nimbus");
const D = await project(path.join(base, "nova"), "Nova");
const E = await project(path.join(base, "side-project"), "Side project");

// A full turn: 4 thoughts, 4 commands/reads/searches, a patch, a failed read, a child and an image.
const scenario = await session(A.id, "Run the full scenario review and finish with the timeline notes.", "All scenarios", { agent: "scenario" });
await idle(scenario.id);
const failing = await session(A.id, "fail cleanly: check the missing notes before changing anything.", "Failed tool");
await idle(failing.id);
const release = await session(A.id, "Hello, summarise the release notes.", "Release notes for 0.10 with a deliberately long title that must truncate");
await idle(release.id);
const diagrams = await session(A.id, "mermaid: draw the diagrams.", "Mermaid diagrams");
await idle(diagrams.id);
// Leave this one running: its live reasoning belongs in the activity group, with no footer yet.
const think = await session(A.id, "think through the timeline footer and live thought placement in detail.", "Thinking now");

const flaky = await session(A.id, "Run a tool to check the flaky test.", "Waiting for approval", { permission: "ask" });
const ap = await waitApproval(flaky.id);

const question = await session(B.id, "Ask me which database to use.", "Pick the migration database");
const q = await waitQuestion(question.id);

// Rail labels and counts need more projects than one.
const notes = await session(B.id, "Hello, what is in the notes?", "Notes backlog");
await idle(notes.id);
for (const [pid, titleText] of [[C.id, "Nimbus ideas"], [D.id, "Nova plan"], [E.id, "Terminal harness"]]) {
  const s = await call("POST", "/api/session", { project: pid, title: titleText });
  await call("POST", `/api/session/${s.id}/prompt`, { text: "Hello, summarise the release notes." });
  await idle(s.id);
}

console.log(JSON.stringify({
  projectA: A.id, projectB: B.id,
  scenario: scenario.id, failing: failing.id, think: think.id,
  refactor: scenario.id, flaky: flaky.id, release: release.id, diagrams: diagrams.id, question: question.id,
  approval: ap.id, question_id: q.id,
}));
