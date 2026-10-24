"""The code_start manual and code_attach reply: grouping, budget, reasons, re-describe (kernel plan §3.2, §6)."""
from __future__ import annotations

from types import SimpleNamespace

from test_codemode_tool import Rig, config, done, text_of, tool

from nexus.tools.codemode.catalog import build_catalog
from nexus.tools.codemode.manual import render_manual, render_stubs

TOOLS = [
    tool('read'), tool('grep'), tool('write', mutates=True),
    tool('subagent'), tool('question'),
    tool('mcp__github__list_issues', bundle='mcp'), tool('mcp__github__create_issue', bundle='mcp', mutates=True),
    tool('mcp__linear__list_tickets', bundle='mcp'),
    tool('mcp__slack__post', bundle='mcp', mutates=True),
]


def section(text, number, nxt):
    head = f'\n{number}. '
    start = text.index(head)
    end = text.index(f'\n{nxt}. ', start) if nxt else len(text)
    return text[start:end]


async def test_manual_has_six_labelled_sections_in_order_and_is_text_not_json():
    r = Rig(TOOLS)
    text = text_of(await r.start())
    assert not text.lstrip().startswith(('{', '['))
    positions = [text.index(h) for h in ('1. How it works', '2. Language', '3. Kernel helpers',
                                         '4. Attached tools', '5. Not attached')]
    assert positions == sorted(positions)
    assert '6. State' not in text  # state is only for a re-describe
    for needle in ('Out [n]', 'asyncio.gather', 'ToolError', 'whos()', 'search_tools', 'describe_tool',
                   'Importable:', 'No `match`', 'keyword arguments', 'not undone'):
        assert needle in text
    assert text.startswith('Kernel ') and 'started for root' in text
    await r.aclose()


async def test_default_start_attaches_builtins_and_pinned_with_stubs():
    r = Rig(TOOLS, cfg=config(pinned=['mcp__linear__list_tickets']))
    text = text_of(await r.start())
    attached = section(text, 4, 5)
    assert 'Built-in tools (2 tools)' in attached and 'async def read(' in attached
    assert 'Pinned tools (1 tool)' in attached and 'async def mcp__linear__list_tickets(' in attached
    assert 'mcp__github__list_issues' not in attached
    assert sorted(r.kernels.get('s1', 'root').attached) == ['grep', 'mcp__linear__list_tickets', 'read']
    await r.aclose()


async def test_per_server_stubs_are_grouped_and_headed_by_server_name():
    r = Rig(TOOLS)
    text = text_of(await r.start(servers=['github', 'linear']))
    attached = section(text, 4, 5)
    assert attached.index('Built-in tools') < attached.index('Server github') < attached.index('Server linear')
    github = attached[attached.index('Server github'):attached.index('Server linear')]
    assert 'Server github (1 tool)' in github and 'async def mcp__github__list_issues(' in github
    assert 'create_issue' not in github
    assert 'Server linear (1 tool)' in attached and 'async def mcp__linear__list_tickets(' in attached
    event = r.of('code.kernel_started')[0]
    assert event['attached']['servers'] == ['github', 'linear']
    assert 'mcp__github__list_issues' in event['attached']['tools'] and event['agent_path'] == 'root'
    await r.aclose()


async def test_budget_clip_is_announced_with_counts_and_names_stay_attached():
    many = [tool(f'mcp__big__t{i}', bundle='mcp') for i in range(40)]
    r = Rig([tool('read'), *many], cfg=config(manual_budget_tokens=500))
    text = text_of(await r.start(servers=['big']))
    clipped = text[text.index('[manual clipped'):]
    assert 'manual_budget_tokens = 500' in clipped
    shown = text.count('async def mcp__big__t') + text.count('async def read(')
    assert f'showing {shown} of 41 stubs' in clipped
    assert 'describe_tool(name)' in clipped and 'mcp__big__t39' in clipped
    assert 0 < shown < 41
    assert len(r.kernels.get('s1', 'root').attached) == 41  # clipped tools are still callable
    await r.aclose()


async def test_not_attached_lists_every_refusal_with_its_reason():
    r = Rig([*TOOLS, tool('whos'), tool('class')])
    text = text_of(await r.start(servers=['github', 'nowhere'],
                                 tools=['subagent', 'no_such_tool', 'mcp__nothing__*'], query='slack post'))
    rows = section(text, 5, None)
    assert 'server nowhere: not an enabled MCP server in this session' in rows
    assert 'server github: 1 of 2 tools: refused by [code_mode] expose' in rows
    assert 'create_issue' in rows  # the refused tool is named
    assert 'subagent: drives the loop' in rows
    assert 'no_such_tool: unknown tool, or disabled for this session' in rows
    assert 'mcp__nothing__*: matches no callable tool' in rows
    assert 'mcp__slack__post: refused by [code_mode] expose' in rows  # query match, mutating
    assert 'class: not a valid identifier' in rows and 'whos: reserved name' in rows
    assert 'subagent' not in r.kernels.get('s1', 'root').attached
    await r.aclose()


async def test_a_session_disabled_server_is_not_attached_and_says_so():
    enabled = [t for t in TOOLS if 'linear' not in t.name]
    r = Rig(enabled)
    text = text_of(await r.start(servers=['linear']))
    assert 'server linear: not an enabled MCP server in this session' in section(text, 5, None)
    await r.aclose()


async def test_explicitly_named_mutating_tools_attach_but_server_selection_does_not():
    r = Rig(TOOLS)
    text = text_of(await r.start(tools=['write', 'mcp__slack__*']))
    attached = section(text, 4, 5)
    assert 'Named tools (2 tools)' in attached
    assert 'async def write(' in attached and 'async def mcp__slack__post(' in attached
    assert sorted(r.kernels.get('s1', 'root').attached) == ['grep', 'mcp__slack__post', 'read', 'write']
    # With expose = all, server selection may include mutating tools too.
    r2 = Rig(TOOLS, cfg=config(expose='all'))
    text2 = text_of(await r2.start(servers=['github']))
    assert 'async def mcp__github__create_issue(' in text2
    await r.aclose()
    await r2.aclose()


async def test_query_attaches_top_matches_within_expose():
    r = Rig(TOOLS)
    text = text_of(await r.start(query='tickets'))
    assert 'Search matches for "tickets"' in section(text, 4, 5)
    assert 'mcp__linear__list_tickets' in r.kernels.get('s1', 'root').attached
    await r.aclose()


async def test_redescribe_lists_state_without_changing_it():
    r = Rig(TOOLS)
    await r.start(servers=['github'])
    state = r.kernels.get('s1', 'root')
    before = (state.kernel_id, state.n, dict(state.attached), len(r.of('code.kernel_started')))
    state.n = 3

    async def script(f):
        return done([['issues', 'list', 42], ['label', 'str', 3]])

    r.host.script = script
    result = await r.start(servers=['linear'], query='ignored')
    text = text_of(result)
    assert 'already running' in text and 'nothing changed' in text
    assert 'Ignored servers, query' in text and 'use code_attach' in text
    state_section = section(text, 6, None)
    assert f'Kernel {before[0]}' in state_section and '3 cells run' in state_section
    assert 'issues  list  42' in state_section and 'label  str  3' in state_section
    assert 'mcp__github__list_issues' in state_section
    assert (state.kernel_id, dict(state.attached), len(r.of('code.kernel_started'))) == (
        before[0], before[2], before[3])
    assert state.n == 3 and 'mcp__linear__list_tickets' not in state.attached
    assert 'Server github' in section(text, 4, 5)
    await r.aclose()


async def test_redescribe_reports_tools_that_are_no_longer_callable():
    r = Rig(TOOLS)
    await r.start(servers=['github'])
    # A later iteration no longer has the github tools (disabled for the session).
    from nexus.tools.codemode.tool import register
    later = {t.name: t for t in register(r.kernels, [t for t in TOOLS if 'github' not in t.name], r.config)}

    async def script(f):
        return done([])

    r.host.script = script
    text = text_of(await later['code_start'].run({'purpose': 'again'}, r.ctx()))
    assert 'mcp__github__list_issues: not callable now (disabled for this session or not available this turn)' in text
    await r.aclose()


async def test_attach_returns_stubs_only_for_what_is_new():
    r = Rig(TOOLS)
    await r.start()
    result = await r.call('code_attach', {'servers': ['github', 'nowhere'], 'tools': ['read', 'write']})
    text = text_of(result)
    assert not result.is_error
    assert 'Kernel ' in text and '2 new tools attached' in text
    assert 'async def mcp__github__list_issues(' in text and 'async def write(' in text
    assert 'async def read(' not in text  # already attached at start
    assert 'Not attached:' in text and 'server nowhere' in text
    event = r.of('code.kernel_attached')[0]
    assert sorted(event['attached']['tools']) == ['mcp__github__list_issues', 'write']
    again = text_of(await r.call('code_attach', {'tools': ['write']}))
    assert 'Everything requested was already attached.' in again
    empty = await r.call('code_attach', {})
    assert empty.is_error and 'Nothing to attach' in text_of(empty)
    await r.aclose()


def _catalog(n):
    return build_catalog([SimpleNamespace(spec=tool(f'mcp__s__t{i}', bundle='mcp').spec) for i in range(n)], 'all')


def test_render_stubs_budget_is_announced_and_pinned_order_is_kept():
    catalog = _catalog(40)
    names = tuple(catalog)
    full, stats = render_stubs((('Server s', names),), catalog, budget_tokens=100_000)
    assert 'clipped' not in full and full.count('async def') == 40 and stats['clipped'] == ()
    ordered = (('Pinned', ('mcp__s__t39',)), ('Server s', names[:-1]))
    small, stats = render_stubs(ordered, catalog, budget_tokens=200)
    assert 0 < small.count('async def') < 40
    assert 'async def mcp__s__t39(' in small
    assert small.index('mcp__s__t39(') < small.index('Server s (39 tools)')
    assert 'manual_budget_tokens = 200' in small and stats['attached'] == 40
    assert stats['shown'] + len(stats['clipped']) == 40


def test_render_manual_with_no_tools_says_how_to_attach():
    text = render_manual(groups=(), catalog={}, skipped=(), budget_tokens=500, context=False)
    assert 'No tools attached. Use code_attach' in text and 'Nothing was refused.' in text


async def test_context_section_appears_only_with_the_context_namespace():
    from test_codemode_tool import Provider
    plain = Rig(TOOLS)
    assert 'context_meta' not in text_of(await plain.start())
    ctx = Rig(TOOLS, cfg=config(context=True), provider=Provider())
    text = text_of(await ctx.start())
    assert 'context_messages' in text and 'Context namespace:' in text
    assert 'async def context_meta' in text
    await plain.aclose()
    await ctx.aclose()
