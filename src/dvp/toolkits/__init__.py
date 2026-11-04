"""Model-visible tools and their trusted workspace handlers."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dvp.toolkits.registry import ToolRegistry

__all__ = ["ToolRegistry"]


def __getattr__(name: str):
    if name == "ToolRegistry":
        from dvp.toolkits.registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
