#!/usr/bin/env python3
"""Generate NSH inputs from the shared interface files (no hand-copied values).

  ../interface/theme.toml    -> gen/theme.json   (nsh.json "theme": cssVars, light + dark)
                             -> src/gen/theme.ts (roles, status glyphs, structure glyphs)
  ../interface/manifest.toml -> src/gen/manifest.ts (commands, settings, navigation, keymap)
  theme-void.toml            -> merged over theme.toml first: NSH's palette overlay

Stdlib only, Python 3.11+. Run: npm run gen
"""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IFACE = ROOT.parent / "interface"


def merge(base: dict, over: dict) -> dict:
    """Merge `over` into `base`, table by table, so the overlay names only what changes."""
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


theme = tomllib.loads((IFACE / "theme.toml").read_text())
merge(theme, tomllib.loads((ROOT / "theme-void.toml").read_text()))
manifest = tomllib.loads((IFACE / "manifest.toml").read_text())


def flat(name: str) -> dict[str, str]:
    t = theme[name]
    return {f"{g}.{k}": v for g, d in t.items() for k, v in d.items()}


def css(mode: str) -> dict[str, str]:
    t = flat(mode)
    g = lambda k: t[k]
    return {
        # shadcn names NSH base components expect
        "background": g("surface.canvas"),
        "foreground": g("text.primary"),
        "card": g("surface.panel"),
        "card-foreground": g("text.primary"),
        "popover": g("surface.raised"),
        "popover-foreground": g("text.primary"),
        "primary": g("interactive.accent"),
        "primary-foreground": g("interactive.on_accent"),
        "secondary": g("surface.raised"),
        "secondary-foreground": g("text.secondary"),
        "muted": g("surface.panel"),
        "muted-foreground": g("text.muted"),
        "accent": g("interactive.hover"),
        "accent-foreground": g("text.primary"),
        "destructive": g("status.error"),
        "border": g("border.default"),
        "input": g("border.default"),
        "ring": g("border.focus"),
        "sidebar": g("surface.sidebar"),
        "sidebar-foreground": g("text.primary"),
        "sidebar-primary": g("interactive.accent"),
        "sidebar-primary-foreground": g("interactive.on_accent"),
        "sidebar-accent": g("interactive.selection"),
        "sidebar-accent-foreground": g("text.primary"),
        "sidebar-border": g("border.default"),
        "sidebar-ring": g("border.focus"),
        "chart-1": g("status.info"),
        "chart-2": g("status.success"),
        "chart-3": g("status.warning"),
        "chart-4": g("status.error"),
        "chart-5": g("interactive.accent"),
        # Nexus tokens beyond shadcn (names = token names, dots to dashes)
        "foreground-secondary": g("text.secondary"),
        "faint": g("text.faint"),
        "raised": g("surface.raised"),
        "band": g("surface.band"),
        "selection": g("interactive.selection"),
        "settings-selection": g("interactive.settings_selection"),
        "settings-focus": g("interactive.settings_focus"),
        "scrollbar": g("interactive.scrollbar"),
        "highlight": g("text.highlight"),
        "active": g("interactive.active"),
        "active-bar": g("interactive.active_bar"),
        "skill-card": g(theme["roles"]["skill_card"]["bg"]),
        "skill-card-focus": g(theme["roles"]["skill_card_focus"]["bg"]),
        "hover": g("interactive.hover"),
        "on-accent": g("interactive.on_accent"),
        "multi": g("interactive.multi"),
        "success": g("status.success"),
        "warning": g("status.warning"),
        "error": g("status.error"),
        "info": g("status.info"),
        "border-strong": g("border.strong"),
        "diff-add": g("diff.add"),
        "diff-del": g("diff.del"),
        "diff-add-bg": g("diff.add_bg"),
        "diff-del-bg": g("diff.del_bg"),
        **{f"agent-{slot}": color for slot, color in theme[mode]["agent"].items()},
        # The Manager's orb states (docs/manager-design.md 10.2): manager-listening, -thinking, -speaking, -idle
        **{f"manager-{state}": color for state, color in theme[mode]["manager"].items()},
    }


def diagram(mode: str) -> dict[str, object]:
    """The Ondine raster palette, from the same tokens as the TUI's."""
    t = flat(mode)
    return {
        "background": t["surface.canvas"],
        "foreground": t["text.primary"],
        "muted": t["text.muted"],
        "line": t["border.strong"],
        "border": t["border.strong"],
        "surface": t["surface.panel"],
        "surface_alt": t["surface.raised"],
        "accent": t["interactive.accent"],
        "critical": t["status.error"],
        "font": "Monaspace Argon",
        "stroke": "2",
        "radius": "0",
        "title_weight": "700",
        "series": [t[f"agent.{slot}"] for slot in ("amber", "blue", "violet", "green", "teal", "rose", "sky", "gold")],
    }


# Radius and fonts are NSH-medium choices (see README.md); colours are not. Controls, cards and
# surfaces are square, matching the TUI's cell grid; only circular indicators keep their shape.
RADIUS = {"radius": "0", "radius-card": "0"}
FONTS = {
    "font-sans": "Monaspace Argon, ui-monospace, monospace",
    "font-mono": "Monaspace Argon, ui-monospace, monospace",
    "font-code": "Monaspace Argon, ui-monospace, monospace",
}
cssvars = {
    "theme": {**FONTS, **RADIUS},
    "light": css("light"),
    "dark": css("dark"),
}
(ROOT / "gen").mkdir(exist_ok=True)
(ROOT / "gen" / "theme.json").write_text(json.dumps({"cssVars": cssvars}, indent=2) + "\n")

hdr = "// GENERATED by scripts/gen.py from ../interface/{}. Do not edit.\n"
out = hdr.format("theme.toml")
out += "export const agentPins = " + json.dumps(theme["agent_pins"], indent=2) + " as const;\n"
out += "export const agentSlots = " + json.dumps(list(theme["dark"]["agent"])) + " as const;\n"
out += "export const roles = " + json.dumps(theme["roles"], indent=2) + " as const;\n"
out += "export const statusGlyphs = " + json.dumps(theme["glyphs"]["status"], indent=2) + " as const;\n"
out += "export const structure = " + json.dumps(theme["glyphs"]["structure"], indent=2) + " as const;\n"
out += "export const attention = " + json.dumps(theme["glyphs"]["attention"], indent=2) + " as const;\n"
out += "export const fold = " + json.dumps(theme["glyphs"]["fold"], indent=2) + " as const;\n"
out += "export const sidebarGlyphs = " + json.dumps(theme["glyphs"]["sidebar"], indent=2) + " as const;\n"
out += "export const verbGlyphs = " + json.dumps(theme["glyphs"]["verb"], indent=2) + " as const;\n"
out += "export const controlGlyphs = " + json.dumps(theme["glyphs"]["control"], indent=2) + " as const;\n"
out += "export const spinner = " + json.dumps(theme["glyphs"]["spinner"]["unicode"]) + ";\n"
out += "export const managerColors = " + json.dumps({"light": theme["light"]["manager"], "dark": theme["dark"]["manager"]}) + " as const;\n"
out += "export const shimmerMotion = " + json.dumps(theme["motion"]["shimmer"]) + " as const;\n"
out += "export const shimmerColors = " + json.dumps({mode: {"base": theme[mode]["text"]["secondary"], "peak": theme[mode]["text"]["highlight"]} for mode in ["dark", "light"]}) + " as const;\n"
out += "export const annotationInks = " + json.dumps(theme["annotation"]["ink"]) + ";\n"
out += "export const diagramPalettes = " + json.dumps({"light": diagram("light"), "dark": diagram("dark")}) + ";\n"
(ROOT / "src" / "gen" / "theme.ts").write_text(out)

out = hdr.format("manifest.toml")
out += "export const manifest = " + json.dumps(manifest, indent=1) + " as const;\n"
(ROOT / "src" / "gen" / "manifest.ts").write_text(out)
print("generated gen/theme.json, src/gen/theme.ts, src/gen/manifest.ts")
