"""What the native client can send back from a Settings page (mirrors rust/tui/src/settings_page/input.rs).

``client_ops(page)`` lists, for every control on the page, the operation the client builds: the
offered operation plus the value, action or index it fills in. The bridge must accept every one
of them (it once dropped all of them, so no Settings control worked) and nothing else.
"""
from nexus.ui_support import settings_page as sp


def _walk(blocks):
    for block in blocks:
        yield block
        if block.get("t") == "section":
            yield from _walk(block["blocks"])


def client_ops(page):
    ops = []
    scope = page.get("scope")
    if scope:
        ops += [{**scope["operation"], "value": i} for i in range(len(scope["options"]))]
    for block in _walk(page["blocks"]):
        kind = block.get("t")
        if kind == "row":
            control = block["control"]
            c, operation = control.get("c"), control.get("operation")
            if c == "toggle":
                ops.append({**operation, "value": not control["on"]})
            elif c == "segmented":
                ops += [{**operation, "value": v} for v in control["values"]]
            elif c == "select":
                ops += [{**operation, "value": v} for _, v in control["options"]]
            elif c == "stepper":
                ops += [{**operation, "value": control["min"]}, {**operation, "value": control["max"]}]
            elif c == "text":
                ops.append({**operation, "value": "typed"})
            elif c == "button":
                ops.append(operation)
        elif kind == "tabs":
            ops += [{**block["operation"], "value": i} for i in range(len(block["items"]))]
        elif kind == "ordered":
            n = len(block["items"])
            ops += [{**block["operation"], "action": a, "index": i} for i in range(n) for a in ("up", "down", "remove")]
            ops.append({**block["operation"], "action": "add", "index": n})
        elif kind == "buttons":
            ops += [item["operation"] for item in block["items"]]
        elif kind == "callout" and block.get("action"):
            ops.append(block["action"]["operation"])
    return ops


def assert_every_client_op_is_accepted(shell):
    page = shell.workflows.settings_page
    assert page is not None
    ops = client_ops(page)
    rejected = [op for op in ops if not sp.accepts(page, op)]
    assert not rejected, f"the bridge would drop {len(rejected)} of {len(ops)} client operations, e.g. {rejected[:3]}"
    return len(ops)
