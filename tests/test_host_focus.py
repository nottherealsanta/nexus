"""Foreground hints are bounded, explicit and expire independently of presence."""
from types import SimpleNamespace

from nexus.host.facade import HostFacade
from nexus.host import protocol as p
import msgspec
import pytest


def facade():
    f = object.__new__(HostFacade)
    f._foreground = {}
    f.runtime = SimpleNamespace(sessions=SimpleNamespace(exists=lambda s: s == 'root'))
    return f


def test_focus_wire_roundtrip():
    command = p.SessionFocus(session='root', client_id='native-1')
    assert msgspec.json.decode(msgspec.json.encode(command), type=p.Command) == command


def test_focus_is_bounded_and_explicit():
    f = facade()
    for i in range(100):
        f.report_focus(f'client-{i}', 'root')
    assert len(f._foreground) == 64
    assert 'client-99' in f.focused_clients('root')
    f.report_focus('client-99', '')
    assert 'client-99' not in f.focused_clients('root')
    with pytest.raises(ValueError):
        f.report_focus('bad id', 'root')
    with pytest.raises(ValueError):
        f.report_focus('client', 'unknown')
    f._foreground['stale'] = ('root', 0)
    assert 'stale' not in f.focused_clients('root')
