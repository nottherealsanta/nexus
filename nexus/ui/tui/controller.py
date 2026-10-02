"""Compatibility import for the shared shell controller (PLAN §14.7)."""
from ...ui_support.session_controller import ModelEffortSelectionError, SessionController

TuiController = SessionController
__all__ = ["ModelEffortSelectionError", "TuiController"]
