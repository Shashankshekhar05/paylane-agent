"""Deterministic mock tools for Phase 2.

Tool argument validation only. No business-policy decisions live here:
no refund eligibility, age/amount thresholds, verification, pending-transaction
handling, or escalation logic. The policy layer (app/policy.py) authorizes
sensitive actions before execution; each tool additionally protects its own
invariants (unknown records, ownership mismatch, malformed amounts, and
already-refunded status).

All data is in-memory mock data. Nothing here accesses real customer
systems or moves real money.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.schemas import (
    CustomerRecord,
    EscalateArgs,
    EscalationResult,
    GetCustomerArgs,
    GetTransactionArgs,
    IssueRefundArgs,
    RefundFailure,
    RefundResult,
    RefundSuccess,
    TransactionRecord,
    TransactionStatus,
)

_CARD_NUMBER_RE = re.compile(r"\b\d{13,19}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _redact_summary(text: str) -> str:
    """Remove card-number-like runs and email-like tokens (technical guard)."""
    return _EMAIL_RE.sub(
        "[redacted-email]", _CARD_NUMBER_RE.sub("[redacted-card]", text)
    )


def _build_customers() -> dict[str, CustomerRecord]:
    return {
        "ava@example.com": CustomerRecord(
            id="c_ava", name="Ava", card_last4="4242", status="active", verified=False
        ),
        "bob@example.com": CustomerRecord(
            id="c_bob", name="Bob", card_last4="9876", status="frozen", verified=False
        ),
    }


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _build_transactions() -> dict[str, TransactionRecord]:
    return {
        "t_ok": TransactionRecord(
            txn_id="t_ok",
            customer_id="c_ava",
            amount=42.50,
            date=_utc(2026, 9, 20),
            merchant="Acme",
            status="settled",
        ),
        "t_big": TransactionRecord(
            txn_id="t_big",
            customer_id="c_ava",
            amount=80.00,
            date=_utc(2026, 9, 20),
            merchant="Acme",
            status="settled",
        ),
        "t_old": TransactionRecord(
            txn_id="t_old",
            customer_id="c_ava",
            amount=20.00,
            date=_utc(2026, 8, 1),
            merchant="Acme",
            status="settled",
        ),
        "t_pending": TransactionRecord(
            txn_id="t_pending",
            customer_id="c_ava",
            amount=10.00,
            date=_utc(2026, 9, 25),
            merchant="Acme",
            status="pending",
        ),
        "t_refunded": TransactionRecord(
            txn_id="t_refunded",
            customer_id="c_ava",
            amount=15.00,
            date=_utc(2026, 9, 10),
            merchant="Acme",
            status="refunded",
        ),
        "t_bob1": TransactionRecord(
            txn_id="t_bob1",
            customer_id="c_bob",
            amount=30.00,
            date=_utc(2026, 9, 20),
            merchant="Acme",
            status="settled",
        ),
    }


_CUSTOMERS: dict[str, CustomerRecord] = _build_customers()
_TRANSACTIONS: dict[str, TransactionRecord] = _build_transactions()
_ticket_seq: int = 0
_ESCALATION_LOG: list[dict[str, str]] = []


def reset_mock_data() -> None:
    """Restore the deterministic mock dataset (test isolation helper)."""
    global _CUSTOMERS, _TRANSACTIONS, _ticket_seq, _ESCALATION_LOG
    _CUSTOMERS = _build_customers()
    _TRANSACTIONS = _build_transactions()
    _ticket_seq = 0
    _ESCALATION_LOG = []


def escalation_log() -> list[dict[str, str]]:
    """Return a copy of recorded (redacted) escalation summaries."""
    return [dict(entry) for entry in _ESCALATION_LOG]


def get_customer(email: str) -> CustomerRecord | None:
    """Return the mock customer for an email, or None if unknown.

    Finding a record does not authorize disclosure; see app/policy.py.
    """
    args = GetCustomerArgs(email=email)
    record = _CUSTOMERS.get(args.email.strip().lower())
    return record.model_copy(deep=True) if record is not None else None


def get_transaction(customer_id: str, txn_id: str) -> TransactionRecord | None:
    """Return the mock transaction, or None if unknown or not owned.

    Ownership mismatch returns None so one customer can never retrieve
    another customer's transaction through this tool.
    """
    args = GetTransactionArgs(customer_id=customer_id, txn_id=txn_id)
    record = _TRANSACTIONS.get(args.txn_id)
    if record is None or record.customer_id != args.customer_id:
        return None
    return record.model_copy(deep=True)


def issue_refund(txn_id: str, amount: float) -> RefundResult:
    """Issue a mock refund or return a structured failure.

    Tool-level invariants only: non-finite amounts are rejected by argument
    validation, non-positive amounts and unknown transactions fail
    structurally, and an already-refunded stored status fails as a
    duplicate. Eligibility thresholds are a policy-layer concern.
    """
    args = IssueRefundArgs(txn_id=txn_id, amount=amount)
    if args.amount <= 0:
        return RefundFailure(ok=False, reason="invalid-amount")
    record = _TRANSACTIONS.get(args.txn_id)
    if record is None:
        return RefundFailure(ok=False, reason="unknown-transaction")
    if record.status == TransactionStatus.REFUNDED:
        return RefundFailure(ok=False, reason="duplicate")
    record.status = TransactionStatus.REFUNDED
    return RefundSuccess(ok=True, refund_id=f"r_{args.txn_id}")


def escalate_to_human(customer_id: str, summary: str) -> EscalationResult:
    """Create a mock support ticket and return its ticket ID."""
    global _ticket_seq
    args = EscalateArgs(customer_id=customer_id, summary=summary)
    _ticket_seq += 1
    _ESCALATION_LOG.append(
        {"customer_id": args.customer_id, "summary": _redact_summary(args.summary)}
    )
    return EscalationResult(
        ticket_id=f"h_{_ticket_seq:04d}", customer_id=args.customer_id
    )
