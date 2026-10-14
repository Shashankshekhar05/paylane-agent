"""Tool behavior tests (argument validation and tool invariants only).

No business-policy assertions here: no $75/30-day thresholds, no
verification gates, no escalation triggers. Those belong to
tests/test_policy.py.
"""

import pytest
from pydantic import ValidationError

from app.schemas import RefundFailure, RefundSuccess
from app.tools import (
    escalate_to_human,
    escalation_log,
    get_customer,
    get_transaction,
    issue_refund,
    reset_mock_data,
)


@pytest.fixture(autouse=True)
def _fresh_mock_data():
    reset_mock_data()
    yield
    reset_mock_data()


def test_get_customer_known_and_unknown():
    ava = get_customer("ava@example.com")
    assert ava is not None
    assert ava.id == "c_ava"
    assert ava.verified is False
    assert get_customer("nobody@example.com") is None


def test_get_customer_rejects_empty_email():
    with pytest.raises(ValidationError):
        get_customer("   ")


def test_get_transaction_valid_and_ownership_mismatch():
    txn = get_transaction("c_ava", "t_ok")
    assert txn is not None
    assert txn.amount == 42.50
    assert get_transaction("c_bob", "t_ok") is None
    assert get_transaction("c_ava", "t_missing") is None


def test_get_transaction_rejects_empty_ids():
    with pytest.raises(ValidationError):
        get_transaction("", "t_ok")
    with pytest.raises(ValidationError):
        get_transaction("c_ava", "")


def test_issue_refund_success_then_duplicate():
    first = issue_refund("t_ok", 42.50)
    assert isinstance(first, RefundSuccess)
    assert first.ok is True
    assert first.refund_id == "r_t_ok"
    second = issue_refund("t_ok", 42.50)
    assert isinstance(second, RefundFailure)
    assert second.reason == "duplicate"


def test_issue_refund_rejects_unknown_and_pre_refunded():
    unknown = issue_refund("t_missing", 10.0)
    assert isinstance(unknown, RefundFailure)
    assert unknown.reason == "unknown-transaction"
    already = issue_refund("t_refunded", 15.0)
    assert isinstance(already, RefundFailure)
    assert already.reason == "duplicate"


@pytest.mark.parametrize("bad_amount", [0.0, -1.0, -75.0])
def test_issue_refund_rejects_non_positive_amounts(bad_amount):
    result = issue_refund("t_ok", bad_amount)
    assert isinstance(result, RefundFailure)
    assert result.reason == "invalid-amount"


@pytest.mark.parametrize("bad_amount", [float("nan"), float("inf")])
def test_issue_refund_rejects_non_finite_amounts(bad_amount):
    with pytest.raises(ValidationError):
        issue_refund("t_ok", bad_amount)


def test_tool_does_not_enforce_policy_thresholds():
    """Separation check: the $75 tool-level over-limit refund succeeds.

    Eligibility is the policy layer's job; the tool protects only its own
    invariants (known txn, positive finite amount, not already refunded).
    """
    result = issue_refund("t_big", 80.00)
    assert isinstance(result, RefundSuccess)


def test_tool_returns_pending_without_policy_gate():
    """Separation check: pending status is returned as data, not blocked."""
    txn = get_transaction("c_ava", "t_pending")
    assert txn is not None
    assert txn.status == "pending"


def test_escalate_returns_ticket_and_redacts_secrets():
    result = escalate_to_human(
        "c_ava", "contact user@example.com about card 4111111111111111 please"
    )
    assert result.ticket_id == "h_0001"
    assert result.customer_id == "c_ava"
    log = escalation_log()
    assert len(log) == 1
    assert "user@example.com" not in log[0]["summary"]
    assert "4111111111111111" not in log[0]["summary"]


def test_escalate_rejects_empty_summary():
    with pytest.raises(ValidationError):
        escalate_to_human("c_ava", "  ")
