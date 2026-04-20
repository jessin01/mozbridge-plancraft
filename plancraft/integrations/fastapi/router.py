"""
Standard billing API routes — mounted once per project.

GET  /billing/catalog     — all plans + features (for UI)
GET  /billing/plan        — current entity's plan + feature access
GET  /billing/usage       — current usage vs limits
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

if TYPE_CHECKING:
    from plancraft.plancraft import PlanCraft


def build_router(pc: "PlanCraft") -> APIRouter:
    router = APIRouter(tags=["billing"])

    from plancraft.integrations.fastapi.depends import _get_db_dep, _get_entity_dep

    entity_dep = _get_entity_dep(pc)
    db_dep = _get_db_dep(pc)

    @router.get("/catalog")
    async def get_catalog():
        """Full plan + feature catalog — used by frontend to render plan cards."""
        plans = {}
        for key, plan in pc.registry.all_plans.items():
            plans[key] = {
                "key": plan.key,
                "name": plan.name,
                "price": plan.price,
                "price_display": f"${plan.price / 100:.0f}" if plan.price else "Free",
                "features": plan.features,
                "limits": {
                    r: (
                        cfg.hard
                        if isinstance(
                            cfg,
                            __import__(
                                "plancraft.core.models", fromlist=["LimitConfig"]
                            ).LimitConfig,
                        )
                        else cfg
                    )
                    for r, cfg in plan.limits.items()
                },
                "stripe_price_id": plan.stripe_price_id,
            }

        features = {}
        for key, feature in pc.registry.all_features.items():
            features[key] = {
                "key": feature.key,
                "name": feature.name,
                "description": feature.description,
                "available_on": pc.registry.which_plan_has_feature(key),
            }

        return {"plans": plans, "features": features, "default_plan": pc.registry.default_plan}

    @router.get("/plan")
    async def get_current_plan(entity: Any = Depends(entity_dep)):
        """Current entity's plan with feature access map."""
        from plancraft.core.models import LimitConfig

        plan_key = pc.enforcer._get_plan_key(entity)
        plan = pc.registry.get_plan(plan_key)

        feature_access = {
            key: pc.enforcer.can(entity, key).allowed for key in pc.registry.all_features
        }

        limits = {}
        for resource, cfg in plan.limits.items():
            if isinstance(cfg, LimitConfig):
                limits[resource] = {"hard": cfg.hard, "soft": cfg.soft}
            else:
                limits[resource] = {"hard": cfg, "soft": None}

        return {
            "plan": plan_key,
            "plan_name": plan.name,
            "price": plan.price,
            "features": feature_access,
            "limits": limits,
        }

    @router.get("/usage")
    async def get_usage(
        entity: Any = Depends(entity_dep),
        db: Any = Depends(db_dep),
    ):
        """Current usage vs plan limits — for usage meters in UI."""
        usage = {}
        plan_key = pc.enforcer._get_plan_key(entity)
        plan = pc.registry.get_plan(plan_key)

        for resource in plan.limits:
            if resource in pc.registry._counters:
                usage[resource] = await pc.enforcer.get_usage(entity, resource, db)

        return {"usage": usage}

    return router
