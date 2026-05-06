"""
Django / DRF mixins for plancraft feature gating and resource limits.

Usage (DRF ViewSet):

    from plancraft.integrations.django.mixins import PlanFeatureMixin, PlanLimitMixin

    class EnvironmentViewSet(PlanFeatureMixin, PlanLimitMixin, ModelViewSet):
        pc_feature = "multi_env"        # blocks all access if feature not in plan
        pc_limit   = "environments"     # blocks create if at limit
        pc_instance = pc                # your PlanCraft instance

        def get_billing_entity(self):
            return self.request.user.organization  # whatever holds .plan

        def get_billing_db(self):
            return None  # unused for Django ORM; override if you need it
"""

from __future__ import annotations

from typing import Any


def _get_http_exception():
    try:
        from rest_framework.exceptions import PermissionDenied
        from rest_framework import status

        class PaymentRequired(PermissionDenied):
            status_code = status.HTTP_402_PAYMENT_REQUIRED

        return PaymentRequired
    except ImportError:
        from django.http import Http404

        return Http404


class PlanFeatureMixin:
    """
    Block ALL actions on the view if the entity's plan lacks the feature.

    Set on the view:
        pc_feature  = "multi_env"     — the feature key to check
        pc_instance = pc              — your PlanCraft instance
    """

    pc_feature: str = ""
    pc_instance: Any = None

    def get_billing_entity(self) -> Any:
        raise NotImplementedError("Override get_billing_entity() to return the billing entity.")

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)  # type: ignore[misc]
        if not self.pc_feature or self.pc_instance is None:
            return
        entity = self.get_billing_entity()
        result = self.pc_instance.can(entity, self.pc_feature)
        if not result.allowed:
            Exc = _get_http_exception()
            detail = {
                "code": "feature_not_available",
                "feature": self.pc_feature,
                "upgrade_to": result.upgrade_to,
                "message": (
                    f"Upgrade to {result.upgrade_to} to access this feature."
                    if result.upgrade_to
                    else "This feature is not available on your current plan."
                ),
            }
            raise Exc(detail=detail)


class PlanLimitMixin:
    """
    Block CREATE actions when the entity is at their resource limit.

    Set on the view:
        pc_limit    = "environments"  — the resource key to check
        pc_instance = pc              — your PlanCraft instance
    """

    pc_limit: str = ""
    pc_instance: Any = None

    def get_billing_entity(self) -> Any:
        raise NotImplementedError("Override get_billing_entity() to return the billing entity.")

    def get_billing_db(self) -> Any:
        return None

    def perform_create(self, serializer):
        import asyncio

        if self.pc_limit and self.pc_instance is not None:
            entity = self.get_billing_entity()
            db = self.get_billing_db()
            # Run async limit check synchronously inside Django's sync context.
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            asyncio.run, self.pc_instance.within_limit(entity, self.pc_limit, db)
                        )
                        result = future.result(timeout=5)
                else:
                    result = loop.run_until_complete(
                        self.pc_instance.within_limit(entity, self.pc_limit, db)
                    )
            except Exception:
                result = type("R", (), {"allowed": True})()

            if not result.allowed:
                Exc = _get_http_exception()
                detail = {
                    "code": "limit_reached",
                    "resource": self.pc_limit,
                    "current": getattr(result, "current", None),
                    "limit": getattr(result, "limit", None),
                    "message": f"Plan limit reached for {self.pc_limit}. Upgrade to add more.",
                }
                raise Exc(detail=detail)

        super().perform_create(serializer)  # type: ignore[misc]

        # Invalidate cache after successful create
        if self.pc_limit and self.pc_instance is not None:
            entity = self.get_billing_entity()
            self.pc_instance.invalidate_resource(str(getattr(entity, "id", "")), self.pc_limit)
