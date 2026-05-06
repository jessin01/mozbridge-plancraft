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
        """Remove all cached keys that contain the entity_id."""
        with self._lock:
            drop = [k for k in self._store if entity_id in k]
            for k in drop:
                del self._store[k]

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
