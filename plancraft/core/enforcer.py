"""
Enforcer — the core entitlement engine.

Answers two questions:
  1. can(entity, feature)         → does this entity's plan include this feature?
  2. within_limit(entity, resource, db) → is this entity under their resource limit?

Framework-agnostic. No FastAPI, no Django here.
Cache is injected — defaults to in-memory (no Redis required).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .models import EntitlementOverride, LimitConfig
from .registry import Registry

logger = logging.getLogger(__name__)


class CheckResult:
    """Result of an entitlement or limit check."""

    def __init__(
        self,
        allowed: bool,
        reason: str = "",
        current: int | None = None,
        limit: int | None = None,
        upgrade_to: str | None = None,
        feature: str | None = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.current = current
        self.limit = limit
        self.upgrade_to = upgrade_to
        self.feature = feature

    def __bool__(self):
        return self.allowed

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "current": self.current,
            "limit": self.limit,
            "upgrade_to": self.upgrade_to,
            "feature": self.feature,
        }


class Enforcer:
    """
    Core enforcement engine.
    Injected with a registry and optional cache + override store.
    """

    def __init__(
        self,
        registry: Registry,
        cache: Any | None = None,
        get_plan_key: Any | None = None,
    ):
        self.registry = registry
        self.cache = cache
        # get_plan_key(entity) → str
        # Project provides this — tells enforcer how to get plan from entity
        self._get_plan_key = get_plan_key or (lambda entity: getattr(entity, "plan", "free"))

    # ------------------------------------------------------------------
    # Feature checks
    # ------------------------------------------------------------------

    def can(
        self,
        entity: Any,
        feature_key: str,
        overrides: list[EntitlementOverride] | None = None,
    ) -> CheckResult:
        """
        Check if entity's plan includes feature_key.
        Checks overrides first, then plan.
        """
        # 1. Check overrides (special deals, trials, support)
        if overrides:
            override = self._find_override(entity, feature_key, overrides)
            if override is not None:
                return CheckResult(
                    allowed=override.granted,
                    reason="override",
                    feature=feature_key,
                )

        # 2. Validate feature exists in registry
        self.registry.get_feature(feature_key)  # raises ValueError if unknown

        # 3. Check plan
        plan_key = self._get_plan_key(entity)
        plan = self.registry.get_plan(plan_key)

        if plan.has_feature(feature_key):
            return CheckResult(allowed=True, feature=feature_key)

        upgrade_to = self.registry.which_plan_has_feature(feature_key)
        return CheckResult(
            allowed=False,
            reason=f"Feature '{feature_key}' not available on '{plan_key}' plan",
            upgrade_to=upgrade_to,
            feature=feature_key,
        )

    # ------------------------------------------------------------------
    # Limit checks
    # ------------------------------------------------------------------

    async def within_limit(
        self,
        entity: Any,
        resource: str,
        db: Any,
    ) -> CheckResult:
        """
        Check if entity is within their plan limit for resource.
        Calls the registered counter to get current usage.
        """
        plan_key = self._get_plan_key(entity)
        limit_config: LimitConfig = self.registry.get_limit(plan_key, resource)

        # Unlimited — always pass
        if limit_config.hard == -1:
            return CheckResult(allowed=True, reason="unlimited", limit=-1)

        # Get current count from project-defined counter
        counter = self.registry.get_counter(resource)
        current = await counter.count(entity, db)

        # Hard limit check
        if current >= limit_config.hard:
            return CheckResult(
                allowed=False,
                reason=f"Hard limit of {limit_config.hard} reached for '{resource}'",
                current=current,
                limit=limit_config.hard,
            )

        # Soft limit warning (still allowed but flagged)
        if limit_config.soft is not None and current >= limit_config.soft:
            logger.warning(
                "Soft limit reached: entity=%s resource=%s current=%d soft=%d",
                getattr(entity, "id", entity),
                resource,
                current,
                limit_config.soft,
            )

        return CheckResult(
            allowed=True,
            current=current,
            limit=limit_config.hard,
        )

    # ------------------------------------------------------------------
    # Usage info (for dashboard/UI)
    # ------------------------------------------------------------------

    async def get_usage(self, entity: Any, resource: str, db: Any) -> dict:
        """Return usage info for display — current, limit, percentage."""
        plan_key = self._get_plan_key(entity)
        limit_config = self.registry.get_limit(plan_key, resource)

        counter = self.registry.get_counter(resource)
        current = await counter.count(entity, db)

        hard = limit_config.hard
        pct = (current / hard * 100) if hard > 0 else 0

        return {
            "resource": resource,
            "current": current,
            "limit": hard,
            "unlimited": hard == -1,
            "percentage": round(pct, 1),
            "exceeded": current >= hard if hard != -1 else False,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_override(
        self,
        entity: Any,
        feature_key: str,
        overrides: list[EntitlementOverride],
    ) -> EntitlementOverride | None:
        entity_id = str(getattr(entity, "id", ""))
        now = datetime.utcnow()
        for o in overrides:
            if o.entity_id == entity_id and o.feature == feature_key:
                if o.expires_at is None or o.expires_at > now:
                    return o
        return None
