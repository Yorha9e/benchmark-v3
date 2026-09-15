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

    # First-appearance registry. IDs let the graph use sets and adjacency
    # lists without requiring node values themselves to be hashable. Hashable
    # values use a hash-bucket fast path; unhashable values use equality
    # lookup, including a cross-check against the other kind of node.
    nodes_by_id = []  # type: list[object]
    hash_buckets = {}  # type: dict[int, list[int]]
    hashable_ids = []  # type: list[int]
    unhashable_ids = []  # type: list[int]

    indegree = []  # type: list[int]
    dependents = []  # type: list[list[int]]

    def _same_node(left, right):
        return left is right or left == right

    def _new_node(value, value_hash):
        node_id = len(nodes_by_id)
        nodes_by_id.append(value)
        indegree.append(0)
        dependents.append([])
        if value_hash is None:
            unhashable_ids.append(node_id)
        else:
            hash_buckets.setdefault(value_hash, []).append(node_id)
            hashable_ids.append(node_id)
        return node_id

    def _id_of(value):
        try:
            value_hash = hash(value)
        except TypeError:
            # There is no general constant-time lookup for an unhashable
            # value, so compare it with every registered candidate. Select
            # the earliest match to preserve first appearance order even if
            # an equal value exists in both registries.
            match = None
            for existing_id in unhashable_ids:
                if _same_node(nodes_by_id[existing_id], value):
                    match = existing_id
                    break
            for existing_id in hashable_ids:
                if _same_node(nodes_by_id[existing_id], value):
                    if match is None or existing_id < match:
                        match = existing_id
                    break
            if match is not None:
                return match
            return _new_node(value, None)

        match = None
        for existing_id in hash_buckets.get(value_hash, ()):
            if _same_node(nodes_by_id[existing_id], value):
                match = existing_id
                break
        # An unhashable object can still compare equal to a hashable one.
        for existing_id in unhashable_ids:
            if _same_node(nodes_by_id[existing_id], value):
                if match is None or existing_id < match:
                    match = existing_id
                break
        if match is not None:
            return match
        return _new_node(value, value_hash)

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
        indegree[node_id] += 1
        dependents[dependency_id].append(node_id)

    total_nodes = len(nodes_by_id)
    if total_nodes == 0:
        return []

    # Non-recursive layered Kahn. IDs are assigned in first-appearance order;
    # sorting only the newly available layer keeps every layer stable without
    # rescanning all unprocessed nodes.
    current = [nid for nid, degree in enumerate(indegree) if degree == 0]
    processed = bytearray(total_nodes)
    processed_count = 0
    layers = []

    while current:
        layers.append([nodes_by_id[nid] for nid in current])
        processed_count += len(current)
        for nid in current:
            processed[nid] = 1

        next_layer = []
        for nid in current:
            for dependent_id in dependents[nid]:
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    next_layer.append(dependent_id)
        next_layer.sort()
        current = next_layer

    if processed_count != total_nodes:
        # Anything not peeled is cyclic itself or transitively blocked by a
        # cycle. IDs preserve the required first-appearance order.
        blocked = [
            nodes_by_id[nid]
            for nid in range(total_nodes)
            if not processed[nid]
        ]
        raise DependencyCycleError(blocked)

    return layers
