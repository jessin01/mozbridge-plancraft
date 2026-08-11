"""
LocalCache unit tests. Not currently wired into PlanCraft's own API
(see test_registry_and_usage.py::TestCacheIsNeverActuallyWired), but it
is public, importable, and documented for direct use, so its own
contract must hold on its own.
"""

from __future__ import annotations

import time

import pytest

from plancraft.cache.local_cache import LocalCache


class TestBasicGetSet:
    def test_set_then_get_round_trips(self):
        cache = LocalCache(ttl=60)
        cache.set("count", "1", "projects", value=5)
        assert cache.get("count", "1", "projects") == 5

    def test_get_missing_key_returns_none(self):
        cache = LocalCache(ttl=60)
        assert cache.get("count", "1", "projects") is None

    def test_size_reflects_number_of_entries(self):
        cache = LocalCache(ttl=60)
        cache.set("a", value=1)
        cache.set("b", value=2)
        assert cache.size() == 2

    def test_clear_empties_the_store(self):
        cache = LocalCache(ttl=60)
        cache.set("a", value=1)
        cache.clear()
        assert cache.size() == 0


class TestTTLExpiry:
    def test_entry_expires_after_ttl(self):
        cache = LocalCache(ttl=0.05)
        cache.set("count", "1", "projects", value=5)
        assert cache.get("count", "1", "projects") == 5
        time.sleep(0.08)
        assert cache.get("count", "1", "projects") is None

    def test_expired_entry_is_evicted_from_store_on_read(self):
        cache = LocalCache(ttl=0.05)
        cache.set("count", "1", "projects", value=5)
        time.sleep(0.08)
        cache.get("count", "1", "projects")
        assert cache.size() == 0


class TestInvalidateSpecificKey:
    def test_invalidate_removes_only_the_targeted_resource(self):
        cache = LocalCache(ttl=60)
        cache.set("count", "1", "projects", value=5)
        cache.set("count", "1", "members", value=3)
        cache.invalidate("1", "projects")
        assert cache.get("count", "1", "projects") is None
        assert cache.get("count", "1", "members") == 3

    def test_invalidate_overrides_removes_only_overrides_key(self):
        cache = LocalCache(ttl=60)
        cache.set("overrides", "1", value=["x"])
        cache.set("count", "1", "projects", value=5)
        cache.invalidate_overrides("1")
        assert cache.get("overrides", "1") is None
        assert cache.get("count", "1", "projects") == 5


class TestInvalidateEntitySubstringBug:
    """
    FINDING (xfail, not fixed here): LocalCache.invalidate_entity() drops
    every key that CONTAINS entity_id as a substring:

        drop = [k for k in self._store if entity_id in k]

    Keys are colon-joined ("count:10:projects"), so invalidate_entity("1")
    also matches and evicts entity "10"'s, "11"'s, "21"'s, etc. cache
    entries, because "1" is a substring of "10". This is a cross-tenant
    cache-invalidation bug: an action on entity 1 can silently blow away
    cached counts for unrelated entities whose numeric id merely contains
    "1". Reported per instructions rather than silently patched, since
    LocalCache isn't currently wired into PlanCraft's own code paths
    (see test_registry_and_usage.py) and the fix (delimiter-aware
    matching, e.g. matching "count:1:" as a prefix segment) is a real
    behavioural change that belongs to a deliberate patch, not this
    test-hardening pass.
    """

    @pytest.mark.xfail(
        reason=(
            "LocalCache.invalidate_entity() matches entity_id as a raw substring "
            "of the joined cache key, so invalidate_entity('1') also evicts "
            "entity '10' (and '11', '21', ...). Cross-tenant cache-invalidation "
            "bug, not fixed here — see class docstring."
        ),
        strict=True,
    )
    def test_invalidate_entity_does_not_evict_unrelated_entity_by_substring(self):
        cache = LocalCache(ttl=60)
        cache.set("count", "10", "projects", value=99)
        cache.invalidate_entity("1")  # entity "1" was never cached here at all
        assert cache.get("count", "10", "projects") == 99, (
            "invalidate_entity('1') evicted entity '10's cache entry via "
            "substring match — see class docstring for the underlying bug"
        )
