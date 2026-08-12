"""
L1 process-local cache with TTL.

Thread-safe via a simple lock. Falls back to re-computing on every cache miss;
no external dependency (no Redis). Used as the primary cache layer when Redis
is not available, and as the hot-path L1 when Redis is configured.

Key schema:
  count:{entity_id}:{resource}    → int   (current resource count)
  plan:{entity_id}                → str   (plan key)
  overrides:{entity_id}           → list  (EntitlementOverride list)
"""

from __future__ import annotations

import threading
import time
from typing import Any


class LocalCache:
    """
    Thread-safe in-process TTL dict.

    Usage:
        cache = LocalCache(ttl=60)
        cache.set("count", entity_id, "projects", value=5)
        cache.get("count", entity_id, "projects")   # → 5 or None after TTL
        cache.invalidate(entity_id, "projects")     # clears count key
        cache.invalidate_entity(entity_id)          # clears all keys for entity
    """

    def __init__(self, ttl: int = 60):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._store: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)

    # ------------------------------------------------------------------
    # Internal key builder
    # ------------------------------------------------------------------

    def _key(self, *parts: str) -> str:
        return ":".join(str(p) for p in parts)

    # ------------------------------------------------------------------
    # Get / Set
    # ------------------------------------------------------------------

    def get(self, *parts: str) -> Any | None:
        key = self._key(*parts)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, *parts: str, value: Any) -> None:
        key = self._key(*parts)
        with self._lock:
            self._store[key] = (value, time.monotonic() + self._ttl)

    # ------------------------------------------------------------------
    # Invalidation helpers
    # ------------------------------------------------------------------

    def invalidate(self, entity_id: str, resource: str) -> None:
        """Remove cached resource count for a specific entity+resource."""
        target = self._key("count", entity_id, resource)
        with self._lock:
            self._store.pop(target, None)

    def invalidate_entity(self, entity_id: str) -> None:
        """
        Remove all cached keys that belong to entity_id.

        Keys are colon-joined ("count:10:projects", "overrides:10"). A key
        belongs to entity_id only if entity_id occupies one whole
        colon-delimited segment of the key — never a raw substring match.
        This keeps invalidate_entity("1") from also evicting entity "10",
        "11", "21", etc.

        entity_id itself may contain the ":" separator (e.g. a composite
        id). A plain `entity_id in key.split(":")` check would split
        entity_id apart too and never match it as a single piece, so
        instead we check every contiguous run of ":"-delimited segments
        of the key for equality with entity_id.
        """
        with self._lock:
            drop = [k for k in self._store if self._key_matches_entity(k, entity_id)]
            for k in drop:
                del self._store[k]

    @staticmethod
    def _key_matches_entity(key: str, entity_id: str) -> bool:
        parts = key.split(":")
        n = len(parts)
        for i in range(n):
            joined = parts[i]
            if joined == entity_id:
                return True
            for j in range(i + 1, n):
                joined = f"{joined}:{parts[j]}"
                if joined == entity_id:
                    return True
        return False

    def invalidate_overrides(self, entity_id: str) -> None:
        """Remove cached overrides for an entity."""
        target = self._key("overrides", entity_id)
        with self._lock:
            self._store.pop(target, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)
