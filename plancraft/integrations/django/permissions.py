"""
Django REST Framework permission classes for plancraft.

Usage:
    from plancraft.integrations.django.permissions import HasFeature, WithinLimit

    class EnvironmentViewSet(ModelViewSet):
        permission_classes = [IsAuthenticated, HasFeature("multi_env", pc)]

    # Or inline:
    @api_view(["POST"])
    @permission_classes([IsAuthenticated, HasFeature("monitoring", pc)])
    def activate_monitoring(request): ...
"""

from __future__ import annotations

from typing import Any


def HasFeature(feature_key: str, pc: Any):  # noqa: N802
    """
    Returns a DRF permission class that checks a plancraft feature.
    Raises 402 if the billing entity's plan does not include the feature.

    Usage:
        permission_classes = [IsAuthenticated, HasFeature("monitoring", pc)]

    The view must implement `get_billing_entity(self)` returning the entity.
    """
    try:
        from rest_framework import permissions, status
        from rest_framework.exceptions import PermissionDenied

        class _PaymentRequired(PermissionDenied):
            status_code = status.HTTP_402_PAYMENT_REQUIRED

        class _HasFeature(permissions.BasePermission):
            def has_permission(self, request, view):
                get_entity = getattr(view, "get_billing_entity", None)
                if get_entity is None:
                    raise RuntimeError("HasFeature: view must implement get_billing_entity()")
                entity = get_entity()
                result = pc.can(entity, feature_key)
                if not result.allowed:
                    raise _PaymentRequired(
                        detail={
                            "code": "feature_not_available",
                            "feature": feature_key,
                            "upgrade_to": result.upgrade_to,
                            "message": (
                                f"Upgrade to {result.upgrade_to} to access this feature."
                                if result.upgrade_to
                                else "This feature is not available on your current plan."
                            ),
                        }
                    )
                return True

        _HasFeature.__name__ = f"HasFeature_{feature_key}"
        return _HasFeature

    except ImportError as e:
        raise ImportError(
            "plancraft django permissions require djangorestframework. "
            "Install it with: pip install djangorestframework"
        ) from e


def WithinLimit(resource: str, pc: Any):  # noqa: N802
    """
    Returns a DRF permission class that checks a plancraft resource limit.
    Only blocks write methods (POST/PUT/PATCH).

    Usage:
        permission_classes = [IsAuthenticated, WithinLimit("environments", pc)]

    The view must implement `get_billing_entity(self)` and optionally
    `get_billing_db(self)` returning the DB session.
    """
    try:
        import asyncio

        from rest_framework import permissions, status
        from rest_framework.exceptions import PermissionDenied

        class _PaymentRequired(PermissionDenied):
            status_code = status.HTTP_402_PAYMENT_REQUIRED

        class _WithinLimit(permissions.BasePermission):
            def has_permission(self, request, view):
                if request.method not in ("POST", "PUT", "PATCH"):
                    return True
                get_entity = getattr(view, "get_billing_entity", None)
                if get_entity is None:
                    raise RuntimeError("WithinLimit: view must implement get_billing_entity()")
                entity = get_entity()
                db = getattr(view, "get_billing_db", lambda: None)()

                # Policy: fail OPEN only on genuine infrastructure failure
                # (the async bridge itself breaking — event loop errors, or
                # the worker thread timing out). Everything raised BY
                # pc.within_limit() itself — an unknown resource key, a
                # missing/misconfigured plan, or any other logic error in
                # the entitlement check — is a real defect, not
                # infrastructure, and must fail CLOSED (deny) so it can
                # never silently grant access. This matches the Django
                # mixins (PlanLimitMixin.perform_create), which have no
                # catch-all at all and would propagate such errors.
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    return True  # no event loop available — infra failure

                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(asyncio.run, pc.within_limit(entity, resource, db))
                        try:
                            result = future.result(timeout=5)
                        except concurrent.futures.TimeoutError:
                            return True  # worker hung — infra failure, fail open
                        # Any other exception is from within_limit() itself
                        # (or code it calls) and propagates uncaught — fail closed.
                else:
                    result = loop.run_until_complete(pc.within_limit(entity, resource, db))

                if not result.allowed:
                    raise _PaymentRequired(
                        detail={
                            "code": "limit_reached",
                            "resource": resource,
                            "current": getattr(result, "current", None),
                            "limit": getattr(result, "limit", None),
                            "message": (
                                f"Plan limit of {result.limit} {resource} reached. "
                                f"Upgrade to add more."
                            ),
                        }
                    )
                return True

        _WithinLimit.__name__ = f"WithinLimit_{resource}"
        return _WithinLimit

    except ImportError as e:
        raise ImportError(
            "plancraft django permissions require djangorestframework. "
            "Install it with: pip install djangorestframework"
        ) from e
