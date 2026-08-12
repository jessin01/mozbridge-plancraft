"""
LocalCache unit tests. Not currently wired into PlanCraft's own API
(see test_registry_and_usage.py::TestCacheIsNeverActuallyWired), but it
is public, importable, and documented for direct use, so its own
contract must hold on its own.
"""

from __future__ import annotations

import time


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


class TestInvalidateEntityIsExact:
    """
    LocalCache.invalidate_entity() used to drop every key that CONTAINED
    entity_id as a raw substring:

        drop = [k for k in self._store if entity_id in k]

    Keys are colon-joined ("count:10:projects"), so invalidate_entity("1")
    also matched and evicted entity "10"'s, "11"'s, "21"'s, etc. cache
    entries, because "1" is a substring of "10". That was a cross-tenant
    cache-invalidation bug: an action on entity 1 could silently blow away
    cached counts for unrelated entities whose numeric id merely contained
    "1". Fixed to match entity_id against whole ":"-delimited segments
    (or the full key / start / end) instead of a raw substring search.
    """

    def test_invalidate_entity_does_not_evict_unrelated_entity_by_substring(self):
        cache = LocalCache(ttl=60)
        cache.set("count", "10", "projects", value=99)
        cache.invalidate_entity("1")  # entity "1" was never cached here at all
        assert cache.get("count", "10", "projects") == 99, (
            "invalidate_entity('1') evicted entity '10's cache entry via "
            "substring match — see class docstring for the underlying bug"
        )

    def test_invalidate_entity_evicts_all_keys_for_the_matching_entity(self):
        cache = LocalCache(ttl=60)
        cache.set("count", "1", "projects", value=5)
        cache.set("overrides", "1", value=["x"])
        cache.invalidate_entity("1")
        assert cache.get("count", "1", "projects") is None
        assert cache.get("overrides", "1") is None

    def test_invalidate_entity_matches_lone_key_equal_to_entity_id(self):
        cache = LocalCache(ttl=60)
        cache.set("1", value="bare-key")
        cache.invalidate_entity("1")
        assert cache.get("1") is None

    def test_invalidate_entity_id_containing_separator_is_matched(self):
        cache = LocalCache(ttl=60)
        # entity id is itself "10:20" (composite id containing ":")
        cache.set("count", "10:20", "projects", value=7)
        cache.invalidate_entity("10:20")
        assert cache.get("count", "10:20", "projects") is None
        # Note: a colon-containing entity_id is inherently ambiguous
        # against an unrelated key whose OTHER segments happen to join to
        # the same string (e.g. cache.set("count", "10", "20", ...) also
        # produces the key "count:10:20"). Colon-joined keys carry no
        # segment-boundary metadata, so that ambiguity can't be resolved
        # without a schema-aware key format; this only guarantees the
        # legitimate entity key itself is matched.

    def test_invalidate_entity_still_ignores_unrelated_short_numeric_id(self):
        cache = LocalCache(ttl=60)
        cache.set("count", "21", "projects", value=1)
        cache.set("count", "12", "projects", value=2)
        cache.invalidate_entity("1")
        assert cache.get("count", "21", "projects") == 1
        assert cache.get("count", "12", "projects") == 2
