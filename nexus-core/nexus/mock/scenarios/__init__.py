"""Built-in offline mock scenarios adapted from old Nexus."""

from .bash_wait import SCENARIO as bash_wait
from .cancel import SCENARIO as cancel
from .context_pressure import SCENARIO as context_pressure
from .hello import SCENARIO as hello
from .parallel_subagents import SCENARIO as parallel_subagents
from .parallel_tools import SCENARIO as parallel_tools
from .provider_failure import SCENARIO as provider_failure
from .question import SCENARIO as question
from .streaming_rich import SCENARIO as streaming_rich
from .stress import SCENARIO as stress
from .tool_marathon import SCENARIO as tool_marathon

CATALOG = {
    s.name: s
    for s in (
        hello,
        tool_marathon,
        bash_wait,
        cancel,
        parallel_tools,
        question,
        streaming_rich,
        stress,
        provider_failure,
        parallel_subagents,
        context_pressure,
    )
}
