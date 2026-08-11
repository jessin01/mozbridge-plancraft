"""
Registry lookup edge cases + get_usage()/reload_catalog() coverage,
plus a pinned finding: PlanCraft's cache layer is fully wired to `None`
and never actually caches anything today.
"""

from __future__ import annotations

import pytest

from plancraft import Feature, LimitConfig, Plan, PlanCraft, ResourceCounter
from plancraft.core.registry import Registry


class FakeOrg:
    def __init__(self, id: int = 1, plan: str = "free"):
        self.id = id
        self.plan = plan


class CountingCounter(ResourceCounter):
    """Counts how many times .count() is invoked, to prove (no) caching."""

    resource = "widgets"

    def __init__(self, value: int = 0):
        self.value = value
        self.calls = 0

    async def count(self, entity, db) -> int:
        self.calls += 1
        return self.value


# ------------------------------------------------------------------
# Registry direct tests
# ------------------------------------------------------------------


class TestRegistryLookupErrors:
    def test_get_counter_raises_for_unregistered_resource(self):
        registry = Registry()
        registry.register(features={}, plans={"free": Plan(name="Free")})
        with pytest.raises(ValueError, match="No counter registered"):
            registry.get_counter("nonexistent")

    def test_get_feature_raises_for_unregistered_feature(self):
        registry = Registry()
        registry.register(features={}, plans={"free": Plan(name="Free")})
        with pytest.raises(ValueError, match="not registered in catalog"):
            registry.get_feature("nonexistent")

    def test_which_plan_has_feature_returns_none_when_no_plan_has_it(self):
        registry = Registry()
        plans = {"free": Plan(name="Free", features=[])}
        registry.register(features={"x": Feature(name="X")}, plans=plans)
        assert registry.which_plan_has_feature("x") is None

    def test_register_accepts_counter_class_not_just_instance(self):
        class ClassCounter(ResourceCounter):
            resource = "seats"

            async def count(self, entity, db) -> int:
                return 0

        registry = Registry()
        registry.register(features={}, plans={"free": Plan(name="Free")}, counters=[ClassCounter])
        counter = registry.get_counter("seats")
        assert isinstance(counter, ClassCounter)


class TestReloadCatalog:
    @pytest.mark.asyncio
    async def test_reload_replaces_plans_and_features_but_keeps_counters(self):
        counter = CountingCounter(value=2)
        pc = PlanCraft()
        old_plans = {
            "free": Plan(name="Free", features=["a"], limits={"widgets": LimitConfig(hard=5)}),
        }
        pc.register(features={"a": Feature(name="A")}, plans=old_plans, counters=[counter])
        org = FakeOrg(plan="free")
        assert pc.can(org, "a")

        # Reload with a catalog that no longer has feature "a".
        new_plans = {
            "free": Plan(name="Free", features=["b"], limits={"widgets": LimitConfig(hard=9)}),
        }
        pc.reload_catalog(features={"b": Feature(name="B")}, plans=new_plans)

        assert pc.can(org, "b")  # "b" is granted under the reloaded catalog
        with pytest.raises(ValueError, match="not registered"):
            pc.can(org, "a")  # old feature key is gone entirely after reload

        # Counter survived the reload and the new limit (9) is in effect.
        result = await pc.within_limit(org, "widgets", db=None)
        assert result.limit == 9
        assert counter.calls >= 1


class TestGetUsage:
    @pytest.mark.asyncio
    async def test_usage_percentage_and_exceeded_for_normal_limit(self):
        pc = PlanCraft()
        pc.register(
            features={},
            plans={"free": Plan(name="Free", limits={"widgets": LimitConfig(hard=4)})},
            counters=[CountingCounter(value=2)],
        )
        usage = await pc.get_usage(FakeOrg(), "widgets", db=None)
        assert usage["current"] == 2
        assert usage["limit"] == 4
        assert usage["percentage"] == 50.0
        assert usage["unlimited"] is False
        assert usage["exceeded"] is False

    @pytest.mark.asyncio
    async def test_usage_unlimited_reports_zero_percent_never_exceeded(self):
        pc = PlanCraft()
        pc.register(
            features={},
            plans={"free": Plan(name="Free", limits={"widgets": LimitConfig(hard=-1)})},
            counters=[CountingCounter(value=999_999)],
        )
        usage = await pc.get_usage(FakeOrg(), "widgets", db=None)
        assert usage["unlimited"] is True
        assert usage["percentage"] == 0
        assert usage["exceeded"] is False

    @pytest.mark.asyncio
    async def test_usage_for_zero_hard_limit_reports_exceeded_true(self):
        # FINDING (pre-existing, informational): get_usage's percentage
        # formula guards `hard > 0` before dividing, so a hard=0 resource
        # reports percentage=0 (not "infinite"/100), while `exceeded` still
        # correctly reports True via the separate `current >= hard` check.
        # The two fields therefore look mutually inconsistent (0% but
        # exceeded) for zero-quota resources — worth a UI note, not a bug.
        pc = PlanCraft()
        pc.register(
            features={},
            plans={"free": Plan(name="Free", limits={"widgets": LimitConfig(hard=0)})},
            counters=[CountingCounter(value=0)],
        )
        usage = await pc.get_usage(FakeOrg(), "widgets", db=None)
        assert usage["percentage"] == 0
        assert usage["exceeded"] is True


# ------------------------------------------------------------------
# Cache wiring — pinned finding
# ------------------------------------------------------------------


class TestCacheIsNeverActuallyWired:
    """
    FINDING: PlanCraft.__init__ stores `cache_ttl`/`cache` (the string
    "redis"/None) but `self._cache` is hardcoded to `None` and nothing in
    plancraft.py ever instantiates `LocalCache` or a Redis-backed cache
    into it. Enforcer is likewise never given a cache instance (PlanCraft
    never passes `cache=` through to `Enforcer(...)`).

    Practical effect today:
      - invalidate_resource / invalidate_entity / invalidate_overrides are
        silent no-ops regardless of what you pass to PlanCraft(cache=...).
      - within_limit()/can() never consult or populate a cache; every call
        recomputes from the counter. The "L1 process-local cache with TTL"
        described in local_cache.py's docstring is dead code from the
        perspective of PlanCraft's own API surface — a caller can only get
        caching by using plancraft.cache.local_cache.LocalCache directly.

    This is not something this test suite should silently paper over by
    wiring the cache in — that is a product/API decision, not a test
    concern. Pinning current (no-op) behaviour here so a future patch that
    "adds" caching is understood as a deliberate change, not a bugfix.
    """

    def test_plancraft_cache_attribute_is_always_none(self):
        pc = PlanCraft(cache="redis", cache_ttl=30)
        assert pc._cache is None

    def test_invalidate_calls_are_no_ops_and_do_not_raise(self):
        pc = PlanCraft(cache="redis")
        pc.register(features={}, plans={"free": Plan(name="Free")})
        # None of these touch any real cache and none of them raise.
        pc.invalidate_resource("1", "widgets")
        pc.invalidate_entity("1")
        pc.invalidate_overrides("1")

    def test_enforcer_never_receives_a_cache_instance(self):
        pc = PlanCraft(cache="redis")
        assert pc.enforcer.cache is None

    @pytest.mark.asyncio
    async def test_within_limit_recomputes_via_counter_every_call_no_caching(self):
        counter = CountingCounter(value=1)
        pc = PlanCraft(cache="redis")
        pc.register(
            features={},
            plans={"free": Plan(name="Free", limits={"widgets": LimitConfig(hard=5)})},
            counters=[counter],
        )
        org = FakeOrg()
        await pc.within_limit(org, "widgets", db=None)
        await pc.within_limit(org, "widgets", db=None)
        await pc.within_limit(org, "widgets", db=None)
        assert counter.calls == 3  # a real cache would have kept this at 1
