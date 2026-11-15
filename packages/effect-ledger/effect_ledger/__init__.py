"""effect-ledger — a durable effect ledger for AI agents.

The problem it solves: an agent retries after a crash. Did the email already
go out? Did the order already get placed? Chat history proves what the agent
*remembered*; it cannot prove what the world *received*.

The mechanism chain (production-proven):

    persist intent (idempotency key + param hash)
    -> execute external op (lease + fencing token)
    -> verify result (independent probe + receipt)
    -> CONFIRMED / UNCERTAIN (reconcile first, blind replay forbidden)
       / PAUSED (high risk unverifiable -> human)

Guarantees:
- ``idem_key`` is UNIQUE at the DB layer — duplicate delivery is blocked.
- Fencing tokens increase monotonically — a stale executor's ``complete()``
  is always rejected after lease-expiry takeover.
- ``UNCERTAIN`` never replays blindly — ``reconcile(probe)`` first.
- Fail-closed — if the ledger write fails, ``begin()`` raises and the caller
  must not touch the external world.

Zero dependencies: stdlib + SQLite.
"""

from .ledger import (
    EffectLedger,
    LedgerStateError,
    LedgerWriteError,
    make_idem_key,
)

__all__ = ["EffectLedger", "LedgerWriteError", "LedgerStateError", "make_idem_key"]
__version__ = "0.1.0"
