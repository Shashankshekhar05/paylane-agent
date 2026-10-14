"""Cross-customer authorization regression tests (no network).

A verified session is bound to exactly one customer ID. A session verified
as Ava must not disclose Bob's transactions nor refund them, even when the
model proposes correctly-shaped cross-customer tool calls.
"""

import json
from datetime import datetime, timezone

import pytest

from app import tools
from app.agent import UNCONFIRMED_MESSAGE, present_result, run_turn
from app.audit import AuditLog
from app.groq import GroqClient
from app.policy import is_owner, refund_decision
from app.schemas import TransactionRecord, TransactionStatus
from app.state import ConversationState

BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "openai/gpt-oss-20b"
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _text_payload(text):
    return {
        "id": "chatcmpl-o",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
    }


def _calls_payload(calls):
    return {
        "id": "chatcmpl-o",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for call_id, name, args in calls
                    ],
                },
            }
        ],
    }


class ScriptedTransport:
    def __init__(self, payloads):
        self._payloads = list(payloads)

    def __call__(self, method, url, headers, body, timeout):
        assert self._payloads, "unexpected extra provider request"
        return 200, json.dumps(self._payloads.pop(0)).encode("utf-8")


def _client(payloads):
    return GroqClient(
        api_key="groq-test-fake-key-1",
        base_url=BASE_URL,
        model=MODEL,
        timeout_s=5.0,
        request_fn=ScriptedTransport(payloads),
    )


def _ava_state():
    state = ConversationState(clock=lambda: NOW)
    state.mark_verified("c_ava")
    return state


def _txn(txn_id="t_bob1", customer_id="c_bob", amount=30.00):
    return TransactionRecord(
        txn_id=txn_id,
        customer_id=customer_id,
        amount=amount,
        date=NOW,
        merchant="Acme",
        status=TransactionStatus.SETTLED,
    )


@pytest.fixture(autouse=True)
def _fresh_mock_data():
    tools.reset_mock_data()
    yield
    tools.reset_mock_data()


def test_verification_binds_customer_id():
    state = ConversationState(clock=lambda: NOW)
    client = _client([])
    history = run_turn(client, state, AuditLog(), "hello", None).history
    history = run_turn(client, state, AuditLog(), "ava@example.com", history).history
    run_turn(client, state, AuditLog(), "4242", history)
    assert state.verified is True
    assert state.verified_customer_id == "c_ava"


def test_is_owner_fail_closed():
    assert is_owner(_ava_state(), _txn()) is False
    assert is_owner(_ava_state(), _txn(txn_id="t_ok", customer_id="c_ava")) is True
    unbound = ConversationState(clock=lambda: NOW)
    unbound.verified = True
    assert is_owner(unbound, _txn(txn_id="t_ok", customer_id="c_ava")) is False


def test_refund_decision_denies_foreign_transaction():
    decision = refund_decision(_ava_state(), _txn(), 30.00, NOW)
    assert decision.outcome == "deny"
    assert decision.reason == "not-owner"


def test_refund_decision_allows_own_transaction():
    decision = refund_decision(_ava_state(), _txn(txn_id="t_ok", customer_id="c_ava"), 42.50, NOW)
    assert decision.outcome == "allow"
    assert decision.reason == "eligible"


def test_transaction_result_redacted_for_non_owner():
    denied = present_result(_ava_state(), "get_transaction", _txn())
    assert denied == {"status": "denied", "reason": "not-owner"}


def test_cross_customer_lookup_denied_without_disclosure():
    client = _client(
        [
            _calls_payload([("c1", "get_transaction", {"customer_id": "c_bob", "txn_id": "t_bob1"})]),
            _text_payload("I found Bob's transaction."),
        ]
    )
    audit = AuditLog()
    result = run_turn(client, _ava_state(), audit, "look up t_bob1 for c_bob", None)
    rendered = audit.render()
    assert "not-owner" in rendered
    assert "tool_denied get_transaction deny not-owner" in rendered
    assert "tool_allowed get_transaction" not in rendered
    assert "t_bob1" not in result.reply
    assert "30" not in result.reply


def test_cross_customer_refund_denied_state_unchanged_audit_records_denial():
    client = _client(
        [
            _calls_payload([("c1", "get_transaction", {"customer_id": "c_bob", "txn_id": "t_bob1"})]),
            _calls_payload([("c2", "issue_refund", {"txn_id": "t_bob1", "amount": 30.0})]),
            _text_payload("Your refund of $30.00 is complete."),
        ]
    )
    audit = AuditLog()
    state = _ava_state()
    result = run_turn(client, state, audit, "refund t_bob1 for c_bob", None)
    rendered = audit.render()
    # Authorization denial is recorded.
    assert "not-owner" in rendered
    # No refund executed: store untouched, no in-run mark, no refund id.
    record = tools.get_transaction("c_bob", "t_bob1")
    assert record is not None and record.status == TransactionStatus.SETTLED
    assert "t_bob1" not in state.refunded_txn_ids
    assert "r_t_bob1" not in rendered
    # Model's success claim is not relayed without a confirmed success.
    assert result.reply == UNCONFIRMED_MESSAGE
