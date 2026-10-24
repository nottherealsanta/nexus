"""Code-mode kernel manifest on compaction (plan code-mode-kernel §3.9) and code-mode guidance (§3.10).

The manifest is part of the summarizer input of a summary that is produced. It holds names,
types and sizes only, is bounded, and announces every clip. An evicted kernel is listed from
metadata without its variables and is never restored to compact.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection, ModelSection
from nexus.context import ContextManager
from nexus.context.compact import SummaryArtifact
from nexus.context.kernel_block import (
    MAX_MANIFEST_CHARS,
    MAX_MANIFEST_VARIABLES,
    KernelBlock,
    render_kernel_manifest,
)
from nexus.context.parts import AssemblyContext, builtin_parts, capture_environment
from nexus.model.message import Message, Text
from nexus.model.request import ToolSchema
from nexus.session.manager import SessionManager
from nexus.tools.codemode.manifest import kernel_blocks


class RecordingSummarizer:
    def __init__(self):
        self.calls = 0
        self.inputs = []

    def summarize(self, messages):
        self.calls += 1
        self.inputs.append(list(messages))
        text = "\n".join(block.text for message in messages for block in message.content)
        return SummaryArtifact(text="SUM\n" + text, source_count=len(messages), tokens=5)


def _config():
    return Config(
        model="scripted/x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/x"),
            context=ContextSection(max_tokens=400, safety_margin_tokens=0, compaction="summarize"),
        ),
    )


def _seed(session, count=8, size=40):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        session.append_message(Message(role=role, content=[Text(text=f"m{index} " * size)]))
    session.append_message(Message(role="user", content=[Text(text="current")]))


def _block(**overrides):
    values = dict(
        kernel_id="k1",
        agent_path="root",
        status="live",
        cells=3,
        attached=("github.search_issues", "read"),
        servers=("github",),
        targets=("self",),
        purpose="triage issues",
        variables=(("issues", "list", 42), ("total", "int", None)),
    )
    values.update(overrides)
    return KernelBlock(**values)


def _assembler(tmp_path, source, summarizer):
    context = ContextManager(tmp_path, config=_config(), summarizer=summarizer, counter=len)
    session = SessionManager(tmp_path, assemble=context).open("s")
    _seed(session)
    assembler = context.for_turn()
    if source is not None:
        assembler._kernel_source = source
    return assembler, session


def _summary_input_text(summarizer) -> str:
    assert summarizer.inputs, "the summarizer was not called"
    return "\n".join(
        block.text for message in summarizer.inputs[-1] for block in message.content
    )


def test_manifest_is_in_the_compaction_input_when_a_kernel_exists(tmp_path):
    summarizer = RecordingSummarizer()
    assembler, session = _assembler(tmp_path, lambda sid: (_block(),), summarizer)

    request = assembler.assemble(session)

    assert summarizer.calls == 1
    text = _summary_input_text(summarizer)
    assert "<code_kernel_manifest>" in text
    assert "Kernel k1 (agent root, live): 3 cells run" in text
    assert "Attached tools (2): github.search_issues, read" in text
    assert "Servers (1): github" in text
    assert "Context targets (1): self" in text
    assert "issues  list  42" in text
    assert "total  int" in text
    assert "code_start re-describes a running kernel" in text
    assert "keep this block in the summary" in text.lower()
    # The summary that is persisted carries the manifest too.
    assert "<code_kernel_manifest>" in request.messages[0].content[0].text


def test_manifest_has_no_values_only_names_types_and_sizes(tmp_path):
    summarizer = RecordingSummarizer()
    # A value that a model could have produced must never appear: rows are (name, type, size) only.
    row = ("secret_payload", "str", 6)
    assembler, session = _assembler(tmp_path, lambda sid: (_block(variables=(row,)),), summarizer)

    assembler.assemble(session)

    text = _summary_input_text(summarizer)
    assert "secret_payload  str  6" in text
    assert "hunter2" not in text


def test_no_manifest_when_no_kernel_exists(tmp_path):
    summarizer = RecordingSummarizer()
    assembler, session = _assembler(tmp_path, lambda sid: (), summarizer)

    assembler.assemble(session)

    assert summarizer.calls == 1
    assert "<code_kernel_manifest>" not in _summary_input_text(summarizer)


def test_no_manifest_source_at_all_leaves_compaction_unchanged(tmp_path):
    summarizer = RecordingSummarizer()
    assembler, session = _assembler(tmp_path, None, summarizer)

    assembler.assemble(session)

    assert summarizer.calls == 1
    assert "<code_kernel_manifest>" not in _summary_input_text(summarizer)


def test_async_kernel_source_is_awaited_before_the_summary_is_built(tmp_path):
    summarizer = RecordingSummarizer()
    seen = []

    async def source(session_id):
        seen.append(session_id)
        return (_block(kernel_id="k-async"),)

    assembler, session = _assembler(tmp_path, source, summarizer)

    async def run():
        result = assembler.assemble(session)
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            result = await result
        return result

    asyncio.run(run())

    assert seen == ["s"]
    assert "Kernel k-async" in _summary_input_text(summarizer)


def test_reused_summary_makes_no_new_manifest_or_model_call(tmp_path):
    summarizer = RecordingSummarizer()
    assembler, session = _assembler(tmp_path, lambda sid: (_block(),), summarizer)
    assembler.assemble(session)
    assert summarizer.calls == 1

    calls = []
    again = ContextManager(tmp_path, config=_config(), summarizer=summarizer, counter=len)
    reopened = SessionManager(tmp_path, assemble=again).open("s")
    second_assembler = again.for_turn()
    second_assembler._kernel_source = lambda sid: calls.append(sid) or (_block(),)
    second_assembler.assemble(reopened)

    assert summarizer.calls == 1
    assert calls == []


def test_manifest_is_bounded_and_announces_every_clip():
    many = tuple((f"var_{index:03d}", "list", index) for index in range(150))
    text = render_kernel_manifest([_block(variables=many)])

    listed = [line for line in text.splitlines() if line.startswith("    var_")]
    assert len(listed) == MAX_MANIFEST_VARIABLES
    assert f"50 more variables not listed (manifest cap {MAX_MANIFEST_VARIABLES}; 150 exist)" in text
    assert "[kernel manifest clipped:" not in text
    assert len(text) <= MAX_MANIFEST_CHARS
    assert text.startswith("<code_kernel_manifest>") and text.endswith("</code_kernel_manifest>")


def test_total_character_cap_is_announced_and_the_block_still_closes():
    long_names = tuple(f"mcp_server.very_long_tool_name_{index:03d}_" + "y" * 60 for index in range(120))
    blocks = [
        _block(kernel_id=f"k{n}", attached=long_names, variables=tuple((f"v{n}_{i}_" + "z" * 75, "dict", 3)
                                                                       for i in range(100)))
        for n in range(3)
    ]
    text = render_kernel_manifest(blocks)

    assert len(text) <= MAX_MANIFEST_CHARS
    assert "[kernel manifest clipped:" in text
    assert text.rstrip().endswith("</code_kernel_manifest>")


def test_names_per_kernel_are_capped_with_a_count():
    attached = tuple(f"tool_{index:03d}" for index in range(60))
    text = render_kernel_manifest([_block(attached=attached, variables=())])

    assert "Attached tools (60): tool_000" in text
    assert "and 20 more (not listed; manifest cap 40)" in text
    assert "Variables: none" in text


def test_evicted_kernel_is_listed_from_metadata_without_restoring_it():
    class Feed:
        def __init__(self):
            self.feeds = 0

        async def feed(self, *args, **kwargs):  # pragma: no cover - must not be reached
            self.feeds += 1
            raise AssertionError("an evicted kernel must not be fed or restored to compact")

    feed = Feed()
    state = SimpleNamespace(
        session_id="s", agent_path="root", kernel_id="k9", n=4, attached={"read": "read"},
        target_info=[], dropped_targets=[], purpose="",
    )

    async def resolve(session_id, agent_path, catalog=None):
        return state

    kernels = SimpleNamespace(
        states=lambda session_id=None: [state],
        log_reader=None,
        resolve=resolve,
        registry=SimpleNamespace(status=lambda sid, agent: "evicted", feed=feed.feed),
    )

    blocks = asyncio.run(kernel_blocks(kernels, "s"))

    assert feed.feeds == 0
    assert len(blocks) == 1
    assert blocks[0].status == "evicted"
    assert blocks[0].variables is None
    text = render_kernel_manifest(blocks)
    assert "variables not listed (kernel evicted)" in text
    assert "Kernel k9 (agent root, evicted): 4 cells run" in text


def _environment_text(tmp_path: Path, tool_names) -> str:
    config = _config()
    schemas = tuple(ToolSchema(name=name, description="d", input_schema={"type": "object"}) for name in tool_names)
    ctx = AssemblyContext(
        workspace=Path(tmp_path),
        config=config,
        identity="",
        soul_text="",
        memory_text="",
        environment=capture_environment(tmp_path),
        tool_schemas=schemas,
        messages=(),
        current_user_index=None,
        capabilities=None,
        model=None,
        provider=None,
        max_file_bytes=1000,
    )
    output = {part.name: part.render(ctx) for part in builtin_parts()}["environment"]
    return output.text if output is not None else ""


def test_code_mode_guidance_appears_for_coding_profile_agents(tmp_path):
    coding = ["read", "edit", "code_start", "code_attach", "code_run", "code_reset"]

    text = _environment_text(tmp_path, coding)

    assert "<code_mode>" in text
    assert "Prefer it for multi-step tool work" in text
    assert "look at slices" in text
    assert "code_start re-describes a kernel" in text


def test_code_mode_guidance_is_absent_without_code_tools(tmp_path):
    text = _environment_text(tmp_path, ["read", "edit"])

    assert "<code_mode>" not in text
    assert "code_start" not in text


def _handoff_config():
    from nexus.config.schema import ConfigV2

    # A 40k-token window: the handoff brief limit (10% of the input budget) is then large enough
    # for the brief and its manifest. The counter scales text so that the eight seeded messages
    # overflow the window and trigger the automatic handoff.
    return Config(
        v2=ConfigV2(
            context=ContextSection(
                max_tokens=40000,
                safety_margin_tokens=0,
                compact_at_fraction=0.3,
                on_overflow="handoff",
                handoff_brief="deterministic",
                compaction="drop_oldest",
            )
        )
    )


def _handoff_session(tmp_path, source):
    from nexus.session import SessionManager as _Sessions

    context = ContextManager(tmp_path, config=_handoff_config(), counter=lambda text: len(text) * 12, identity="")
    session = _Sessions(tmp_path).open("demo")
    for index in range(8):
        session.append_message(
            Message(role="user" if index % 2 == 0 else "assistant",
                    content=[Text(text=f"original-{index} " + "x" * 150)])
        )
    assembler = context.for_turn()
    if source is not None:
        assembler._kernel_source = source
    return assembler, session


def test_handoff_brief_keeps_the_kernel_manifest_under_the_default_overflow_path(tmp_path):
    assembler, session = _handoff_session(tmp_path, lambda sid: (_block(),))

    with session.begin_turn():
        assembler.assemble(session)

    brief = session.latest_summary().text
    assert "Handoff brief v1" in brief
    assert "<code_kernel_manifest>" in brief
    assert "Kernel k1 (agent root, live): 3 cells run" in brief
    assert "issues  list  42" in brief
    assert "code_start re-describes a running kernel" in brief


def test_handoff_brief_has_no_manifest_without_kernels(tmp_path):
    assembler, session = _handoff_session(tmp_path, None)

    with session.begin_turn():
        assembler.assemble(session)

    brief = session.latest_summary().text
    assert "Handoff brief v1" in brief
    assert "<code_kernel_manifest>" not in brief
