# plancraft

A pluggable plan, feature-gating, and usage-limit engine for SaaS applications.

`plancraft` answers two questions for a request:

1. **Can this entity use this feature?** (`require_feature`, `can`)
2. **Is this entity under their plan's limit for this resource?** (`require_limit`, `within_limit`)

It is framework-agnostic at the core (`plancraft.core`) with thin integration layers for
FastAPI and Django/DRF. It does not talk to a database itself — you tell it how to count
resources (`ResourceCounter`) and how to resolve the current billing entity and plan.

**Payment-processor / billing integration is not implemented.** There is a `stripe` extra
and a `plancraft.webhooks` module with processor-agnostic Standard Webhooks signature
verification, but nothing in this package charges cards, creates subscriptions, or talks
to Stripe's API. Do not adopt `plancraft` expecting billing — it is entitlement and limit
enforcement only.

## Install

Distribution name on PyPI will be **`mozbridge-plancraft`** (the name `plancraft` is
taken by an unrelated LLM-eval dataset). The import stays `plancraft`:

```bash
pip install mozbridge-plancraft
# or from a Mozbridge checkout:
pip install ./backend/vendor/plancraft-py
```

Optional extras, install what your project needs:

```bash
pip install "mozbridge-plancraft[fastapi]"   # fastapi + sqlalchemy
pip install "mozbridge-plancraft[django]"    # django + djangorestframework
pip install "mozbridge-plancraft[redis]"     # redis client
pip install "mozbridge-plancraft[all]"       # fastapi + redis + stripe deps (no django)
pip install "mozbridge-plancraft[dev]"       # pytest, ruff, build, twine
```

Note: `all` deliberately excludes `django` (Django and FastAPI integrations are normally
installed one at a time, not together) and includes `stripe` only because a future
processor integration will need it — `plancraft` itself does not use the `stripe` package
today.

Requires Python >= 3.11. Core dependency: `pydantic>=2.0`.

## Quick start — defining plans and enforcing a limit

```python
from plancraft import Feature, LimitConfig, Plan, PlanCraft, ResourceCounter

# 1. Define your catalog — features and plans are plain dataclasses.
FEATURES = {
    "monitoring": Feature(name="Grafana Monitoring"),
    "custom_domain": Feature(name="Custom Domain"),
}

PLANS = {
    "free": Plan(
        name="Free",
        price=0,
        features=[],
        limits={"projects": LimitConfig(hard=2)},
    ),
    "pro": Plan(
        name="Pro",
        price=2900,  # integer cents
        features=["monitoring"],
        limits={"projects": LimitConfig(hard=10)},
    ),
    "enterprise": Plan(
        name="Enterprise",
        price=9900,
        features=["monitoring", "custom_domain"],
        limits={"projects": LimitConfig(hard=-1)},  # -1 = unlimited
    ),
}


# 2. Tell plancraft how to count a resource for an entity. It never queries
#    your database itself.
class ProjectCounter(ResourceCounter):
    resource = "projects"  # must match the key used in Plan.limits

    async def count(self, entity, db) -> int:
        return db.query(Project).filter(Project.org_id == entity.id).count()


# 3. Wire it up. get_entity/get_db are only needed for the FastAPI/Django
#    dependency helpers below — can(), within_limit() work without them too.
def get_current_org(request) -> "Org":
    ...  # however your app resolves the current org/tenant


pc = PlanCraft(get_entity=get_current_org, get_db=get_db)
pc.register(features=FEATURES, plans=PLANS, counters=[ProjectCounter], default_plan="free")

# 4. Direct checks — usable anywhere, no framework required.
result = pc.can(org, "monitoring")
if not result.allowed:
    print(result.reason, "-> upgrade to", result.upgrade_to)

usage_result = await pc.within_limit(org, "projects", db)
if not usage_result.allowed:
    print(f"at limit: {usage_result.current}/{usage_result.limit}")
```

`CheckResult` (returned by `can`/`within_limit`) is truthy/falsy via `__bool__`, so
`if not result:` works directly, and `result.to_dict()` gives a JSON-serializable form for
API responses.

After any create/delete of a counted resource, or a plan change, invalidate the relevant
cache entry:

```python
pc.invalidate_resource(str(org.id), "projects")   # after create/delete
pc.invalidate_entity(str(org.id))                  # after a plan change
pc.invalidate_overrides(str(org.id))               # after adding/removing an override
```

## FastAPI integration

```python
from fastapi import FastAPI, Depends
from plancraft import PlanCraft

app = FastAPI()
pc = PlanCraft(get_entity=get_current_org, get_db=get_db)
pc.register(features=FEATURES, plans=PLANS, counters=[ProjectCounter])

# Mounts GET /billing/catalog, /billing/plan, /billing/usage
app.include_router(pc.router, prefix="/api/v1/billing")


@app.post("/projects")
async def create_project(
    _feature=Depends(pc.require_feature("monitoring")),
    _limit=Depends(pc.require_limit("projects")),
):
    ...
```

Both `require_feature` and `require_limit` raise `HTTPException(402)` with a structured
`detail` dict (`code`, `feature`/`resource`, `reason`/`current`/`limit`, `message`) when
the check fails. For several checks in one dependency, use `require`:

```python
_ = Depends(pc.require("feature:monitoring", "limit:projects"))
```

`require_feature`/`require_limit`/`require`/`router` all need `get_entity` (and `get_db`
for limit checks) passed to `PlanCraft(...)` — without them the dependency raises a
`RuntimeError` telling you what's missing, rather than failing silently.

## Django / DRF integration

**ViewSet mixins** (`plancraft.integrations.django.mixins`):

```python
from plancraft.integrations.django.mixins import PlanFeatureMixin, PlanLimitMixin

class ProjectViewSet(PlanFeatureMixin, PlanLimitMixin, ModelViewSet):
    pc_feature = "monitoring"     # blocks ALL actions if the plan lacks this feature
    pc_limit = "projects"         # blocks create() once the plan limit is reached
    pc_instance = pc              # your PlanCraft instance

    def get_billing_entity(self):
        return self.request.user.organization

    def get_billing_db(self):
        return None  # unused for the Django ORM; override if your counter needs it
```

`PlanFeatureMixin` requires djangorestframework to raise a proper 402
(`rest_framework.exceptions.PermissionDenied` subclass); without DRF installed it falls
back to `Http404`, which is why the `django` extra pulls in `djangorestframework` too.

**DRF permission classes** (`plancraft.integrations.django.permissions`), as an
alternative to the mixins:

```python
from plancraft.integrations.django.permissions import HasFeature, WithinLimit

class ProjectViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated, HasFeature("monitoring", pc), WithinLimit("projects", pc)]

    def get_billing_entity(self):
        return self.request.user.organization
```

`HasFeature`/`WithinLimit` both require `djangorestframework` and raise `ImportError`
with an install hint if it's missing — they are not usable with plain Django views.
`WithinLimit` only checks on `POST`/`PUT`/`PATCH`; it fails open (allows the request) if
the limit check itself raises, since a broken infra call should not take down writes.

## What's implemented vs. not

| Area | Status |
|---|---|
| Feature gating (`can`, `require_feature`) | Implemented |
| Resource limits (`within_limit`, `require_limit`) | Implemented |
| Per-entity overrides (trials, support grants) | Implemented (`EntitlementOverride`, pass `overrides=` to `can()`) |
| FastAPI dependencies + router | Implemented |
| Django/DRF mixins + permissions | Implemented |
| In-process TTL cache (`plancraft.cache.local_cache.LocalCache`) | Implemented, but not yet wired into `Enforcer`/`PlanCraft` automatically — `cache="redis"` on `PlanCraft.__init__` is accepted but not currently connected to anything |
| Webhook signature verification (`plancraft.webhooks`) | Standard Webhooks (Whop/Svix/Resend/Clerk/Polar-shaped) HMAC verification is implemented; **not payment-processor billing** |
| Payment processor integration (Stripe, etc.) | Not implemented. `Plan.stripe_price_id` and the `stripe` extra exist for a future integration only |
| `plancraft.gateways` | Empty stub package, reserved for future gateway implementations |

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

## Building the distribution

```bash
make build   # builds sdist + wheel into dist/, then twine check
```

See `Makefile` / `scripts/build.sh`. This does not upload — publishing to PyPI is a
separate, deliberate step.

## Versioning

See [CHANGELOG.md](CHANGELOG.md) for the version policy and release history.
