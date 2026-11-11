"""Shared failures used at the boundary between memory rules and storage adapters."""


class ReconciliationError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {"operation": "memory_reconciliation", "issues": []}


class RetrievalError(RuntimeError):
    pass
