"""Compatibility module path for the daemon diagnostics implementation."""

import sys

from ..observability import daemon as _implementation

sys.modules[__name__] = _implementation
