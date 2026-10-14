"""Structural schema tests (no business-policy decisions)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    CustomerRecord,
    EscalationResult,
    RefundFailure,
    RefundSuccess,
    TransactionRecord,
)


def test_valid_customer_record():
    rec = CustomerRecord(
        id="c_001", name="Ava", card_last4="4242", status="active", verified=False
    )
    assert rec.card_last4 == "4242"
    assert rec.verified is False


@pytest.mark.parametrize("bad_last4", ["12", "12345", "abcd", "42 42", ""])
def test_customer_card_last4_shape_rejected(bad_last4):
    with pytest.raises(ValidationError):
        CustomerRecord(
            id="c_001", name="Ava", card_last4=bad_last4, status="active", verified=False
        )


def test_customer_status_enum_rejected():
    with pytest.raises(ValidationError):
        CustomerRecord(
            id="c_001", name="Ava", card_last4="4242", status="blocked", verified=False
        )


def test_valid_transaction_record():
    txn = TransactionRecord(
        txn_id="t_001",
        customer_id="c_001",
        amount=42.50,
        date=datetime(2026, 9, 20, tzinfo=timezone.utc),
        merchant="Acme",
        status="settled",
    )
    assert txn.amount == 42.50


def test_transaction_rejects_negative_amount_and_bad_status():
    with pytest.raises(ValidationError):
        TransactionRecord(
            txn_id="t_001",
            customer_id="c_001",
            amount=-1.0,
            date=datetime(2026, 9, 20, tzinfo=timezone.utc),
            merchant="Acme",
            status="settled",
        )
    with pytest.raises(ValidationError):
        TransactionRecord(
            txn_id="t_001",
            customer_id="c_001",
            amount=10.0,
            date=datetime(2026, 9, 20, tzinfo=timezone.utc),
            merchant="Acme",
            status="unknown",
        )


def test_refund_and_escalation_shapes():
    assert RefundSuccess(ok=True, refund_id="r_1").refund_id == "r_1"
    assert RefundFailure(ok=False, reason="duplicate").reason == "duplicate"
    assert EscalationResult(ticket_id="h_1", customer_id="c_1").ticket_id == "h_1"
    with pytest.raises(ValidationError):
        RefundSuccess(ok=True, refund_id="")
    with pytest.raises(ValidationError):
        RefundFailure(ok=False, reason="")
    with pytest.raises(ValidationError):
        CustomerRecord(
            id="c_001",
            name="Ava",
            card_last4="4242",
            status="active",
            verified=False,
            extra_field="nope",
        )
