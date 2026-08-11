"""Exception types owned by ``plancraft.webhooks``.

Package-owned types exist so a host can tell "this request could not be
authenticated" apart from "plancraft has a bug". Conflating the two is how a
broken verifier gets "fixed" by disabling it: every request starts looking like
a server error, someone wraps the call in ``try/except Exception``, and the
endpoint silently becomes unauthenticated.
"""

from __future__ import annotations

__all__ = ["WebhookError", "SignatureVerificationError"]


class WebhookError(Exception):
    """Base class for every failure raised by ``plancraft.webhooks``."""


class SignatureVerificationError(WebhookError):
    """Raised when a delivery cannot be proven to have come from the sender.

    The message says which check failed, because that belongs in the host's
    logs. It must NEVER be returned to the caller: a precise error is a probe
    oracle for anyone testing signature forgeries -- it tells them whether they
    got the timestamp, the version tag, or the MAC wrong, one bit at a time.

    Use :attr:`client_detail` for the response body::

        except SignatureVerificationError as e:
            logger.warning("webhook rejected: %s", e)
            raise HTTPException(401, detail=e.client_detail) from None
    """

    #: The only string safe to hand back over the wire. Deliberately uniform
    #: across every rejection reason.
    client_detail = "invalid signature"
