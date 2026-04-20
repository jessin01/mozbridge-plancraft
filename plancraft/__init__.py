"""
plancraft — pluggable plan, feature gating, and billing engine.

Quick start:
    from plancraft import PlanCraft, Feature, Plan, ResourceCounter

    pc = PlanCraft(get_entity=get_current_project, get_db=get_db)
    pc.register(features=FEATURES, plans=PLANS, counters=[MyCounter])

    app.include_router(pc.router, prefix="/api/v1/billing")
"""

from .core.counter import ResourceCounter
from .core.models import EntitlementOverride, Feature, LimitConfig, Plan
from .plancraft import PlanCraft

__all__ = [
    "PlanCraft",
    "Feature",
    "Plan",
    "LimitConfig",
    "ResourceCounter",
    "EntitlementOverride",
]

__version__ = "0.1.0"
