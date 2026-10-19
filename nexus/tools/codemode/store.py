"""Transactional event-reduced JSON store, without interpreter serialization.

Plan 03 section 3.7. Writes are buffered in ``pending`` and become one
``code.store`` event only when the program succeeds. Values are plain JSON that
never alias sandbox objects (they cross the IPC boundary serialized), so sizes
are tracked incrementally instead of deep-copying the whole store per call.
"""
from __future__ import annotations

import json

from .ipc import validate

MAX_KEY = 128
MAX_VALUE_BYTES = 65536
MAX_TOTAL_BYTES = 524288


def size(value):
    return len(json.dumps(validate(value), ensure_ascii=True, allow_nan=False).encode())


def _parts(event):
    if isinstance(event, dict):
        return event.get('type'), event.get('data', {})
    return getattr(event, 'type', None), getattr(event, 'data', {})


class Store:
    def __init__(self, events=(), *, values=None):
        self.values = {}
        self._sizes = {}
        self._total = 0
        self.pending = {}
        self.deleted = []
        if values:
            self.apply({'set': values})
        for event in events:
            kind, data = _parts(event)
            if kind == 'code.store':
                self.apply(data)

    @staticmethod
    def key(key):
        if not isinstance(key, str) or not key or len(key) > MAX_KEY:
            raise ValueError('store key must contain 1..128 characters')

    def apply(self, data):
        """Validate then commit one set/delete batch atomically."""
        deletes = list(data.get('delete', []))
        sets = dict(data.get('set', {}))
        total = self._total
        for key in deletes:
            self.key(key)
            total -= self._sizes.get(key, 0)
        new_sizes = {}
        for key, value in sets.items():
            self.key(key)
            new_sizes[key] = size(value)
            if new_sizes[key] > MAX_VALUE_BYTES:
                raise ValueError('store value exceeds 64 KiB')
        # Sets apply after deletes, so a key in both ends up set.
        for key, length in new_sizes.items():
            total += length - (0 if key in deletes else self._sizes.get(key, 0))
        if total > MAX_TOTAL_BYTES:
            raise ValueError('session store exceeds 512 KiB')
        for key in deletes:
            self.values.pop(key, None)
            self._sizes.pop(key, None)
        for key, value in sets.items():
            self.values[key] = value
            self._sizes[key] = new_sizes[key]
        self._total = total

    def _check_pending(self, set_, delete):
        # Validate against current totals without committing.
        total = self._total
        for key in delete:
            total -= self._sizes.get(key, 0)
        for key, value in set_.items():
            length = size(value)
            if length > MAX_VALUE_BYTES:
                raise ValueError('store value exceeds 64 KiB')
            total += length - (0 if key in delete else self._sizes.get(key, 0))
        if total > MAX_TOTAL_BYTES:
            raise ValueError('session store exceeds 512 KiB')

    def store(self, key, value):
        self.key(key)
        sets = {**self.pending, key: value}
        self._check_pending(sets, self.deleted)
        self.pending = sets

    def delete(self, key):
        self.key(key)
        self.pending.pop(key, None)
        if key not in self.deleted:
            self.deleted.append(key)

    def load(self, key):
        self.key(key)
        if key in self.pending:
            return self.pending[key]
        if key in self.deleted:
            return None
        return self.values.get(key)

    def dirty(self):
        return bool(self.pending or self.deleted)

    def event(self, call_id):
        deleted = [k for k in self.deleted if k not in self.pending]
        return {'call_id': call_id, 'set': dict(self.pending), 'delete': deleted}
