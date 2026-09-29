"""One assistant message with many parallel read-only calls, then mixed writes."""
from ..checks import no_unexpected_errors, tool_called
from ..dsl import Scenario, call, calls, verdict

SCENARIO = Scenario(
    name="parallel-tools",
    summary="8 parallel read-only calls in one message, then serial and parallel writes",
    tags=("tools", "parallel"),
    est_seconds=6,
    prompt="Exercise parallel tool calls.",
    actors={
        "main": [
            calls(
                call("read", path="README.md"), call("read", path="src/app.py"),
                call("read", path="src/util.py"), call("read", path="src/config.py"),
                call("grep", pattern="TODO"), call("glob", pattern="**/*.txt"),
                call("read", path="src"), call("read", path="notes"),
                text="Reading eight things at once.",
            ),
            calls(
                call("write", path="notes/a.txt", content="a\n"),
                call("write", path="notes/b.txt", content="b\n"),
                call("write", path="notes/c.txt", content="c\n"),
                text="Three writes in one message.",
            ),
            calls(call("read", path="notes/a.txt"), call("read", path="notes/b.txt"), call("read", path="notes/c.txt")),
            verdict("parallel-tools", intro="All parallel batches returned in order.", checks=[
                tool_called("read", 9), tool_called("write", 3), tool_called("grep", 1),
                no_unexpected_errors(0),
            ]),
        ]
    },
)
