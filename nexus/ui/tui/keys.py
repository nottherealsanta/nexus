"""Terminal key-protocol compatibility for the Nexus Textual shell.

The editor uses ``Enter`` to queue, ``Ctrl+Enter`` to steer, ``Alt+Enter``
to interrupt, and ``Shift+Enter`` to insert a newline. Whether a *terminal* can
report that distinction is a property of the terminal, not the app, so this
module exists to close the one gap Textual leaves open:

* **Kitty keyboard protocol** (CSI-u), used by kitty, WezTerm, foot, Ghostty and
  recent iTerm2. Shift+Enter arrives as ``ESC [ 13 ; 2 u``. Textual's
  Linux/macOS driver already negotiates this mode (``ESC [ > 25 u``) and its
  parser resolves the sequence to ``shift+enter``, so the app needs no help.
* **xterm ``modifyOtherKeys``**: Shift+Enter arrives as ``ESC [ 27 ; 2 ; 13 ~``.
  Textual neither enables the mode nor decodes its special keys (Enter becomes
  the unusable key ``"shift+\\r"``). This driver rewrites that form to the
  equivalent CSI-u form before Textual parses it, so an emulator already sending
  it (a user's ``.Xresources``, tmux ``extended-keys``, some terminals) works.
  The mode is deliberately *not* force-enabled: ``modifyOtherKeys=2`` re-encodes
  every shifted printable key, which Textual's parser then handles differently,
  and ``=1`` does not distinguish Shift+Enter. See the README limitations.
* **Legacy terminals** with neither protocol collapse Enter and Shift+Enter onto
  a bare carriage return. No decoder can separate those bytes; the shell offers
  ``Ctrl+J`` instead, the ``LF`` byte (``0x0A``) that every terminal reports
  distinctly from Enter's ``CR`` (``0x0D``).

The core app only depends on the canonical key *names*; the byte-level detail is
contained here and is never imported by non-terminal code paths.
"""

from __future__ import annotations

import re
import sys
from typing import Final

from textual._xterm_parser import XTermParser

#: xterm ``modifyOtherKeys`` special keys, keyed by the decimal codepoint xterm
#: sends as ``ESC [ 27 ; <modifier> ; <codepoint> ~``. Printable "other" keys are
#: intentionally excluded: Textual already decodes them (``ESC [ 27 ; 2 ; 70 ~``
#: becomes ``"F"``), while blindly rewriting them to CSI-u would drop the
#: associated text and break that path.
MODIFY_OTHER_KEYS_SPECIAL: Final[frozenset[int]] = frozenset({9, 13, 27, 127})

_RE_MODIFY_OTHER_KEYS: Final[re.Pattern[str]] = re.compile(r"\x1b\[27;(\d+);(\d+)~")


def normalize_modify_other_keys(sequence: str) -> str | None:
    """Translate an xterm ``modifyOtherKeys`` special key to its CSI-u form.

    Returns ``None`` for anything that is not a special-key ``ESC [ 27 ; m ; c ~``
    sequence, so the caller can fall through to Textual's own parser unchanged.
    """
    match = _RE_MODIFY_OTHER_KEYS.fullmatch(sequence)
    if match is None:
        return None
    if (codepoint := int(match.group(2))) not in MODIFY_OTHER_KEYS_SPECIAL:
        return None
    return f"\x1b[{codepoint};{match.group(1)}u"


class NexusXTermParser(XTermParser):
    """XTerm parser that also accepts the xterm ``modifyOtherKeys`` encoding."""

    def _parse_extended_key(self, sequence: str):
        normalized = normalize_modify_other_keys(sequence)
        return super()._parse_extended_key(normalized or sequence)


if sys.platform != "win32":
    from textual.drivers import linux_driver as _linux_driver

    class NexusDriver(_linux_driver.LinuxDriver):
        """Linux/macOS driver that installs :class:`NexusXTermParser`.

        Textual instantiates the parser inside ``run_input_thread`` from a module
        global, with no injection seam, so the parser is swapped for the duration
        of that (thread-local, app-lifetime) call. This keeps the override to a
        few lines instead of copying Textual's input loop.
        """

        parser_class: type[XTermParser] = NexusXTermParser

        def run_input_thread(self) -> None:
            previous = _linux_driver.XTermParser
            _linux_driver.XTermParser = self.parser_class
            try:
                super().run_input_thread()
            finally:
                _linux_driver.XTermParser = previous

else:  # pragma: no cover - Windows keeps Textual's own driver
    NexusDriver = None  # type: ignore[assignment,misc]


__all__ = [
    "MODIFY_OTHER_KEYS_SPECIAL",
    "NexusDriver",
    "NexusXTermParser",
    "normalize_modify_other_keys",
]
