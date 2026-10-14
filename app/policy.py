"""Deterministic policy checks for Phase 2.

Implements only the business rules explicitly required by the original
assignment (AGENTS.md sections 4-5, ARCHITECTURE.md section 6). Enforcement
lives here in Python, not in prompts.

Not decided here: pending-transaction handling, partial-refund semantics,
frozen-card/refund interaction, and the other open questions recorded in
docs/POLICY_DECISIONS.md. A ``needs_clarification`` outcome preserves that
documented uncertainty so callers ask a focused question; it must not be
silently converted into a mandatory escalation. Mandatory escalation
follows only the assignment's explicit triggers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.schemas import (
    CustomerRecord,
    RefundResult,
    RefundSuccess,
    TransactionRecord,
    TransactionStatus,
)
from app.state import ConversationState

# Explicit thresholds from the original assignment. Exactly $75 satisfies
# the amount limit; the age window is strictly under 30 days.
REFUND_AMOUNT_LIMIT = 75.0
REFUND_WINDOW = timedelta(days=30)
FRAUD_ESCALATION_THRESHOLD = 500.0

ALLOW = "allow"
DENY = "deny"
ESCALATE = "escalate"
NEEDS_CLARIFICATION = "needs_clarification"

DecisionOutcome = Literal["allow", "deny", "escalate", "needs_clarification"]
EscalationIntent = Literal[
    "legal_action", "fraud_over_500", "account_closure", "unfreeze_request"
]


@dataclass(frozen=True)
class RefundDecision:
    outcome: DecisionOutcome
    reason: str


def _normalize_email(value: str) -> str:
    """Strip whitespace and casefold (technical input normalization)."""
    return value.strip().lower()


def verify_identity(
    record: CustomerRecord,
    email_on_file: str,
    supplied_email: str,
    supplied_last4: str,
) -> bool:
    """Return True only when both supplied values match the record.

    The mock record carries no email field, so the email on file is the
    address used to retrieve the record. Both the email and the last four
    card digits must match; either mismatch fails verification.
    """
    return (
        _normalize_email(supplied_email) == _normalize_email(email_on_file)
        and supplied_last4.strip() == record.card_last4
    )


def can_disclose(state: ConversationState) -> bool:
    """Account details require successful verification first."""
    return state.verified


def is_owner(state: ConversationState, txn: TransactionRecord) -> bool:
    """True only when the session is verified as this transaction's owner.

    Verification binds to one customer ID (`verified_customer_id`); a
    verified session for customer A must never authorize actions on
    customer B's transactions. Fail closed: an unbound session owns
    nothing, even if the `verified` flag is set.
    """
    return (
        state.verified
        and state.verified_customer_id is not None
        and txn.customer_id == state.verified_customer_id
    )


def is_duplicate(state: ConversationState, txn: TransactionRecord) -> bool:
    """True when this transaction was already refunded in this run."""
    return (
        txn.txn_id in state.refunded_txn_ids
        or txn.status == TransactionStatus.REFUNDED
    )


def transaction_age(txn: TransactionRecord, now: datetime) -> timedelta:
    """Age of a transaction; naive datetimes are treated as UTC."""
    txn_date = txn.date
    if txn_date.tzinfo is None:
        txn_date = txn_date.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return current - txn_date


def needs_escalation_for_amount(requested_amount: float) -> bool:
    """Explicit rule: refunds over $75 must be escalated."""
    return requested_amount > REFUND_AMOUNT_LIMIT


def needs_escalation_for_age(age: timedelta) -> bool:
    """Explicit rule: transactions outside the under-30-day window escalate."""
    return age >= REFUND_WINDOW


def fraud_amount_over_threshold(amount: float) -> bool:
    """Explicit rule: fraud claims over $500 must be escalated."""
    return amount > FRAUD_ESCALATION_THRESHOLD


def should_escalate_intent(kind: EscalationIntent) -> bool:
    """Explicit triggers (legal action, fraud over $500, account closure,
    unfreeze request) always require escalation. Intent *detection* from
    free text belongs to a later phase; this predicate only encodes the
    assignment's mandatory-escalation set."""
    return True


def refund_decision(
    state: ConversationState,
    txn: TransactionRecord,
    requested_amount: float,
    now: datetime,
) -> RefundDecision:
    """Decide a requested refund using only the explicit assignment rules.

    The requested amount is gated against the explicit over-$75 trigger;
    no requested-vs-transaction-amount comparison is added here because the
    assignment does not specify partial-refund semantics (see
    docs/POLICY_DECISIONS.md). Frozen-card status adds no rule here for the
    same reason. Pending transactions return needs_clarification to preserve
    the documented uncertainty.
    """
    if not state.verified:
        return RefundDecision(outcome=DENY, reason="unverified-customer")
    if not is_owner(state, txn):
        return RefundDecision(outcome=DENY, reason="not-owner")
    if is_duplicate(state, txn):
        return RefundDecision(outcome=DENY, reason="duplicate-refund")
    if txn.status == TransactionStatus.PENDING:
        return RefundDecision(outcome=NEEDS_CLARIFICATION, reason="pending-unresolved")
    if needs_escalation_for_age(transaction_age(txn, now)):
        return RefundDecision(outcome=ESCALATE, reason="outside-refund-window")
    if needs_escalation_for_amount(requested_amount):
        return RefundDecision(outcome=ESCALATE, reason="amount-over-limit")
    return RefundDecision(outcome=ALLOW, reason="eligible")


def is_refund_success(result: RefundResult) -> bool:
    """True only for a well-formed successful refund result.

    Any failure, malformed, or ambiguous outcome returns False and must
    never be described as a successful refund.
    """
    return (
        isinstance(result, RefundSuccess)
        and result.ok is True
        and len(result.refund_id) > 0
    )


def refund_id_matches(result: RefundResult, txn_id: str) -> bool:
    """True only when a success result names the requested transaction.

    Mock-tool contract correlation: `issue_refund(txn_id, ...)` issues
    `refund_id == f"r_{txn_id}"`. A success naming any other transaction —
    or any malformed result — must never confirm the requested refund, mark
    it refunded, or be described as successful. Fail closed.
    """
    return is_refund_success(result) and result.refund_id == f"r_{txn_id}"
