"""Rank fusion with one vote per retrieval method and discounted provenance expansion."""

from collections import defaultdict
from dataclasses import replace

from memory_dm.retrieval.evidence import EvidenceKey, RetrievedEvidence
from memory_dm.retrieval.filters import SearchFilters


class EvidenceFusion:
    def __init__(self):
        self.views: dict[EvidenceKey, list[RetrievedEvidence]] = defaultdict(list)
        self.votes: dict[EvidenceKey, dict[str, float]] = defaultdict(dict)
        self.support: dict[EvidenceKey, dict[EvidenceKey, float]] = defaultdict(dict)

    def collect(
        self, items: list[RetrievedEvidence], filters: SearchFilters, *, method: str
    ) -> None:
        """Aliases and overlapping tasks improve a method's best rank, not its vote count."""
        for rank, item in enumerate(items, 1):
            key = (item.layer, item.id)
            self.views[key].append(replace(item, scope=filters))
            score = 1.0 / (60 + rank)
            self.votes[key][method] = max(self.votes[key].get(method, 0), score)

    def expand(
        self,
        items: list[RetrievedEvidence],
        filters: SearchFilters,
        *,
        parents: frozenset[EvidenceKey],
        discount: float,
    ) -> None:
        """Reading a source or graph neighbor does not independently corroborate a fact."""
        for item in items:
            key = (item.layer, item.id)
            self.views[key].append(replace(item, scope=filters, primary=False, parents=parents))
            for parent in parents - {key}:
                self.support[key][parent] = max(self.support[key].get(parent, 0), discount)

    def ranked(self, limit: int) -> list[RetrievedEvidence]:
        scores = {}

        def score(key: EvidenceKey, path: frozenset[EvidenceKey] = frozenset()) -> float:
            if key in path:
                return 0
            if key not in scores:
                scores[key] = max(
                    sum(self.votes[key].values()),
                    max(
                        (
                            discount * score(parent, path | {key})
                            for parent, discount in self.support[key].items()
                        ),
                        default=0,
                    ),
                )
            return scores[key]

        keys = sorted(self.views, key=lambda key: (-score(key), *key))[:limit]
        # Keep scoped views: different branches can legitimately expose different neighbors.
        return [replace(view, score=score(key)) for key in keys for view in self.views[key]]
