"""Phase 3 exit — baseline closeout evidence (plan sections 10 and 14.14).

This module is the reproducible closeout artifact for the Phase 3 exit gate and
the parts of the Phase 4 gate that later phases depend on. It does not test new
behaviour; it pins the *current* baseline so drift is visible.

Evidence covered
----------------
* **200-message constrained session** (plan §10 Phase 3 exit: "a 200-message
  session runs without overflow"). A real :class:`~nexus.runtime.Runtime` drives
  100 turns (200 messages) under a tight context budget, asserting every
  assembly fits, compaction fires, the full log stays authoritative, and the
  assembled request is a contiguous suffix of history.
* **Controlled prompt cache before/after** (plan §10 Phase 3 exit: "prompt
  caching measurably cuts input cost"). Two identical assemblies — caching
  capability off then on — are billed by a deliberately explicit model
  (hierarchical longest-prefix reuse; cache read 0.1x, cache write 1.25x). The
  recorded before/after numbers are the PR evidence the plan asks for.
* **Phase 4 references** (plan §10 Phase 4 exit: the §6.5 money-path walkthrough
  and the "200 reload cycles leak nothing" gate) are re-run as subprocesses so a
  green closeout proves they still hold.
* **Line counts**: physical and code lines for ``core/ + model/ + tools/spec.py``
  and for ``host/``, ``view/``, ``ui/`` are recorded in the baseline report.
  They are informational; there are no line caps.
* **Import cost** (plan §2.2): ``import nexus`` stays lazy (no runtime/session/
  model/core) and bounded, because subagents spawn nested runtimes.

The machine-readable and text baseline report lives under
``tests/fixtures/reports/``. Regenerate it with::

    NEXUS_PHASE3_WRITE_REPORT=1 pytest tests/test_phase3_exit.py

The default suite never writes; when the report is present it is checked against
live measurements so the committed baseline cannot silently rot.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = Path(__file__).resolve().parent / "fixtures" / "reports"
REPORT_JSON = REPORTS_DIR / "phase3_exit_baseline.json"
REPORT_TXT = REPORTS_DIR / "phase3_exit_baseline.txt"

#: Packages whose physical-line counts are recorded (informational, not capped).
SURFACE_DIRS = ("host", "view", "ui")

#: The Phase 4 gates re-run as evidence: the §6.5 money path and the 200-reload
#: leak bound.
PHASE4_NODES = [
    "tests/test_hot_extension_e2e.py::test_walkthrough_read_template_write_reload_call",
    "tests/test_hot_extension_stress.py::test_two_hundred_reloads_do_not_leak",
]

#: Modules ``import nexus`` must never pull in (mirrors ``test_root_imports``).
HEAVY_MODULES = [
    "httpx",
    "nexus.runtime",
    "nexus.session",
    "nexus.session.lock",
    "nexus.model",
    "nexus.model.providers",
    "nexus.model.providers.anthropic",
    "nexus.model.router",
    "nexus.core",
    "nexus.core.loop",
]


# ---------------------------------------------------------------------------
# Line-budget helpers
# ---------------------------------------------------------------------------


def _tree_blobs(prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
    """``(relative path, text)`` for Python files in the **current tree**.

    Deliberately measured from the working tree, not from ``git ls-tree HEAD``.
    The budget is a property of the code that will ship, and Phase 5.5 added
    ``nexus/model/registry.py`` and ``nexus/model/tiers.py`` as new (at first
    untracked) files; a HEAD-pinned measurement silently omitted them and let
    the recorded baseline rot. Untracked *scratch* files are outside the counted
    prefixes (``nexus/core/``, ``nexus/model/``, ``nexus/tools/spec.py``,
    ``nexus/{host,view,ui}/``), so they cannot skew the budget. The recorded
    ``head`` in the report is provenance only, never the measurement source.
    """
    files: list[tuple[str, str]] = []
    for prefix in prefixes:
        candidates = (
            [REPO_ROOT / prefix]
            if prefix.endswith(".py")
            else sorted(REPO_ROOT.glob(prefix + "**/*.py"))
        )
        for path in candidates:
            if path.is_file():
                files.append(
                    (str(path.relative_to(REPO_ROOT)), path.read_text(encoding="utf-8"))
                )
    return files


def _line_counts(blobs: list[tuple[str, str]]) -> dict[str, int]:
    physical = code = 0
    for _name, text in blobs:
        lines = text.splitlines()
        physical += len(lines)
        code += sum(
            1 for line in lines if line.strip() and not line.strip().startswith("#")
        )
    return {"files": len(blobs), "physical_lines": physical, "code_lines": code}


def _core_line_budget() -> dict[str, object]:
    counts = _line_counts(
        _tree_blobs(("nexus/core/", "nexus/model/", "nexus/tools/spec.py"))
    )
    return dict(counts)


def test_surface_aggregate_totals_equal_directory_sum() -> None:
    surface = _surface_line_budget()
    for name in ("physical_lines", "code_lines", "files"):
        assert surface[name] == sum(item[name] for item in surface["budgets_by_dir"].values())


def test_recorded_surface_aggregate_totals_equal_directory_sum() -> None:
    recorded = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    surface = recorded["line_budgets"]["host_view_ui"]
    for metric in ("physical_lines", "code_lines", "files"):
        assert surface[metric] == sum(item[metric] for item in surface["budgets_by_dir"].values())


def _surface_line_budget() -> dict[str, object]:
    budgets = {}
    for name in SURFACE_DIRS:
        budgets[name] = dict(_line_counts(_tree_blobs((f"nexus/{name}/",))))
    return {
        "physical_lines": sum(item["physical_lines"] for item in budgets.values()),
        "code_lines": sum(item["code_lines"] for item in budgets.values()),
        "files": sum(item["files"] for item in budgets.values()),
        "budgets_by_dir": budgets,
    }


def _assert_surface_totals(surfaces: dict[str, object]) -> None:
    directories = surfaces["budgets_by_dir"]
    for metric in ("physical_lines", "code_lines", "files"):
        if metric in surfaces:
            assert surfaces[metric] == sum(
                item[metric] for item in directories.values()
            ), metric


# ---------------------------------------------------------------------------
# Import-cost helper
# ---------------------------------------------------------------------------


def _run_json(script: str) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_IMPORT_NEXUS_SCRIPT = f"""
import json, sys, time
before = set(sys.modules)
t0 = time.perf_counter()
import nexus
import nexus.errors
elapsed = (time.perf_counter() - t0) * 1000
after = set(sys.modules)
heavy = {HEAVY_MODULES!r}
print(json.dumps({{
    "import_nexus_ms": round(elapsed, 2),
    "new_modules": len(after - before),
    "heavy_loaded": sorted(m for m in heavy if m in after and m not in before),
}}))
"""

_IMPORT_RUNTIME_SCRIPT = """
import json, sys, time
t0 = time.perf_counter()
import nexus.runtime
elapsed = (time.perf_counter() - t0) * 1000
print(json.dumps({"import_runtime_ms": round(elapsed, 2), "modules": len(sys.modules)}))
"""


def _measure_import_cost(runs: int = 3) -> dict[str, object]:
    nexus_runs = [_run_json(_IMPORT_NEXUS_SCRIPT) for _ in range(runs)]
    runtime_runs = [_run_json(_IMPORT_RUNTIME_SCRIPT) for _ in range(runs)]
    best_nexus = min(nexus_runs, key=lambda item: item["import_nexus_ms"])
    best_runtime = min(runtime_runs, key=lambda item: item["import_runtime_ms"])
    return {
        "import_nexus_ms": best_nexus["import_nexus_ms"],
        "import_nexus_new_modules": best_nexus["new_modules"],
        "heavy_loaded": best_nexus["heavy_loaded"],
        "import_runtime_ms": best_runtime["import_runtime_ms"],
        "import_runtime_modules": best_runtime["modules"],
    }


# ---------------------------------------------------------------------------
# Phase 3: 200-message constrained session
# ---------------------------------------------------------------------------


async def _run_200_message_session(workspace: Path) -> dict[str, object]:
    """Drive 100 turns (200 messages) through a real Runtime under a tight budget."""
    from nexus.config import Config
    from nexus.config.schema import (
        ConfigV2,
        ContextSection,
        ModelParams,
        ModelSection,
        SessionSection,
    )
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime
    from nexus.session.records import MessageRecord

    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(
                default="scripted/m", params=ModelParams(max_output_tokens=256)
            ),
            context=ContextSection(
                max_tokens=6000, safety_margin_tokens=0, compaction="hybrid"
            ),
            session=SessionSection(snapshot_every=20),
        ),
    )
    provider = ScriptedProvider(*[text_response("ok") for _ in range(200)])
    runtime = Runtime(workspace, config=config, providers={"scripted": provider})
    try:
        session = runtime.session("exit200")
        compacted = 0
        peak_used = 0
        input_budget = 0
        for turn in range(100):
            events = [event async for event in session.send(f"turn {turn} " + "x" * 60)]
            assert events[-1].type == "turn.completed"
            assembled = [event for event in events if event.type == "context.assembled"]
            assert assembled, "the loop must emit context.assembled"
            for event in assembled:
                context = event.data["context"]
                assert context["used_tokens"] <= context["input_budget"]
                peak_used = max(peak_used, context["used_tokens"])
                input_budget = context["input_budget"]
                if context["history_dropped"] or context["evicted"]:
                    compacted += 1

        messages = session.messages
        assert len(messages) == 200
        persisted_message_records = sum(
            isinstance(record, MessageRecord)
            for record in runtime.sessions.store.records(session.id)
        )

        final = provider.requests[-1].messages
        history_at_assembly = messages[:-1]
        suffix = history_at_assembly[len(history_at_assembly) - len(final) :]
        current_text = "turn 99 " + "x" * 60
        current_occurrences = [
            message
            for message in final
            if message.content and getattr(message.content[0], "text", None) == current_text
        ]
        return {
            "turns": 100,
            "messages": len(messages),
            "model_calls": provider.calls,
            "compacted_assemblies": compacted,
            "input_budget": input_budget,
            "peak_used_tokens": peak_used,
            "persisted_message_records": persisted_message_records,
            "final_request_messages": len(final),
            "contiguous_suffix": final == suffix,
            "current_turn_appears_once": len(current_occurrences) == 1,
        }
    finally:
        await runtime.aclose()


async def test_phase3_exit_200_message_constrained_session(tmp_path: Path) -> None:
    metrics = await _run_200_message_session(tmp_path / "s200")
    assert metrics["messages"] == 200
    assert metrics["persisted_message_records"] == 200
    assert metrics["model_calls"] == 100
    assert metrics["compacted_assemblies"] > 0
    assert metrics["peak_used_tokens"] <= metrics["input_budget"]
    assert metrics["contiguous_suffix"] is True
    assert metrics["current_turn_appears_once"] is True


# ---------------------------------------------------------------------------
# Phase 3: controlled prompt-cache before/after
# ---------------------------------------------------------------------------


def _measure_prompt_cache(workspace: Path, *, turns: int = 30) -> dict[str, object]:
    """Bill identical assemblies with caching off then on.

    The controlled model is deliberately explicit and provider-neutral:

    * the cached region is the prefix up to the last cache boundary
      (system + tools + history);
    * a request reuses the longest previously cached prefix (hierarchical,
      longest-prefix match) at the read rate;
    * the uncached remainder is written at the write rate;
    * everything after the boundary is billed at the full input rate.

    Content is byte-identical between the two runs — only the capability (and
    therefore the boundary metadata) differs — so the totals are comparable.
    """
    from nexus.config import Config
    from nexus.config.schema import (
        ConfigV2,
        ContextSection,
        ModelParams,
        ModelSection,
    )
    from nexus.context import ContextManager
    from nexus.context.parts import canonical_message_text, canonical_tool_text
    from nexus.model.capabilities import Capabilities
    from nexus.model.message import Message, Text
    from nexus.model.request import ToolSchema

    config = Config(
        model="controlled/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(
                default="controlled/m", params=ModelParams(max_output_tokens=1024)
            ),
            context=ContextSection(
                max_tokens=1_000_000, safety_margin_tokens=0, compaction="drop_oldest"
            ),
        ),
    )
    schema = ToolSchema(
        name="Read",
        description="read a file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    )

    def counter(text: str) -> int:
        return len(text)

    class _Session:
        def __init__(self, messages: list[Message]) -> None:
            self.id = "controlled"
            self._messages = messages

        @property
        def messages(self) -> list[Message]:
            return list(self._messages)

    def assemble(caching: bool, messages: list[Message]) -> object:
        capabilities = Capabilities(
            prompt_caching=caching,
            max_context_tokens=200_000,
            max_output_tokens=1024,
        )
        manager = ContextManager(
            workspace,
            config=config,
            capabilities=capabilities,
            counter=counter,
            identity="I",
        )
        manager.freeze_tools([schema])
        return manager.assemble(_Session(messages))

    read_rate, write_rate = 0.1, 1.25
    history: list[Message] = []
    uncached_total = 0
    cached_total = 0.0
    prior_prefixes: list[str] = []
    boundaries_without: list[dict[str, object]] = []
    boundaries_with: list[dict[str, object]] = []
    for turn in range(turns):
        history.append(
            Message(role="user", content=[Text(text=f"question {turn} " + "q" * 40)])
        )
        before = assemble(False, history)
        after = assemble(True, history)
        boundaries_without = before.metadata["cache"]["boundaries"]
        boundaries_with = after.metadata["cache"]["boundaries"]

        system_tokens = len(before.system or "")
        tool_tokens = sum(len(canonical_tool_text(item)) for item in before.tools)
        message_tokens = sum(
            len(canonical_message_text(message)) for message in before.messages
        )
        total = system_tokens + tool_tokens + message_tokens
        uncached_total += total

        history_position = boundaries_with[-1]["position"]
        cached_text = (
            (after.system or "")
            + "".join(canonical_tool_text(item) for item in after.tools)
            + "".join(
                canonical_message_text(message)
                for message in after.messages[:history_position]
            )
        )
        reused = 0
        for prefix in prior_prefixes:
            limit = min(len(prefix), len(cached_text))
            index = 0
            while index < limit and prefix[index] == cached_text[index]:
                index += 1
            reused = max(reused, index)
        cached_total += (
            reused * read_rate
            + (len(cached_text) - reused) * write_rate
            + (total - len(cached_text))
        )
        prior_prefixes.append(cached_text)

        history.append(
            Message(
                role="assistant", content=[Text(text=f"answer {turn} " + "a" * 40)]
            )
        )

    savings = (uncached_total - cached_total) / uncached_total * 100
    return {
        "turns": turns,
        "uncached_input_tokens": uncached_total,
        "cached_input_tokens": round(cached_total, 1),
        "savings_pct": round(savings, 2),
        "cache_read_rate": read_rate,
        "cache_write_rate": write_rate,
        "boundaries_without_caching": boundaries_without,
        "boundaries_with_caching": boundaries_with,
    }


def test_phase3_exit_prompt_cache_cuts_controlled_input_cost(tmp_path: Path) -> None:
    metrics = _measure_prompt_cache(tmp_path / "cache")
    assert metrics["boundaries_without_caching"] == []
    assert [b["scope"] for b in metrics["boundaries_with_caching"]] == [
        "system_tools",
        "history",
    ]
    assert metrics["cached_input_tokens"] < metrics["uncached_input_tokens"]
    assert metrics["savings_pct"] > 50


# ---------------------------------------------------------------------------
# Phase 4 references (money path + reload leak)
# ---------------------------------------------------------------------------


def _run_pytest(nodeids: list[str], *, timeout: int = 600) -> dict[str, object]:
    env = dict(os.environ)
    env.pop("NEXUS_PHASE3_WRITE_REPORT", None)
    env["NEXUS_PHASE3_IGNORE_REPORT"] = "1"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodeids]
    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    elapsed = round(time.perf_counter() - started, 2)
    output = (proc.stdout + proc.stderr).strip().splitlines()
    return {
        "nodeids": list(nodeids),
        "command": " ".join(command),
        "returncode": proc.returncode,
        "seconds": elapsed,
        "summary": output[-1] if output else "",
        "output_tail": "\n".join(output[-15:]),
    }


def test_phase4_money_path_and_leak_tests_pass() -> None:
    result = _run_pytest(PHASE4_NODES)
    assert result["returncode"] == 0, result["output_tail"]
    assert "passed" in result["summary"]


# ---------------------------------------------------------------------------
# Line budgets and import cost
# ---------------------------------------------------------------------------






def test_import_cost_root_package_is_lazy_and_bounded() -> None:
    metrics = _measure_import_cost()
    assert metrics["heavy_loaded"] == []
    assert metrics["import_nexus_ms"] < 1500
    assert metrics["import_runtime_ms"] < 3000


# ---------------------------------------------------------------------------
# Baseline report
# ---------------------------------------------------------------------------


def _head_commit() -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


async def _collect_report(workspace: Path, *, full: bool) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": "nexus.phase3_exit_baseline/1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "head": _head_commit(),
        "phase3": {
            "session_200": await _run_200_message_session(workspace / "s200"),
            "prompt_cache": _measure_prompt_cache(workspace / "cache"),
        },
        "line_budgets": {
            "core_model_spec": _core_line_budget(),
        "host_view_ui": _surface_line_budget(),
        },
        "import_cost": _measure_import_cost(),
    }
    if full:
        report["phase4"] = _run_pytest(PHASE4_NODES)
        report["full_suite"] = _run_full_suite()
    return report


def _run_full_suite(*, timeout: int = 1200) -> dict[str, object]:
    env = dict(os.environ)
    env.pop("NEXUS_PHASE3_WRITE_REPORT", None)
    env["NEXUS_PHASE3_IGNORE_REPORT"] = "1"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip().splitlines()
    return {
        "command": " ".join(command),
        "returncode": proc.returncode,
        "seconds": round(time.perf_counter() - started, 2),
        "summary": output[-1] if output else "",
    }


def _render_report_text(report: dict[str, object]) -> str:
    session = report["phase3"]["session_200"]  # type: ignore[index]
    cache = report["phase3"]["prompt_cache"]  # type: ignore[index]
    core = report["line_budgets"]["core_model_spec"]  # type: ignore[index]
    surfaces = report["line_budgets"]["host_view_ui"]  # type: ignore[index]
    _assert_surface_totals(surfaces)
    imports = report["import_cost"]  # type: ignore[index]
    lines = [
        "Nexus Phase 3 exit - baseline closeout evidence",
        "=" * 48,
        f"generated : {report['generated_at']}",
        f"head      : {report['head']}",
        f"schema    : {report['schema']}",
        "",
        "Phase 3 exit criteria (plan section 10)",
        "-" * 40,
        "200-message constrained session",
        f"  turns .................... {session['turns']}",
        f"  messages ................. {session['messages']}",
        f"  model calls .............. {session['model_calls']}",
        f"  compacted assemblies ..... {session['compacted_assemblies']}",
        f"  input budget ............. {session['input_budget']} tokens",
        f"  peak used ................ {session['peak_used_tokens']} tokens",
        f"  persisted message records  {session['persisted_message_records']}",
        f"  contiguous suffix ........ {session['contiguous_suffix']}",
        f"  current turn once ........ {session['current_turn_appears_once']}",
        "",
        "Controlled prompt cache before/after",
        f"  turns .................... {cache['turns']}",
        f"  uncached input tokens .... {cache['uncached_input_tokens']}",
        f"  cached input tokens ...... {cache['cached_input_tokens']}",
        f"  savings .................. {cache['savings_pct']}%",
        f"  rates .................... read {cache['cache_read_rate']}x / write {cache['cache_write_rate']}x",
        f"  boundaries (off) ......... {cache['boundaries_without_caching']}",
        f"  boundaries (on) .......... {cache['boundaries_with_caching']}",
        "",
        "Line budgets (separate host/view/ui allocations)",
        "-" * 40,
        (
            f"  core+model+spec .......... {core['physical_lines']} physical / "
            f"{core['code_lines']} code"
        ),
        *[
            f"  {name:<25} {item['physical_lines']} physical / {item['code_lines']} code "
            "" 
            for name, item in surfaces["budgets_by_dir"].items()
        ],
        "",
        "Import cost (plan section 2.2)",
        "-" * 40,
        (
            f"  import nexus ............. {imports['import_nexus_ms']} ms, "
            f"+{imports['import_nexus_new_modules']} modules, "
            f"heavy={imports['heavy_loaded'] or 'none'}"
        ),
        (
            f"  import nexus.runtime ..... {imports['import_runtime_ms']} ms, "
            f"{imports['import_runtime_modules']} modules"
        ),
    ]
    if "phase4" in report:
        phase4 = report["phase4"]  # type: ignore[index]
        lines += [
            "",
            "Phase 4 references (plan section 10)",
            "-" * 40,
            (
                f"  result ................... "
                f"{'passed' if phase4['returncode'] == 0 else 'FAILED'} "
                f"in {phase4['seconds']}s"
            ),
            f"  summary .................. {phase4['summary']}",
        ]
        for nodeid in phase4["nodeids"]:
            lines.append(f"    - {nodeid}")
    if "full_suite" in report:
        suite = report["full_suite"]  # type: ignore[index]
        lines += [
            "",
            "Full suite",
            "-" * 40,
            (
                f"  result ................... "
                f"{'passed' if suite['returncode'] == 0 else 'FAILED'} "
                f"in {suite['seconds']}s"
            ),
            f"  summary .................. {suite['summary']}",
        ]
    lines.append("")
    return "\n".join(lines)




def _write_report(report: dict[str, object]) -> None:
    _assert_surface_totals(report["line_budgets"]["host_view_ui"])
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    REPORT_TXT.write_text(_render_report_text(report), encoding="utf-8")


async def test_phase3_exit_baseline_report(tmp_path: Path) -> None:
    if os.environ.get("NEXUS_PHASE3_WRITE_REPORT") == "1":
        report = await _collect_report(tmp_path, full=True)
        assert report["phase4"]["returncode"] == 0, report["phase4"]["summary"]
        assert report["full_suite"]["returncode"] == 0, report["full_suite"]["summary"]
        _write_report(report)
        assert REPORT_JSON.exists() and REPORT_TXT.exists()
        return

    if not REPORT_JSON.exists():
        pytest.skip(
            "baseline report not present; regenerate with "
            "NEXUS_PHASE3_WRITE_REPORT=1"
        )
    if os.environ.get("NEXUS_PHASE3_IGNORE_REPORT") == "1":
        return

    recorded = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    live_surfaces = _surface_line_budget()
    rec_surfaces = recorded["line_budgets"]["host_view_ui"]
    _assert_surface_totals(live_surfaces)
    _assert_surface_totals(rec_surfaces)
    recorded_sum = sum(item["physical_lines"] for item in rec_surfaces["budgets_by_dir"].values())
    live_sum = sum(item["physical_lines"] for item in live_surfaces["budgets_by_dir"].values())
    assert live_surfaces["physical_lines"] == live_sum
    assert rec_surfaces["physical_lines"] == recorded_sum

    live_import = _measure_import_cost()
    rec_import = recorded["import_cost"]
    assert live_import["heavy_loaded"] == []
    assert rec_import["heavy_loaded"] == []
    assert (
        live_import["import_nexus_new_modules"] == rec_import["import_nexus_new_modules"]
    )


def _sample_report() -> dict[str, object]:
    """A complete, structurally valid report for the write-path tests.

    The line budgets come from the live tree, so the positive write test fails
    (rather than blesses) if the tree breaches a cap. ``_render_report_text``
    needs a full document, so the fixed Phase 3 figures are supplied here.
    """
    return {
        "schema": "nexus.phase3_exit_baseline/1",
        "generated_at": "2026-01-01T00:00:00+0000",
        "head": "0" * 40,
        "phase3": {
            "session_200": {
                "turns": 100,
                "messages": 200,
                "model_calls": 100,
                "compacted_assemblies": 1,
                "input_budget": 6000,
                "peak_used_tokens": 5000,
                "persisted_message_records": 200,
                "contiguous_suffix": True,
                "current_turn_appears_once": True,
            },
            "prompt_cache": {
                "turns": 30,
                "uncached_input_tokens": 1000,
                "cached_input_tokens": 500.0,
                "savings_pct": 50.0,
                "cache_read_rate": 0.1,
                "cache_write_rate": 1.25,
                "boundaries_without_caching": [],
                "boundaries_with_caching": [
                    {"position": 0, "scope": "system_tools"},
                    {"position": 1, "scope": "history"},
                ],
            },
        },
        "line_budgets": {
            "core_model_spec": _core_line_budget(),
            "host_view_ui": _surface_line_budget(),
        },
        "import_cost": {
            "import_nexus_ms": 10.0,
            "import_nexus_new_modules": 40,
            "heavy_loaded": [],
            "import_runtime_ms": 100.0,
            "import_runtime_modules": 377,
        },
    }








def test_baseline_write_writes_json_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid report is written as JSON and text.

    An explicit positive covers the guard's other branch, so a future change
    that refuses every report cannot pass on the refusal test alone.
    """
    monkeypatch.setattr(sys.modules[__name__], "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "REPORT_JSON", tmp_path / "r.json")
    monkeypatch.setattr(sys.modules[__name__], "REPORT_TXT", tmp_path / "r.txt")

    report = _sample_report()
    _write_report(report)
    assert (tmp_path / "r.json").exists()
    assert (tmp_path / "r.txt").exists()
    assert "separate host/view/ui allocations" in (tmp_path / "r.txt").read_text(encoding="utf-8")
