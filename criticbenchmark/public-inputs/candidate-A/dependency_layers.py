"""Public contract from TASKS.md: layered topological sort with cycle detection."""


class DependencyCycleError(ValueError):
    """Raised when the dependency graph contains a real directed cycle.

    ``.nodes`` is an immutable tuple of the remaining cyclic and blocked
    nodes in stable first-appearance order.
    """

    def __init__(self, nodes):
        self.nodes = tuple(nodes)
        message = "dependency cycle detected involving: {0}".format(
            ", ".join(repr(n) for n in self.nodes)
        )
        super().__init__(message)


def dependency_layers(edges):
    """Return a layered topological ordering from a one-shot edge iterable.

    Each pair ``(node, dependency)`` means ``node`` depends on ``dependency``;
    ``dependency`` therefore appears in an earlier layer than ``node``. Nodes
    that appear only in the dependency position are still included. The
    iterable is consumed exactly once.
    """

    # First-appearance registry. ``_id_of`` assigns each unique node value
    # a monotonically increasing int id and stores the first-seen object so
    # the caller observes the values they passed in. A hash-bucket fast path
    # covers hashable values; unhashable values fall back to a linear scan
    # over a small list of id/object pairs.
    next_id = 0
    nodes_by_id = []  # type: list[object]
    hash_buckets = {}  # type: dict[int, list[int]]
    unhashable_ids = []  # type: list[int]
    unhashable_nodes = []  # type: list[object]

    def _id_of(value):
        nonlocal next_id
        try:
            bucket = hash_buckets.setdefault(value, [])
        except TypeError:
            for idx, candidate in zip(unhashable_ids, unhashable_nodes):
                if candidate is value or candidate == value:
                    return idx
            new_id = next_id
            next_id += 1
            unhashable_ids.append(new_id)
            unhashable_nodes.append(value)
            nodes_by_id.append(value)
            return new_id
        for existing in bucket:
            stored = nodes_by_id[existing]
            if stored is value or stored == value:
                return existing
        new_id = next_id
        next_id += 1
        bucket.append(new_id)
        nodes_by_id.append(value)
        return new_id

    indegree = []  # type: list[int]
    dependents = []  # type: list[list[int]]
    seen_edges = set()  # type: set[tuple[int, int]]

    # Single pass over the input iterable. Per pair, register ``node`` first
    # so a node that appears in both positions keeps the earlier slot.
    for node, dependency in edges:
        node_id = _id_of(node)
        dependency_id = _id_of(dependency)
        edge = (node_id, dependency_id)
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        # Grow adjacency structures to cover both endpoints.
        while len(indegree) < node_id + 1:
            indegree.append(0)
            dependents.append([])
        while len(indegree) < dependency_id + 1:
            indegree.append(0)
            dependents.append([])
        indegree[node_id] += 1
        dependents[dependency_id].append(node_id)

    total_nodes = next_id
    if total_nodes == 0:
        return []

    # Non-recursive layered Kahn: each round collects all indegree-zero
    # nodes (sorted by id == first-appearance order), then decrements the
    # indegrees of their dependents.  Newly-zero nodes become the next
    # round's ready set.
    ready = [nid for nid in range(total_nodes) if indegree[nid] == 0]
    ready.sort()
    layers = []
    processed = 0

    while ready:
        layers.append([nodes_by_id[nid] for nid in ready])
        processed += len(ready)
        next_ready = []
        for nid in ready:
            for dep_id in dependents[nid]:
                indegree[dep_id] -= 1
                if indegree[dep_id] == 0:
                    next_ready.append(dep_id)
        next_ready.sort()
        ready = next_ready

    if processed < total_nodes:
        # Remaining nodes are part of a real directed cycle or blocked
        # by one.  Surface them in first-appearance (id) order.
        blocked = [nodes_by_id[nid] for nid in range(total_nodes)
                   if indegree[nid] > 0]
        raise DependencyCycleError(blocked)

    return layers
