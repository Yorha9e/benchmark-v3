"""Peak resident-memory probe based on :mod:`tracemalloc`."""

from __future__ import annotations

import tracemalloc
from collections.abc import Callable
from typing import Any


class MemoryProbe:
    """Context manager sampling peak traced memory in bytes.

    Uses :mod:`tracemalloc` so it works anywhere with the standard
    library and nests safely (only the outermost probe starts/stops
    tracing).
    """

    def __init__(self) -> None:
        self.peak_bytes: int = 0
        self._started_here = False

    def __enter__(self) -> MemoryProbe:
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            self._started_here = True
        else:
            tracemalloc.reset_peak()
            self._started_here = False
        return self

    def __exit__(self, *exc_info: Any) -> None:
        _, peak = tracemalloc.get_traced_memory()
        self.peak_bytes = peak
        if self._started_here:
            tracemalloc.stop()
            self._started_here = False

    @property
    def peak_mb(self) -> float:
        return self.peak_bytes / (1024 * 1024)

    @staticmethod
    def measure(func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, int]:
        """Run ``func`` and return ``(result, peak_bytes)``."""
        with MemoryProbe() as probe:
            result = func(*args, **kwargs)
        return result, probe.peak_bytes

    def to_dict(self) -> dict[str, Any]:
        return {"peak_bytes": self.peak_bytes, "peak_mb": round(self.peak_mb, 4)}
