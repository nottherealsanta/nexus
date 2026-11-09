import { draftKey } from "./commands";
import { confirmDialog, connection, draft, viewed } from "./store";
import { terminalError } from "./terminal-panel";

/** The preview owns an immutable payload; only normal composer send commits it. */
export function previewTerminalEvidence(target: string, payload: string) {
  confirmDialog.set({title:"Attach terminal output", body:payload, confirm:"Add to draft", destructive:false, scrollBody:true, run:() => {
    if (draftKey() !== target || viewed()?.kind === "manager" || connection().kind !== "live") {
      terminalError.set("Draft changed or is read-only; capture terminal output again."); confirmDialog.set(null); return;
    }
    draft.update(d => ({...d, [target]:(d[target] ?? "") + ((d[target] ?? "") ? "\n\n" : "") + payload}));
    confirmDialog.set(null);
  }});
}
