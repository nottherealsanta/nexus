"""Shared daemon routing, isolation and lifecycle over real Unix sockets."""
import asyncio

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, VoiceSection
from nexus.host import protocol as p
from nexus.host.daemon import Daemon, default_socket_path
from nexus.host.transports import ProtocolError, ResultFrame
from nexus.host.transports.uds import UDSClient
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


@pytest.fixture
async def shared(tmp_path):
    roots = [tmp_path / 'a', tmp_path / 'b']
    for root in roots:
        root.mkdir()
    made = []
    def factory(workspace, **kwargs):
        made.append((workspace, kwargs['environ']))
        config = Config(model='scripted/m', version=2,
                        v2=ConfigV2(model=ModelSection(default='scripted/m'),
                                    voice=VoiceSection(enabled=False)))
        return Runtime(workspace, config=config, providers={'scripted': ScriptedProvider(
            *[text_response('ok') for _ in range(10)])}, **kwargs)
    # macOS Unix socket paths must stay short.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix='nx-', dir='/tmp') as short:
        daemon = Daemon(roots[0], home=tmp_path, socket_path=Path(short) / 's.sock',
                        runtime_factory=factory, idle_timeout=0)
        await daemon.start()
        try:
            yield daemon, roots, made
        finally:
            await daemon.aclose()


async def connect(daemon, root, env=None):
    return await UDSClient.connect(daemon.socket, focus=str(root), environ=env or {})


async def test_focus_and_explicit_project_are_isolated(shared):
    daemon, (a, b), made = shared
    assert not made  # Lazy construction.
    client = await connect(daemon, a, {'MARKER': 'a'})
    try:
        first = client.info.focus
        opened = await client.call(p.ProjectOpen(path=str(b)))
        assert isinstance(opened, p.ProjectOpenResult)
        assert len(made) == 2
        assert made[0][1] == made[1][1] == {'MARKER': 'a'}
        assert len(daemon.projects.hosts) == 2
        (a / 'only-a').write_text('a')
        (b / 'only-b').write_text('b')
        browser = await client.call(p.ProjectDirectoryList(path=str(b)))
        assert isinstance(browser, p.DirectoryListResult)
        assert browser.path == str(b.resolve())
        assert [row["name"] for row in browser.entries] == ["only-b"]
        switched = await client.call(p.ProjectFocus(project=opened.project.key))
        assert switched.project.root == str(b)
        listing = await client.call(p.DirectoryList())
        assert [e["name"] for e in listing.entries] == ['only-b']
        listing = await client.call(p.DirectoryList(project=first.key))
        assert [e["name"] for e in listing.entries] == ['only-a']
        health = await client.call(p.Health())
        assert len(health.projects) == 2
    finally:
        await client.close()


async def test_sessions_route_after_switch_and_subscriptions_survive(shared):
    daemon, (a, b), _ = shared
    client = await connect(daemon, a)
    try:
        await client.call(p.SessionOpen(session='a-session'))
        stream = await client.subscribe('a-session')
        await client.call(p.ProjectFocus(project=str(b)))
        await client.call(p.SessionOpen(session='b-session'))
        started = await client.call(p.SessionStart(session='a-session', content='hello'))
        assert isinstance(started, p.SessionStartResult)
        events = []
        async def collect():
            async for event in stream:
                events.append(event)
                if event.type in ('turn.completed', 'turn.failed'):
                    return
        try:
            await asyncio.wait_for(collect(), 5)
        except TimeoutError:
            pytest.fail(repr(events))
        assert not any(e.type == 'turn.failed' for e in events), events
        assert any(e.type == 'turn.started' for e in events)
        assert (await client.call(p.SessionState(session='a-session'))).session == 'a-session'
        opened = await client.call(p.ProjectSessionOpen(workspace=str(a), session='a-session'))
        assert opened.project.root == str(a)
        assert opened.session == 'a-session'
        await stream.aclose()
    finally:
        await client.close()


async def test_project_environments_do_not_leak_between_clients(shared):
    daemon, (a, b), made = shared
    c1 = await connect(daemon, a, {'SECRET': 'a'})
    c2 = await connect(daemon, b, {'SECRET': 'b'})
    try:
        assert [env for _, env in made] == [{'SECRET': 'a'}, {'SECRET': 'b'}]
        assert daemon.projects.hosts[c1.info.focus.key].runtime._environ == {'SECRET': 'a'}
        assert daemon.projects.hosts[c2.info.focus.key].runtime._environ == {'SECRET': 'b'}
        health = await c1.call(p.Health())
        assert 'SECRET' not in repr(health)
    finally:
        await c1.close()
        await c2.close()


async def test_invalid_handshake_is_refused_without_loading(shared):
    daemon, (a, _), made = shared
    with pytest.raises(ProtocolError, match='absolute'):
        await UDSClient.connect(daemon.socket, focus='relative')
    with pytest.raises(ProtocolError, match='1024'):
        await connect(daemon, a, {f'k{i}': 'v' for i in range(1025)})
    assert not made


def test_socket_is_shared_by_projects_and_separate_by_home(tmp_path):
    assert default_socket_path('/a', home=tmp_path) == default_socket_path('/b', home=tmp_path)
    assert default_socket_path('/a', home=tmp_path) != default_socket_path('/a', home=tmp_path / 'other')


async def test_browser_tickets_and_cookies_are_project_scoped(shared):
    from urllib.parse import urlsplit
    import httpx
    daemon, (a, b), _ = shared
    c1 = await connect(daemon, a)
    c2 = await connect(daemon, b)
    try:
        urls = [(await c.call(p.WebLaunch())).url for c in (c1, c2)]
        first, second = map(urlsplit, urls)
        assert first.netloc == second.netloc
        async with httpx.AsyncClient(base_url=f'http://{first.netloc}') as browser:
            r1 = await browser.post(first.path + 'v1/web/ticket/redeem',
                json={'ticket': first.fragment.removeprefix('ticket=')},
                headers={'Origin': f'http://{first.netloc}'})
            assert r1.status_code == 200, r1.text
            assert f'Path={first.path.rstrip("/")}' in r1.headers['set-cookie']
            rejected = await browser.get(second.path + 'v1/web/bootstrap')
            assert rejected.status_code == 401
            wrong_ticket = await browser.post(second.path + 'v1/web/ticket/redeem',
                json={'ticket': first.fragment.removeprefix('ticket=')},
                headers={'Origin': f'http://{first.netloc}'})
            assert wrong_ticket.status_code == 401
            r2 = await browser.post(second.path + 'v1/web/ticket/redeem',
                json={'ticket': second.fragment.removeprefix('ticket=')},
                headers={'Origin': f'http://{first.netloc}'})
            assert r2.status_code == 200
            assert r1.headers['set-cookie'].split('=')[0] != r2.headers['set-cookie'].split('=')[0]
            page = await browser.get(first.path)
            assert page.status_code == 200
            assert first.path + 'js/app.js' in page.text
    finally:
        await c1.close()
        await c2.close()


async def test_unload_reload_and_control_status_do_not_capture_another_environment(shared):
    daemon, (a, _), made = shared
    client = await connect(daemon, a, {'MARKER': 'old'})
    key = client.info.focus.key
    await client.close()
    # Wait for server-side disconnect to release its pin.
    for _ in range(100):
        if daemon.projects.hosts[key].references == 0:
            break
        await asyncio.sleep(.01)
    control = await UDSClient.connect(daemon.socket, focus=str(a), client='control', environ={'MARKER': 'new'})
    try:
        assert len(made) == 1
        assert len((await control.call(p.Health())).projects) == 1
        loaded = await control.call(p.ProjectReload())
        assert isinstance(loaded, p.ProjectFocusResult), loaded
        assert made[-1][1] == {'MARKER': 'new'}
        assert daemon.projects.hosts[key].references == 0
        unloaded = await control.call(p.ProjectUnload())
        assert isinstance(unloaded, p.ProjectFocusResult), unloaded
        assert unloaded.project.key == key
        assert not daemon.projects.hosts
        assert (await control.call(p.Health())).projects == []
    finally:
        await control.close()


async def test_bad_project_does_not_break_another_project_and_slow_load_is_off_loop(shared):
    import threading
    daemon, (a, b), _ = shared
    client = await connect(daemon, a)
    original = daemon.projects.factory
    started, release = threading.Event(), threading.Event()
    def bad_factory(workspace, **kwargs):
        if workspace == b:
            started.set()
            release.wait(5)
            raise ValueError('bad project config')
        return original(workspace, **kwargs)
    daemon.projects.factory = bad_factory
    pending = asyncio.create_task(connect(daemon, b))
    try:
        await asyncio.to_thread(started.wait, 2)
        health = await asyncio.wait_for(client.call(p.Health()), 1)
        assert health.ok
        release.set()
        with pytest.raises(ProtocolError, match='bad project config'):
            await pending
        assert len(daemon.projects.hosts) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await client.close()


async def test_registry_refuses_busy_capacity_and_evicts_only_idle_hosts(shared, monkeypatch):
    import nexus.host.projects as projects
    monkeypatch.setattr(projects, 'MAX_PROJECTS', 2)
    daemon, (a, b), _ = shared
    c = a.parent / 'c'
    c.mkdir()
    c1, c2 = await connect(daemon, a), await connect(daemon, b)
    try:
        with pytest.raises(ProtocolError, match='project slots are busy'):
            await connect(daemon, c)
        first_key = c1.info.focus.key
        await c1.close()
        for _ in range(100):
            if daemon.projects.hosts[first_key].references == 0:
                break
            await asyncio.sleep(.01)
        c3 = await connect(daemon, c)
        try:
            assert len(daemon.projects.hosts) == 2
            assert first_key not in daemon.projects.hosts
            assert c2.info.focus.key in daemon.projects.hosts
        finally:
            await c3.close()
    finally:
        await c1.close()
        await c2.close()


async def test_deleted_loaded_project_keeps_session_reads_but_refuses_writes(shared):
    daemon, (a, _), _ = shared
    client = await connect(daemon, a)
    try:
        await client.call(p.SessionOpen(session='retained'))
        await client.call(p.SessionStart(session='retained', content='saved message'))
        await daemon.projects.hosts[client.info.focus.key].facade.wait_idle(timeout=5)
        a.rename(a.with_name('moved'))
        state = await client.call(p.SessionState(session='retained'))
        assert isinstance(state, p.SessionStateResult)
        refused = await client.call(p.SessionStart(session='retained', content='must not run'))
        assert isinstance(refused, p.ErrorResult) and 'read-only' in refused.message
        rows = (await client.call(p.Health())).projects
        assert len(rows) == 1 and not rows[0].exists
        assert not a.exists()
    finally:
        await client.close()


async def test_failed_project_session_open_releases_temporary_host_reference(shared):
    daemon, (a, _), _ = shared
    client = await connect(daemon, a)
    host = daemon.projects.hosts[client.info.focus.key]
    original_handle = host.facade.handle

    async def fail_open(command):
        if isinstance(command, p.SessionOpen):
            return p.ErrorResult(kind='missing', message='session missing')
        return await original_handle(command)

    host.facade.handle = fail_open
    try:
        assert host.references == 1  # The connected client's focus pin.
        result = await client.call(p.ProjectSessionOpen(workspace=str(a), session='missing'))
        assert isinstance(result, p.ErrorResult)
        assert host.references == 1
    finally:
        host.facade.handle = original_handle
        await client.close()


async def test_acquire_waits_for_unload_teardown_before_rebuilding(shared):
    daemon, (a, _), made = shared
    client = await connect(daemon, a)
    host = daemon.projects.hosts[client.info.focus.key]
    identity = host.identity
    await client.close()
    for _ in range(100):
        if host.references == 0:
            break
        await asyncio.sleep(.01)
    assert host.references == 0

    shutdown_started = asyncio.Event()
    allow_shutdown = asyncio.Event()
    shutdown_finished = asyncio.Event()
    original_shutdown = host.facade.shutdown

    async def gated_shutdown(reason=''):
        shutdown_started.set()
        await allow_shutdown.wait()
        try:
            return await original_shutdown(reason)
        finally:
            shutdown_finished.set()

    host.facade.shutdown = gated_shutdown
    unload = asyncio.create_task(daemon.projects.unload(identity.key))
    acquire = None
    try:
        await asyncio.wait_for(shutdown_started.wait(), 2)
        assert identity.key not in daemon.projects.hosts
        acquire = asyncio.create_task(daemon.projects.acquire(identity, {}, 'test'))
        await asyncio.sleep(.02)
        assert not acquire.done()
        assert len(made) == 1

        allow_shutdown.set()
        await asyncio.wait_for(unload, 5)
        replacement = await asyncio.wait_for(acquire, 5)
        try:
            assert shutdown_finished.is_set()
            assert replacement is not host
            assert replacement.facade is not host.facade
            assert len(made) == 2
        finally:
            daemon.projects.release(replacement)
            acquire = None
    finally:
        allow_shutdown.set()
        await asyncio.gather(unload, return_exceptions=True)
        if acquire is not None:
            result = await asyncio.gather(acquire, return_exceptions=True)
            if result and not isinstance(result[0], BaseException):
                daemon.projects.release(result[0])


async def test_completed_non_follow_subscription_releases_host_and_server_slot(shared):
    daemon, (a, _), _ = shared
    client = await connect(daemon, a)
    host = daemon.projects.hosts[client.info.focus.key]
    server_connection = next(c for c in daemon._clients if c.client_id == client.client_id)
    try:
        await client.call(p.SessionOpen(session='snapshot'))
        baseline = host.references
        send_entered = asyncio.Event()
        allow_send = asyncio.Event()
        original_send = server_connection._send

        async def gated_send(frame):
            if (isinstance(frame, ResultFrame)
                    and isinstance(frame.result, p.SessionSubscribeResult)):
                send_entered.set()
                await allow_send.wait()
            await original_send(frame)

        server_connection._send = gated_send
        subscribing = asyncio.create_task(client.subscribe('snapshot', follow=False))
        await asyncio.wait_for(send_entered.wait(), 2)
        # The subscription must pin its project before the result is written:
        # a concurrent drain/unload cannot retire it in this gap.
        assert host.references == baseline + 1
        allow_send.set()
        subscription = await asyncio.wait_for(subscribing, 2)
        server_connection._send = original_send
        async for _event in subscription:
            pass

        for _ in range(100):
            if host.references == baseline and not server_connection._subs:
                break
            await asyncio.sleep(.01)
        assert host.references == baseline
        assert server_connection._subs == {}
        assert subscription.id not in client.subscriptions
    finally:
        if 'allow_send' in locals():
            allow_send.set()
        if 'original_send' in locals():
            server_connection._send = original_send
        await client.close()


async def test_project_session_open_creates_a_fresh_session_in_a_new_project(shared, tmp_path):
    daemon, (a, _), _ = shared
    target = tmp_path / 'new-project'
    target.mkdir()
    client = await connect(daemon, a)
    try:
        result = await client.call(p.ProjectSessionOpen(workspace=str(target), create=True))
        assert isinstance(result, p.ProjectSessionOpenResult), result
        assert result.project.root == str(target.resolve())
        assert result.session
        state = await client.call(p.SessionState(result.session, project=result.project.key))
        assert isinstance(state, p.SessionStateResult), state
        assert state.view.get('turns', []) == []
        again = await client.call(p.ProjectSessionOpen(workspace=str(target), create=True))
        assert isinstance(again, p.ProjectSessionOpenResult), again
        assert again.session != result.session
        assert again.project.key == result.project.key
    finally:
        await client.close()
