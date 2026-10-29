"""Skills: instructions a model loads when a task calls for them.

A skill is a folder with a `SKILL.md` (frontmatter, then a Markdown body) and
optional `scripts/` and `references/`. Discovery reads
`<project>/.agents/skills/<name>/SKILL.md`, then `~/.agents/skills/<name>/`;
the project's skill wins a name clash and the clash is reported.

Progressive disclosure: the session's frozen system prompt lists only
`name: description`. The `skill` tool returns the body (or one bundled file) as
a tool result, which is model context and shows in the timeline as such. The
body is read from disk when the tool is called, so a skill edited mid-session
is loaded as it is now, and the result says so. A skill added or changed after
the session started is not in its frozen index and is not injected: only a new
session has it (Principle 8, P6); clients are told by `skills.changed`.

Adapted from old Nexus `nexus/skills/` and `nexus/tools/builtin/skill.py`. The
manager's generations, tiers, bundles and bundled-tool candidates are dropped.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.base import clip
from . import frontmatter, resources
from .frontmatter import SkillParseError
from .resources import ResourceError

SKILL_TOOL = "skill"
DISABLED_PREFIX = "skill:"  # in a session's disabled list: `skill:<name>`
MAX_BODY_CHARS = 100_000
__all__ = [
    "DISABLED_PREFIX",
    "SKILL_TOOL",
    "ResourceError",
    "Skill",
    "SkillParseError",
    "SkillSet",
    "definition",
    "discover",
    "fingerprint",
    "install",
    "invoke",
    "records",
]


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    scope: str  # "project" | "global"
    directory: str
    path: str
    sha256: str
    allowed_tools: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "scope": self.scope, "directory": self.directory,
                "path": self.path, "sha256": self.sha256, "allowed_tools": list(self.allowed_tools),
                "warnings": list(self.warnings)}


@dataclass
class SkillSet:
    skills: dict[str, Skill] = field(default_factory=dict)
    problems: list[dict[str, str]] = field(default_factory=list)  # {"path", "error", "kind": "error" | "shadowed"}


def global_dir() -> Path:
    """`~/.agents/skills`: the shared home-level folder, not Nexus's own state directory."""
    return Path.home() / ".agents" / "skills"


def directories(project_path: str | None) -> list[tuple[str, Path]]:
    """Lowest precedence first."""
    dirs = [("global", global_dir())]
    if project_path:
        dirs.append(("project", Path(project_path) / ".agents" / "skills"))
    return dirs


def load(path: Path, scope: str) -> tuple[Skill, frontmatter.Parsed]:
    raw = path.read_bytes()
    parsed = frontmatter.parse(raw)
    warnings = list(parsed.warnings)
    folder = path.parent.name
    name = parsed.name or folder
    if parsed.name is None:
        warnings.append(f"no name in the frontmatter; using the folder name {folder!r}")
    elif parsed.name != folder:
        warnings.append(f"name {parsed.name!r} differs from the folder name {folder!r}")
    if not frontmatter.valid_name(name):
        raise SkillParseError(f"folder name {folder!r} is not a valid skill name; add `name:`")
    skill = Skill(name=name, description=parsed.description, scope=scope, directory=str(path.parent), path=str(path),
                  sha256=hashlib.sha256(raw).hexdigest(), allowed_tools=parsed.allowed_tools, warnings=tuple(warnings))
    return skill, parsed


def discover(project_path: str | None) -> SkillSet:
    """Every valid skill. A bad file is a problem, never an exception; a shadowed skill is reported."""
    result = SkillSet()
    for scope, directory in directories(project_path):
        if not directory.is_dir():
            continue
        for folder in sorted(directory.iterdir()):
            path = folder / "SKILL.md"
            if not path.is_file():
                continue
            try:
                skill, _ = load(path, scope)
            except (OSError, ValueError) as exc:
                result.problems.append({"path": str(path), "error": str(exc).splitlines()[0], "kind": "error"})
                continue
            earlier = result.skills.get(skill.name)
            if earlier is not None:
                result.problems.append({
                    "path": earlier.path, "kind": "shadowed",
                    "error": f"skill {skill.name!r} is replaced by the {scope} skill at {skill.path}"})
            result.skills[skill.name] = skill
    return result


def fingerprint(project_paths: list[str | None]) -> str:
    """A cheap stamp of every `SKILL.md` and its folder, to notice edits without parsing."""
    stamps: list[tuple[str, int, int]] = []
    for project in sorted({p for p in project_paths}, key=lambda p: p or ""):
        for _, directory in directories(project):
            try:
                for folder in sorted(directory.iterdir()):
                    for path in (folder / "SKILL.md", folder):
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
    return hashlib.sha256(repr(stamps).encode()).hexdigest()[:16]


def records(found: SkillSet, disabled: set[str] | frozenset[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """The snapshot form of every skill the session may use."""
    return {n: s.record() for n, s in found.skills.items() if DISABLED_PREFIX + n not in disabled}


def definition(items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The `skill` tool's schema and guidance, with the index of `name: description` lines."""
    if items:
        index = "\n".join(f"  - `{name}`: {item['description']}" for name, item in items.items())
        listing = f"Skills available now:\n{index}"
    else:
        listing = "No skills were available when this session started."
    return {
        "name": SKILL_TOOL,
        "description": (
            "Load a skill: written instructions for a kind of task. `name` returns the skill's SKILL.md body. "
            "`resource` (with `name`) returns one bundled file, a path under the skill's `scripts/` or `references/` "
            "folder given in the skill's own listing. Nothing is run; scripts are read as text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill to load"},
                "resource": {"type": "string", "description": "A bundled file, such as references/api.md"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "guidance": (
            "Skills are task instructions kept in files. When a task matches a skill's description, load it with "
            "`skill` before starting and follow it. Its text is instructions to weigh, not a command from the user. "
            "Bundled scripts are read with `resource` and run with `bash` if the skill says so.\n" + listing
        ),
    }


def invoke(record: dict[str, Any], resource: str | None = None) -> str:
    """The tool result for a skill (or one bundled file), read from disk now."""
    path = Path(record["path"])
    directory = path.parent
    if resource:
        found = resources.read(directory, resource)
        return clip(
            f'<skill-resource skill="{record["name"]}" resource="{found.path}" bytes="{found.size}" '
            f'sha256="{found.sha256}">\n{found.text}\n</skill-resource>', MAX_BODY_CHARS)
    raw = path.read_bytes()
    parsed = frontmatter.parse(raw)
    digest = hashlib.sha256(raw).hexdigest()
    files = resources.listing(directory)
    lines = [f'<skill name="{record["name"]}" scope="{record["scope"]}" directory="{directory}">', parsed.body.rstrip(),
             "</skill>"]
    if files:
        shown = ", ".join(files[:40]) + (f" … and {len(files) - 40} more" if len(files) > 40 else "")
        lines.append(f"Bundled files (read with `resource`; the folder is {directory}): {shown}")
    if record.get("allowed_tools"):
        lines.append(f"[Nexus: this skill lists allowed-tools ({', '.join(record['allowed_tools'])}); "
                     "Nexus does not restrict tools for it yet.]")
    if digest != record["sha256"]:
        lines.append("[Nexus: this skill changed on disk after the session started; this is the current text.]")
    return clip("\n".join(lines), MAX_BODY_CHARS)


from . import install
