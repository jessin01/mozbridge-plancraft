"""Whop billing integration — webhook verification and entitlement mapping.

Whop rather than Stripe because Whop onboards individuals without a registered
company (no KVK), and acts as merchant of record — so it also carries the EU VAT
obligation, which matters more for a solo operator than the registration itself.

Verified against the live docs on 2026-08-11:

* API base is ``https://api.whop.com/api/v1``; auth is ``Authorization: Bearer <key>``.
* Webhooks follow the Standard Webhooks spec: headers ``webhook-id``,
  ``webhook-timestamp`` and ``webhook-signature``, the signed content is
  ``{id}.{timestamp}.{body}``, HMAC-SHA256 keyed with the **base64-decoded**
  secret, and the signature header is ``v1,<base64>`` (possibly several
  space-separated versions).
* Events: ``payment.succeeded``, ``membership.activated``,
  ``membership.deactivated``, ``membership.trial_ending_soon``,
  ``refund.created``, ``dispute.created``, ``entry.created``,
  ``ledger_account.funds_available``.

NOT verified, and it shapes the design: the public docs do not describe an
endpoint for querying whether a given membership is currently valid. Until that
is confirmed against a real account, **the webhook stream is the only source of
truth** and there is no reconciliation job that could repair drift after a
missed delivery. Hence: strict signature checking, strict replay bounds, and
idempotent persistence of every event -- if the stream is authoritative, losing
or double-applying one event is a billing error.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger(__name__)

WHOP_API_BASE = "https://api.whop.com/api/v1"

# Standard Webhooks recommends five minutes. Beyond this a replayed request is
# rejected even with a valid signature.
DEFAULT_TOLERANCE_SECONDS = 300

# Events that change entitlement. Anything outside this set is stored and
# acknowledged but does not touch a plan -- an unknown event must never be
# guessed at, and must never 4xx either, or Whop will retry it forever.
GRANTS_ACCESS = frozenset({"membership.activated"})
REVOKES_ACCESS = frozenset({"membership.deactivated", "refund.created", "dispute.created"})
KNOWN_EVENTS = (
    GRANTS_ACCESS
    | REVOKES_ACCESS
    | frozenset(
        {
            "payment.succeeded",
            "membership.trial_ending_soon",
            "entry.created",
            "ledger_account.funds_available",
        }
    )
)


class WhopSignatureError(Exception):
    """Raised when a webhook cannot be proven to have come from Whop.

    A distinct type so the route can answer 401 for this and 500 for a genuine
    bug. Conflating the two is how a broken verifier gets "fixed" by disabling
    it: every request looks like a server error, someone adds a try/except, and
    the endpoint silently becomes unauthenticated.
    """


def _secret_bytes(secret: str) -> bytes:
    """Standard Webhooks secrets are base64, optionally ``whsec_``-prefixed.

    Falls back to the raw UTF-8 bytes when the value is not valid base64, so a
    plain-text secret configured by hand still works rather than failing in a
    way that looks like a signature mismatch.
    """
    raw = secret.removeprefix("whsec_")
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        return secret.encode()


def verify_signature(
    payload: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    secret: str | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Verify a Standard Webhooks signature, or raise :class:`WhopSignatureError`.

    Returns ``None`` on success and raises otherwise, so a caller cannot ignore
    the result by accident -- ``if verify(...)`` on a function that returns a
    bool is a single missing ``not`` away from accepting everything.

    ``payload`` MUST be the raw request body. Re-serialising the parsed JSON
    changes key order and whitespace and will never match.
    """
    secret = secret if secret is not None else os.getenv("WHOP_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed. An unset secret must not mean "skip verification" --
        # that is the single most common way a webhook endpoint ends up open.
        raise WhopSignatureError("WHOP_WEBHOOK_SECRET is not configured")

    if not (webhook_id and webhook_timestamp and webhook_signature):
        raise WhopSignatureError("missing webhook-id, webhook-timestamp or webhook-signature")

    try:
        sent_at = int(webhook_timestamp)
    except ValueError:
        raise WhopSignatureError("webhook-timestamp is not an integer") from None

    drift = abs(time.time() - sent_at)
    if drift > tolerance_seconds:
        # Bounded both ways: a future timestamp is as suspicious as an old one.
        raise WhopSignatureError(
            f"timestamp outside tolerance ({drift:.0f}s > {tolerance_seconds}s)"
        )

    signed = f"{webhook_id}.{sent_at}.".encode() + payload
    expected = base64.b64encode(
        hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest()
    ).decode()

    # The header may carry several space-separated versioned signatures, so a
    # key can be rotated without dropping deliveries. Accept if ANY v1 matches.
    for part in webhook_signature.split():
        version, _, candidate = part.partition(",")
        if version == "v1" and hmac.compare_digest(candidate, expected):
            return

    raise WhopSignatureError("no matching v1 signature")


def extract_membership(event: dict) -> dict:
    """Pull the fields we persist out of a webhook body.

    Deliberately tolerant about shape. The exact envelope is not pinned in the
    public docs, so this reads the common spellings rather than asserting one
    and raising on real traffic. Every field is optional; the caller decides
    what it can act on.
    """
    data = event.get("data") or event.get("payload") or {}
    if not isinstance(data, dict):
        data = {}

    def pick(*names: str) -> str | None:
        for n in names:
            v = data.get(n)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                inner = v.get("id")
                if isinstance(inner, str) and inner:
                    return inner
        return None

    return {
        "membership_id": pick("id", "membership_id", "membership"),
        "user_id": pick("user_id", "user"),
        "email": pick("email", "user_email"),
        "plan_id": pick("plan_id", "plan"),
        "product_id": pick("product_id", "product"),
        # Where the org linkage is expected to arrive. Whop metadata passthrough
        # at checkout is UNVERIFIED -- see the module docstring. Until it is
        # confirmed against a real account, `resolve_org` also falls back to
        # matching on email.
        "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    }
