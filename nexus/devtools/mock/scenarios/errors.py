"""Provider failures and tool failures: everything must surface, nothing may hang."""
from ..checks import tool_errors
from ..dsl import Scenario, call, calls, verdict

SCENARIO = Scenario(
    name="errors",
    summary="Unknown tool, bad args, missing file, escaped path, absent edit text",
    tags=("errors",),
    est_seconds=8,
    prompt="Trigger every kind of error.",
    actors={
        "main": [
            calls(call("no_such_tool", x=1), text="Calling a tool that does not exist."),
            calls(call("read", path="does/not/exist.txt"), text="Reading a missing file."),
            calls(call("read", path="../../etc/hosts"), text="Trying to escape the workspace."),
            calls(call("edit", path="src/app.py", old_string="THIS TEXT IS NOT THERE", new_string="x"),
                  text="Editing text that is absent."),
            calls(call("read"), text="Calling read with no arguments."),
            verdict("errors", intro="Every failure above was reported back to me as a tool error.", checks=[
                tool_errors("no_such_tool", 1),
                tool_errors("read", 3),
                tool_errors("edit", 1),
            ]),
        ]
    },
)
