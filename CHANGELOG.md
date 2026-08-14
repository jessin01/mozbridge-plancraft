# Changelog

All notable changes to this project are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## Version policy

This project follows semantic-ish versioning ahead of a 1.0 release (`0.MINOR.PATCH`):

- **PATCH** (`0.1.0` -> `0.1.1`): bug fixes, doc fixes, packaging fixes, and additive
  internal changes that do not touch any name importable from `plancraft` or a documented
  integration point (FastAPI deps/router, Django mixins/permissions, `PlanCraft` public
  methods, `plancraft.webhooks` public functions).
- **MINOR** (`0.1.0` -> `0.2.0`): new public API (new class, new method, new extra, new
  integration), or a behavioral change to an existing public API that is backwards
  compatible for typical usage (e.g. a new optional parameter, a widened accepted input).
- Anything that removes or renames a public name, changes a method's required arguments,
  or changes the meaning of an existing return value is a **breaking change**. Before
  1.0 this still ships as a MINOR bump (per the `0.y.z` convention where `y` carries
  breaking changes), but it will always be called out explicitly in this file under
  "Breaking" — never silently folded into "Changed".

Once the API is stable enough to commit to, this moves to standard SemVer
(`MAJOR.MINOR.PATCH` from `1.0.0`) and PATCH/MINOR keep their usual meaning, with MAJOR
reserved for breaking changes.

Distribution name is a separate, independent decision from the import name (`plancraft`
imports the same regardless of what `pip install` name is chosen) — this file tracks the
import-level API and behavior.

## [Unreleased]

### Changed
- Distribution name locked to **`mozbridge-plancraft`** (import remains `plancraft`).
  README install instructions updated; CruxHive branding rejected (platform package).

## [0.1.0] - 2026-08-11

Initial vendored version.

### Added
- Core engine (`plancraft.core`): `Feature`, `Plan`, `LimitConfig`, `EntitlementOverride`
  models; `Registry` for catalog storage; `Enforcer` with `can()` and `within_limit()`;
  `ResourceCounter` base class for project-defined usage counting.
- `PlanCraft` façade (`plancraft.plancraft.PlanCraft`): `register()`, `require_feature()`,
  `require_limit()`, `require()`, `.router`, `can()`, `within_limit()`, `get_usage()`,
  cache-invalidation helpers (`invalidate_resource`, `invalidate_entity`,
  `invalidate_overrides`), and `reload_catalog()` for hot-reloading the catalog from a DB.
- FastAPI integration (`plancraft.integrations.fastapi`): `require_feature`/`require_limit`
  dependency factories raising `HTTPException(402)`, and a router exposing
  `GET /catalog`, `GET /plan`, `GET /usage`.
- Django/DRF integration (`plancraft.integrations.django`): `PlanFeatureMixin` /
  `PlanLimitMixin` for ViewSets, and `HasFeature()` / `WithinLimit()` DRF permission
  class factories.
- In-process TTL cache (`plancraft.cache.local_cache.LocalCache`) — not yet wired into
  the enforcer automatically.
- `plancraft.webhooks`: processor-agnostic Standard Webhooks HMAC signature verification
  (`verify_standard_webhook` and helpers) and idempotency support, plus dedicated
  exception types (`WebhookError`, `SignatureVerificationError`).
- `plancraft.gateways`: reserved empty package for future gateway implementations.
- Optional extras: `fastapi`, `django`, `redis`, `stripe`, `all`, `dev`.
- Packaging: explicit hatchling wheel/sdist file selection (independent of the eventual
  distribution name), `py.typed` marker, classifiers, `make build` / `scripts/build.sh`
  producing a `twine`-checked sdist + wheel.

### Known limitations
- No payment-processor (Stripe or otherwise) integration. `Plan.stripe_price_id` and the
  `stripe` extra are placeholders for a future integration.
- `cache="redis"` accepted by `PlanCraft.__init__` but not currently connected to any
  Redis client — the constructor stores the value and nothing reads it yet.
