"""
PlanCraft — main entry point.

Projects instantiate this once and use it everywhere:
    pc = PlanCraft()
    pc.register(features=FEATURES, plans=PLANS, counters=[...])
    app.include_router(pc.router)

    @router.post("/environments")
    async def create(
        _ = Depends(pc.require_feature("multi_env")),
        _ = Depends(pc.require_limit("environments")),
    ): ...
"""

from __future__ import annotations

import logging
from typing import Any

from .core.counter import ResourceCounter
from .core.enforcer import CheckResult, Enforcer
from .core.models import Feature, Plan
from .core.registry import Registry

logger = logging.getLogger(__name__)


class PlanCraft:
    """
    Main plancraft instance. One per project/app.

    Usage:
        pc = PlanCraft()
        pc.register(features=FEATURES, plans=PLANS, counters=[MyCounter])
    """

    def __init__(
        self,
        gateway: str | None = None,
        stripe_key: str | None = None,
        cache: str | None = None,  # "redis" | None (in-memory)
        cache_ttl: int = 60,
        get_entity: Any | None = None,  # callable: request → billing entity
        get_plan_key: Any | None = None,  # callable: entity → plan key string
        get_db: Any | None = None,  # callable: request → db session
    ):
        self.registry = Registry()
        self._gateway_type = gateway
        self._stripe_key = stripe_key
        self._cache_type = cache
        self._cache_ttl = cache_ttl
        self._get_entity = get_entity
        self._get_db = get_db

        self.enforcer = Enforcer(
            registry=self.registry,
            get_plan_key=get_plan_key,
        )

        self._gateway = None
        self._cache = None
        self._router = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        features: dict[str, Feature],
        plans: dict[str, Plan],
        counters: list[type[ResourceCounter]] | None = None,
        default_plan: str = "free",
    ) -> "PlanCraft":
        """Register the billing catalog. Call once at startup."""
        self.registry.register(
            features=features,
            plans=plans,
            counters=counters,
            default_plan=default_plan,
        )
        return self

    # ------------------------------------------------------------------
    # FastAPI dependencies — require_feature / require_limit
    # ------------------------------------------------------------------

    def require_feature(self, feature_key: str):
        """
        FastAPI dependency — raises 402 if entity's plan
        does not include feature_key.

        Usage:
            @router.post("/monitoring/activate")
            async def activate(
                _ = Depends(pc.require_feature("monitoring"))
            ): ...
        """
        from .integrations.fastapi.depends import make_feature_dependency

        return make_feature_dependency(self, feature_key)

    def require_limit(self, resource: str):
        """
        FastAPI dependency — raises 402 if entity is at or over
        their plan limit for resource.

        Usage:
            @router.post("/environments")
            async def create(
                _ = Depends(pc.require_limit("environments"))
            ): ...
        """
        from .integrations.fastapi.depends import make_limit_dependency

        return make_limit_dependency(self, resource)

    def require(self, *checks: str):
        """
        Shorthand for multiple checks in one dependency.
        Prefix feature keys with "feature:" and limit keys with "limit:".

        Usage:
            _ = Depends(pc.require("feature:monitoring", "limit:environments"))
        """
        from .integrations.fastapi.depends import make_combined_dependency

        return make_combined_dependency(self, list(checks))

    # ------------------------------------------------------------------
    # FastAPI router
    # ------------------------------------------------------------------

    @property
    def router(self):
        """
        FastAPI router exposing billing API endpoints.
        Mount once: app.include_router(pc.router, prefix="/api/v1/billing")
        """
        if self._router is None:
            from .integrations.fastapi.router import build_router

            self._router = build_router(self)
        return self._router

    # ------------------------------------------------------------------
    # Direct checks (non-FastAPI usage)
    # ------------------------------------------------------------------

    def can(self, entity: Any, feature_key: str) -> CheckResult:
        """Direct feature check — use outside of FastAPI context."""
        return self.enforcer.can(entity, feature_key)

    async def within_limit(self, entity: Any, resource: str, db: Any) -> CheckResult:
        """Direct limit check — use outside of FastAPI context."""
        return await self.enforcer.within_limit(entity, resource, db)

    async def get_usage(self, entity: Any, resource: str, db: Any) -> dict:
        """Get current usage info for display."""
        return await self.enforcer.get_usage(entity, resource, db)

    # ------------------------------------------------------------------
    # Cache invalidation — call these after any create/delete that
    # changes a resource count or override state.
    # ------------------------------------------------------------------

    def invalidate_resource(self, entity_id: str, resource: str) -> None:
        """
        Invalidate the cached resource count for (entity_id, resource).
        Call after a create or delete of a counted resource.

        Example:
            plancraft.invalidate_resource(str(org.id), "projects")
        """
        if self._cache is not None:
            self._cache.invalidate(entity_id, resource)

    def invalidate_entity(self, entity_id: str) -> None:
        """
        Invalidate all cached data for an entity (plan key, counts, overrides).
        Call when an org's plan is changed.
        """
        if self._cache is not None:
            self._cache.invalidate_entity(entity_id)

    def invalidate_overrides(self, entity_id: str) -> None:
        """
        Invalidate cached feature overrides for an entity.
        Call after adding or removing a per-org override.
        """
        if self._cache is not None:
            self._cache.invalidate_overrides(entity_id)

    # ------------------------------------------------------------------
    # Hot-reload — swap the in-memory catalog from DB-loaded data.
    # ------------------------------------------------------------------

    def reload_catalog(
        self,
        features: dict[str, Feature],
        plans: dict[str, Plan],
        default_plan: str = "free",
    ) -> None:
        """
        Hot-reload the billing catalog from new data (e.g. after a platform
        admin edits plans in the UI). Fully replaces registry contents;
        existing Depends/counters keep working.

        Counters are preserved — they're project code, not catalog data.
        The replace is atomic from the caller's perspective: the registry
        dicts are cleared and re-populated in a single synchronous call.
        """
        existing_counters = list(self.registry._counters.values())
        # Clear so DB state is authoritative — remove plans that no longer exist.
        self.registry._features.clear()
        self.registry._plans.clear()
        self.registry.register(
            features=features,
            plans=plans,
            counters=existing_counters,
            default_plan=default_plan,
        )
        logger.info(
            "plancraft: catalog reloaded plans=%d features=%d default=%s",
            len(plans),
            len(features),
            default_plan,
        )
