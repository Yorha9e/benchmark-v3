"""Public contract from TASKS.md: duration string parser and normalizer."""

# Unit metadata. Ranks increase as units get smaller so that the required
# strict ascending rank order matches the d,h,m,s,ms ordering constraint.
_UNIT_ORDER = ("d", "h", "m", "s", "ms")
_UNIT_RANK = {u: i for i, u in enumerate(_UNIT_ORDER)}
_UNIT_MS = {"d": 86400000, "h": 3600000, "m": 60000, "s": 1000, "ms": 1}
# None marks the unbounded unit (days).
_UNIT_LIMIT = {"d": None, "h": 24, "m": 60, "s": 60, "ms": 1000}
_UNIT_TO_KEY = {
    "d": "days",
    "h": "hours",
    "m": "minutes",
    "s": "seconds",
    "ms": "milliseconds",
}
_OUTPUT_KEYS = ("days", "hours", "minutes", "seconds", "milliseconds")


class DurationParseError(ValueError):
    """Raised for any invalid input. ``code`` and ``position`` are stable."""

    def __init__(self, code, position, message=None):
        self.code = code
        self.position = position
        if message is None:
            message = "invalid duration at {0}: {1}".format(position, code)
        super().__init__(message)


def _scan_fields(text):
    """Tokenize ``text`` into a list of (value, unit, digit_start, unit_start).

    Raises :class:`DurationParseError` with a stable code/position for any
    structural problem. Subordinate range checks are intentionally absent so
    :func:`normalize_duration` can reuse this scanner.
    """

    if not isinstance(text, str):
        raise DurationParseError("type", 0)
    if text == "":
        raise DurationParseError("empty", 0)

    n = len(text)
    fields = []
    i = 0
    last_rank = -1

    while i < n:
        # Digits phase. Use explicit ASCII range to avoid Unicode digits.
        digit_start = i
        ch = text[i]
        if not ("0" <= ch <= "9"):
            raise DurationParseError("syntax", i)
        if ch == "0":
            # Multi-digit with leading zero is forbidden; the single 0 is OK.
            if i + 1 < n and "0" <= text[i + 1] <= "9":
                raise DurationParseError("leading_zero", i)
            value = 0
            i += 1
        else:
            j = i
            while j < n and "0" <= text[j] <= "9":
                j += 1
            value = int(text[i:j])
            i = j

        # Unit phase. Try the two-character unit first so we never split
        # an "ms" into an "m" + orphan "s".
        unit_start = i
        if i + 1 < n and text[i] == "m" and text[i + 1] == "s":
            unit = "ms"
            i += 2
        elif i < n and text[i] in _UNIT_RANK:
            unit = text[i]
            i += 1
        else:
            # No unit available: either we ran off the end (i == n) or the
            # next character is something else. ``i`` is the correct zero-
            # based position in both cases (len(text) for unexpected end).
            raise DurationParseError("syntax", i)

        rank = _UNIT_RANK[unit]
        if rank <= last_rank:
            raise DurationParseError("order", unit_start)
        last_rank = rank
        fields.append((value, unit, digit_start, unit_start))

    if not fields:
        # Defensive: empty input is handled above, but keep the contract
        # explicit so this function never returns an empty list.
        raise DurationParseError("empty", 0)
    return fields


def parse_duration(text):
    """Parse ``text`` into a plain dict in fixed key order."""
    fields = _scan_fields(text)
    result = {key: 0 for key in _OUTPUT_KEYS}
    for value, unit, digit_start, _unit_start in fields:
        limit = _UNIT_LIMIT[unit]
        if limit is not None and value >= limit:
            raise DurationParseError("range", digit_start)
        result[_UNIT_TO_KEY[unit]] = value
    return result


def _format_components(components):
    parts = []
    for key, unit in zip(_OUTPUT_KEYS, _UNIT_ORDER):
        value = components[key]
        if value:
            parts.append("{0}{1}".format(value, unit))
    if not parts:
        return "0s"
    return "".join(parts)


def normalize_duration(text):
    """Parse and convert ``text`` to its shortest canonical form.

    Subordinate range violations are accepted and carried into larger units.
    Syntax rules are unchanged.
    """
    fields = _scan_fields(text)
    total_ms = 0
    for value, unit, _ds, _us in fields:
        total_ms += value * _UNIT_MS[unit]
    days, rem = divmod(total_ms, 86400000)
    hours, rem = divmod(rem, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, ms = divmod(rem, 1000)
    return _format_components(
        {
            "days": days,
            "hours": hours,
            "minutes": minutes,
            "seconds": seconds,
            "milliseconds": ms,
        }
    )
