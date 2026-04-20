"""
Phase 1 tests — core engine, no FastAPI, no Stripe, no Redis.
Tests drive the API design and prove the engine works standalone.
"""

import pytest
from plancraft import Feature, LimitConfig, Plan, PlanCraft, ResourceCounter


# ------------------------------------------------------------------
# Fixtures — sample catalog (Mozbridge-like)
# ------------------------------------------------------------------

FEATURES = {
    "monitoring": Feature(name="Grafana Monitoring"),
    "snapshots": Feature(name="DB Snapshots"),
    "custom_domain": Feature(name="Custom Domain"),
}

PLANS = {
    "free": Plan(
        name="Free",
        price=0,
        features=[],
        limits={
            "projects": LimitConfig(hard=2),
            "members": LimitConfig(hard=3),
            "environments": LimitConfig(hard=1, soft=1),
        },
    ),
    "pro": Plan(
        name="Pro",
        price=2900,
        features=["monitoring", "snapshots"],
        limits={
            "projects": LimitConfig(hard=10),
            "members": LimitConfig(hard=15),
            "environments": LimitConfig(hard=3, soft=2),
        },
    ),
    "enterprise": Plan(
        name="Enterprise",
        price=9900,
        features=["monitoring", "snapshots", "custom_domain"],
        limits={
            "projects": LimitConfig(hard=-1),
            "members": LimitConfig(hard=-1),
            "environments": LimitConfig(hard=-1),
        },
    ),
}


class FakeOrg:
    def __init__(self, id: int, plan: str):
        self.id = id
        self.plan = plan


class EnvironmentCounter(ResourceCounter):
    resource = "environments"

    def __init__(self, counts: dict[int, int]):
        self._counts = counts  # org_id → count

    async def count(self, entity, db) -> int:
        return self._counts.get(entity.id, 0)


class ProjectCounter(ResourceCounter):
    resource = "projects"

    def __init__(self, counts: dict[int, int]):
        self._counts = counts

    async def count(self, entity, db) -> int:
        return self._counts.get(entity.id, 0)


# ------------------------------------------------------------------
# Feature check tests
# ------------------------------------------------------------------


class TestFeatureChecks:
    def setup_method(self):
        self.pc = PlanCraft()
        self.pc.register(features=FEATURES, plans=PLANS)

    def test_free_plan_has_no_features(self):
        org = FakeOrg(id=1, plan="free")
        assert not self.pc.can(org, "monitoring")
        assert not self.pc.can(org, "snapshots")
        assert not self.pc.can(org, "custom_domain")

    def test_pro_plan_has_monitoring_and_snapshots(self):
        org = FakeOrg(id=2, plan="pro")
        assert self.pc.can(org, "monitoring")
        assert self.pc.can(org, "snapshots")
        assert not self.pc.can(org, "custom_domain")

    def test_enterprise_has_all_features(self):
        org = FakeOrg(id=3, plan="enterprise")
        assert self.pc.can(org, "monitoring")
        assert self.pc.can(org, "snapshots")
        assert self.pc.can(org, "custom_domain")

    def test_upgrade_to_is_correct(self):
        org = FakeOrg(id=1, plan="free")
        result = self.pc.enforcer.can(org, "monitoring")
        assert result.upgrade_to == "pro"

    def test_upgrade_to_enterprise_for_custom_domain(self):
        org = FakeOrg(id=2, plan="pro")
        result = self.pc.enforcer.can(org, "custom_domain")
        assert result.upgrade_to == "enterprise"

    def test_unknown_plan_falls_back_to_default(self):
        org = FakeOrg(id=99, plan="nonexistent")
        # should not raise — falls back to free
        result = self.pc.enforcer.can(org, "monitoring")
        assert not result.allowed

    def test_unregistered_feature_raises(self):
        org = FakeOrg(id=1, plan="free")
        with pytest.raises(ValueError, match="not registered"):
            self.pc.enforcer.can(org, "nonexistent_feature")


# ------------------------------------------------------------------
# Limit check tests
# ------------------------------------------------------------------


class TestLimitChecks:
    def setup_method(self):
        env_counts = {1: 0, 2: 1, 3: 3, 4: 0}
        proj_counts = {1: 1, 2: 2, 3: 10}
        self.pc = PlanCraft()
        self.pc.register(
            features=FEATURES,
            plans=PLANS,
            counters=[
                EnvironmentCounter(env_counts),
                ProjectCounter(proj_counts),
            ],
        )

    @pytest.mark.asyncio
    async def test_free_within_environment_limit(self):
        org = FakeOrg(id=1, plan="free")  # 0 environments, limit 1
        result = await self.pc.within_limit(org, "environments", db=None)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_free_at_environment_limit(self):
        org = FakeOrg(id=2, plan="free")  # 1 environment, limit 1
        result = await self.pc.within_limit(org, "environments", db=None)
        assert not result.allowed
        assert result.current == 1
        assert result.limit == 1

    @pytest.mark.asyncio
    async def test_enterprise_unlimited_environments(self):
        org = FakeOrg(id=3, plan="enterprise")  # 3 environments, unlimited
        result = await self.pc.within_limit(org, "environments", db=None)
        assert result.allowed
        assert result.reason == "unlimited"

    @pytest.mark.asyncio
    async def test_free_within_project_limit(self):
        org = FakeOrg(id=1, plan="free")  # 1 project, limit 2
        result = await self.pc.within_limit(org, "projects", db=None)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_free_at_project_limit(self):
        org = FakeOrg(id=2, plan="free")  # 2 projects, limit 2
        result = await self.pc.within_limit(org, "projects", db=None)
        assert not result.allowed


# ------------------------------------------------------------------
# Registry tests
# ------------------------------------------------------------------


class TestRegistry:
    def setup_method(self):
        self.pc = PlanCraft()
        self.pc.register(features=FEATURES, plans=PLANS)

    def test_all_plans_registered(self):
        assert set(self.pc.registry.all_plans.keys()) == {"free", "pro", "enterprise"}

    def test_all_features_registered(self):
        assert set(self.pc.registry.all_features.keys()) == {
            "monitoring",
            "snapshots",
            "custom_domain",
        }

    def test_default_plan_is_free(self):
        assert self.pc.registry.default_plan == "free"

    def test_plan_keys_injected(self):
        assert self.pc.registry.get_plan("free").key == "free"
        assert self.pc.registry.get_plan("pro").key == "pro"

    def test_feature_keys_injected(self):
        assert self.pc.registry.get_feature("monitoring").key == "monitoring"
