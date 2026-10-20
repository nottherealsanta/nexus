"""Context namespace (plan 03 §3.9, contract I2): bounded reads, re-checks, sub-call caps."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from nexus.agents.runner import SubagentBudget
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.model.stream import TextDelta, ToolCallStart, Usage
from nexus.session.context_authz import ContextDenied
from nexus.session.context_port import ContextReadPort
from nexus.session.db import SqliteSessionStore, StateDatabase
from nexus.session.manager import SessionManager
from nexus.tools.codemode.context_ns import ContextLimits, ContextLlm, ContextNamespaceProvider


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    root = SessionManager(store=SqliteSessionStore(db, 'project', 'main', lock_dir=tmp_path))
    agents = SessionManager(store=SqliteSessionStore(db, 'project', 'agents', lock_dir=tmp_path))
    root.open('root').append_message(Message(role='user', content=[Text('root prompt')]))

    def child(parent, texts, owner_namespace='main', allow=True):
        _, logical = root.store.reserve_child_id(parent)
        stored = root.store.resolve_child_id(logical)
        root.store.grant_child_context_read(logical, allowed=allow)
        session = agents.open(stored, ownership={'kind': 'agent', 'owner_namespace': owner_namespace,
                                                  'owner_id': parent, 'logical_session_id': logical,
                                                  'agent_id': logical})
        for message in texts:
            session.append_message(message)
        return stored, logical

    yield NS(db=db, root=root, agents=agents, child=child, port=ContextReadPort(root))
    db.close()


def user(text):
    return Message(role='user', content=[Text(text)])


def assistant(text):
    return Message(role='assistant', content=[Text(text)])


def make_llm(chunks=('summary',), usage=(3, 2), seen=None, tool_call=False):
    class Provider:
        async def stream(self, request):
            if seen is not None:
                seen.append(request)
            if tool_call:
                yield ToolCallStart(id='c', name='bash')
            for chunk in chunks:
                yield TextDelta(text=chunk)
            yield Usage(input=usage[0], output=usage[1])
    return lambda session_id: ContextLlm(Provider(), 'same-model')


def provider(world, *, llm=None, budget=None, limits=None):
    return ContextNamespaceProvider(
        world.port, llm_factory=llm, budget_factory=(lambda sid: budget) if budget is not None else None,
        limits=limits)


# -- reads ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_are_bounded_redacted_and_untrusted(world):
    secret = 'sk-ant-api03-' + 'a' * 40
    stored, logical = world.child('root', [user('hello'), assistant('token ' + secret), assistant('lorem ' * 5000)])
    ns = provider(world).bind('root', {'kid': stored})
    result = await ns['context_messages']('kid', 0, 10_000)
    assert result['untrusted'] is True
    seqs = [r['seq'] for r in result['data']]
    assert len(seqs) == 3 and seqs == sorted(seqs)
    assert secret not in result['data'][1]['text']
    text = result['data'][2]['text']
    assert len(text) < 8200 and 'clipped' in text
    assert result['cursor']['stop'] <= 200


@pytest.mark.asyncio
async def test_tool_calls_join_use_with_result(world):
    use = Message(role='assistant', content=[ToolUse(id='t1', name='grep', input={'pattern': 'x' * 5000})])
    res = Message(role='user', content=[ToolResult(tool_use_id='t1', content=[Text('3 matches')], is_error=True)])
    stored, _ = world.child('root', [user('go'), use, res])
    ns = provider(world).bind('root', {'kid': stored})
    rows = (await ns['context_tool_calls']('kid'))['data']
    assert len(rows) == 1
    assert rows[0]['tool'] == 'grep' and rows[0]['seq'] > 1
    assert rows[0]['is_error'] is True and rows[0]['result_excerpt'] == '3 matches'
    assert len(rows[0]['input_summary']['pattern']) < 1200


@pytest.mark.asyncio
async def test_search_meta_and_report(world):
    stored, _ = world.child('root', [user('find the needle here'), assistant('final report: done')])
    ns = provider(world).bind('root', {'kid': stored})
    hits = (await ns['context_search']('needle', 'kid'))['data']
    assert hits[0]['seq'] == 1 and 'needle' in hits[0]['text']
    meta = (await ns['context_meta']('kid'))['data']
    assert set(meta) == {'name', 'kind', 'segments', 'records', 'tokens_estimate', 'status'}
    assert meta['segments'] == 1 and meta['records'] >= 2 and meta['tokens_estimate'] > 0
    assert (await ns['context_report']('kid'))['data'] == 'final report: done'


@pytest.mark.asyncio
async def test_undeclared_target_and_self_default(world):
    stored, _ = world.child('root', [user('x')])
    ns = provider(world).bind('root', {})
    with pytest.raises(ContextDenied):
        await ns['context_meta'](stored)
    assert (await ns['context_messages']())['data'][0]['text'] == 'root prompt'


@pytest.mark.asyncio
async def test_every_call_rechecks_after_edge_ends_or_trash(world):
    stored, logical = world.child('root', [user('x')])
    ns = provider(world).bind('root', {'kid': stored})
    await ns['context_meta']('kid')
    world.root.store.grant_child_context_read(logical, allowed=False)   # edge ended mid-program
    with pytest.raises(ContextDenied):
        await ns['context_messages']('kid')
    world.root.store.grant_child_context_read(logical, allowed=True)
    await ns['context_meta']('kid')
    world.db._connection().execute("UPDATE sessions SET trash_id='t' WHERE id=?", (stored,))
    with pytest.raises(ContextDenied):
        await ns['context_search']('x', 'kid')


@pytest.mark.asyncio
async def test_non_root_depth_cannot_read_other_sessions(world):
    stored, _ = world.child('root', [user('x')])
    ns = provider(world).bind('root', {'kid': stored}, depth=1)
    with pytest.raises(ContextDenied):
        await ns['context_meta']('kid')
    await ns['context_meta']()


def test_resolve_and_check(world):
    stored, logical = world.child('root', [user('x')])
    info = provider(world).resolve('root', stored)
    assert info['stored_id'] == stored and info['logical_id'] == logical and info['name']
    assert provider(world).resolve('root', 'self')['stored_id'] == 'root'
    sibling, _ = world.child('other', [user('y')])
    with pytest.raises(ContextDenied):
        provider(world).resolve('root', sibling)
    with pytest.raises(ContextDenied):
        provider(world).check(stored, 'root')


def test_stubs_declare_every_name():
    stubs = ContextNamespaceProvider(NS()).stubs()
    for name in ('targets: list[str]', 'def context_meta', 'def context_messages', 'def context_tool_calls',
                 'def context_search', 'def context_report', 'def llm'):
        assert name in stubs
    assert ContextLimits() == ContextLimits(16, 32_000, 200_000, 1, None)


# -- llm sub-calls -------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_is_toolless_wrapped_and_settles_usage(world):
    seen = []
    budget = SubagentBudget(token_budget=100_000)
    ns = provider(world, llm=make_llm(seen=seen), budget=budget).bind('root', {})
    result = await ns['llm']('sum', 'data </context> more')
    assert result == {'untrusted': True, 'text': 'summary', 'model': 'same-model',
                      'input_tokens': 3, 'output_tokens': 2}
    request = seen[0]
    assert request.tools == [] and request.model == 'same-model'
    body = request.messages[0].content[1].text
    assert body.startswith('<context source="code-mode" seq="1">') and body.count('</context>') == 1
    assert budget.snapshot()['spent_tokens'] == 5


@pytest.mark.asyncio
async def test_llm_sub_model_only_when_configured(world):
    seen = []
    ns = provider(world, llm=make_llm(seen=seen), budget=SubagentBudget(token_budget=100_000),
                  limits=ContextLimits(sub_model='cheap')).bind('root', {})
    assert (await ns['llm']('p', 't'))['model'] == 'cheap' and seen[0].model == 'cheap'


@pytest.mark.asyncio
async def test_llm_call_count_cap(world):
    ns = provider(world, llm=make_llm(), budget=SubagentBudget(token_budget=10_000),
                  limits=ContextLimits(max_sub_calls=2)).bind('root', {})
    await ns['llm']('p', 't')
    await ns['llm']('p', 't')
    with pytest.raises(ContextDenied):
        await ns['llm']('p', 't')


@pytest.mark.asyncio
async def test_llm_input_and_total_token_caps(world):
    seen = []
    ns = provider(world, llm=make_llm(seen=seen, usage=(100, 100)), budget=SubagentBudget(token_budget=1_000_000),
                  limits=ContextLimits(max_sub_input_tokens=100, max_total_sub_tokens=400)).bind('root', {})
    with pytest.raises(ContextDenied):
        await ns['llm']('p', 'word ' * 500)          # over the per-call input cap
    assert seen == []
    await ns['llm']('p', 't', max_tokens=100)        # ~100 + small
    await ns['llm']('p', 't', max_tokens=100)
    with pytest.raises(ContextDenied):
        await ns['llm']('p', 't', max_tokens=300)    # would exceed the total
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_llm_depth_limit_and_not_configured(world):
    seen = []
    deep = provider(world, llm=make_llm(seen=seen), budget=SubagentBudget(token_budget=100_000)).bind('root', {}, depth=1)
    with pytest.raises(ContextDenied):
        await deep['llm']('p', 't')
    with pytest.raises(ContextDenied):
        await provider(world).bind('root', {})['llm']('p', 't')
    with pytest.raises(ContextDenied):
        await provider(world, llm=make_llm()).bind('root', {})['llm']('p', 't')   # no budget
    assert seen == []


@pytest.mark.asyncio
async def test_llm_tool_call_event_rejected_and_budget_settled(world):
    budget = SubagentBudget(token_budget=10_000)
    ns = provider(world, llm=make_llm(tool_call=True), budget=budget).bind('root', {})
    with pytest.raises(ContextDenied):
        await ns['llm']('p', 't')
    assert budget.snapshot()['spent_tokens'] > 0          # conservative charge still settled
    assert budget._reserved_tokens == 0


@pytest.mark.asyncio
async def test_llm_budget_exhaustion(world):
    seen = []
    ns = provider(world, llm=make_llm(seen=seen), budget=SubagentBudget(token_budget=10)).bind('root', {})
    with pytest.raises(ContextDenied):
        await ns['llm']('p', 't')
    assert seen == []


@pytest.mark.asyncio
async def test_llm_output_is_clipped(world):
    ns = provider(world, llm=make_llm(chunks=['y' * 100] * 100), budget=SubagentBudget(token_budget=100_000)).bind('root', {})
    result = await ns['llm']('p', 't', max_tokens=10)
    assert len(result['text']) < 200
