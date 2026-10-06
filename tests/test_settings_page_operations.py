"""The bridge runs only operations a Settings page offered: the client's completed ones, never forged ones."""
from settings_page_ops import client_ops
from nexus.ui_support import settings_page as sp

OP = sp.op("demo", "k")


def page():
    return sp.page("demo", "Demo", [
        sp.row("t", "Toggle", sp.toggle(True, sp.op("demo", "toggle"))),
        sp.row("s", "Seg", sp.segmented(["Dark", "Light"], 0, sp.op("demo", "seg"), values=["nexus-dark", "nexus-light"])),
        sp.row("d", "Select", sp.select("auto", [("auto", "auto"), ("cpu", 7)], sp.op("demo", "sel"))),
        sp.row("n", "Step", sp.stepper(60, sp.op("demo", "step"), minimum=10, maximum=300, step=10)),
        sp.row("x", "Text", sp.text("", sp.op("demo", "text"), secret=True)),
        sp.row("b", "Button", sp.button("Go", sp.op("demo", "go"))),
        sp.tabs("tabs", ["Low", "High"], 0, sp.op("demo", "tab")),
        sp.ordered("chain", [("a/b", "", ""), ("c/d", "", "")], sp.op("demo", "chain")),
        sp.buttons("bs", [("Edit", {"kind": "settings_read", "id": "build"}, "primary")]),
        sp.section("sec", "Sec", [sp.row("n2", "Inner", sp.toggle(False, sp.op("demo", "inner")))]),
        sp.callout("warning", "Careful", {"label": "Fix", "operation": sp.op("demo", "fix")}),
    ], scope={"options": ["Global", "Project"], "value": 0, "operation": sp.op("demo", "scope")})


def test_every_operation_the_client_builds_from_this_page_is_accepted():
    p = page()
    ops = client_ops(p)
    assert len(ops) > 20
    assert all(sp.accepts(p, op) for op in ops), [op for op in ops if not sp.accepts(p, op)]


def test_forged_or_malformed_operations_are_refused():
    p = page()
    forged = [
        {**sp.op("demo", "toggle"), "value": "yes"},                      # not a boolean
        {**sp.op("demo", "seg"), "value": "neon"},                        # not an offered option
        {**sp.op("demo", "sel"), "value": "metal"},
        {**sp.op("demo", "step"), "value": 9999},                          # outside the range
        {**sp.op("demo", "step"), "value": True},                          # a bool is not a number here
        {**sp.op("demo", "text"), "value": "x" * 401},
        {**sp.op("demo", "tab"), "value": 2},                              # no such tab
        {**sp.op("demo", "tab"), "value": -1},
        {**sp.op("demo", "chain"), "action": "explode", "index": 0},
        {**sp.op("demo", "chain"), "action": "up", "index": 99},
        {**sp.op("demo", "scope"), "value": 2},
        {**sp.op("demo", "unknown"), "value": True},                        # never offered
        {**sp.op("other", "toggle"), "value": True},                        # another area's operation
        {"kind": "settings_delete", "id": "build"},                        # a destructive kind that was not offered
        {**sp.op("demo", "toggle"), "extra": 1, "value": True},            # offered base plus an unexpected field
        {**sp.op("demo", "go"), "value": 1},                               # a button takes no value
    ]
    assert not any(sp.accepts(p, op) for op in forged), [op for op in forged if sp.accepts(p, op)]
    assert not sp.accepts(None, {**sp.op("demo", "toggle"), "value": True}), "no page open: nothing is accepted"
    assert not sp.accepts(p, None) and not sp.accepts(p, "kind")


def test_plain_offered_operations_still_match_exactly():
    p = page()
    assert sp.accepts(p, sp.op("demo", "go"))
    assert sp.accepts(p, {"kind": "settings_read", "id": "build"})
    assert not sp.accepts(p, {"kind": "settings_read", "id": "task"}), "a button is exact, not a pattern"
