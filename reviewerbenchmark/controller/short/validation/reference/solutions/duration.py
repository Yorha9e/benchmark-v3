"""Reference implementation for validation; not a leaderboard result."""

_UNITS = (("d", 86400000), ("h", 3600000), ("m", 60000), ("s", 1000), ("ms", 1))
_RANK = {unit: index for index, (unit, _) in enumerate(_UNITS)}
_LIMIT = {"h": 24, "m": 60, "s": 60, "ms": 1000}
_KEYS = ("days", "hours", "minutes", "seconds", "milliseconds")


class DurationParseError(ValueError):
    def __init__(self, code, position):
        self.code = code
        self.position = position
        super().__init__(f"{code} at position {position}")


def _fields(text, allow_overflow):
    if not isinstance(text, str):
        raise DurationParseError("type", 0)
    if not text:
        raise DurationParseError("empty", 0)
    fields = []
    position = 0
    previous = -1
    while position < len(text):
        start = position
        while position < len(text) and text[position].isdigit():
            position += 1
        if start == position:
            raise DurationParseError("syntax", position)
        digits = text[start:position]
        if len(digits) > 1 and digits[0] == "0":
            raise DurationParseError("leading_zero", start)
        unit_start = position
        if text.startswith("ms", position):
            unit = "ms"
            position += 2
        elif position < len(text) and text[position] in "dhms":
            unit = text[position]
            position += 1
        else:
            raise DurationParseError("syntax", position)
        rank = _RANK[unit]
        if rank <= previous:
            raise DurationParseError("order", unit_start)
        previous = rank
        value = int(digits)
        if not allow_overflow and unit in _LIMIT and value >= _LIMIT[unit]:
            raise DurationParseError("range", start)
        fields.append((unit, value))
    return fields


def parse_duration(text):
    values = dict.fromkeys(_KEYS, 0)
    key_for = dict(zip((unit for unit, _ in _UNITS), _KEYS))
    for unit, value in _fields(text, False):
        values[key_for[unit]] = value
    return values


def normalize_duration(text):
    total = 0
    for unit, value in _fields(text, True):
        total += value * dict(_UNITS)[unit]
    if total == 0:
        return "0s"
    pieces = []
    for unit, size in _UNITS:
        value, total = divmod(total, size)
        if value:
            pieces.append(f"{value}{unit}")
    return "".join(pieces)
