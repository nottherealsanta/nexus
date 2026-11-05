import { mount } from "nsh";
import scenario from "../interface/scenarios/first-prompt.json";
import { NewSessionView } from "./src/ui/newsession";
import { currentProject, firstPrompts, model, sessionDraft } from "./src/store";
import { setApiBase } from "./src/core/transport";

export default function FirstPromptFixture() {
  setApiBase("http://nexus.test");
  model.select(null);
  model.projects.set("prj_instant", { id: "prj_instant", path: "/work/demo", name: "demo", created: 1, updated: 1, revision: 1 });
  currentProject.set("prj_instant");
  const choices = { agent: scenario.agent, model: scenario.model };
  sessionDraft.set(choices);
  firstPrompts.set({ prj_instant: { text: scenario.text, images: 0, choices } });
  return <NewSessionView />;
}

mount(FirstPromptFixture);
