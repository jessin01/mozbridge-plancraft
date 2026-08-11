"""
Django/DRF integration tests.

This venv does not have django/rest_framework installed (matches the
vendored package's actual optional-dependency story: `django` is an
extra, not a hard dependency). That makes "imports and initialises
cleanly" the single most important thing to pin here — plancraft must
be importable by a project that has plancraft installed but has not
yet added the django extra, and only fail loudly at the point where
Django/DRF-specific functionality is actually invoked.
"""

from __future__ import annotations

import pytest

from plancraft import PlanCraft


class TestModulesImportWithoutDjangoInstalled:
    def test_mixins_module_imports_cleanly(self):
        import plancraft.integrations.django.mixins as mixins

        assert hasattr(mixins, "PlanFeatureMixin")
        assert hasattr(mixins, "PlanLimitMixin")

    def test_permissions_module_imports_cleanly(self):
        import plancraft.integrations.django.permissions as permissions

        assert hasattr(permissions, "HasFeature")
        assert hasattr(permissions, "WithinLimit")

    def test_django_package_import_does_not_require_django(self):
        # The package __init__ itself must not eagerly import django/DRF.
        import plancraft.integrations.django  # noqa: F401


class TestPermissionFactoriesRequireDRF:
    """
    HasFeature()/WithinLimit() are called at Django view-class-definition
    time (module import time for the consuming project), so failing loudly
    and immediately when DRF is absent is the correct, safe behaviour —
    pinning it so it doesn't quietly change to e.g. returning None.
    """

    def test_has_feature_raises_import_error_without_drf(self):
        pc = PlanCraft()
        pc.register(features={}, plans={})
        with pytest.raises(ImportError, match="djangorestframework"):
            from plancraft.integrations.django.permissions import HasFeature

            HasFeature("monitoring", pc)

    def test_within_limit_permission_raises_import_error_without_drf(self):
        pc = PlanCraft()
        pc.register(features={}, plans={})
        with pytest.raises(ImportError, match="djangorestframework"):
            from plancraft.integrations.django.permissions import WithinLimit

            WithinLimit("widgets", pc)


class TestMixinContractWithoutDjangoRuntime:
    """
    PlanFeatureMixin/PlanLimitMixin are designed to be mixed into a DRF
    ViewSet (which supplies `.initial()`/`.perform_create()` via MRO). We
    can't exercise the full `.initial()`/`.perform_create()` call chain
    without django+DRF installed, but the parts of the contract that are
    plancraft's own responsibility — no plancraft-owned entity resolver,
    and the class-level defaults — can and should be pinned here.
    """

    def test_get_billing_entity_is_not_implemented_by_default(self):
        from plancraft.integrations.django.mixins import PlanFeatureMixin

        class Bare(PlanFeatureMixin):
            pass

        with pytest.raises(NotImplementedError):
            Bare().get_billing_entity()

    def test_limit_mixin_get_billing_entity_is_not_implemented_by_default(self):
        from plancraft.integrations.django.mixins import PlanLimitMixin

        class Bare(PlanLimitMixin):
            pass

        with pytest.raises(NotImplementedError):
            Bare().get_billing_entity()

    def test_limit_mixin_get_billing_db_defaults_to_none(self):
        from plancraft.integrations.django.mixins import PlanLimitMixin

        class Bare(PlanLimitMixin):
            def get_billing_entity(self):
                return object()

        assert Bare().get_billing_db() is None

    def test_pc_feature_and_pc_instance_default_to_falsy(self):
        from plancraft.integrations.django.mixins import PlanFeatureMixin

        assert PlanFeatureMixin.pc_feature == ""
        assert PlanFeatureMixin.pc_instance is None
