import { bindKeys } from "./src/commands";
import { centre, currentProject, model } from "./src/store";

export const railProject = { id: "prj_rail", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
export const railSession = { id: "ses_rail", project_id: "prj_rail", directory: "/work/demo", title: "Rail session", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };

/** Alternating user messages and tall replies, so one turn fills most of a desktop viewport. */
export const railMessages = Array.from({ length: 20 }, (_, i) => {
  const text = i % 2 === 0
    ? `Message ${i}\n\nThe user message that the rail marks.`
    : `Reply ${i}\n\n${Array.from({ length: 40 }, (_, line) => `Line ${line + 1} of the reply.`).join("\n\n")}`;
  return { id: `m${i}`, session_id: railSession.id, seq: i + 1, role: i % 2 ? "assistant" : "user", parts: [{ type: "text", text }], created: i + 1 };
});

export const railSingleMessages = [
  { id: "solo-user", session_id: railSession.id, seq: 1, role: "user", parts: [{ type: "text", text: "Only one message" }], created: 1 },
  ...Array.from({ length: 10 }, (_, i) => ({ id: `solo-a${i}`, session_id: railSession.id, seq: i + 2, role: "assistant", parts: [{ type: "text", text: `Reply ${i}\n\n${"Tall reply line. ".repeat(30)}` }], created: i + 2 })),
];

export function seedRail(messages: typeof railMessages) {
  model.sessions.set(railSession.id, railSession as any);
  model.projects.set(railProject.id, railProject);
  model.select(railSession.id);
  model.applyHistory({ sessionId: railSession.id, messages: messages as any, messagesMore: false, starting: null, turns: [], turnsMore: false, floor: null, usage: null, turnsGap: null });
  currentProject.set(railProject.id);
  centre.set("session");
  bindKeys();
}
