"""Nested-call pipeline for code mode (plan 03 sections 3.2 and 3.5, test 6).

A fake ``drive`` tool plays the code tool: it calls ``ctx.invoke_tool`` /
``ctx.authorize_nested`` directly, so Monty is not needed. The real
``ToolManager``, dispatcher adapter, permission gate adapter and loop run.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nexus.config import Config
from nexus.config.schema import CodeModeSection, ConfigV2, ModelSection
from nexus.core.cancel import CancelToken
from nexus.core.loop import ResolvedModel, run_turn
from nexus.core.turn import TurnState
from nexus.events import Event
from nexus.model.message import Message
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.model.request import ModelRequest
from nexus.runtime import _PermissionGateAdapter, _ToolDispatcherAdapter
from nexus.tools.manager import ToolManager
from nexus.tools.permissions import Decision, PermissionEngine
from nexus.tools.spec import RegisteredTool, ToolExecutionResult, ToolSpec

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False}


class Lease:
    def __init__(self):
        self.turn_id = "t1"
        self.state = TurnState.new(turn_id="t1", session_id="s1").start()
        self.cancel_token = CancelToken()
        self.limits = None

    def release(self):
        pass


class Session:
    id = "s1"

    def __init__(self):
        self.messages: list[Message] = []

    def append_message(self, message, *, seq=None):
        self.messages.append(message)
        return SimpleNamespace(seq=len(self.messages), message=message)

    def begin_turn(self, *, turn_id=None, limits=None):
        return Lease()


class Assembler:
    def assemble(self, session):
        return ModelRequest(messages=list(session.messages))


class Hooks:
    """Records every hook call; blocks tools named in ``block``."""

    def __init__(self, block=()):
        self.block = set(block)
        self.pre: list[dict] = []
        self.post: list[dict] = []

    async def pre_tool_use(self, **kw):
        self.pre.append(kw)
        blocked = kw["tool"] in self.block
        return SimpleNamespace(
            blocked=blocked, reason="nope", modified=False, modified_input=None,
            to_dict=lambda: {},
        )

    async def post_tool_use(self, **kw):
        self.post.append(kw)
        return SimpleNamespace(blocked=False, to_dict=lambda: {})


class Harness:
    def __init__(self, tmp_path, *, deny=(), allow=(), ask=(), attended=True, hooks=None,
                 profile="coding", max_nested=64, hide=(), bundle="ext", on_request=None):
        self.events: list[Event] = []
        self.outcomes: list = []
        self.runs: list[str] = []
        self.active = 0
        self.max_active = 0
        self.poke_active: list[int] = []
        self.hooks = hooks
        self.on_request = on_request
        harness = self

        async def peek(args, ctx):
            harness.runs.append("peek")
            harness.active += 1
            harness.max_active = max(harness.max_active, harness.active)
            await asyncio.sleep(0.05)
            harness.active -= 1
            return ToolExecutionResult.text("peeked")

        async def poke(args, ctx):
            harness.runs.append("poke")
            harness.active += 1
            harness.poke_active.append(harness.active)
            await asyncio.sleep(0.05)
            harness.active -= 1
            return ToolExecutionResult.text("poked")

        async def drive(args, ctx):
            await harness.script(ctx)
            return ToolExecutionResult.text("driven")

        def tool(name, fn, mutates=False, spec_bundle=bundle):
            return RegisteredTool(
                spec=ToolSpec(name=name, description=f"{name} test tool", input_schema=SCHEMA,
                              bundle=spec_bundle, mutates=mutates),
                run=fn,
            )

        tools = [tool("drive", drive, spec_bundle="fs" if profile == "research" else "code"),
                 tool("peek", peek), tool("poke", poke, mutates=True),
                 tool("hidden", peek)]
        if profile == "research":
            tools = [tool(t.name, t.run, t.spec.mutates, "fs") for t in tools]
        config = Config(
            model="scripted/sm", version=2,
            v2=ConfigV2(model=ModelSection(default="scripted/sm"),
                        code_mode=CodeModeSection(max_nested_calls=max_nested)),
        )
        self.manager = ToolManager(
            config, workspace=tmp_path, profile=profile, tools=tools,
            restrict=[t.name for t in tools if t.name not in hide],
        )
        engine = PermissionEngine(
            workspace=tmp_path, allow=["drive", "peek", *allow], deny=list(deny), ask=list(ask),
        )
        self.gate = _PermissionGateAdapter(engine, manager=self.manager, attended=attended)
        self.dispatcher = _ToolDispatcherAdapter(
            self.manager, workspace=tmp_path, session_id="s1", turn_id="t1", config=config,
        )
        self.session = Session()

    def sink(self, event: Event):
        self.events.append(event)
        if event.type == "permission.requested" and self.on_request:
            self.on_request(self, event.data)
        return SimpleNamespace(seq=len(self.events))

    async def run(self, script, *, then=()):
        self.script = script
        provider = ScriptedProvider(
            tool_response(("c1", "drive", {})), *then, text_response("done"),
        )
        caps = provider.capabilities("m")
        return await run_turn(
            session=self.session, user_input="go", assemble=Assembler(),
            provider_for=lambda request: ResolvedModel(provider, "m", caps),
            emit=self.sink, tools=self.dispatcher, gate=self.gate, lease=Lease(),
            hooks=self.hooks,
        )

    def of(self, kind):
        return [e.data for e in self.events if e.type == kind]


async def call(ctx, name, args=None, n=1):
    outcome = await ctx.invoke_tool(name, args or {}, parent_call_id=ctx.call_id,
                                    nested_id=f"{ctx.call_id}.{n}")
    return outcome


async def test_hooks_prepare_permission_dispatch_once_and_parent_call_id(tmp_path):
    hooks = Hooks()
    h = Harness(tmp_path, hooks=hooks)

    async def script(ctx):
        h.outcomes.append(await call(ctx, "peek", {"q": "x"}))

    outcome = await h.run(script)
    assert outcome.ok
    assert h.runs == ["peek"]
    got = h.outcomes[0]
    assert got.status == "ok" and got.result.content[0].text == "peeked"
    assert got.permission == {"decision": "allow", "rule": "peek"}
    nested_pre = [p for p in hooks.pre if p.get("parent_call_id")]
    nested_post = [p for p in hooks.post if p.get("parent_call_id")]
    assert [p["tool"] for p in nested_pre] == ["peek"]
    assert nested_pre[0]["parent_call_id"] == "c1"
    assert [p["tool"] for p in nested_post] == ["peek"]
    assert not any(e.type.startswith("tool.") and e.data.get("call_id") == "c1.1" for e in h.events)


async def test_deny_rule_blocks_with_the_direct_call_text(tmp_path):
    h = Harness(tmp_path, deny=["poke"])

    async def script(ctx):
        h.outcomes.append(await call(ctx, "poke"))

    await h.run(script, then=[tool_response(("d1", "poke", {}))])
    got = h.outcomes[0]
    assert got.status == "denied" and h.runs == []
    assert got.permission["decision"] == "deny" and got.permission["rule"] == "poke"
    direct = next(m for m in h.session.messages
                  if m.role == "user" and getattr(m.content[0], "tool_use_id", "") == "d1")
    assert got.result.content[0].text == direct.content[0].content[0].text


async def test_hook_block_is_denied_with_direct_text(tmp_path):
    h = Harness(tmp_path, hooks=Hooks(block={"peek"}))

    async def script(ctx):
        h.outcomes.append(await call(ctx, "peek"))

    await h.run(script)
    got = h.outcomes[0]
    assert got.status == "denied" and h.runs == []
    assert got.result.content[0].text == "peek: blocked by PreToolUse hook: nope"


async def test_unknown_invalid_and_disabled_tools_are_errors_not_raises(tmp_path):
    h = Harness(tmp_path, hide=("hidden",))

    async def script(ctx):
        h.outcomes.append(await call(ctx, "nope", n=1))
        h.outcomes.append(await call(ctx, "peek", {"bad": 1}, n=2))
        h.outcomes.append(await call(ctx, "hidden", n=3))

    assert (await h.run(script)).ok
    assert [o.status for o in h.outcomes] == ["error", "error", "error"]
    assert h.runs == []


async def test_ask_suspends_until_permission_resolve(tmp_path):
    state = {}

    def on_request(harness, data):
        state["before"] = list(harness.runs)
        state["data"] = data
        asyncio.get_running_loop().call_later(
            0.05, harness.gate.resolve, data["id"], Decision.ALLOW_ONCE
        )

    h = Harness(tmp_path, on_request=on_request)

    async def script(ctx):
        h.outcomes.append(await call(ctx, "poke"))

    await h.run(script)
    assert state["before"] == [] and h.runs == ["poke"]
    assert state["data"]["parent_call_id"] == "c1" and "parent_program" in state["data"]
    assert h.outcomes[0].status == "ok"
    assert h.outcomes[0].permission["decision"] == "ask:allow"
    assert h.of("permission.resolved")[0]["parent_call_id"] == "c1"


async def test_ask_denied_is_denied(tmp_path):
    h = Harness(tmp_path, on_request=lambda hh, d: hh.gate.resolve(d["id"], Decision.DENY_ONCE))

    async def script(ctx):
        h.outcomes.append(await call(ctx, "poke"))

    await h.run(script)
    assert h.outcomes[0].status == "denied" and h.runs == []
    assert h.outcomes[0].permission["decision"] == "ask:deny"


async def test_research_profile_refuses_mutating_nested_call(tmp_path):
    h = Harness(tmp_path, profile="research")

    async def script(ctx):
        h.outcomes.append(await call(ctx, "poke"))
        h.outcomes.append(await call(ctx, "peek", n=2))

    await h.run(script)
    assert h.outcomes[0].status == "denied" and h.runs == ["peek"]
    assert "research profile refuses" in h.outcomes[0].result.content[0].text


async def test_reads_overlap_and_mutations_never_do(tmp_path):
    h = Harness(tmp_path, allow=["poke"])

    async def script(ctx):
        await asyncio.gather(*(call(ctx, "peek", n=i) for i in range(1, 4)))
        assert h.max_active >= 2
        await asyncio.gather(
            call(ctx, "peek", n=4), call(ctx, "poke", n=5), call(ctx, "peek", n=6),
            call(ctx, "poke", n=7),
        )

    assert (await h.run(script)).ok
    assert h.max_active >= 2
    assert h.poke_active == [1, 1]


async def test_max_nested_calls_enforced(tmp_path):
    h = Harness(tmp_path, max_nested=2)

    async def script(ctx):
        for n in range(1, 4):
            h.outcomes.append(await call(ctx, "peek", n=n))

    await h.run(script)
    assert [o.status for o in h.outcomes] == ["ok", "ok", "error"]
    assert "limit: calls" in h.outcomes[2].result.content[0].text
    assert h.runs == ["peek", "peek"]


async def test_authorize_nested_context_query(tmp_path):
    def on_request(harness, data):
        harness.gate.resolve(data["id"], Decision.ALLOW_ONCE)

    h = Harness(tmp_path, deny=["ContextQuery(bad-*)"], allow=["ContextQuery(good-*)"],
                on_request=on_request)

    async def script(ctx):
        for key in ("ContextQuery(quiet-otter)", "ContextQuery(bad-one)", "ContextQuery(good-one)"):
            h.outcomes.append(
                await ctx.authorize_nested(key, parent_call_id=ctx.call_id, summary={"op": "meta"})
            )

    assert (await h.run(script)).ok
    asked, denied, allowed = h.outcomes
    assert (asked.allowed, asked.decision) == (True, "ask:allow")
    assert (denied.allowed, denied.decision, denied.rule) == (False, "deny", "ContextQuery(bad-*)")
    assert denied.message
    assert (allowed.allowed, allowed.decision, allowed.rule) == (True, "allow", "ContextQuery(good-*)")
    requested = h.of("permission.requested")
    assert len(requested) == 1 and requested[0]["parent_call_id"] == "c1"
    assert requested[0]["parent_program"] == {} or "parent_program" in requested[0]
    assert h.of("permission.resolved")[0]["parent_call_id"] == "c1"


async def test_authorize_nested_unattended_default_denies_and_unknown_key_fails_closed(tmp_path):
    h = Harness(tmp_path, attended=False)

    async def script(ctx):
        h.outcomes.append(await ctx.authorize_nested(
            "ContextQuery(x)", parent_call_id=ctx.call_id, summary={}))
        h.outcomes.append(await ctx.authorize_nested(
            "Bash(x)", parent_call_id=ctx.call_id, summary={}))

    await h.run(script)
    assert [o.allowed for o in h.outcomes] == [False, False]
    assert h.of("permission.requested") == []


def test_context_query_mode_allow_still_asks_unless_rule_or_grant(tmp_path):
    from nexus.tools.permissions import Grant, Outcome, synthetic_call

    def verdict(*, grants=(), **rules):
        engine = PermissionEngine(mode="allow", workspace=tmp_path, **rules)
        c, spec = synthetic_call("ContextQuery(quiet-otter)", "c1")
        return engine.evaluate(c, spec, grants=grants)

    assert verdict().outcome is Outcome.ASK
    assert verdict(allow=["ContextQuery(*)"]).outcome is Outcome.ALLOW
    assert verdict(deny=["ContextQuery(*)"]).outcome is Outcome.DENY
    assert verdict(deny=["ContextQuery(*)"], allow=["ContextQuery(*)"]).outcome is Outcome.DENY
    assert verdict(grants=[Grant(effect="allow", rule="ContextQuery(quiet-otter)")]).outcome is Outcome.ALLOW
    # Unattended: the unattended policy applies, never a mode allow.
    engine = PermissionEngine(mode="allow", workspace=tmp_path)
    c, spec = synthetic_call("ContextQuery(x)", "c2")
    assert engine.evaluate(c, spec, attended=False).outcome is not Outcome.ALLOW
