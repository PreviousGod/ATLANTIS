from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class MemoryProviderInterface(ABC):
    """Abstract interface for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    @abstractmethod
    def system_prompt_block(self) -> str:
        pass

    @abstractmethod
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        pass

    @abstractmethod
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        pass

    @abstractmethod
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    @abstractmethod
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        pass

    @abstractmethod
    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, **kwargs) -> None:
        pass

    @abstractmethod
    def on_pre_compress(self, messages: list) -> str:
        pass

    @abstractmethod
    def on_session_end(self, messages: list) -> None:
        pass

    @abstractmethod
    def on_memory_write(self, action: str, target: str, content: str, metadata: Dict[str, Any] | None = None) -> None:
        pass

    @abstractmethod
    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs) -> None:
        pass

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        pass

    @abstractmethod
    def shutdown(self) -> None:
        pass


class ToolErrorInterface(ABC):
    """Abstract interface for tool error handling."""

    @abstractmethod
    def __call__(self, message: str) -> str:
        pass


class HermesAdapter:
    """Adapter for Hermes-specific implementations."""

    @staticmethod
    def get_memory_provider_base():
        """Get the Hermes MemoryProvider base class."""
        try:
            from agent.memory_provider import MemoryProvider
            return MemoryProvider
        except ImportError:
            return MemoryProviderInterface

    @staticmethod
    def get_tool_error():
        """Get the Hermes tool_error function."""
        try:
            from tools.registry import tool_error
            return tool_error
        except ImportError:
            return lambda msg: f'{{"error": "{msg}"}}'


__all__ = [
    "HermesAdapter",
    "MemoryProviderInterface",
    "ToolErrorInterface",
]
