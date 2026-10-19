import pytest
from nexus.session.db import StateDatabase, SqliteSessionStore
from nexus.session.manager import SessionManager
from nexus.session.context_authz import ContextAuthorizer, ContextDenied
from nexus.session.context_port import ContextReadPort
from nexus.model.message import Message, Text
from nexus.events import Event

@pytest.fixture
def allocated(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    root = SessionManager(store=SqliteSessionStore(db, 'project', 'main'))
    agents = SessionManager(store=SqliteSessionStore(db, 'project', 'agents'))
    root.open('root').append_message(Message(role='user', content=[Text('root')]))
    def child(parent, owner_namespace='main', allow=True):
        _, logical = root.store.reserve_child_id(parent)
        stored = root.store.resolve_child_id(logical)
        root.store.grant_child_context_read(logical, allowed=allow)
        session = agents.open(stored, ownership={'kind':'agent', 'owner_namespace':owner_namespace,
            'owner_id':parent, 'logical_session_id':logical, 'agent_id':logical})
        session.append_message(Message(role='user', content=[Text('child text')]))
        return stored
    yield root, agents, child
    db.close()

def test_allocated_ancestry(allocated):
    root, agents, child = allocated
    a = child('root')
    b = child(a, 'agents')
    sibling = child('other')
    external = child('root', allow=False)
    descendant = child(external, 'agents')
    auth = ContextAuthorizer(root)
    for target in ['root', a, b]:
        auth.check('root', target)
    for target in [sibling, external, descendant, 'missing']:
        with pytest.raises(ContextDenied): auth.check('root', target)
    with pytest.raises(ContextDenied): auth.check(a, 'root')
    with pytest.raises(ContextDenied): ContextAuthorizer(root, max_depth=1).check('root', b)
    agents.open(a).append_event(Event(type='turn.completed', data={}))
    auth.check('root', a)
    assert ContextReadPort(root).messages('root', a)['data'][0]['text'] == 'child text'
    conn = root.store.db._connection()
    conn.execute("UPDATE sessions SET trash_id='trash' WHERE id=?", (a,))
    with pytest.raises(ContextDenied): auth.check('root', b)
    conn.execute('DELETE FROM sessions WHERE id=?', (a,))
    with pytest.raises(ContextDenied): auth.check('root', b)

def test_forged_event_and_project(allocated):
    root, agents, child = allocated
    a = child('root', allow=False)
    root.open('root').append_event(Event(type='agent.spawned', data={'id':a,'context_read':True}))
    with pytest.raises(ContextDenied): ContextAuthorizer(root).check('root', a)
    other = SessionManager(store=SqliteSessionStore(root.store.db, "elsewhere", "main"))
    other.open("foreign").append_message(Message(role="user", content=[Text("foreign")]))
    with pytest.raises(ContextDenied): ContextAuthorizer(root).check("root", "foreign")
    with pytest.raises(ContextDenied): ContextAuthorizer(root).check('root', a)


# -- plan 03 §3.2 / §7 test 3: the authorization matrix ----------------------

def _allowed(auth, caller, target):
    try:
        auth.check(caller, target)
        return True
    except ContextDenied:
        return False


def test_authz_matrix(allocated):
    root, agents, child = allocated
    child_a = child('root')
    child_b = child('root')                       # sibling of child_a
    grand = child(child_a, 'agents')
    external = child('root', allow=False)         # external worker: edge not opted in
    under_external = child(external, 'agents')    # opted-in edge below a denied edge
    fork = root.fork('root').id
    auth = ContextAuthorizer(root)
    # allowed
    assert _allowed(auth, 'root', 'root')                 # self
    assert _allowed(auth, child_a, child_a)               # a child reads itself
    assert _allowed(auth, 'root', child_a)                # child
    assert _allowed(auth, 'root', grand)                  # grandchild
    assert _allowed(auth, child_a, grand)                 # child reads its own child
    # denied
    assert not _allowed(auth, child_a, child_b)           # sibling
    assert not _allowed(auth, child_a, 'root')            # parent (upward)
    assert not _allowed(auth, grand, 'root')
    assert not _allowed(auth, 'root', fork)               # fork is lineage, not supervision
    assert not _allowed(auth, fork, 'root')
    assert not _allowed(auth, 'root', external)           # external worker, no context_read
    assert not _allowed(auth, 'root', under_external)     # a denied edge on the path
    assert not _allowed(auth, 'root', 'does-not-exist')
    # depth limit counts edges
    assert _allowed(ContextAuthorizer(root, max_depth=2), 'root', grand)
    assert not _allowed(ContextAuthorizer(root, max_depth=1), 'root', grand)
    assert _allowed(ContextAuthorizer(root, max_depth=1), 'root', child_a)
    assert _allowed(ContextAuthorizer(root, max_depth=0), 'root', 'root')
    assert not _allowed(ContextAuthorizer(root, max_depth=0), 'root', child_a)


def test_ended_edge_and_trash_deny_immediately(allocated):
    root, agents, child = allocated
    a = child('root')
    b = child(a, 'agents')
    auth = ContextAuthorizer(root)
    assert _allowed(auth, 'root', b)
    # The grant is revoked (edge ended): both hops that depended on it stop.
    root.store.grant_child_context_read(root.store.db._connection().execute(
        "SELECT logical_id FROM child_allocations WHERE stored_id=?", (a,)).fetchone()[0], allowed=False)
    assert not _allowed(auth, 'root', a)
    assert not _allowed(auth, 'root', b)
    assert _allowed(auth, a, b)
    # Trashing the target (or any ancestor on the path) denies, even for self.
    root.store.db._connection().execute("UPDATE sessions SET trash_id='t' WHERE id=?", (b,))
    assert not _allowed(auth, a, b)


def test_managed_root_is_not_readable_by_the_manager(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    root = SessionManager(store=SqliteSessionStore(db, 'project', 'main'))
    root.open('nexus-manager', ownership={'kind': 'manager'}).append_message(Message(role='user', content=[Text('m')]))
    root.open('launched', ownership={'owner_namespace': 'main', 'owner_id': 'nexus-manager'}).append_message(
        Message(role='user', content=[Text('r')]))
    auth = ContextAuthorizer(root)
    assert not _allowed(auth, 'nexus-manager', 'launched')
    assert not _allowed(auth, 'launched', 'nexus-manager')
    db.close()


def test_cross_project_denied_even_with_matching_edges(allocated):
    root, agents, child = allocated
    other_root = SessionManager(store=SqliteSessionStore(root.store.db, 'elsewhere', 'main'))
    other_root.open('foreign-root').append_message(Message(role='user', content=[Text('f')]))
    auth = ContextAuthorizer(root)
    assert not _allowed(auth, 'root', 'foreign-root')
    assert not _allowed(auth, 'foreign-root', 'root')
