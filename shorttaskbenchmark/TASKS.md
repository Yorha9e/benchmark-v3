# Public tasks

Implement each file in `solutions/` using only the Python standard library. Do not rename public objects. Public smoke tests cover basic contracts only; official criteria are hidden from a submission.

After finishing, create `instruction_ack.json` beside `solutions/`:

```json
{"completed_tasks":["recursive_patch","dependency_layers","ttl_set","duration"],"ack":"I followed TASKS.md and changed only solution files."}
```

## 1. `recursive_patch.py`

Export `DELETE` (a unique sentinel) and `apply_patch(base, patch, delete=DELETE)`. `base` and `patch` must be plain `dict` objects (not subclasses). A patch value is a deletion only when it *is* `delete`. Recursively merge only where both old and patch values are plain dicts; otherwise replace the entire value. Return a fully detached result: no mutable container in the result aliases either input, and inputs are unchanged. Preserve deterministic dict order: retained base keys keep their positions and genuinely new keys follow patch encounter order, recursively and across replacement boundaries.

## 2. `dependency_layers.py`

Export `DependencyCycleError(ValueError)` with a `.nodes` tuple and `dependency_layers(edges)`. `edges` is a one-shot iterable of `(node, dependency)` pairs; consume it exactly once. Return `list[list[object]]`, with dependencies in earlier layers than dependents. Include nodes appearing only in the dependency position. Deduplicate repeated edges. Within a layer, order nodes by first appearance anywhere in the edge stream. Raise `DependencyCycleError` only for a real directed cycle, with remaining cyclic/blocked nodes in stable first-appearance order. Handle very deep acyclic inputs without recursion.

## 3. `ttl_set.py`

Export `BoundedTTLSet(capacity, ttl, clock)`. `capacity` must be an `int` but not `bool`, and be positive. `ttl` and every clock result must be finite `int`/`float` but not `bool`; `ttl` must be nonnegative. `clock` must be callable. Provide `add(value)`, `discard(value)`, `__contains__`, and `__len__`. Expiration occurs exactly when `clock() >= deadline`. Purge expired entries before observable operations. Capacity evicts the oldest live insertion; adding an existing equal key replaces its stored object, deadline, and insertion position. Deletion, replacement, and expiration must not retain stale object references.

## 4. `duration.py`

Export `DurationParseError(ValueError)` with stable `.code` and `.position` attributes, `parse_duration(text)`, and `normalize_duration(text)`. Grammar is one or more adjacent integer-unit fields in strict descending unit order `d`, `h`, `m`, `s`, `ms`; fields cannot repeat, contain whitespace/signs/decimals, or use leading zeroes (except the single value `0`). For `parse_duration`, subordinate ranges are `h<24`, `m<60`, `s<60`, `ms<1000`; days are unbounded. It returns a plain dict containing all keys in the fixed order `days`, `hours`, `minutes`, `seconds`, `milliseconds`. `normalize_duration` accepts syntactically valid out-of-range subordinate fields and converts them to the shortest canonical equivalent, carrying overflow into larger units, omitting zero fields except canonical zero is `0s`. Normalization is idempotent. All other invalid input raises `DurationParseError` deterministically; use codes `type`, `empty`, `syntax`, `leading_zero`, `order`, or `range` and a zero-based error position.
