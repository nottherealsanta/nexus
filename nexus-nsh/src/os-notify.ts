// System notifications (macOS, iOS, web) through nsh's `Notifications`.
// Shown for sessions out of view, or for any session while the app is not in front;
// presses open the session. Best effort: no permission or no platform support stays silent.
import { Notifications, lifecycle } from "nsh";

type Open = (sessionId: string) => void;
type Kind = "finished" | "input";

let asked = false;
async function allowed(): Promise<boolean> {
  let state = await Notifications.permission();
  if (state === "default" && !asked) {
    asked = true;
    state = await Notifications.requestPermission();
  }
  return state === "granted";
}

export function listenNotificationPresses(open: Open) {
  try {
    Notifications.on("press", ({ id }) => {
      const sessionId = id.slice(id.indexOf(":") + 1);
      if (sessionId) open(sessionId);
    });
  } catch { /* the notifications capability or platform is missing */ }
}

export async function notifySession(kind: Kind, sessionId: string, title: string, viewedId: string | null) {
  try {
    if (lifecycle() === "active" && viewedId === sessionId) return;
    if (!(await allowed())) return;
    await Notifications.show({
      id: `${kind}:${sessionId}`,
      title: title || "Nexus",
      body: kind === "input" ? "Needs your input" : "Finished",
    });
  } catch { /* notifications are optional */ }
}
