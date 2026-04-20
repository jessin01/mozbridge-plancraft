"""
Core domain models for plancraft.
Framework-agnostic — no FastAPI, no Django, no SQLAlchemy here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Feature:
    """A single gatable feature."""

    key: str = ""
    name: str = ""
    description: str = ""
    tier: str = ""  # informational — which tier this appears in

    def __post_init__(self):
        # Allow Feature("Grafana Monitoring") shorthand — name only
        if self.key == "" and self.name != "":
            self.key = self.name.lower().replace(" ", "_")


@dataclass
class LimitConfig:
    """
    Defines hard/soft limits for a resource.

    soft        — warn at this count (optional)
    hard        — block at this count (-1 = unlimited)
    grace_days  — days allowed between soft and hard before blocking
    """

    hard: int  # -1 = unlimited
    soft: int | None = None
    grace_days: int = 0


@dataclass
class Plan:
    """A single pricing tier."""

    key: str = ""
    name: str = ""
    price: int = 0  # integer cents — never floats
    features: list[str] = field(default_factory=list)
    limits: dict[str, int | LimitConfig] = field(default_factory=dict)
    stripe_price_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_feature(self, feature_key: str) -> bool:
        return feature_key in self.features

    def get_limit(self, resource: str) -> LimitConfig:
        raw = self.limits.get(resource)
        if raw is None:
            return LimitConfig(hard=0)
        if isinstance(raw, int):
            return LimitConfig(hard=raw)
        return raw

    def is_unlimited(self, resource: str) -> bool:
        return self.get_limit(resource).hard == -1


@dataclass
class EntitlementOverride:
    """
    Per-org/project override — grant or revoke a feature outside of plan.
    Used for trials, partner deals, support exceptions.
    """

    entity_id: str  # org_id or project_id as string
    feature: str
    granted: bool
    reason: str = ""
    expires_at: Any = None  # datetime | None
