"""Reference implementation for validation; not a leaderboard result."""

import copy

DELETE = object()


def _clone(value, sentinel):
    if type(value) is object:
        return value
    return copy.deepcopy(value, {id(sentinel): sentinel})


def _merge(base, patch, sentinel):
    result = {}
    for key, value in base.items():
        result[_clone(key, sentinel)] = _clone(value, sentinel)
    for key, value in patch.items():
        if value is sentinel:
            result.pop(key, None)
        elif key in base and type(base[key]) is dict and type(value) is dict:
            result[key] = _merge(base[key], value, sentinel)
        else:
            result[key] = _clone(value, sentinel)
    return result


def apply_patch(base, patch, delete=DELETE):
    if type(base) is not dict or type(patch) is not dict:
        raise TypeError("base and patch must be plain dicts")
    return _merge(base, patch, delete)
