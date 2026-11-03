// Pure Mermaid helpers, kept apart from the native module so tests and other
// views can import them without loading `nexus/native`.

/**
 * The source of every closed `mermaid` fence, in order. An unclosed last fence
 * is absent, so a half-written diagram shows its source instead of flickering.
 */
export function closedFenceSources(markdown: string): string[] {
  const out: string[] = [];
  let open: { mark: string; run: number; body: string[] } | null = null;
  for (const line of markdown.split("\n")) {
    const trimmed = line.trimStart();
    if (!open) {
      const match = /^(`{3,}|~{3,})\s*mermaid(?:\s|$)/.exec(trimmed);
      if (match) open = { mark: match[1][0], run: match[1].length, body: [] };
      continue;
    }
    const close = /^(`{3,}|~{3,})\s*$/.exec(trimmed);
    if (close && close[1][0] === open.mark && close[1].length >= Math.max(3, open.run)) {
      out.push(open.body.join("\n").trimEnd());
      open = null;
    } else {
      open.body.push(line);
    }
  }
  return out;
}

/** The TUI's tone-to-role mapping, expressed with NSH's theme classes. */
export function toneClass(tone: string): string {
  switch (tone) {
    case "title": return "text-foreground-secondary font-semibold";
    case "heading": return "text-foreground font-semibold";
    case "bar": return "text-primary";
    case "quiet": return "text-muted-foreground";
    default: return "text-foreground";
  }
}
