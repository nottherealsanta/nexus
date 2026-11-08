// A workspace watcher is leased only while the desktop Files/editor surface is visible.
import { computed, effect, signal } from "nsh";
import { currentClientView, live, openFile, phone } from "./store";
import { request } from "./core/transport";

export const fileWatchStatus = signal("");
const client = `nsh-files-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
async function lease(project: string, session: string | undefined, action: "renew" | "release") {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    return await request<{ status: string }>("POST", `/api/project/${project}/watch`, {
      query: { session }, body: { client_id: client, action }, signal: controller.signal,
    });
  } finally { clearTimeout(timeout); }
}
export function installFileWatch() {
  const target = computed(() => {
    if (phone || !live()) return "";
    const view = currentClientView();
    if (!view.project || (view.panel !== "files" && view.panel !== "file")) return "";
    return JSON.stringify([view.project, view.panel === "file" ? openFile()?.session ?? null : null]);
  });
  effect(() => {
    const key = target();
    if (!key) { fileWatchStatus.set(""); return; }
    const [project, session] = JSON.parse(key) as [string, string | null];
    let closed = false, pending = false;
    const renew = async () => {
      if (pending || closed) return;
      pending = true;
      try { const response = await lease(project, session ?? undefined, "renew"); if (!closed) fileWatchStatus.set(response.status); }
      catch { if (!closed) fileWatchStatus.set("disconnected"); }
      finally { pending = false; }
    };
    void renew();
    const timer = setInterval(() => void renew(), 20000);
    return () => {
      closed = true; clearInterval(timer); fileWatchStatus.set("");
      void lease(project, session ?? undefined, "release").catch(() => {});
    };
  });
}
