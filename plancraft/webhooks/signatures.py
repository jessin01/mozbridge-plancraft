"""Standard Webhooks signature verification -- processor-agnostic.

Implements the `Standard Webhooks <https://www.standardwebhooks.com/>`_ spec
itself, not any one processor's reading of it. Whop, Svix, Resend, Clerk,
Polar and others all emit this shape:

* headers ``webhook-id``, ``webhook-timestamp``, ``webhook-signature``
* signed content is the bytes ``{id}.{timestamp}.`` followed by the raw body
* HMAC-SHA256 keyed with the **base64-decoded** secret
* the header carries one or more space-separated ``v1,<base64>`` parts, and
  ANY matching ``v1`` part accepts -- which is what makes secret rotation
  possible without dropping deliveries

Extracted verbatim from ``app/services/whop.py`` in Mozbridge. The one
deliberate behavioural change is documented at :func:`verify_standard_webhook`.

Two properties this module will not give up:

**The secret is a required caller-supplied argument.** This package never
reads an environment variable and never stores the secret in module or object
state. Where the secret lives is the host's decision, and it is a real one:
Mozbridge keeps it in the process environment specifically so that webhook
verification keeps working while Vault is sealed. A package that reached for
``os.getenv`` would quietly overrule that, and would also make "the secret is
unset" indistinguishable from "the caller passed nothing" -- the exact
ambiguity that turns into an open endpoint.

**There is no verifier object.** A ``Verifier(secret=...)`` would hold the
secret for the lifetime of the process, which is what "never store it" rules
out. Callers pass it per call and choose their own caching.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .errors import SignatureVerificationError

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_VERSION",
    "SECRET_PREFIX",
    "derive_key",
    "signed_content",
    "compute_signature",
    "verify_standard_webhook",
]

#: Standard Webhooks recommends five minutes. Past this a replayed request is
#: rejected even though its signature is still perfectly valid -- a captured
#: delivery must not stay usable forever.
DEFAULT_TOLERANCE_SECONDS = 300

#: The only signature version this module accepts. Parts tagged anything else
#: are skipped, not rejected, so a sender can roll out ``v2`` alongside ``v1``.
SIGNATURE_VERSION = "v1"

#: Conventional prefix on Standard Webhooks secrets. Stripped before decoding.
SECRET_PREFIX = "whsec_"


def derive_key(secret: str) -> bytes:
    """Turn a configured secret string into HMAC key bytes.

    Standard Webhooks secrets are base64, conventionally ``whsec_``-prefixed.
    When the value is not valid base64 this falls back to its raw UTF-8 bytes,
    so a plain-text secret typed in by hand still verifies instead of failing
    as an indistinguishable signature mismatch -- a failure mode that costs
    hours because the symptom (401) looks nothing like the cause (encoding).

    One quirk is preserved bit-for-bit from the Mozbridge original: the base64
    path decodes the value *without* the prefix, while the fallback path
    returns the bytes of the value *with* it. So ``whsec_hunter2`` keys on
    ``b"whsec_hunter2"``, not ``b"hunter2"``. That is arguably wrong, but it is
    what already-configured secrets were validated against, and changing it
    would silently invalidate every live signature.
    """
    raw = secret.removeprefix(SECRET_PREFIX)
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        return secret.encode()


def signed_content(webhook_id: str, timestamp: int, payload: bytes) -> bytes:
    """Build the exact byte string the signature covers.

    ``payload`` MUST be the raw request body. Re-serialising parsed JSON
    reorders keys and normalises whitespace, and will never match.

    Note ``timestamp`` is the *parsed integer*, not the header string. The
    original does the same, and it matters: a header of ``" 1700000000 "`` or
    the Unicode digits ``"١٧٠٠٠٠٠٠٠٠"`` both normalise to ``1700000000`` here,
    so a sender that pads or exoticises the header still verifies.
    """
    return f"{webhook_id}.{timestamp}.".encode() + payload


def compute_signature(
    payload: bytes,
    webhook_id: str,
    timestamp: int,
    secret: str,
) -> str:
    """Return the base64 MAC for one delivery (without the ``v1,`` tag).

    Exposed because verifying a signature and producing one are the same
    computation, and a host testing its own endpoint needs to produce one.
    Prefer :func:`verify_standard_webhook` for the inbound path -- comparing
    this return value with ``==`` reintroduces the timing leak it avoids.
    """
    mac = hmac.new(
        derive_key(secret), signed_content(webhook_id, timestamp, payload), hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()


def verify_standard_webhook(
    payload: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    secret: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Verify a Standard Webhooks signature, or raise.

    Returns ``None`` on success and raises :class:`SignatureVerificationError`
    otherwise, so a caller cannot ignore the result by accident. A function
    that returned ``bool`` would be one missing ``not`` away from accepting
    everything, and that mistake is invisible in review.

    :param payload: the raw request body, exactly as received.
    :param webhook_id: the ``webhook-id`` header.
    :param webhook_timestamp: the ``webhook-timestamp`` header (unix seconds).
    :param webhook_signature: the ``webhook-signature`` header.
    :param secret: the signing secret. Required -- see the module docstring.
    :param tolerance_seconds: replay window, symmetric around ``now``.
    :param now: unix seconds to compare against; defaults to ``time.time()``.
        Injectable so tests do not have to sleep or freeze the global clock.

    Rejects, in order: an unset secret; a missing header; a non-integer
    timestamp; a timestamp outside the window in *either* direction; and
    finally a body whose MAC matches no ``v1`` part.

    Differential note -- the single intended divergence from the Mozbridge
    original: when the header's base64 field contains non-ASCII characters,
    ``hmac.compare_digest`` on ``str`` raises ``TypeError``. In the original
    that escapes the verifier and surfaces as a 500. Here it is caught at the
    same point in the same loop iteration and turned into a rejection. Because
    it aborts where the original aborts, the accept set is *identical* -- a
    valid part earlier in the header still wins in both. This only converts a
    fail-open-shaped 500 into a fail-closed 401.
    """
    if not secret:
        # Fail closed. An unset secret must never mean "skip verification" --
        # that is the single most common way a webhook endpoint ends up open.
        raise SignatureVerificationError("signing secret is not configured")

    if not (webhook_id and webhook_timestamp and webhook_signature):
        raise SignatureVerificationError(
            "missing webhook-id, webhook-timestamp or webhook-signature"
        )

    try:
        sent_at = int(webhook_timestamp)
    except ValueError:
        raise SignatureVerificationError("webhook-timestamp is not an integer") from None

    reference = time.time() if now is None else now
    drift = abs(reference - sent_at)
    if drift > tolerance_seconds:
        # Bounded both ways: a timestamp from the future is as suspicious as
        # one from the past, and a one-sided check is trivially defeated.
        raise SignatureVerificationError(
            f"timestamp outside tolerance ({drift:.0f}s > {tolerance_seconds}s)"
        )

    expected = compute_signature(payload, webhook_id, sent_at, secret)

    # The header may carry several space-separated versioned signatures so a
    # key can be rotated without dropping deliveries. Accept if ANY v1 matches.
    for part in webhook_signature.split():
        version, _, candidate = part.partition(",")
        if version != SIGNATURE_VERSION:
            continue
        try:
            if hmac.compare_digest(candidate, expected):
                return
        except TypeError:
            # Non-ASCII in the candidate: `compare_digest` refuses str it
            # cannot compare byte-wise. Reject the delivery here rather than
            # skipping the part -- the original aborts at exactly this point
            # too (as a TypeError), so stopping here keeps the accept set
            # identical instead of accepting a header the original never did.
            raise SignatureVerificationError("malformed signature part") from None

    raise SignatureVerificationError(f"no matching {SIGNATURE_VERSION} signature")
