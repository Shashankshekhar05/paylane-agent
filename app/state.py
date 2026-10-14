"""Per-conversation state for Phase 2.

Tracks identity-verification success separately from the mock customer
record's initial ``verified: false`` field, tracks refunded transaction
IDs so the same mock transaction cannot be refunded twice within the run,
and carries an injectable UTC clock for deterministic age-boundary tests.

Scope limitation (ARCHITECTURE.md section 7): state is local to a
conversation/run and lives only in memory. It is lost on application
restart and provides no distributed or atomic idempotency. A real payment
integration would require backend-enforced idempotency; that is out of
scope here. Nothing in this module claims persistence across restarts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.schemas import CustomerRecord

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"


@dataclass
class ConversationState:
    verified: bool = False
    refunded_txn_ids: set[str] = field(default_factory=set)
    clock: Clock = field(default=_utcnow, repr=False, compare=False)
    # Phase 3c verification machine (additive; defaults preserve old behavior).
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    email_on_file: Optional[str] = field(default=None, repr=False)
    # Customer this conversation is verified as. Set exactly once by the
    # local verification flow; authorization checks bind to this ID so one
    # customer's session can never act on another customer's records.
    verified_customer_id: Optional[str] = field(default=None, repr=False)
    pending_customer_id: Optional[str] = field(default=None, repr=False)
    pending_record: Optional[CustomerRecord] = field(default=None, repr=False)
    known_customer_ids: set[str] = field(default_factory=set)
    verification_attempts: int = 0
    awaiting_verification_reply: bool = False

    def now(self) -> datetime:
        return self.clock()

    def mark_verified(self, customer_id: str) -> None:
        self.verified = True
        self.verification = VerificationStatus.VERIFIED
        self.verified_customer_id = customer_id

    def mark_refunded(self, txn_id: str) -> None:
        self.refunded_txn_ids.add(txn_id)
