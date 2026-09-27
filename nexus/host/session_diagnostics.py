"""Compatibility module path for the session diagnostics implementation."""

import sys

from ..observability import session as _implementation

sys.modules[__name__] = _implementation
