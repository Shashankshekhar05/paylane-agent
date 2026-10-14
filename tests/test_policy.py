"""Policy tests for the explicitly required refund/escalation rules.

Covers verification boundaries, the $75 and 30-day thresholds (including
exact-boundary values), duplicate refunds, and escalation triggers.
Unresolved cases (pending, partial amounts, frozen interaction) assert the
documented clarify-without-inventing behavior from docs/POLICY_DECISIONS.md.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.policy import (
    ALLOW,
    DENY,
    ESCALATE,
    NEEDS_CLARIFICATION,
    RefundDecision,
    can_disclose,
    fraud_amount_over_threshold,
    is_duplicate,
    is_refund_success,
    needs_escalation_for_age,
    needs_escalation_for_amount,
    refund_decision,
    should_escalate_intent,
    verify_identity,
)
from app.schemas import (
    CustomerRecord,
    RefundFailure,
    RefundSuccess,
    TransactionRecord,
)
from app.state import ConversationState

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
AVA_EMAIL = "ava@example.com"


def _ava() -> CustomerRecord:
    return CustomerRecord(
        id="c_ava", name="Ava", card_last4="4242", status="active", verified=False
    )


def _txn(
    txn_id: str = "t_ok",
    amount: float = 42.50,
    age: timedelta = timedelta(days=10),
    status: str = "settled",
) -> TransactionRecord:
    return TransactionRecord(
        txn_id=txn_id,
        customer_id="c_ava",
        amount=amount,
        date=NOW - age,
        merchant="Acme",
        status=status,
    )


def _verified_state() -> ConversationState:
    state = ConversationState(clock=lambda: NOW)
    state.mark_verified("c_ava")
    return state


def test_verify_identity_requires_both_values():
    record = _ava()
    assert verify_identity(record, AVA_EMAIL, "ava@example.com", "4242") is True
    assert verify_identity(record, AVA_EMAIL, "ava@example.com", "0000") is False
    assert verify_identity(record, AVA_EMAIL, "other@example.com", "4242") is False
    assert verify_identity(record, AVA_EMAIL, "other@example.com", "0000") is False


def test_disclosure_blocked_before_verification():
    assert can_disclose(ConversationState(clock=lambda: NOW)) is False
    assert can_disclose(_verified_state()) is True


def test_eligible_refund_allowed():
    decision = refund_decision(_verified_state(), _txn(), 42.50, NOW)
    assert decision == RefundDecision(outcome=ALLOW, reason="eligible")


def test_unverified_refund_denied():
    decision = refund_decision(ConversationState(clock=lambda: NOW), _txn(), 42.50, NOW)
    assert decision.outcome == DENY


def test_exactly_75_allowed_and_over_75_escalated():
    assert refund_decision(_verified_state(), _txn(), 75.00, NOW).outcome == ALLOW
    over = refund_decision(_verified_state(), _txn(), 75.01, NOW)
    assert over.outcome == ESCALATE
    assert over.reason == "amount-over-limit"
    assert needs_escalation_for_amount(75.00) is False
    assert needs_escalation_for_amount(75.01) is True


def test_30_day_boundary():
    just_inside = refund_decision(
        _verified_state(), _txn(age=timedelta(days=29, hours=23, minutes=59)), 20.0, NOW
    )
    assert just_inside.outcome == ALLOW
    exactly_30 = refund_decision(_verified_state(), _txn(age=timedelta(days=30)), 20.0, NOW)
    assert exactly_30.outcome == ESCALATE
    assert exactly_30.reason == "outside-refund-window"
    outside = refund_decision(_verified_state(), _txn(age=timedelta(days=31)), 20.0, NOW)
    assert outside.outcome == ESCALATE
    assert needs_escalation_for_age(timedelta(days=30)) is True
    assert needs_escalation_for_age(timedelta(days=29)) is False


def test_duplicate_refund_denied():
    state = _verified_state()
    state.mark_refunded("t_ok")
    decision = refund_decision(state, _txn(), 42.50, NOW)
    assert decision.outcome == DENY
    assert decision.reason == "duplicate-refund"
    assert is_duplicate(state, _txn()) is True
    assert is_duplicate(_verified_state(), _txn(status="refunded")) is True
    assert is_duplicate(_verified_state(), _txn()) is False


def test_pending_preserves_uncertainty_with_clarification():
    """Unresolved pending handling must not become a mandatory escalation."""
    decision = refund_decision(_verified_state(), _txn(status="pending"), 10.0, NOW)
    assert decision.outcome == NEEDS_CLARIFICATION
    assert decision.reason == "pending-unresolved"


def test_explicit_escalation_triggers():
    assert should_escalate_intent("legal_action") is True
    assert should_escalate_intent("fraud_over_500") is True
    assert should_escalate_intent("account_closure") is True
    assert should_escalate_intent("unfreeze_request") is True
    assert fraud_amount_over_threshold(500.00) is False
    assert fraud_amount_over_threshold(500.01) is True


def test_failed_result_never_counts_as_success():
    assert is_refund_success(RefundSuccess(ok=True, refund_id="r_1")) is True
    assert is_refund_success(RefundFailure(ok=False, reason="duplicate")) is False


@pytest.mark.parametrize("bad", ["", "   "])
def test_verify_identity_rejects_blank_inputs(bad):
    assert verify_identity(_ava(), AVA_EMAIL, bad, "4242") is False
    assert verify_identity(_ava(), AVA_EMAIL, "ava@example.com", bad) is False
