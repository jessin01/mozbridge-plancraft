"""
Boundary tests for within_limit — the classic off-by-one surface.

Covers: exactly at the hard limit, one below, one above, unlimited (-1),
and a resource with no limit configured in the plan at all (fail-closed
default of hard=0).
"""

from __future__ import annotations

import pytest

from plancraft import Feature, LimitConfig, Plan, PlanCraft, ResourceCounter

FEATURES: dict[str, Feature] = {}


class FixedCounter(ResourceCounter):
    """Counter that always returns a pre-set count regardless of entity."""

    resource = "widgets"

    def __init__(self, count: int):
        self._count = count

    async def count(self, entity, db) -> int:
        return self._count


class FakeOrg:
    def __init__(self, id: int = 1, plan: str = "capped"):
        self.id = id
        self.plan = plan


def make_pc(hard: int, soft: int | None = None, count: int = 0) -> PlanCraft:
    plans = {
        "capped": Plan(
            name="Capped",
            features=[],
            limits={"widgets": LimitConfig(hard=hard, soft=soft)},
        ),
    }
    pc = PlanCraft()
    pc.register(features=FEATURES, plans=plans, counters=[FixedCounter(count)])
    return pc


class TestHardLimitBoundary:
    """hard=5: 4 must pass, 5 must block, 6 must block."""

    @pytest.mark.asyncio
    async def test_one_below_limit_is_allowed(self):
        pc = make_pc(hard=5, count=4)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert result.allowed
        assert result.current == 4
        assert result.limit == 5

    @pytest.mark.asyncio
    async def test_exactly_at_limit_is_blocked(self):
        pc = make_pc(hard=5, count=5)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert not result.allowed
        assert result.current == 5
        assert result.limit == 5

    @pytest.mark.asyncio
    async def test_one_above_limit_is_blocked(self):
        pc = make_pc(hard=5, count=6)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert not result.allowed


class TestZeroHardLimit:
    """hard=0 — a resource the plan explicitly forbids entirely."""

    @pytest.mark.asyncio
    async def test_zero_count_against_zero_hard_limit_is_blocked(self):
        # current(0) >= hard(0) -> blocked. A plan with hard=0 grants none at all.
        pc = make_pc(hard=0, count=0)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert not result.allowed


class TestUnlimited:
    @pytest.mark.asyncio
    async def test_hard_minus_one_is_always_allowed_regardless_of_count(self):
        pc = make_pc(hard=-1, count=10_000)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert result.allowed
        assert result.reason == "unlimited"
        assert result.limit == -1

    @pytest.mark.asyncio
    async def test_unlimited_never_calls_the_counter(self):
        # Regression guard: unlimited plans must short-circuit before counting.
        calls = {"n": 0}

        class CountingCounter(ResourceCounter):
            resource = "widgets"

            async def count(self, entity, db) -> int:
                calls["n"] += 1
                return 0

        plans = {"capped": Plan(name="Capped", limits={"widgets": LimitConfig(hard=-1)})}
        pc = PlanCraft()
        pc.register(features=FEATURES, plans=plans, counters=[CountingCounter()])
        await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert calls["n"] == 0


class TestSoftLimit:
    @pytest.mark.asyncio
    async def test_at_soft_limit_still_allowed(self):
        pc = make_pc(hard=10, soft=3, count=3)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert result.allowed  # soft limit only warns, never blocks

    @pytest.mark.asyncio
    async def test_below_soft_limit_allowed_no_warning_path_difference(self):
        pc = make_pc(hard=10, soft=3, count=2)
        result = await pc.within_limit(FakeOrg(), "widgets", db=None)
        assert result.allowed


class TestResourceMissingFromPlanLimits:
    """
    Plan.get_limit() defaults to LimitConfig(hard=0) when the resource key
    is entirely absent from plan.limits. This is a fail-CLOSED default:
    an unconfigured resource is treated as a zero-quota resource, not as
    unlimited. Pinning this because it is the exact kind of ambiguity that
    silently flips between fail-open/fail-closed across refactors.
    """

    @pytest.mark.asyncio
    async def test_unconfigured_resource_defaults_to_hard_zero_and_blocks(self):
        plan = Plan(name="Bare", limits={})
        plans = {"bare": plan}
        pc = PlanCraft()
        pc.register(features=FEATURES, plans=plans, counters=[FixedCounter(0)])
        org = FakeOrg(plan="bare")
        result = await pc.within_limit(org, "widgets", db=None)
        assert not result.allowed
        assert result.limit == 0

    def test_get_limit_directly_returns_hard_zero_for_missing_resource(self):
        plan = Plan(name="Bare", limits={})
        cfg = plan.get_limit("nonexistent_resource")
        assert cfg.hard == 0
        assert cfg.soft is None


class TestMissingCounterForConfiguredLimit:
    """
    A finite (non-unlimited) limit for a resource with NO registered counter
    is a project misconfiguration. Today this raises ValueError from
    Registry.get_counter rather than returning a CheckResult. Pinning
    the raise so a future refactor doesn't silently start failing open
    (returning allowed=True) or failing closed with a swallowed exception.
    """

    @pytest.mark.asyncio
    async def test_finite_limit_without_counter_raises_value_error(self):
        plans = {"capped": Plan(name="Capped", limits={"widgets": LimitConfig(hard=5)})}
        pc = PlanCraft()
        pc.register(features=FEATURES, plans=plans, counters=[])  # no counter registered
        with pytest.raises(ValueError, match="No counter registered"):
            await pc.within_limit(FakeOrg(), "widgets", db=None)
