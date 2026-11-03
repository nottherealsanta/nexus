"""A scripted Manager for tests, screenshots and client development (`[manager] model = "mock/manager_demo"`).

It reads the user's words from the `<user_said>` block of each turn and answers with real Manager tool calls, so
every card, navigation and `via Manager` label is produced by the same code path a real model would drive. It then
speaks a short reply built from the tool results, saying plainly when a tool failed.

Say (typed or spoken text; case does not matter):

| words contain | does |
| --- | --- |
| `going on`, `needs me`, `board` | `overview`, then `show` a board card |
| `list <words>`, `find <words>` | `find_sessions`, then `show` a sessions card of the matches |
| `diagram` | `show` a small flowchart card |
| `note` | `show` a note card |
| `excerpt <handle>` | `show` an excerpt of that session's last answer |
| `approval` | `session_status` of the first session that needs the user, then `show` its approval card |
| `question` | the same for a waiting question |
| `open the first/second/third one` (or `open 2`) | `navigate` to that row of the card on the stage |
| `open <handle>` | `navigate` to that session |
| `go back` | `navigate` back |
| `what am i looking at` | `current_view` |
| `clear`, `close` | `clear_stage` |
| `yes`, `approve` | `decide_approval` (allow) for the approval last shown; `no`, `deny` denies |
| `start <task> in <project>` | `start_session` |
| `tell <handle> <text>` | `send_to_session` |
| `stop <handle>` | `interrupt_session` |
| anything else | a short spoken line |
"""
from __future__ import annotations

import re

from ..dsl import Ctx, Scenario, Turn, call, calls, dyn, say

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
_SAID = re.compile(r"<user_said[^>]*>\n?(.*?)\n?</user_said>", re.DOTALL)
_HANDLE = r"([a-z0-9][a-z0-9-]*)"


def _words(ctx: Ctx) -> str:
    text = ctx.last_user_text
    match = _SAID.search(text)
    return (match.group(1) if match else text).strip().lower()


def _results_text(ctx: Ctx) -> str:
    return "\n".join(r.text for r in ctx.all_results)


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _handles(text: str) -> list[str]:
    return re.findall(r"^\s*(?:\d+\.\s*)?" + _HANDLE + r": ", text, flags=re.MULTILINE)


def _reply(ctx: Ctx) -> Turn:
    """Speak from what the tools said. A failure is reported as one."""
    results = ctx.last_results
    failed = [r for r in results if r.is_error]
    if failed:
        return say("I could not do that. " + failed[0].text.split(". ")[0].splitlines()[0][:160].rstrip(".") + ".")
    names = [r.name for r in results]
    if "decide_approval" in names:
        return say(next(r.text for r in results if r.name == "decide_approval").splitlines()[0])
    if "start_session" in names or "send_to_session" in names or "interrupt_session" in names:
        return say(results[0].text.splitlines()[0])
    if "navigate" in names:
        return say("Opened it.")
    if "current_view" in names:
        return say(results[0].text.splitlines()[0])
    if "show" in names:
        return say("I put it on the screen.")
    if "clear_stage" in names:
        return say("Closed.")
    if "find_sessions" in names:
        text = results[0].text
        if "AMBIGUOUS" in text:
            return say("I found a few close matches. " + text.split("Ask the user:")[-1].strip().splitlines()[0])
        return say(text.splitlines()[0] + " I put them on the screen." if "show" in names else text.splitlines()[0])
    return say("Done.")


def _decide(ctx: Ctx) -> Turn:
    if ctx.messages[-1].role == "tool":
        # After an overview/find/status, a card comes next; after the card, speak.
        names = [r.name for r in ctx.last_results]
        words = _words(ctx)
        if names == ["overview"] and re.search(r"approval|question", words):
            found = re.findall(r"(approval|question) (?:per|qst)_\w+ for " + _HANDLE, ctx.last_results[0].text)
            if found:
                return calls(calls_item("session_status", handle=found[-1][1]))
            return say("Nothing is waiting on you.")
        if names and names[-1] == "overview":
            return calls(calls_item("show", type="board"))
        if names == ["find_sessions"] and "show" not in names:
            handles = _handles(ctx.last_results[0].text.split("matches:")[-1])
            if handles and "AMBIGUOUS" not in ctx.last_results[0].text:
                return calls(calls_item("show", type="sessions", handles=handles[:8]))
        if names == ["session_status"]:
            approval = _first(r"approval (per_\w+) for", ctx.last_results[0].text)
            question = _first(r"question (qst_\w+) for", ctx.last_results[0].text)
            if approval:
                return calls(calls_item("show", type="approval", approval=approval))
            if question:
                return calls(calls_item("show", type="question", question=question))
        return _reply(ctx)

    words = _words(ctx)
    text = _results_text(ctx)
    if re.search(r"going on|needs me|board|status", words):
        return calls(calls_item("overview"))
    if m := re.search(r"(?:list|find|search for)\s+(.+)", words):
        return calls(calls_item("find_sessions", query=m.group(1)))
    if "diagram" in words:
        return calls(calls_item("show", type="diagram",
                                source="flowchart TD\n  Client --> Core\n  Core --> Store\n  Core --> Provider"))
    if re.search(r"\bnote\b", words):
        return calls(calls_item("show", type="note", markdown="**Next steps**\n\n- Review the migration\n- Run the tests"))
    if m := re.search(r"excerpt\s+" + _HANDLE, words):
        return calls(calls_item("show", type="excerpt", handle=m.group(1)))
    if re.search(r"approval|question", words):
        return calls(calls_item("overview"))
    if "go back" in words or words.strip(" .!") == "back":
        return calls(calls_item("navigate", back=True))
    if m := re.search(r"open (?:the )?(first|second|third|fourth|fifth|[1-5])(?: one)?\b", words):
        return calls(calls_item("navigate", row=_ORDINALS[m.group(1)]))
    if m := re.search(r"open\s+" + _HANDLE, words):
        return calls(calls_item("navigate", handle=m.group(1)))
    if "looking at" in words:
        return calls(calls_item("current_view"))
    if re.search(r"\b(clear|close)\b", words):
        return calls(calls_item("clear_stage"))
    if re.search(r"\b(yes|yeah|approve|allow|go ahead)\b", words) or re.search(r"\b(no|deny|don't)\b", words):
        allow = not re.search(r"\b(no|deny|don't)\b", words)
        found = re.findall(r"approval (per_\w+) for " + _HANDLE, text)
        if found:
            approval, handle = found[-1]
            return calls(calls_item("decide_approval", handle=handle, approval=approval, allow=allow))
        return say("There is nothing waiting that I have shown you.")
    if m := re.search(r"start\s+(.+?)\s+in\s+(\S+)", words):
        return calls(calls_item("start_session", project=m.group(2), prompt=m.group(1)))
    if m := re.search(r"tell\s+" + _HANDLE + r"\s+(.+)", words):
        return calls(calls_item("send_to_session", handle=m.group(1), text=m.group(2)))
    if m := re.search(r"stop\s+" + _HANDLE, words):
        return calls(calls_item("interrupt_session", handle=m.group(1)))
    return say("Okay. Ask me what's going on, or to find a session.")


calls_item = call


SCENARIO = Scenario(
    "manager_demo",
    "A scripted Manager that drives real Manager tools from plain phrases (see this module's docstring)",
    {"main": [dyn(_decide)] * 400},
    prompt="what's going on?",
)
