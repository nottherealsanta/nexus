// The Files tab's tree, flattened in draw order: depth-first through expanded
// folders, as the TUI's `App::file_rows` does. A folder that has not been read
// yet simply has no children; Core is asked when it expands.
import type { FileDir, FileEntry } from "./store";

export type FileRow = {
  path: string;
  name: string;
  kind: string;
  depth: number;
  expanded: boolean;
  git_status?: string | null;
  ignored: boolean;
};

/** Rows for the directories read so far; `.git` is never listed, as in the TUI. */
export function fileRows(dirs: Record<string, FileDir>, expanded: ReadonlySet<string>): FileRow[] {
  const out: FileRow[] = [];
  const walk = (dir: string, depth: number) => {
    const listing = dirs[dir];
    if (!listing) return;
    if (listing.error) {
      out.push({ path: dir, name: listing.error, kind: "error", depth, expanded: false, ignored: false });
      return;
    }
    for (const entry of listing.entries) {
      if (entry.name === ".git") continue;
      const path = dir ? `${dir}/${entry.name}` : entry.name;
      const open = entry.kind === "directory" && expanded.has(path);
      out.push({ path, name: entry.name, kind: entry.kind, depth, expanded: open, git_status: entry.git_status, ignored: entry.ignored });
      if (open) walk(path, depth + 1);
    }
  };
  walk("", 0);
  return out;
}

export const isDirectory = (entry: FileEntry | FileRow) => entry.kind === "directory";
