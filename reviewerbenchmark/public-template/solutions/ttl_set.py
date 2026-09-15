"""Public contract from TASKS.md: capacity- and TTL-bounded set."""

import collections
import math


def _validate_finite_number(value, name):
    """Reject bool, NaN, infinities, and non-numeric values."""
    if isinstance(value, bool):
        raise TypeError("{0} must be a finite number, got bool".format(name))
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("{0} must be a finite number".format(name))
    raise TypeError("{0} must be a finite number".format(name))


class BoundedTTLSet:
    def __init__(self, capacity, ttl, clock):
        # capacity: int, not bool, strictly positive.
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity must be a positive int")
        if capacity <= 0:
            raise ValueError("capacity must be a positive int")
        # ttl: finite int/float, not bool, nonnegative.
        validated_ttl = _validate_finite_number(ttl, "ttl")
        if validated_ttl < 0:
            raise ValueError("ttl must be nonnegative")
        # clock: callable, and we read+validate it once to fail fast.
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._capacity = capacity
        self._ttl = validated_ttl
        self._clock = clock
        # The single storage structure: insertion-ordered map of the actual
        # key object to its absolute deadline. No tombstones, no second
        # history queue — every purge/eviction/drop removes the reference
        # from this dict so stale objects can be reclaimed.
        self._entries = collections.OrderedDict()
        # Normalize state and validate the initial clock reading.
        self._purge()

    def _read_now(self):
        return _validate_finite_number(self._clock(), "clock()")

    def _purge(self):
        if not self._entries:
            return
        now = self._read_now()
        # The clock may be non-monotonic, so a later-inserted key may
        # expire before an earlier one. Scan every live entry.
        expired = [k for k, deadline in self._entries.items() if now >= deadline]
        for key in expired:
            del self._entries[key]

    def add(self, value):
        self._purge()
        # When an equal key is already present, delete the old mapping
        # explicitly so the previous object reference is released before
        # the new one is stored. ``move_to_end`` alone would not drop the
        # stale object.
        if value in self._entries:
            del self._entries[value]
        now = self._read_now()
        self._entries[value] = now + self._ttl
        # Capacity evicts the oldest live insertion. While-loop covers the
        # pathological case where a previously-purged state could let the
        # size briefly exceed the limit; in normal flow len is at most
        # capacity + 1 here.
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def discard(self, value):
        self._purge()
        if value in self._entries:
            del self._entries[value]

    def __contains__(self, value):
        self._purge()
        return value in self._entries

    def __len__(self):
        self._purge()
        return len(self._entries)
