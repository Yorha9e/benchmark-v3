"""Reference implementation for validation; not a leaderboard result."""

import math
from collections import OrderedDict


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


class BoundedTTLSet:
    def __init__(self, capacity, ttl, clock):
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity must be an int, not bool")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        ttl = _number(ttl, "ttl")
        if ttl < 0:
            raise ValueError("ttl must be nonnegative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._capacity = capacity
        self._ttl = ttl
        self._clock = clock
        self._entries = OrderedDict()

    def _now(self):
        return _number(self._clock(), "clock result")

    def _purge(self, now):
        expired = [key for key, deadline in self._entries.items() if now >= deadline]
        for key in expired:
            del self._entries[key]

    def add(self, value):
        now = self._now()
        self._purge(now)
        if value in self._entries:
            del self._entries[value]
        self._entries[value] = now + self._ttl
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def discard(self, value):
        now = self._now()
        self._purge(now)
        self._entries.pop(value, None)

    def __contains__(self, value):
        now = self._now()
        self._purge(now)
        return value in self._entries

    def __len__(self):
        now = self._now()
        self._purge(now)
        return len(self._entries)
