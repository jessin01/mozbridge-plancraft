"""The idempotent delivery-store contract.

Every Standard Webhooks sender retries on any non-2xx, and several of them
(Whop among them) expose no endpoint for asking what the current state *is* --
so the delivery stream is the only source of truth. Losing an event or
applying one twice is then a billing error with nothing to repair it. That
makes exactly-once handling a correctness requirement, not an optimisation.

The one thing that is genuinely reusable here is the *shape* of the check:

    Insert against a UNIQUE constraint on the delivery id and treat the
    integrity violation as the duplicate signal.

NOT check-then-act. ``SELECT ... ; if not found: INSERT`` reads correct and is
wrong: two concurrent deliveries of the same id both pass the SELECT, both
insert, and both apply. The window is small and the traffic is retries, which
means the window is hit precisely when it costs the most. The database's
uniqueness check is the only part of this that is actually atomic, so it has
to be the part that decides.

What is deliberately NOT here: a concrete table. Hosts differ on declarative
base, session lifetime, transaction boundaries, and which columns they want to
keep, and a table shipped by this package would have to assume all four. The
host owns persistence; this module owns the contract and the control flow.

So the surface is small on purpose: a :class:`DeliveryStore` protocol, one
:func:`record_once` helper that encodes the insert-then-catch flow (including
the rollback that is easy to forget and poisons the session when omitted), and
an in-memory implementation for tests.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = ["DeliveryStore", "record_once", "InMemoryDeliveryStore"]


@runtime_checkable
class DeliveryStore(Protocol):
    """What a host must provide to make delivery handling idempotent.

    One method, because one question is being asked: *have I already taken
    responsibility for this delivery id?*
    """

    def record(self, delivery_id: str, payload: bytes) -> bool:
        """Durably claim ``delivery_id``; return whether this call claimed it.

        ``True``  -- first time seen. The caller owns processing this delivery.
        ``False`` -- already recorded. The caller must not process it again,
        and should answer 2xx so the sender stops retrying.

        Two requirements on any implementation:

        * The claim must be **durable before it returns True**. Returning True
          from an uncommitted insert means a crash mid-handler loses the event
          with the sender already told 200.
        * Concurrency must be resolved by the **storage layer**, not by a read
          in Python. See the module docstring.

        ``payload`` is the raw body. Storing it is what makes an event
        replayable after a mapping bug is fixed -- without it, a delivery that
        arrived before the host could act on it is simply gone.
        """
        ...


def record_once(
    insert: Callable[[], Any],
    *,
    duplicate_exc: type[BaseException] | tuple[type[BaseException], ...],
    rollback: Callable[[], Any] | None = None,
) -> bool:
    """Run ``insert``; return True if it claimed the delivery, False on replay.

    The insert-then-catch flow, with the two things people get wrong built in:
    the duplicate is caught rather than pre-checked, and ``rollback`` is called
    on the duplicate path. Skipping the rollback leaves a SQLAlchemy session in
    a failed transaction where every later statement raises
    ``PendingRollbackError`` -- so the bug surfaces somewhere unrelated, long
    after the replay that caused it.

    ``duplicate_exc`` is supplied by the caller because the exception is a
    driver concern this package must not import: ``sqlalchemy.exc.IntegrityError``,
    ``django.db.IntegrityError``, ``psycopg.errors.UniqueViolation``, and so on.

        first_seen = record_once(
            lambda: (db.add(WebhookEvent(...)), db.commit()),
            duplicate_exc=IntegrityError,
            rollback=db.rollback,
        )
        if not first_seen:
            return {"status": "duplicate"}

    Caveat worth knowing before you widen ``duplicate_exc``: an IntegrityError
    means *some* constraint failed, not necessarily the delivery-id one. A NOT
    NULL violation on another column would be swallowed as a replay and the
    event dropped. Keep the row narrow, or inspect the constraint name in your
    own ``insert`` wrapper and re-raise what is not a duplicate.
    """
    try:
        insert()
    except duplicate_exc:
        if rollback is not None:
            rollback()
        return False
    return True


class InMemoryDeliveryStore:
    """A :class:`DeliveryStore` backed by a set. For tests and single-process use.

    Deliberately not a production store: it is per-process and non-durable, so
    it forgets everything on restart and two workers would each accept the same
    delivery. It exists so a host can exercise its handler's idempotency
    without a database, and as an executable statement of the contract.

    The lock is not decoration -- ``add`` on a set is atomic under the GIL but
    "check membership, then add" is not, and check-then-act is the exact bug
    this module is about.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, bytes] = {}

    def record(self, delivery_id: str, payload: bytes) -> bool:
        with self._lock:
            if delivery_id in self._seen:
                return False
            self._seen[delivery_id] = payload
            return True

    def payload_for(self, delivery_id: str) -> bytes | None:
        """The stored body, or None. Mirrors the replay-after-fix use case."""
        with self._lock:
            return self._seen.get(delivery_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)
