"""Single, batched, failing-member and write batches: parallel call states."""
from ..checks import no_unexpected_errors, tool_called, tool_errors
from ..dsl import Scenario, call, calls, verdict

SCENARIO = Scenario(
    name="parallel-tools",
    summary="one lone call, 8 parallel reads, a batch with a failing member, then writes",
    tags=("tools", "parallel"),
    est_seconds=6,
    prompt="Exercise parallel tool calls.",
    actors={
        "main": [
            calls(call("glob", pattern="*.md"), text="One lone call: no gutter."),
            calls(
                call("read", path="README.md"), call("read", path="src/app.py"),
                call("read", path="src/util.py"), call("read", path="src/config.py"),
                call("grep", pattern="TODO"), call("glob", pattern="**/*.txt"),
                call("read", path="src"), call("read", path="notes"),
                text="Reading eight things at once.",
            ),
            calls(
                call("read", path="README.md"), call("read", path="missing/nope.txt"),
                call("grep", pattern="FIXME"),
                text="A batch whose middle member fails.",
            ),
            calls(
                call("write", path="notes/a.txt", content="a\n"),
                call("write", path="notes/b.txt", content="b\n"),
                call("write", path="notes/c.txt", content="c\n"),
                text="Three writes in one message.",
            ),
            calls(call("read", path="notes/a.txt"), call("read", path="notes/b.txt"), call("read", path="notes/c.txt")),
            verdict("parallel-tools", intro="All parallel batches returned in order.", checks=[
                tool_called("read", 11), tool_called("write", 3), tool_called("grep", 2),
                tool_errors("read", 1), no_unexpected_errors(1),
            ]),
        ]
    },
)
