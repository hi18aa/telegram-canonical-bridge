"""Hermes Agent Task Bridge 的核心模組。"""

from .config import hermes_machine_root, shared_state_path
from .state import BridgeState

__all__ = [
    "BridgeState",
    "hermes_machine_root",
    "shared_state_path",
]
