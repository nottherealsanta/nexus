#!/usr/bin/env node
// Seeds a scripted Core (e2e/scripted_core.py) through its public HTTP API: projects, sessions, a
// finished tool run, and sessions left waiting for an approval and for a question.
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
const idle = async (id) => { for (let i = 0; i < 100; i++) { const s = await call("GET", `/api/session/${id}`); if (s.status !== "busy") return; await sleep(100); } throw new Error("still busy " + id); };
const waitApproval = async (id) => { for (let i = 0; i < 100; i++) { const a = await call("GET", `/api/session/${id}/permission`); if (a.length) return a[0]; await sleep(100); } throw new Error("no approval"); };
const waitQuestion = async (id) => { for (let i = 0; i < 100; i++) { const a = await call("GET", `/api/session/${id}/question`); if (a.length) return a[0]; await sleep(100); } throw new Error("no question"); };
const title = (id, t) => call("PATCH", `/api/session/${id}`, { title: t });

for (const p of [dirA, dirB]) fs.mkdirSync(p, { recursive: true });
const B = await call("POST", "/api/project", { path: dirB, name: "docs-site" });
await sleep(50);
const A = await call("POST", "/api/project", { path: dirA, name: "nexus-demo" });

const s1 = await call("POST", "/api/session", { project: A.id });
await call("POST", `/api/session/${s1.id}/prompt`, { text: "Please look at how the loader reads its config and summarise it." }); await idle(s1.id);
await call("POST", `/api/session/${s1.id}/prompt`, { text: "Now run a tool to list the source directory." });
const ap = await waitApproval(s1.id);
await call("POST", `/api/session/${s1.id}/permission/${ap.id}`, { reply: "allow" }); await idle(s1.id);
await title(s1.id, "Refactor loader");
await call("POST", "/api/session", { parent: s1.id, title: "sweep: config call sites" });

const s2 = await call("POST", "/api/session", { project: A.id });
await call("POST", `/api/session/${s2.id}/prompt`, { text: "Run a tool to check the flaky test." }); await waitApproval(s2.id);
await title(s2.id, "Fix flaky test in CI");

const s3 = await call("POST", "/api/session", { project: A.id });
await call("POST", `/api/session/${s3.id}/prompt`, { text: "Hello, summarise the release notes." }); await idle(s3.id);
await title(s3.id, "Release notes for 0.10 with a deliberately long title that must truncate");

const s4 = await call("POST", "/api/session", { project: B.id });
await call("POST", `/api/session/${s4.id}/prompt`, { text: "Ask me which database to use." }); await waitQuestion(s4.id);
await title(s4.id, "Pick the migration database");

console.log(JSON.stringify({ projectA: A.id, projectB: B.id, refactor: s1.id, flaky: s2.id, release: s3.id, question: s4.id }));
