"""
FastAPI integration: dependencies + router import/mount cleanly, and the
actual HTTP behaviour (402 vs 200) at the boundary consumers rely on.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from plancraft import Feature, LimitConfig, Plan, PlanCraft, ResourceCounter

FEATURES = {"monitoring": Feature(name="Monitoring")}
PLANS = {
    "free": Plan(name="Free", features=[], limits={"widgets": LimitConfig(hard=1)}),
    "pro": Plan(name="Pro", features=["monitoring"], limits={"widgets": LimitConfig(hard=10)}),
}


class FakeOrg:
    def __init__(self, id: int = 1, plan: str = "free"):
        self.id = id
        self.plan = plan


class ZeroCounter(ResourceCounter):
    resource = "widgets"

    async def count(self, entity, db) -> int:
        return 0


def build_app(plan: str = "free") -> FastAPI:
    pc = PlanCraft(
        get_entity=lambda: FakeOrg(plan=plan),
        get_db=lambda: None,
    )
    pc.register(features=FEATURES, plans=PLANS, counters=[ZeroCounter()])
    app = FastAPI()
    app.include_router(pc.router, prefix="/billing")

    @app.post("/monitoring/activate")
    async def activate(_=Depends(pc.require_feature("monitoring"))):
        return {"ok": True}

    @app.post("/widgets")
    async def create_widget(_=Depends(pc.require_limit("widgets"))):
        return {"ok": True}

    @app.post("/combined")
    async def combined(_=Depends(pc.require("feature:monitoring", "limit:widgets"))):
        return {"ok": True}

    return app


class TestRouterImportsAndMounts:
    def test_router_property_builds_without_error(self):
        pc = PlanCraft(get_entity=lambda: FakeOrg(), get_db=lambda: None)
        pc.register(features=FEATURES, plans=PLANS, counters=[ZeroCounter()])
        router = pc.router
        assert router is not None
        # Idempotent — repeated access returns the cached router.
        assert pc.router is router

    def test_catalog_endpoint_returns_plans_and_features(self):
        app = build_app(plan="free")
        client = TestClient(app)
        resp = client.get("/billing/catalog")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["plans"].keys()) == {"free", "pro"}
        assert "monitoring" in body["features"]
        assert body["default_plan"] == "free"

    def test_plan_endpoint_reports_feature_access(self):
        app = build_app(plan="pro")
        client = TestClient(app)
        resp = client.get("/billing/plan")
        assert resp.status_code == 200
        assert resp.json()["features"]["monitoring"] is True

    def test_usage_endpoint_reports_current_and_limit(self):
        app = build_app(plan="free")
        client = TestClient(app)
        resp = client.get("/billing/usage")
        assert resp.status_code == 200
        assert resp.json()["usage"]["widgets"]["current"] == 0
        assert resp.json()["usage"]["widgets"]["limit"] == 1


class TestRequireFeatureDependency:
    def test_missing_feature_returns_402(self):
        app = build_app(plan="free")
        client = TestClient(app)
        resp = client.post("/monitoring/activate")
        assert resp.status_code == 402
        assert resp.json()["detail"]["code"] == "feature_not_available"
        assert resp.json()["detail"]["upgrade_to"] == "pro"

    def test_present_feature_returns_200(self):
        app = build_app(plan="pro")
        client = TestClient(app)
        resp = client.post("/monitoring/activate")
        assert resp.status_code == 200


class TestRequireLimitDependency:
    def test_within_limit_returns_200(self):
        app = build_app(plan="free")
        client = TestClient(app)
        resp = client.post("/widgets")
        assert resp.status_code == 200

    def test_at_limit_returns_402(self):
        pc = PlanCraft(get_entity=lambda: FakeOrg(plan="free"), get_db=lambda: None)

        class OneCounter(ResourceCounter):
            resource = "widgets"

            async def count(self, entity, db) -> int:
                return 1  # free plan hard limit is 1 -> at limit

        pc.register(features=FEATURES, plans=PLANS, counters=[OneCounter()])
        app = FastAPI()

        @app.post("/widgets")
        async def create_widget(_=Depends(pc.require_limit("widgets"))):
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/widgets")
        assert resp.status_code == 402
        assert resp.json()["detail"]["code"] == "limit_reached"


class TestRequireCombinedDependency:
    def test_combined_fails_fast_on_first_failed_check(self):
        app = build_app(plan="free")
        client = TestClient(app)
        resp = client.post("/combined")
        # free plan fails the feature check before the limit check even runs
        assert resp.status_code == 402
        assert resp.json()["detail"]["code"] == "feature_not_available"

    def test_combined_passes_when_both_checks_pass(self):
        app = build_app(plan="pro")
        client = TestClient(app)
        resp = client.post("/combined")
        assert resp.status_code == 200


class TestMissingEntityOrDbConfiguration:
    @pytest.mark.asyncio
    async def test_require_feature_raises_runtime_error_without_get_entity(self):
        pc = PlanCraft()  # no get_entity configured
        pc.register(features=FEATURES, plans=PLANS)
        # Building the dependency must not raise; the failure surfaces later,
        # inside its own entity-resolution sub-dependency.
        pc.require_feature("monitoring")
        from plancraft.integrations.fastapi.depends import _get_entity_dep

        entity_dep = _get_entity_dep(pc)
        with pytest.raises(RuntimeError, match="get_entity not configured"):
            await entity_dep()

    @pytest.mark.asyncio
    async def test_require_limit_raises_runtime_error_without_get_db(self):
        pc = PlanCraft(get_entity=lambda: FakeOrg())
        pc.register(features=FEATURES, plans=PLANS)
        from plancraft.integrations.fastapi.depends import _get_db_dep

        db_dep = _get_db_dep(pc)
        with pytest.raises(RuntimeError, match="get_db not configured"):
            await db_dep()
