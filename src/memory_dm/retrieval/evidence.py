"""Retrieved records and their relationship to directly matched evidence."""

from dataclasses import dataclass

from memory_dm.models import AtomicMemory
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.models import GrepMatch

EvidenceKey = tuple[str, str]


@dataclass
class RetrievedEvidence:
    layer: str
    id: str
    score: float
    memory: AtomicMemory | None = None
    raw: GrepMatch | None = None
    scope: SearchFilters | None = None
    primary: bool = True
    parents: frozenset[EvidenceKey] = frozenset()
    derived: dict | None = None
