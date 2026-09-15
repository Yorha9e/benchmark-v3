"""Public contract from TASKS.md: deep recursive dict patch with DELETE sentinel."""

import copy


class _DeleteSentinel:
    """Identity-stable deletion marker. Returns self from deepcopy so the
    public ``DELETE`` remains unique even after nested deep copies."""

    __slots__ = ()

    def __deepcopy__(self, _memo):
        return self

    def __repr__(self):
        return "DELETE"


DELETE = _DeleteSentinel()


def _is_plain_dict(obj):
    return type(obj) is dict


def apply_patch(base, patch, delete=DELETE):
    if not _is_plain_dict(base):
        raise TypeError("base must be a plain dict")
    if not _is_plain_dict(patch):
        raise TypeError("patch must be a plain dict")

    # Deep copy of base detaches every built-in mutable container and
    # preserves the original key order for keys that survive the patch.
    result = copy.deepcopy(base)

    for key, patch_value in patch.items():
        if patch_value is delete:
            if key in result:
                del result[key]
            continue

        if key in result:
            old_value = result[key]
            if _is_plain_dict(old_value) and _is_plain_dict(patch_value):
                # Recursive merge only when both sides are plain dicts;
                # the inner call performs its own deepcopy so the merged
                # result shares no storage with either input.
                result[key] = apply_patch(old_value, patch_value, delete)
            else:
                # Whole-value replacement: deepcopy so the stored value
                # is independent of the patch object.
                result[key] = copy.deepcopy(patch_value)
        else:
            # Genuinely new key: append at the end and detach from patch.
            result[key] = copy.deepcopy(patch_value)

    return result
