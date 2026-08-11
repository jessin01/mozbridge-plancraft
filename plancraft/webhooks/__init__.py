"""Processor-agnostic webhook handling: signature verification and idempotency.

Scope is deliberately narrow. This package covers the two halves of webhook
intake that are *verifiable without a live payment processor account*:

1. proving a delivery is authentic (:mod:`.signatures`), and
2. handling each authentic delivery exactly once (:mod:`.idempotency`).

Both are pure specification. Standard Webhooks is a published spec with test
vectors, and insert-on-unique is a database property; neither needs a real
processor to be checked. Everything downstream of them -- normalising a body
into an event, mapping a processor's plan id onto a catalog plan, deciding what
grants and what revokes -- depends on payload shapes the public docs do not
pin down. Guessing those produces code that looks finished and is wrong in a
way no test catches, so they stay out until a real delivery is observed.

Typical intake, framework-independent::

    from plancraft.webhooks import (
        SignatureVerificationError,
        record_once,
        verify_standard_webhook,
    )

    raw = await request.body()            # RAW bytes, never re-serialised JSON
    try:
        verify_standard_webhook(raw, wh_id, wh_ts, wh_sig, secret=settings.secret)
    except SignatureVerificationError as e:
        logger.warning("webhook rejected: %s", e)   # reason -> logs
        raise HTTPException(401, detail=e.client_detail)  # never the reason

    first_seen = record_once(
        lambda: (db.add(Delivery(id=wh_id, body=raw)), db.commit()),
        duplicate_exc=IntegrityError,
        rollback=db.rollback,
    )
    if not first_seen:
        return {"status": "duplicate"}

Three rules that the shape above encodes, in the order they bite:

* **Verify against the raw body.** Re-serialising parsed JSON reorders keys
  and normalises whitespace; the MAC covers the exact bytes sent.
* **Reject without a reason.** A specific 4xx message is a probe oracle for
  signature forgery. The detail belongs in logs.
* **Acknowledge what you cannot act on.** Once a delivery is authentic, a body
  you do not understand should be stored and answered 2xx. A 4xx makes the
  sender redeliver it forever, and the retry storm is indistinguishable from
  an outage.
"""

from .errors import SignatureVerificationError, WebhookError
from .idempotency import DeliveryStore, InMemoryDeliveryStore, record_once
from .signatures import (
    DEFAULT_TOLERANCE_SECONDS,
    SECRET_PREFIX,
    SIGNATURE_VERSION,
    compute_signature,
    derive_key,
    signed_content,
    verify_standard_webhook,
)

__all__ = [
    # errors
    "WebhookError",
    "SignatureVerificationError",
    # signatures
    "verify_standard_webhook",
    "compute_signature",
    "signed_content",
    "derive_key",
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_VERSION",
    "SECRET_PREFIX",
    # idempotency
    "DeliveryStore",
    "record_once",
    "InMemoryDeliveryStore",
]
