"""Public inference and replay entry points for DaVinci Prover."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .driver import ProverDriver
    from .episode import ProverEpisode

__all__ = ["ProverDriver", "ProverEpisode"]


def __getattr__(name: str):
    # Keep low-level policy modules importable without initializing the driver.
    if name == "ProverDriver":
        from .driver import ProverDriver

        return ProverDriver
    if name == "ProverEpisode":
        from .episode import ProverEpisode

        return ProverEpisode
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
