"""
Fail-direction tests: what plancraft does today when inputs are ambiguous,
missing, or malformed. Every test here PINS current behaviour so a future
change to these paths is a deliberate decision, not an accident.

If a test below documents an "allow" outcome for an ambiguous/misconfigured
input, that is flagged in the docstring as a fail-OPEN finding.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from plancraft import EntitlementOverride, Feature, LimitConfig, Plan, PlanCraft, ResourceCounter
from plancraft.core.registry import Registry

FEATURES = {
    "monitoring": Feature(name="Monitoring"),
}

PLANS = {
    "free": Plan(name="Free", features=[], limits={"widgets": LimitConfig(hard=1)}),
    "pro": Plan(name="Pro", features=["monitoring"], limits={"widgets": LimitConfig(hard=5)}),
}


class FakeOrg:
    def __init__(self, id: int = 1, plan: str = "free"):
        self.id = id
        self.plan = plan


class NoPlanAttrEntity:
    """An entity with no .plan attribute at all."""

    def __init__(self, id: int = 1):
        self.id = id


class ZeroCounter(ResourceCounter):
    resource = "widgets"

    async def count(self, entity, db) -> int:
        return 0


# ------------------------------------------------------------------
# Unknown plan key
# ------------------------------------------------------------------


class TestUnknownPlanKey:
    def setup_method(self):
        self.pc = PlanCraft()
        self.pc.register(features=FEATURES, plans=PLANS, counters=[ZeroCounter()])

    def test_can_falls_back_to_default_plan_silently(self):
        # FINDING (pre-existing, not introduced by these tests): an entity
        # whose stored plan key does not exist in the catalog is silently
        # treated as being on the default plan rather than raising or
        # denying outright. A typo'd/stale plan key on an entity therefore
        # degrades gracefully to "free" rather than surfacing as an error.
        org = FakeOrg(plan="typo_pln")
        result = self.pc.enforcer.can(org, "monitoring")
        assert not result.allowed  # free plan lacks monitoring, so this stays safe here

    @pytest.mark.asyncio
    async def test_within_limit_falls_back_to_default_plan_silently(self):
        org = FakeOrg(plan="typo_pln")
        result = await self.pc.within_limit(org, "widgets", db=None)
        # default ("free") plan's widgets hard limit is 1; count is 0 -> allowed.
        assert result.allowed
        assert result.limit == 1

    def test_registry_raises_when_neither_key_nor_default_exist(self):
        registry = Registry()
        registry.register(features={}, plans={}, default_plan="ghost")
        with pytest.raises(ValueError, match="not found in registry"):
            registry.get_plan("also_missing")


# ------------------------------------------------------------------
# Missing/absent plan attribute on the entity itself
# ------------------------------------------------------------------


class TestEntityMissingPlanAttribute:
    def test_default_get_plan_key_falls_back_to_free_string(self):
        pc = PlanCraft()
        pc.register(features=FEATURES, plans=PLANS, counters=[ZeroCounter()])
        entity = NoPlanAttrEntity()
        # Default get_plan_key is `getattr(entity, "plan", "free")`.
        result = pc.enforcer.can(entity, "monitoring")
        assert not result.allowed  # resolves to "free", which lacks monitoring


# ------------------------------------------------------------------
# Overrides — trials / support exceptions
# ------------------------------------------------------------------


class TestOverrides:
    def setup_method(self):
        self.pc = PlanCraft()
        self.pc.register(features=FEATURES, plans=PLANS, counters=[ZeroCounter()])

    def test_granted_override_bypasses_free_plan_denial(self):
        org = FakeOrg(id=1, plan="free")
        override = EntitlementOverride(entity_id="1", feature="monitoring", granted=True)
        result = self.pc.enforcer.can(org, "monitoring", overrides=[override])
        assert result.allowed
        assert result.reason == "override"

    def test_revoked_override_blocks_even_on_pro_plan(self):
        org = FakeOrg(id=2, plan="pro")
        override = EntitlementOverride(entity_id="2", feature="monitoring", granted=False)
        result = self.pc.enforcer.can(org, "monitoring", overrides=[override])
        assert not result.allowed
        assert result.reason == "override"

    def test_expired_override_is_ignored_falls_through_to_plan(self):
        org = FakeOrg(id=1, plan="free")
        expired = EntitlementOverride(
            entity_id="1",
            feature="monitoring",
            granted=True,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        result = self.pc.enforcer.can(org, "monitoring", overrides=[expired])
        assert not result.allowed  # falls through to plan check, free lacks monitoring
        assert result.reason != "override"

    def test_future_expiry_override_is_honored(self):
        org = FakeOrg(id=1, plan="free")
        future = EntitlementOverride(
            entity_id="1",
            feature="monitoring",
            granted=True,
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        result = self.pc.enforcer.can(org, "monitoring", overrides=[future])
        assert result.allowed

    def test_override_for_different_entity_is_not_applied(self):
        org = FakeOrg(id=1, plan="free")
        other_entity_override = EntitlementOverride(
            entity_id="999", feature="monitoring", granted=True
        )
        result = self.pc.enforcer.can(org, "monitoring", overrides=[other_entity_override])
        assert not result.allowed

    def test_override_hit_skips_registry_feature_validation(self):
        # FINDING (pre-existing): when an override matches, Enforcer.can()
        # returns immediately WITHOUT calling registry.get_feature(), so an
        # override can grant/deny a feature key that was never registered
        # in the catalog at all. Contrast with the no-override path, where
        # an unregistered feature key raises ValueError (see test_core.py
        # test_unregistered_feature_raises). Pinning both directions here
        # so this asymmetry cannot regress silently.
        org = FakeOrg(id=1, plan="free")
        override = EntitlementOverride(
            entity_id="1", feature="totally_unregistered_feature", granted=True
        )
        result = self.pc.enforcer.can(org, "totally_unregistered_feature", overrides=[override])
        assert result.allowed  # no ValueError, unlike the no-override path

    def test_same_unregistered_feature_without_override_raises(self):
        org = FakeOrg(id=1, plan="free")
        with pytest.raises(ValueError, match="not registered"):
            self.pc.enforcer.can(org, "totally_unregistered_feature")


# ------------------------------------------------------------------
# Plan with no entitlements at all
# ------------------------------------------------------------------


class TestPlanWithNoEntitlements:
    def test_plan_with_empty_features_and_limits_denies_everything(self):
        bare_plan = Plan(name="Bare")  # features=[], limits={}
        pc = PlanCraft()
        pc.register(features=FEATURES, plans={"bare": bare_plan}, counters=[ZeroCounter()])
        org = FakeOrg(plan="bare")
        assert not pc.can(org, "monitoring")

    @pytest.mark.asyncio
    async def test_plan_with_empty_limits_blocks_any_finite_resource_immediately(self):
        bare_plan = Plan(name="Bare")
        pc = PlanCraft()
        pc.register(features=FEATURES, plans={"bare": bare_plan}, counters=[ZeroCounter()])
        org = FakeOrg(plan="bare")
        result = await pc.within_limit(org, "widgets", db=None)
        assert not result.allowed
        assert result.limit == 0
