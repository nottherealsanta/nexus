"""The four code-mode kernel tools (plan code-mode-kernel §3.1, §3.6, §3.10, §3.13).

``code_start``, ``code_attach``, ``code_run`` and ``code_reset`` replace the removed
one-shot ``code`` tool. The runtime registers them per iteration manager for the
``coding`` profile (``register``); the kernels themselves live in the runtime's
``CodeKernels``, which outlives turns. A tool is a request, never authority: the
callable catalog is rebuilt from this iteration's tools on every call and every
nested call still passes the nested pipeline and permissions. ``code_run`` keeps the
permission key ``code`` (a rule or agent ``tools`` list naming the old ``code`` tool
grants it) and is ``exclusive`` while mutating tools are attached anywhere. Errors
never reset a kernel; ``code_run`` without ``code_start`` is ``KernelNotStarted``.
Each tool emits its ``code.*`` lifecycle event with its own ``call_id``.
"""
from __future__ import annotations

from importlib import metadata, util

from ...model.message import Text
from ...session.context_authz import ContextDenied
from ...util import redact_secrets
from ..spec import RegisteredTool, ToolExecutionResult, ToolSpec
from .catalog import BUILTIN_NAMESPACE, CODE_TOOLS, namespace
from .cell import monty_version, read_variables, run_cell
from .kernels import (MAX_QUERY, MAX_SERVERS, MAX_TARGETS, MAX_TOOLS, CodeKernels, KernelState,
                      SearchCache, catalogs, missing_reason, new_kernel_id, select)
from .manual import render_manual, render_skipped, render_stubs, targets_line

DESCRIPTIONS = {
    'code_start': (
        "Open this agent's code-mode kernel: a persistent Python sandbox for multi-step or fan-out tool "
        'work, preferred over many direct calls. Returns the manual and typed stubs for the attached tools '
        '(built-ins plus pinned by default; add MCP tools with servers, tools or query, and sessions to read '
        'with targets). Variables then persist across code_run cells and big intermediate results stay out '
        'of context. On a running kernel it only re-describes it.'),
    'code_attach': (
        'Add tools or context targets to the running kernel (servers, tools, query, targets as in code_start). '
        'Returns stubs for the newly attached tools only; variables are kept. Attaching is a request: every '
        "call still follows this turn's tool list and permissions."),
    'code_run': (
        'Run one Python cell in the kernel. Variables, functions and imports persist across cells; a last-line '
        'expression is shown as Out [n]. Call attached tools with await (asyncio.gather for fan-out), keep big '
        'results in variables and look at slices. Errors never clear the kernel and earlier tool side effects '
        'are not undone. Needs code_start first.'),
    'code_reset': (
        'Clear all kernel variables and restart cell numbering at In [1]; attachments are kept. With stop=true '
        'the kernel is freed entirely and code_start must be called again.'),
}
NOT_STARTED = 'KernelNotStarted: call code_start first (no kernel is running for this agent).'


def availability():
    try:
        version = metadata.version('pydantic-monty')
    except metadata.PackageNotFoundError:
        return {'available': False, 'reason': 'reinstall nexus-harness: pydantic-monty==1.1.0 is missing', 'version': None}
    spec = util.find_spec('pydantic_monty')
    return {'available': version == '1.1.0', 'version': version,
            'reason': '' if version == '1.1.0' else 'requires pydantic-monty==1.1.0',
            'package_path': spec.origin if spec else None}


def _list_schema(limit, item=128):
    return {'type': 'array', 'maxItems': limit, 'items': {'type': 'string', 'maxLength': item}}


def _selection_properties():
    return {
        'servers': {**_list_schema(MAX_SERVERS), 'description': 'MCP server names'},
        'tools': {**_list_schema(MAX_TOOLS), 'description': 'tool names or mcp__server__* globs'},
        'query': {'type': 'string', 'maxLength': MAX_QUERY, 'description': 'search terms for tools'},
        'targets': {**_list_schema(MAX_TARGETS), 'description': 'session names or ids for context reads'},
        'purpose': {'type': 'string', 'maxLength': 512},
    }


def _error(text, note='code: error'):
    return ToolExecutionResult.text(text, is_error=True, context_note=note)


def _group_attached(names, full):
    """Attached tools that are callable now, grouped by namespace (built-ins first)."""
    groups: dict[str, list[str]] = {}
    for name in names:
        if name not in full:
            continue
        ns = namespace(full[name].spec.name)
        heading = 'Built-in tools' if ns == BUILTIN_NAMESPACE else f'Server {ns[4:]}'
        groups.setdefault(heading, []).append(name)
    return tuple((h, tuple(v)) for h, v in groups.items())


def register(kernels: CodeKernels, tools, config) -> tuple[RegisteredTool, ...]:
    """The four kernel tools bound to this iteration's selected ``tools``."""
    tools = tuple(tools)
    full, ceiling = catalogs(tools, config)
    search = SearchCache(full)
    provider = kernels.context_provider if getattr(config, 'context', False) else None
    budget = int(getattr(config, 'manual_budget_tokens', 6000))
    mutating = any(full[n].spec.mutates for st in kernels.states() for n in st.attached if n in full)

    async def emit(ctx, kind, data):
        if ctx.emit:
            outcome = ctx.emit(kind, data)
            if hasattr(outcome, '__await__'):
                await outcome

    async def resolve_targets(ctx, refs, purpose, *, existing=()):
        """Approve context targets up front: (approved, info, skipped). Never raises."""
        approved: dict[str, str] = {}
        info: list[dict] = []
        skipped: list[tuple[str, str]] = []
        wanted = list(dict.fromkeys(refs))
        if provider is None:
            return approved, info, [(r, 'context namespace unavailable: enable [code_mode] context')
                                    for r in wanted if r != 'self']
        for ref in wanted:
            if ref in existing:
                continue
            try:
                found = provider.resolve(ctx.session_id, ref)
                provider.check(ctx.session_id, found['stored_id'])
            except ContextDenied as exc:
                skipped.append((ref, redact_secrets(str(exc))))
                continue
            if ref != 'self':
                if ctx.authorize_nested is None:
                    skipped.append((ref, 'context access needs approval but none is available'))
                    continue
                verdict = await ctx.authorize_nested(
                    f'ContextQuery({found.get("name") or ref})', parent_call_id=ctx.call_id,
                    summary={'purpose': redact_secrets(purpose)[:512], 'target': ref,
                             'name': found.get('name'), 'logical_id': found.get('logical_id'),
                             'program': f'code kernel targets: {ref}'})
                if not verdict.allowed:
                    skipped.append((ref, verdict.message or f'context access to {ref} was denied'))
                    continue
            approved[ref] = found['stored_id']
            info.append({'ref': found['ref'], 'name': found.get('name'), 'logical_id': found.get('logical_id')})
        return approved, info, skipped

    def attached_event(state, names):
        servers = sorted({namespace(full[n].spec.name)[4:] for n in names
                          if n in full and namespace(full[n].spec.name) != BUILTIN_NAMESPACE})
        return {'servers': servers, 'tools': list(names),
                'targets': [{'ref': i['ref'], 'name': i.get('name'), 'logical_id': i.get('logical_id')}
                            for i in state.target_info]}

    def skipped_event(skipped):
        return [{'tool': n, 'reason': redact_secrets(r)[:500]} for n, r in skipped[:50]]

    # -- code_start -----------------------------------------------------------
    async def code_start(args, ctx):
        await kernels.maintain()
        purpose = args['purpose']
        agent = ctx.agent_id
        state = await kernels.resolve(ctx.session_id, agent, full)
        if state is not None:
            return await redescribe(state, args, ctx)
        selection = select(tools=tools, full=full, ceiling=ceiling, search=search, config=config,
                           servers=args.get('servers') or (), names=args.get('tools') or (),
                           query=args.get('query'), defaults=True)
        refs = list(args.get('targets') or [])
        approved, info, target_skips = await resolve_targets(
            ctx, (['self'] if provider is not None else []) + refs, purpose)
        skipped = [*selection.skipped, *((f'target {r}', why) for r, why in target_skips)]
        attached = {n: full[n].spec.name for n in selection.names}
        state = KernelState(kernel_id=new_kernel_id(), session_id=ctx.session_id, agent_path=agent,
                            attached=attached, approved=approved, target_info=info,
                            purpose=redact_secrets(purpose)[:512])
        try:
            await kernels.open(state)
        except Exception as exc:  # noqa: BLE001 - reported, never raised past the tool boundary
            return _error(f'Kernel not started: {type(exc).__name__}: {redact_secrets(str(exc))[:300]}',
                          'code_start: kernel not started')
        kernels.put(state)
        await emit(ctx, 'code.kernel_started', {
            'kernel_id': state.kernel_id, 'call_id': ctx.call_id, 'agent_path': agent,
            'purpose': state.purpose, 'attached': attached_event(state, list(attached)),
            'skipped': skipped_event(skipped), 'monty_version': monty_version()})
        text = render_manual(
            groups=selection.groups, catalog=full, skipped=skipped, budget_tokens=budget,
            context=provider is not None, context_stubs=provider.stubs() if provider else '',
            targets=info if provider is not None else (),
            header=f'Kernel {state.kernel_id} started for {agent}: {len(attached)} tools attached.')
        return ToolExecutionResult(
            content=[Text(text=text)], display=text,
            context_note=f'code_start: {state.purpose or "kernel"} → {len(attached)} tools attached',
            metrics={'kernel_id': state.kernel_id, 'attached': len(attached)})

    async def redescribe(state, args, ctx):
        """A running kernel is described again; nothing about it changes."""
        header = [f'Kernel {state.kernel_id} is already running; this is a re-description, nothing changed.']
        ignored = [k for k in ('servers', 'tools', 'query', 'targets') if args.get(k)]
        if ignored:
            header.append(f'Ignored {", ".join(ignored)}: use code_attach to add tools or targets.')
        prepared = await kernels.prepare(state, lambda kind, data: emit(ctx, kind, data), ctx.call_id)
        if prepared is not None:
            header.append(prepared.notice)
        status = kernels.registry.status(ctx.session_id, state.agent_path)
        if prepared is None and status in (None, 'lost'):
            await kernels.restart(state)
            await emit(ctx, 'code.kernel_reset', {'kernel_id': state.kernel_id, 'call_id': ctx.call_id,
                                                  'stopped': False, 'reason': 'lost'})
            header.append('Its worker had been lost: variables are gone, In [n] restarts at 1, attachments are kept.')
        variables = await read_variables(kernels, state)
        names = list(state.attached)
        gone = [n for n in names if n not in full]
        skipped = [(n, f'not callable now ({missing_reason(state.attached[n], tools, full, attached=True)})')
                   for n in gone]
        info = state.target_info
        text = render_manual(
            groups=_group_attached(names, full), catalog=full, skipped=skipped, budget_tokens=budget,
            context=provider is not None, context_stubs=provider.stubs() if provider else '',
            targets=info if provider is not None else (), header='\n'.join(header),
            state={'kernel_id': state.kernel_id, 'cells': state.n, 'attached': names,
                   'variables': variables if variables is not None else (),
                   'status': kernels.registry.status(ctx.session_id, state.agent_path) or 'live'})
        if variables is None:
            text += '\nVariables: unavailable right now'
        return ToolExecutionResult(
            content=[Text(text=text)], display=text,
            context_note=f'code_start: re-described kernel ({len(names)} tools, {state.n} cells)',
            metrics={'kernel_id': state.kernel_id, 'attached': len(names)})

    # -- code_attach ----------------------------------------------------------
    async def code_attach(args, ctx):
        await kernels.maintain()
        state = await kernels.resolve(ctx.session_id, ctx.agent_id, full)
        if state is None:
            return _error(NOT_STARTED, 'code_attach: KernelNotStarted')
        if not any(args.get(k) for k in ('servers', 'tools', 'query', 'targets')):
            return _error('Nothing to attach: give servers, tools, query or targets.', 'code_attach: nothing requested')
        selection = select(tools=tools, full=full, ceiling=ceiling, search=search, config=config,
                           servers=args.get('servers') or (), names=args.get('tools') or (),
                           query=args.get('query'), already=state.attached)
        approved, info, target_skips = await resolve_targets(
            ctx, args.get('targets') or [], args['purpose'], existing=state.approved)
        skipped = [*selection.skipped, *((f'target {r}', why) for r, why in target_skips)]
        new = {n: full[n].spec.name for n in selection.names}
        state.attached.update(new)
        state.approved.update(approved)
        state.target_info.extend(info)
        await emit(ctx, 'code.kernel_attached', {
            'kernel_id': state.kernel_id, 'call_id': ctx.call_id,
            'attached': attached_event(state, list(new)) | {
                'targets': [{'ref': i['ref'], 'name': i.get('name'), 'logical_id': i.get('logical_id')}
                            for i in info]},
            'skipped': skipped_event(skipped)})
        body, _ = render_stubs(selection.groups, full, budget_tokens=budget)
        parts = [f'Kernel {state.kernel_id}: {len(new)} new tools attached, {len(state.attached)} in all.']
        if body:
            parts.append(body)
        line = targets_line(info)
        if line:
            parts.append(line)
        if skipped:
            parts.append('Not attached:\n' + render_skipped(skipped))
        if not new and not info and not skipped:
            parts.append('Everything requested was already attached.')
        text = '\n\n'.join(parts)
        return ToolExecutionResult(
            content=[Text(text=text)], display=text,
            context_note=f'code_attach: {args["purpose"]} → {len(new)} new tools',
            metrics={'kernel_id': state.kernel_id, 'attached': len(new)})

    # -- code_run -------------------------------------------------------------
    async def code_run(args, ctx):
        state = await kernels.resolve(ctx.session_id, ctx.agent_id, full)
        if state is None:
            return _error(NOT_STARTED, 'code_run: KernelNotStarted')
        timeout = args.get('timeout_s', min(60, config.max_tool_seconds))
        if timeout < 1 or timeout > config.max_tool_seconds:
            return _error('invalid code timeout', 'code_run: invalid timeout')
        await kernels.maintain()
        return await run_cell(kernels=kernels, state=state, full=full, tools=tools, config=config, ctx=ctx,
                              source=args['cell'], purpose=args['purpose'], timeout=timeout, search=search)

    # -- code_reset -----------------------------------------------------------
    async def code_reset(args, ctx):
        state = await kernels.resolve(ctx.session_id, ctx.agent_id, full)
        if state is None:
            return ToolExecutionResult.text('No kernel is running; nothing to reset.',
                                            context_note='code_reset: no kernel')
        stop = bool(args.get('stop', False))
        try:
            await kernels.registry.close(ctx.session_id, state.agent_path)
            # A reset or stop invalidates the checkpoint: it must never be restored afterwards.
            kernels.forget_checkpoint(state)
            if stop:
                kernels.drop(ctx.session_id, state.agent_path)
            else:
                state.n = 0
                await kernels.open(state)
        except Exception as exc:  # noqa: BLE001 - e.g. KernelBusy while a cell runs
            return _error(f'Kernel not reset: {type(exc).__name__}: {redact_secrets(str(exc))[:300]}',
                          'code_reset: failed')
        await emit(ctx, 'code.kernel_reset', {'kernel_id': state.kernel_id, 'call_id': ctx.call_id,
                                              'stopped': stop, 'reason': 'reset'})
        text = ('Kernel stopped and freed. Call code_start to open a new one.' if stop else
                f'Kernel {state.kernel_id} reset: all variables are cleared, In [n] restarts at 1, '
                f'{len(state.attached)} tools stay attached.')
        return ToolExecutionResult.text(text, context_note=f'code_reset: {"stopped" if stop else "cleared"}')

    def spec(name, properties, required, run, *, mutates=False, concurrency='parallel', key=None):
        return RegisteredTool(ToolSpec(
            name=name, bundle='code', description=DESCRIPTIONS[name],
            input_schema={'type': 'object', 'properties': properties, 'required': required,
                          'additionalProperties': False},
            mutates=mutates, concurrency=concurrency, permission_key=key,
            timeout_s=config.max_tool_seconds), run)

    selection_props = _selection_properties()
    reset_props = {'stop': {'type': 'boolean'}, 'purpose': {'type': 'string', 'maxLength': 512}}
    run_props = {'cell': {'type': 'string', 'maxLength': 16384}, 'purpose': {'type': 'string', 'maxLength': 512},
                 'timeout_s': {'type': 'integer', 'minimum': 1, 'maximum': config.max_tool_seconds}}
    built = (
        spec('code_start', selection_props, ['purpose'], code_start, concurrency='exclusive'),
        spec('code_attach', selection_props, ['purpose'], code_attach, concurrency='exclusive'),
        spec('code_run', run_props, ['cell', 'purpose'], code_run, mutates=mutating,
             concurrency='exclusive' if mutating else 'parallel', key=lambda args: 'code'),
        spec('code_reset', reset_props, ['purpose'], code_reset, concurrency='exclusive'),
    )
    assert tuple(t.name for t in built) == CODE_TOOLS
    return built


