"""Long-term memory: SQLite lasting knowledge + lean per-turn retrieval.

See docs/stack/redmine-memory.md for the three kinds (stable rules, lessons, per-run log)
and the injection budget.

Imports of Memory / MemoryStore are lazy so lightweight modules (env_guard) stay importable
without optional heavy deps during tooling scripts.
"""

__all__ = ["Memory", "MemoryStore"]


def __getattr__(name: str):
    if name == "Memory":
        from .service import Memory
        return Memory
    if name == "MemoryStore":
        from .store import MemoryStore
        return MemoryStore
    raise AttributeError(name)
