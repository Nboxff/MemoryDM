"""Public sparse-index adapter; SQLite FTS persistence lives in storage."""

from memory_dm.storage.fts import FTSIndexStore as SparseIndex

__all__ = ["SparseIndex"]
