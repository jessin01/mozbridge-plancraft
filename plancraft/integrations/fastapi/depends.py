"""
FastAPI dependencies — require_feature() and require_limit().

These are the primary integration points. Projects add them to routes:
    @router.post("/environments")
    async def create(
        _ = Depends(pc.require_feature("multi_env")),
        _ = Depends(pc.require_limit("environments")),
    ): ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException

if TYPE_CHECKING:
    from plancraft.plancraft import PlanCraft


def make_feature_dependency(pc: "PlanCraft", feature_key: str):
    """
    Returns a FastAPI dependency that checks feature access.
    Raises HTTP 402 if entity's plan does not include the feature.
    """

    async def _check_feature(
        entity: Any = Depends(_get_entity_dep(pc)),
    ):
        result = pc.enforcer.can(entity, feature_key)
        if not result:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "feature_not_available",
                    "feature": feature_key,
                    "reason": result.reason,
                    "upgrade_to": result.upgrade_to,
                    "message": (
                        f"Upgrade to {result.upgrade_to} to access this feature."
                        if result.upgrade_to
                        else "This feature is not available on your current plan."
                    ),
                },
            )
        return result

    return _check_feature


def make_limit_dependency(pc: "PlanCraft", resource: str):
    """
    Returns a FastAPI dependency that checks resource limits.
    Raises HTTP 402 if entity is at or over their plan limit.
    """

    async def _check_limit(
        entity: Any = Depends(_get_entity_dep(pc)),
        db: Any = Depends(_get_db_dep(pc)),
    ):
        result = await pc.enforcer.within_limit(entity, resource, db)
        if not result:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "limit_reached",
                    "resource": resource,
                    "current": result.current,
                    "limit": result.limit,
                    "reason": result.reason,
                    "message": (
                        f"You have reached your plan limit of {result.limit} {resource}. "
                        f"Upgrade your plan to add more."
                    ),
                },
            )
        return result

    return _check_limit


def make_combined_dependency(pc: "PlanCraft", checks: list[str]):
    """
    Returns a FastAPI dependency that runs multiple checks in order.
    checks format: ["feature:monitoring", "limit:environments"]
    Fail-fast — first failed check raises immediately.
    """

    async def _combined(
        entity: Any = Depends(_get_entity_dep(pc)),
        db: Any = Depends(_get_db_dep(pc)),
    ):
        for check in checks:
            if check.startswith("feature:"):
                feature_key = check.removeprefix("feature:")
                result = pc.enforcer.can(entity, feature_key)
                if not result:
                    raise HTTPException(
                        status_code=402,
                        detail={
                            "code": "feature_not_available",
                            "feature": feature_key,
                            "upgrade_to": result.upgrade_to,
                            "message": f"Upgrade to {result.upgrade_to} to access this feature.",
                        },
                    )

            elif check.startswith("limit:"):
                resource = check.removeprefix("limit:")
                result = await pc.enforcer.within_limit(entity, resource, db)
                if not result:
                    raise HTTPException(
                        status_code=402,
                        detail={
                            "code": "limit_reached",
                            "resource": resource,
                            "current": result.current,
                            "limit": result.limit,
                            "message": f"Plan limit of {result.limit} {resource} reached.",
                        },
                    )

        return entity

    return _combined


# ------------------------------------------------------------------
# Internal helpers — resolve entity and db from PlanCraft config
# ------------------------------------------------------------------


def _get_entity_dep(pc: "PlanCraft"):
    """
    Returns a dependency that resolves the billing entity.
    Uses pc._get_entity if provided, otherwise raises a clear error.
    """
    if pc._get_entity is not None:
        return pc._get_entity

    async def _missing():
        raise RuntimeError(
            "PlanCraft: get_entity not configured. "
            "Pass get_entity=your_dependency to PlanCraft() or "
            "override _get_entity on the instance."
        )

    return _missing


def _get_db_dep(pc: "PlanCraft"):
    """Returns a dependency that resolves the DB session."""
    if pc._get_db is not None:
        return pc._get_db

    async def _missing():
        raise RuntimeError(
            "PlanCraft: get_db not configured. " "Pass get_db=your_db_dependency to PlanCraft()."
        )

    return _missing
