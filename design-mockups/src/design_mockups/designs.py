"""Layouts and the named designs.

A design = a layout (where regions go) + a palette + one variant per element
("picks"; unlisted elements use variant ``a``). ``design mix`` builds the same
thing from your own picks, so anything seen here can be recombined.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Layout:
    name: str
    note: str
    left: bool = True            # sessions sidebar
    right: str = "details"       # "details" | "inspector" | ""
    boxed: bool = False          # titled borders around regions
    center_max: int = 0          # cap the conversation width (0 = fill)
    left_width: int = 34
    right_width: int = 42
    pad: int = 2                 # horizontal padding of the conversation
    gap: int = 1                 # blank rows between turns
    meter: str = "dock"          # context meter: "dock" (under composer) | "top" | "none"
    rules: bool = True           # hairlines between regions


LAYOUTS: dict[str, Layout] = {x.name: x for x in (
    Layout("classic", "Today: sessions · conversation · details, three columns."),
    Layout("focus", "One centred column, no sidebars ([ and ] open them).", left=False, right="", center_max=104, pad=2, rules=False),
    Layout("workbench", "Every region a titled box; right column stacked.", boxed=True, left_width=32, right_width=44, pad=1),
    Layout("ledger", "Classic columns with the context meter pinned under the top bar.", meter="top", left_width=40, right_width=38),
    Layout("inspector", "Timeline left, a wide inspector right for the selected row.", left=False, right="inspector", right_width=64),
    Layout("paper", "A narrow reading column with wide margins and no borders.", left=False, right="", center_max=92, pad=4, gap=2,
           meter="none", rules=False),
)}


@dataclass(frozen=True)
class Design:
    slug: str
    title: str
    idea: str
    layout: str
    palette: str
    picks: dict[str, str] = field(default_factory=dict)
    light: bool = False


DESIGNS: list[Design] = [
    Design("baseline", "Baseline", "Today's Nexus TUI rebuilt as a static mock-up: the control every other design is compared with. "
           "Every element uses variant A.", "classic", "nexus"),
    Design("focus", "Focus", "Chrome on demand. One centred column, no sidebars, one-line rows that expand. Tests whether "
           "Nexus can drop the sidebars without hiding anything: everything stays one key away and labelled.", "focus", "nexus", {
               "topbar": "d", "context-header": "c", "system-prompt": "c", "tools": "c", "user-message": "b", "thought": "b",
               "tool-call": "b", "diff": "d", "reply-footer": "b", "composer": "b", "recording": "c", "activity": "b",
               "context-meter": "c", "permission": "b", "question": "b", "subagent": "c", "error": "d", "empty": "b",
               "command-palette": "c", "tool-details": "c"}),
    Design("workbench", "Workbench", "IDE / lazygit style. Every region is a titled box, the right column stacks files, agents, "
           "jobs and worktrees so they are all visible at once. Dense and keyboard-first.", "workbench", "workbench", {
               "topbar": "c", "sessions": "b", "details": "b", "context-header": "d", "system-prompt": "d", "tools": "d",
               "user-message": "e", "thought": "d", "tool-call": "b", "diff": "b", "assistant": "b", "reply-footer": "b",
               "composer": "e", "recording": "f", "activity": "c", "context-meter": "c", "permission": "d", "subagent": "b",
               "error": "b", "logs": "b", "command-palette": "b", "model-picker": "b", "tool-details": "b", "empty": "c"}),
    Design("ledger", "Ledger", "Context first. Every row shows what it added to the context window, the header is a token "
           "table, and the stacked context meter is pinned to the top. Shows exactly what the model sees.", "ledger", "ledger", {
               "topbar": "c", "sessions": "c", "details": "d", "context-header": "b", "system-prompt": "e", "tools": "b",
               "skills-mcp": "b", "user-message": "e", "thought": "d", "tool-call": "d", "diff": "c", "reply-footer": "d",
               "composer": "c", "recording": "b", "activity": "b", "context-meter": "b", "permission": "d", "question": "b",
               "subagent": "d", "error": "b", "logs": "b", "model-picker": "b"}),
    Design("inspector", "Inspector", "Master / detail. The timeline stays one line per row; the selected row (here the failing "
           "pytest run) is shown in full on the right: parameters, output, timing, tokens, permission.", "inspector", "nexus", {
               "topbar": "b", "context-header": "e", "system-prompt": "b", "tools": "c", "user-message": "d", "thought": "b",
               "tool-call": "a", "diff": "c", "assistant": "b", "reply-footer": "b", "composer": "c", "recording": "e",
               "activity": "b", "context-meter": "b", "error": "d", "command-palette": "c", "model-picker": "c"}),
    Design("signal", "Signal", "The web app's original Signal language brought to the terminal: capitalised labels on rules, "
           "solid swatches, tinted tags, one job per colour, a solid banner when it needs you. Same layout as baseline, so it "
           "isolates the visual language.", "classic", "signal", {
               "topbar": "e", "details": "c", "context-header": "f", "system-prompt": "d", "tools": "c", "skills-mcp": "c",
               "user-message": "c", "thought": "c", "tool-call": "e", "assistant": "b", "reply-footer": "c", "composer": "d",
               "recording": "d", "activity": "a", "context-meter": "b", "permission": "c", "question": "c", "subagent": "d",
               "error": "c", "logs": "c", "command-palette": "b", "model-picker": "b"}),
    Design("paper", "Paper", "Reading mode, light first. No borders; hierarchy by weight, spacing and indentation. Tool calls "
           "read as sentences. Built for reviewing what an agent did.", "paper", "paper", {
               "topbar": "d", "context-header": "c", "system-prompt": "c", "tools": "c", "user-message": "d", "thought": "b",
               "tool-call": "f", "diff": "d", "assistant": "d", "composer": "c", "recording": "e", "activity": "d",
               "context-meter": "c", "permission": "b", "question": "b", "subagent": "c", "empty": "b", "model-picker": "c",
               "tool-details": "c"}, light=True),
]

NUMBER_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def find(name: str) -> int | None:
    """Index of a design by number word, digit, or slug."""
    n = name.lower()
    if n in NUMBER_WORDS:
        i = NUMBER_WORDS.index(n)
    elif n.isdigit():
        i = int(n) - 1
    else:
        i = next((k for k, d in enumerate(DESIGNS) if d.slug == n), -1)
    return i if 0 <= i < len(DESIGNS) else None
