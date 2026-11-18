import { openUrl } from "nsh";
import { openFileTab } from "./layout-state";
import { currentProject, model, phone } from "./store";

/** Resolve a Markdown file target within the workspace. */
export function markdownFilePath(url: string, root?: string, source?: string): string | null {
  if (!url || url.startsWith("#") || /^[a-z][a-z\d+.-]*:/i.test(url) && !url.startsWith("file:")) return null;
  let path = url.replace(/^file:\/\//, "").split("#", 1)[0].replace(/:\d+(?::\d+)?$/, "");
  try { path = decodeURIComponent(path); } catch { return null; }
  root = root?.replace(/\/$/, "");
  if (path.startsWith("/")) {
    if (!root || !path.startsWith(`${root}/`)) return null;
    path = path.slice(root.length + 1);
  } else if (source) {
    const dir = source.includes("/") ? source.slice(0, source.lastIndexOf("/") + 1) : "";
    path = dir + path;
  }
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") { if (!parts.length) return null; parts.pop(); } else parts.push(part);
  }
  return parts.length ? parts.join("/") : null;
}

/** Open web links externally and workspace links in a file tab. */
export function openMarkdownLink(url: string, source?: string) {
  if (/^[a-z][a-z\d+.-]*:/i.test(url) && !url.startsWith("file:")) { void openUrl(url).catch(() => {}); return; }
  if (phone) return;
  const root = model.projects.get(currentProject() ?? "")?.path;
  const path = markdownFilePath(url, root, source);
  if (path) openFileTab(path);
}
