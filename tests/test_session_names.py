import multiprocessing
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import msgspec
import pytest

from nexus.errors import SessionError
from nexus.model.message import Message, Text
from nexus.session import db as db_mod
from nexus.session import export as export_mod
from nexus.session import names
from nexus.session.records import SessionRecord
from nexus.session.db import SqliteSessionStore, StateDatabase
from nexus.session.names import ADJECTIVES, ANIMALS, BASE_NAME_CAPACITY, COLORS, NAMES_VERSION, valid_name


def test_names_are_unique_immutable_and_ascii(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    def create(i):
        store.create(str(i))
        return store.session_row(str(i))['name']
    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(create, range(40)))
    assert len(set(names)) == 40
    assert all(valid_name(name) for name in names)
    store.create('0')
    assert store.session_row('0')['name'] == names[0]
    assert store.resolve_name(names[0]) == '0'
    assert store.resolve_name('0') == '0'
    other = SqliteSessionStore(db, 'other')
    with pytest.raises(SessionError):
        other.resolve_name(names[0])
    for value in ('CALM-blue-fox', 'calm-blue-foх', 'calm-blue-fox\n', '', '../calm-blue-fox'):
        assert not valid_name(value)


def test_retirement_and_exact_id_precedence(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    store.create('first')
    name = store.session_row('first')['name']
    store.create(name)
    assert store.resolve_name(name) == name
    store.delete_row('first')
    store.create('first')
    assert store.session_row('first')['name'] != name


def test_vocabulary_capacity_and_durable_named_event(tmp_path):
    assert BASE_NAME_CAPACITY == len(ADJECTIVES) * len(COLORS) * len(ANIMALS) >= 1_000_000
    assert NAMES_VERSION == 2
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    store.create('named')
    # The name lives in the row, not the log: no record is appended, seqs stay stable.
    assert store.read('named').records == ()
    row = store.session_row('named')
    assert valid_name(row['name']) and row['last_seq'] == 0 and row['last_activity'] == 0
    db.close()


# -- word-list lint ---------------------------------------------------------

_BLOCKED_WORDS = {
    'sudo', 'kill', 'exec', 'rm', 'drop', 'root', 'admin', 'null', 'true', 'false', 'delete', 'test',
    'stop', 'open', 'cat', 'ls', 'chmod', 'eval', 'python', 'ruby', 'swift', 'llama', 'jaguar', 'puma',
    'orange', 'apple', 'gray', 'grey', 'lynx', 'gecko', 'panda', 'dolphin', 'falcon',
}
_BLOCKED_STEMS = (
    'ass', 'cock', 'coon', 'nig', 'fag', 'tit', 'rape', 'sex', 'hell', 'damn', 'piss', 'crap', 'dick',
    'butt', 'anal', 'spic', 'kik', 'slut', 'shit', 'fuck', 'whor', 'porn', 'nazi', 'gook', 'chink',
    'dyke', 'homo', 'gay', 'poo', 'pee', 'cum', 'tard',
)


def test_word_lists_lint():
    all_words = [w for words in (ADJECTIVES, COLORS, ANIMALS) for w in words]
    assert len(all_words) == len(set(all_words)), 'duplicates within or across lists'
    for w in all_words:
        assert re.fullmatch(r'[a-z]{3,9}', w, re.ASCII), w
        assert w not in _BLOCKED_WORDS, w
        assert not any(stem in w for stem in _BLOCKED_STEMS), w
    # Sizing and the longest possible name (+ '-99') still fits the 32 character limit.
    assert len(ADJECTIVES) >= 200 and len(COLORS) >= 40 and len(ANIMALS) >= 150
    longest = sum(max(map(len, words)) for words in (ADJECTIVES, COLORS, ANIMALS)) + 2 + 3
    assert longest <= 32


def test_format_and_valid_name():
    for _ in range(200):
        assert valid_name(names.draw_name())
    assert valid_name('quiet-cobalt-otter') and valid_name('quiet-cobalt-otter-2') and valid_name('quiet-cobalt-otter-99')
    for bad in ('quiet-cobalt', 'quiet-cobalt-otter-1', 'quiet-cobalt-otter-100', 'quiet-cobalt-otter-02',
                'qu-cobalt-otter', 'quiet_cobalt_otter', ' quiet-cobalt-otter', 'quiet-cobalt-otterxxxxxxxxxxxxxxxxxxxxxx', None, 7):
        assert not valid_name(bad)


# -- collisions: redraw, suffix, loud failure -----------------------------------


def _tiny_vocabulary(monkeypatch):
    monkeypatch.setattr(names, 'ADJECTIVES', ('aaa',))
    monkeypatch.setattr(names, 'COLORS', ('bbb',))
    monkeypatch.setattr(names, 'ANIMALS', ('ccc',))


def test_forced_collisions_redraw_then_suffix_then_fail(tmp_path, monkeypatch):
    _tiny_vocabulary(monkeypatch)
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    store.create('s0')
    assert store.session_row('s0')['name'] == 'aaa-bbb-ccc'
    for index in range(1, 99):
        store.create(f's{index}')
    got = [store.session_row(f's{i}')['name'] for i in range(99)]
    assert got[1:] == [f'aaa-bbb-ccc-{n}' for n in range(2, 100)]
    assert all(valid_name(n) for n in got)
    with pytest.raises(SessionError, match='exhausted'):
        store.create('overflow')
    assert not store.row_exists('overflow')  # the failed allocation rolled back the row
    assert db._connection().execute('SELECT count(*) FROM session_names').fetchone()[0] == 99
    db.close()


def test_retired_names_are_never_reused(tmp_path, monkeypatch):
    _tiny_vocabulary(monkeypatch)
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    store.create('a')
    assert store.session_row('a')['name'] == 'aaa-bbb-ccc'
    store.delete_row('a')
    row = db._connection().execute("SELECT retired_at FROM session_names WHERE name='aaa-bbb-ccc'").fetchone()
    assert row is not None and row[0] is not None  # kept, retired
    store.create('b')
    assert store.session_row('b')['name'] == 'aaa-bbb-ccc-2'
    # Purge of a trashed session retires its name too.
    store.trash('b', reason='test', retention_seconds=0)
    trash_id = store_trash_id(db)
    assert store.purge_expired(now=time.time() + 10) == [trash_id]
    store.create('c')
    assert store.session_row('c')['name'] == 'aaa-bbb-ccc-3'
    retired = db._connection().execute('SELECT count(*) FROM session_names WHERE retired_at IS NOT NULL').fetchone()[0]
    assert retired == 2
    db.close()


def store_trash_id(db):
    return db._connection().execute('SELECT trash_id FROM sessions WHERE trash_id IS NOT NULL').fetchone()[0]


# -- two real processes ---------------------------------------------------------


def _worker(path, project, prefix, count):
    db = StateDatabase(Path(path))
    store = SqliteSessionStore(db, project)
    out = []
    for i in range(count):
        store.create(f'{prefix}{i}')
        out.append(store.session_row(f'{prefix}{i}')['name'])
    db.close()
    return out


def test_uniqueness_across_two_processes(tmp_path):
    path = tmp_path / 'state.db'
    StateDatabase(path).close()
    ctx = multiprocessing.get_context('spawn')
    with ctx.Pool(2) as pool:
        first = pool.apply_async(_worker, (str(path), 'p1', 'a', 60))
        second = pool.apply_async(_worker, (str(path), 'p2', 'b', 60))
        names_seen = first.get(120) + second.get(120)
    assert len(names_seen) == 120 and len(set(names_seen)) == 120
    db = StateDatabase(path)
    assert db._connection().execute('SELECT count(*), count(DISTINCT name) FROM session_names').fetchone()[:] == (120, 120)
    db.close()


# -- replay / export / backfill -----------------------------------------------------


def test_export_carries_name_and_import_keeps_it_when_free(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    store.create('named')
    store.append_message('named', Message(role='user', content=[Text(text='hi')]))
    row = store.session_row('named')
    exported = export_mod.render('named', store.read('named'), format='jsonl', meta={'name': row['name']})
    first = exported.splitlines()[0]
    assert '"session.named"' in first and row['name'] in first
    assert exported.count('session.named') == 1
    # Import into a fresh database keeps the exported name.
    other_db = StateDatabase(tmp_path / 'other.db')
    other = SqliteSessionStore(other_db, 'project')
    records = [msgspec.json.decode(line, type=SessionRecord) for line in exported.splitlines()]
    other.create_from_records('copy', records)
    assert other.session_row('copy')['name'] == row['name']
    # Name taken (here: same DB): a fresh name is allocated, never a duplicate.
    store.create_from_records('copy2', records)
    assert valid_name(store.session_row('copy2')['name']) and store.session_row('copy2')['name'] != row['name']
    other_db.close()
    db.close()


def test_backfill_names_in_bounded_deterministic_batches_and_is_idempotent(tmp_path, monkeypatch):
    db = StateDatabase(tmp_path / 'state.db')
    db.touch_project('project', str(tmp_path))
    conn = db._connection()
    total = 1203
    rows = [('project', 'main', f's{i:05d}', 40.0, 0, 50.0) for i in range(total)]
    conn.execute('BEGIN IMMEDIATE')
    conn.executemany('INSERT INTO sessions(project_id,namespace,id,created_at,last_seq,last_activity) VALUES(?,?,?,?,?,?)', rows)
    conn.execute('COMMIT')
    statements = []

    def factory():
        c = db._raw_connect()
        c.set_trace_callback(statements.append)
        return c

    assert db_mod._backfill_names(factory) == total
    begins = [s for s in statements if s.startswith('BEGIN')]
    assert len(begins) == 3  # 500 + 500 + 203, one transaction each
    selects = [s for s in statements if 'name IS NULL' in s]
    assert all('ORDER BY project_id,namespace,id' in s for s in selects)
    unnamed = conn.execute('SELECT count(*) FROM sessions WHERE name IS NULL').fetchone()[0]
    assert unnamed == 0
    assert conn.execute('SELECT count(*), count(DISTINCT name) FROM session_names').fetchone()[:] == (total, total)
    # Recency is preserved and no record is appended (the name lives in the row).
    assert conn.execute('SELECT count(*) FROM sessions WHERE last_activity<>50.0 OR created_at<>40.0 OR last_seq<>0').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM records').fetchone()[0] == 0
    before = conn.execute('SELECT group_concat(name) FROM (SELECT name FROM sessions ORDER BY id)').fetchone()[0]
    statements.clear()
    assert db_mod._backfill_names(factory) == 0  # idempotent: nothing left, nothing rewritten
    assert conn.execute('SELECT group_concat(name) FROM (SELECT name FROM sessions ORDER BY id)').fetchone()[0] == before
    assert conn.execute('SELECT count(*) FROM records').fetchone()[0] == 0
    db.close()
    StateDatabase(tmp_path / 'state.db').close()  # reopening runs the migration/backfill path again


def test_project_session_query_matches_names(tmp_path):
    db = StateDatabase(tmp_path / 'state.db')
    store = SqliteSessionStore(db, 'project')
    store.create('one')
    store.append_message('one', Message(role='user', content=[Text(text='hello')]))
    name = store.session_row('one')['name']
    db.touch_project('project', str(tmp_path))
    fragment = name.split('-')[1]
    hits = db.project_sessions(query=fragment)
    assert [h['id'] for h in hits] == ['one'] and hits[0]['name'] == name
    assert db.project_sessions(query='zzzz-nothing') == []
    db.close()
