// The shared Mermaid presentation: the same text rows and PNG the TUI draws.
// The native module owns compilation and rasterization; this file only caches
// the results and resolves the palette for the current appearance.
import { colorScheme, nativeModule, platform } from "nsh";
import type { MermaidModule } from "nexus/native";
import { diagramPalettes } from "./gen/theme";
import { closedFenceSources, toneClass } from "./mermaid-logic";

export { closedFenceSources, toneClass };

// A static `nexus/native` import makes every test that imports the timeline
// (even for a pure helper) load the native module outside the app bundle. Look
// it up at call time instead; the app registers it on every target.
let native: MermaidModule | null = null;
function mermaid(): MermaidModule {
  native ??= nativeModule<{ Mermaid: MermaidModule }>("nexus/native").Mermaid;
  return native;
}

export type MermaidTextRun = { text: string; tone: string };
export type MermaidTextRow = { runs: MermaidTextRun[] };
export type MermaidTerminalRow = { runs: { text: string; foreground: string; background: string }[] };
export type MermaidImage = { url: string; width: number; height: number; cells?: MermaidTerminalRow[] };

const CACHE_LIMIT = 64;
const textCache = new Map<string, MermaidTextRow[] | null>();
const imageCache = new Map<string, MermaidImage | null>();

function remember<T>(cache: Map<string, T>, key: string, value: T): T {
  if (cache.size >= CACHE_LIMIT) cache.clear();
  cache.set(key, value);
  return value;
}

/** The chart types that read as rows, or null when they need the raster. */
export function mermaidText(source: string, columns: number): MermaidTextRow[] | null {
  const key = `${columns}:${source}`;
  if (textCache.has(key)) return textCache.get(key)!;
  let rows: MermaidTextRow[] | null = null;
  try {
    rows = mermaid().renderText(source, columns);
  } catch {
    rows = null;
  }
  return remember(textCache, key, rows);
}

/** The shared PNG, at the width the measured column has. */
export function mermaidImage(source: string, columns: number, maxHeight?: number): MermaidImage | null {
  const mode = colorScheme() === "light" ? "light" : "dark";
  const maxRows = Math.min(80, Math.max(1, Math.floor((maxHeight ?? 1280) / 16)));
  const terminal = platform.idiom === "terminal";
  const key = `${terminal}:${maxRows}:${mode}:${columns}:${source}`;
  if (imageCache.has(key)) return imageCache.get(key)!;
  let image: MermaidImage | null = null;
  try {
    const palette = diagramPalettes[mode];
    const theme = {
      background: palette.background,
      foreground: palette.foreground,
      muted: palette.muted,
      line: palette.line,
      border: palette.border,
      surface: palette.surface,
      surfaceAlt: palette.surface_alt,
      accent: palette.accent,
      critical: palette.critical,
      font: palette.font,
      stroke: palette.stroke,
      radius: palette.radius,
      titleWeight: palette.title_weight,
      series: palette.series,
    };
    if (terminal) {
      const cells = mermaid().renderTerminal(source, columns, maxRows, theme);
      image = { url: "", cells, width: Math.max(0, ...cells.map((row) => row.runs.reduce((n, run) => n + run.text.length, 0))) * 8, height: cells.length * 16 };
      return remember(imageCache, key, image);
    }
    const raster = mermaid().renderPng(source, columns * 10, columns * 40, theme);
    if (raster && raster.png?.length) {
      // The raster is laid out for 10x20 cells; the client sets text at 12.5px
      // with a 20px line, so scale it to the measured column.
      const scale = (columns * 7.75) / (columns * 10);
      image = {
        url: `data:image/png;base64,${base64(raster.png)}`,
        width: Math.ceil(raster.cssWidth * scale),
        height: Math.ceil(raster.cssHeight * scale),
      };
    }
  } catch {
    image = null;
  }
  return remember(imageCache, key, image);
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return btoa(binary);
}

