"""Reference implementation for validation; not a leaderboard result."""


class DependencyCycleError(ValueError):
    def __init__(self, nodes):
        self.nodes = tuple(nodes)
        super().__init__("dependency cycle: " + ", ".join(map(repr, self.nodes)))


def dependency_layers(edges):
    order = []
    seen_nodes = set()
    pairs = set()
    outgoing = {}
    indegree = {}

    for pair in edges:
        try:
            node, dependency = pair
        except (TypeError, ValueError):
            raise ValueError("each edge must contain exactly two items") from None
        for item in (node, dependency):
            if item not in seen_nodes:
                seen_nodes.add(item)
                order.append(item)
                outgoing[item] = []
                indegree[item] = 0
        edge = (node, dependency)
        if edge not in pairs:
            pairs.add(edge)
            outgoing[dependency].append(node)
            indegree[node] += 1

    import heapq

    index = {node: position for position, node in enumerate(order)}
    ready = [index[node] for node in order if indegree[node] == 0]
    heapq.heapify(ready)
    emitted = set()
    layers = []
    while ready:
        current = []
        while ready:
            current.append(heapq.heappop(ready))
        layer = [order[position] for position in current]
        layers.append(layer)
        next_ready = []
        for position in current:
            dependency = order[position]
            emitted.add(dependency)
            for node in outgoing[dependency]:
                indegree[node] -= 1
                if indegree[node] == 0:
                    heapq.heappush(next_ready, index[node])
        ready = next_ready

    if len(emitted) != len(order):
        raise DependencyCycleError(node for node in order if node not in emitted)
    return layers
