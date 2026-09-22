"""Exception taxonomy shared across Nexus.

This module is the bottom of the dependency graph and imports nothing from the
rest of the package, so every other layer can depend on it.
"""


class NexusError(Exception):
    """Base class for all Nexus-originated errors."""


class ConfigError(NexusError, ValueError):
    """Configuration is invalid, unknown, or contradictory."""


class ProviderError(NexusError, RuntimeError):
    """A model/provider transport or protocol failure."""


class MalformedToolCall(ProviderError):
    """A provider stream produced tool-call arguments that are not valid JSON.

    The loop converts this into a ``tool_result(is_error=True)`` so the model can
    self-correct instead of failing the turn (plan section 3.3).
    """

    def __init__(self, tool_call_id: str, message: str, *, raw: str = ""):
        super().__init__(f"Malformed tool call {tool_call_id!r}: {message}")
        self.tool_call_id = tool_call_id
        self.raw = raw


class ToolError(NexusError):
    """A tool failed in a way the harness reports back to the model."""


class ExtensionError(NexusError):
    """An extension could not be loaded, validated, or swapped into a manifest."""


class ManagerClosed(ExtensionError, RuntimeError):
    """An extension manager was used after it was closed.

    A closed manager is terminal: no reload may start and no compare-and-swap
    may run, so a rebuild racing a close can never install a generation into a
    manager whose resources are being released.
    """


class ManifestError(ExtensionError, ValueError):
    """A manifest is malformed, or a manifest operation is invalid."""


class StaleGenerationError(ManifestError):
    """A swap was attempted with a generation older than the current one."""


class SessionError(NexusError, RuntimeError):
    """Session storage, locking, or format failure."""


class SessionBusy(SessionError):
    """The requested session is already locked by another turn."""


class BusClosed(NexusError, RuntimeError):
    """An event bus or subscription was used after it was closed."""


class OperationCancelled(NexusError):
    """Cooperative cancellation was requested at an await point."""
