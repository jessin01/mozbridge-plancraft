"""
ResourceCounter — project-defined classes that tell plancraft
how to count current usage of a resource.

Projects subclass ResourceCounter and register them in catalog.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ResourceCounter(ABC):
    """
    Base class for all resource counters.

    Projects subclass this to tell plancraft how to count
    their specific resources (environments, members, etc.)

    Example:
        class EnvironmentCounter(ResourceCounter):
            resource = "environments"

            async def count(self, entity, db) -> int:
                return db.query(Environment)\\
                         .filter(Environment.project_id == entity.id)\\
                         .count()
    """

    resource: str = ""  # must match key in Plan.limits

    @abstractmethod
    async def count(self, entity: Any, db: Any) -> int:
        """
        Return current count of this resource for the given entity.

        entity  — the billing entity (org or project model instance)
        db      — database session (SQLAlchemy, Django ORM, etc.)
        """

    async def on_create(self, entity: Any, db: Any) -> None:
        """
        Called after a resource is successfully created.
        Override to invalidate cache, update counters, etc.
        Default: no-op.
        """

    async def on_delete(self, entity: Any, db: Any) -> None:
        """
        Called after a resource is deleted.
        Override to invalidate cache, update counters, etc.
        Default: no-op.
        """
