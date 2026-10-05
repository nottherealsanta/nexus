"""Bounded immutable-tool presentation cache (TUI_LOCAL_INTERACTION_PLAN §4)."""
from collections import OrderedDict

from ...ui_support.timeline import tool_heading, tool_row_text, tool_summary
from ...ui_support.tool_details import tool_detail_sections


def tool_presentation(tool, shell):
    cache = getattr(shell, "tool_presentation_cache", None) if shell else None
    if cache is None and shell:
        cache = shell.tool_presentation_cache = OrderedDict()
    prior = cache.get(tool.call_id) if cache is not None else None
    if prior and prior[0] is tool:
        cache.move_to_end(tool.call_id)
        return prior[1]
    value = (tool_detail_sections(tool), tool_row_text(tool, 0), tool_heading(tool), tool_summary(tool))
    if cache is not None:
        size = sum(len(row.label) + len(row.value) for section in value[0] for row in section.rows)
        size += len(tool.display or "") + sum(len(text) for text in value[1:])
        used = getattr(shell, "tool_presentation_bytes", 0) - (prior[2] if prior else 0)
        cache[tool.call_id] = (tool, value, size)
        used += size
        cache.move_to_end(tool.call_id)
        while len(cache) > 1024 or used > 8 * 1024 * 1024:
            _, removed = cache.popitem(last=False)
            used -= removed[2]
        shell.tool_presentation_bytes = used
    return value
