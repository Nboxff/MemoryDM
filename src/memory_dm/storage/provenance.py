"""RAW/fact/experience provenance plus an unlinked, user-scoped profile document."""

from dataclasses import dataclass

from memory_dm.storage.experience import ExperienceStore
from memory_dm.storage.profile import ProfileStore

NodeKey = tuple[str, str]
LAYER_ORDER = {"RAW": 0, "ATOMIC": 1, "EXPERIENCE": 2, "PROFILE": 3}


@dataclass(frozen=True)
class ProvenanceNode:
    key: NodeKey
    content: str
    groups: tuple[tuple[NodeKey, ...], ...]
    record: object


class ProvenanceGraph:
    def __init__(self, store, user_id: str):
        self.snapshot = source = ExperienceStore(store).snapshot(user_id)
        self.nodes: dict[NodeKey, ProvenanceNode] = {}
        for turn in source.atomic.turns.values():
            key = ("RAW", turn.turn_id)
            self.nodes[key] = ProvenanceNode(key, turn.content, (), turn)
        for memory in source.atomic.memories.values():
            if memory.status != "ACTIVE":
                continue
            key = ("ATOMIC", memory.id)
            self.nodes[key] = ProvenanceNode(
                key, memory.content, (tuple(("RAW", e.turn_id) for e in memory.evidence),), memory
            )
        for experience in source.experiences.values():
            key = ("EXPERIENCE", experience.id)
            groups = tuple(
                tuple(("ATOMIC", k) for k in g.memory_ids) for g in experience.evidence_groups
            )
            if any(all(k in self.nodes for k in group) for group in groups):
                self.nodes[key] = ProvenanceNode(key, experience.content, groups, experience)
        for entry in ProfileStore(store).read(user_id).values():
            key = ("PROFILE", entry.id)
            self.nodes[key] = ProvenanceNode(key, entry.content, (), entry)

    def ancestors(self, key: NodeKey, *, max_hops: int = 3) -> list[NodeKey]:
        return self._traverse({key}, reverse=False, max_hops=max_hops)

    def descendants(self, keys: set[NodeKey], *, max_hops: int = 3) -> list[NodeKey]:
        return self._traverse(keys, reverse=True, max_hops=max_hops)

    def _traverse(self, keys, *, reverse, max_hops):
        seen, frontier, result = set(keys), set(keys), []
        for _ in range(min(max_hops, 3)):
            adjacent = set()
            if reverse:
                adjacent = {
                    k
                    for k, n in self.nodes.items()
                    if any(frontier.intersection(g) for g in n.groups)
                }
            else:
                adjacent = {
                    p
                    for k in frontier
                    if k in self.nodes
                    for g in self.nodes[k].groups
                    for p in g
                    if p in self.nodes
                }
            frontier = adjacent - seen
            if not frontier:
                break
            seen.update(frontier)
            result.extend(sorted(frontier, key=lambda k: (LAYER_ORDER[k[0]], k[1])))
        return result

    def repair_order(self, keys: set[NodeKey]) -> list[NodeKey]:
        closure = keys | set(self.descendants(keys))
        return sorted(closure, key=lambda k: (LAYER_ORDER[k[0]], k[1]))

    def supported(self, key: NodeKey, eligible_atoms: set[str]) -> bool:
        node = self.nodes.get(key)
        if node is None:
            return False
        if key[0] == "ATOMIC":
            return key[1] in eligible_atoms
        if key[0] == "RAW":
            return True
        if key[0] == "PROFILE":
            # User isolation is enforced while reading the graph, not via invented links.
            return True
        return any(all(self.supported(p, eligible_atoms) for p in g) for g in node.groups)

    def record(self, key: NodeKey) -> dict:
        node = self.nodes[key]
        return {
            "id": key[1],
            "layer": key[0],
            "content": node.content,
            "source_groups": [
                [{"layer": p[0], "id": p[1]} for p in group] for group in node.groups
            ],
            "derived": True,
        }
