"""
Registry — holds the catalog of plans and features defined by the project.
Projects call register() once at startup with their catalog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Feature, LimitConfig, Plan

if TYPE_CHECKING:
    from .counter import ResourceCounter


class Registry:
    """
    Central catalog. Holds plans, features, counters registered by the project.
    One instance per PlanCraft instance.
    """

    def __init__(self):
        self._features: dict[str, Feature] = {}
        self._plans: dict[str, Plan] = {}
        self._counters: dict[str, "ResourceCounter"] = {}
        self._default_plan: str = "free"

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        features: dict[str, Feature],
        plans: dict[str, Plan],
        counters: list[type["ResourceCounter"]] | None = None,
        default_plan: str = "free",
    ) -> None:
        """
        Register the project's billing catalog.

        features    — dict of feature_key → Feature
        plans       — dict of plan_key → Plan
        counters    — list of ResourceCounter subclasses (not instances)
        default_plan— plan assigned to new entities on creation
        """
        # Inject keys from dict into models (allow shorthand definitions)
        for key, feature in features.items():
            feature.key = key
            self._features[key] = feature

        for key, plan in plans.items():
            plan.key = key
            self._plans[key] = plan

        if counters:
            for counter in counters:
                # Accept both classes (instantiated with no args) and instances
                if isinstance(counter, type):
                    instance = counter()
                else:
                    instance = counter
                self._counters[instance.resource] = instance

        self._default_plan = default_plan

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_plan(self, plan_key: str) -> Plan:
        plan = self._plans.get(plan_key)
        if not plan:
            # Fall back to default rather than raising — graceful degradation
            plan = self._plans.get(self._default_plan)
        if not plan:
            raise ValueError(f"Plan '{plan_key}' not found in registry and no default plan set.")
        return plan

    def get_feature(self, feature_key: str) -> Feature:
        feature = self._features.get(feature_key)
        if not feature:
            raise ValueError(f"Feature '{feature_key}' not registered in catalog.")
        return feature

    def get_counter(self, resource: str) -> "ResourceCounter":
        counter = self._counters.get(resource)
        if not counter:
            raise ValueError(
                f"No counter registered for resource '{resource}'. "
                f"Add a ResourceCounter subclass to your register() call."
            )
        return counter

    def which_plan_has_feature(self, feature_key: str) -> str | None:
        """Return the lowest plan key that includes this feature."""
        for key, plan in self._plans.items():
            if plan.has_feature(feature_key):
                return key
        return None

    @property
    def default_plan(self) -> str:
        return self._default_plan

    @property
    def all_plans(self) -> dict[str, Plan]:
        return dict(self._plans)

    @property
    def all_features(self) -> dict[str, Feature]:
        return dict(self._features)

    def get_limit(self, plan_key: str, resource: str) -> LimitConfig:
        return self.get_plan(plan_key).get_limit(resource)
