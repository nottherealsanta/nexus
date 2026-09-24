"""Optional Textual shell for the daemon-backed Nexus client.

Textual and Rich imports are deliberately confined to this package. Importing
``nexus.ui.tui`` itself remains safe when the ``tui`` extra is not installed.
"""

from __future__ import annotations

__all__: list[str] = []
