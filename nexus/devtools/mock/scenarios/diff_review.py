"""Edits that produce a real git diff in the sandbox repo."""
from ..checks import no_unexpected_errors, tool_called, tool_result_contains
from ..dsl import Scenario, call, calls, verdict

SCENARIO = Scenario(
    name="diff-review",
    summary="Edits several files, then shows git diff so /diff and /review have content",
    tags=("git", "diff"),
    est_seconds=8,
    prompt="Make a few changes I can review.",
    actors={
        "main": [
            calls(call("edit", path="src/app.py", old_string='return f"hello, {name}"', new_string='return f"Hello, {name}!"')),
            calls(call("write", path="src/newmod.py", content='"""New module."""\n\n\ndef ping() -> str:\n    return "pong"\n')),
            calls(call("edit", path="src/util.py", old_string="max(low, min(high, value))", new_string="min(high, max(low, value))")),
            calls(call("bash", command="git diff --stat && git status --short")),
            verdict("diff-review", intro="Changes are in the working tree; try /diff.", checks=[tool_called("edit", 2), tool_called("write", 1),
                                    tool_result_contains("bash", "src/app.py"), no_unexpected_errors(0)]),
        ]
    },
)
