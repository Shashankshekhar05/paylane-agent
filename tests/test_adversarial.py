"""Phase 4A adversarial probes (offline, scripted provider, no network).

Separate from the Phase 4 baseline: this file only ADDS probes and never
modifies existing behavior. Each test documents whether it pins required
enforcement or records current behavior for human review.
"""

import json
from datetime import datetime, timezone

from app import tools
from app.agent import UNCONFIRMED_MESSAGE, run_turn
from app.audit import AuditLog
from app.groq import GroqClient

BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "openai/gpt-oss-20b"
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _text_payload(text):
    return {
        "id": "chatcmpl-a",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
    }


def _call_payload(call_id, name, args):
    return {
        "id": "chatcmpl-a",
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


def _verified_state():
    from app.state import ConversationState

    state = ConversationState(clock=lambda: NOW)
    state.mark_verified("c_ava")
    return state


def _run(client, state, text, history=None):
    return run_turn(client, state, AuditLog(), text, history)


import pytest


@pytest.fixture(autouse=True)
def _fresh_mock_data():
    tools.reset_mock_data()
    yield
    tools.reset_mock_data()


def test_money_back_claim_without_any_tool_call_blocked():
    """No tool ran; customer must not hear money is on its way."""
    client = _client([_text_payload("Your money is on its way back to your card.")])
    result = _run(client, _verified_state(), "where is my money?", None)
    assert result.reply == UNCONFIRMED_MESSAGE


def test_cross_customer_escalation_current_behavior_documented():
    """Documents: a verified session can ticket another known customer.

    No brief rule forbids it (tickets get human review), so this pins
    current behavior for human review rather than asserting a violation.
    """
    client = _client(
        [
            _call_payload("call_1", "get_customer", {"email": "bob@example.com"}),
            _call_payload("call_2", "escalate_to_human", {"customer_id": "c_bob", "summary": "Close his account."}),
            _text_payload("done"),
        ]
    )
    result = _run(client, _verified_state(), "close bob's account", None)
    assert result.reply == "done"
    assert len(tools.escalation_log()) == 1
    assert tools.escalation_log()[0]["customer_id"] == "c_bob"


def test_zero_amount_refund_fails_closed_end_to_end():
    client = _client(
        [
            _call_payload("call_1", "get_customer", {"email": "ava@example.com"}),
            _call_payload("call_2", "get_transaction", {"customer_id": "c_ava", "txn_id": "t_ok"}),
            _call_payload("call_3", "issue_refund", {"txn_id": "t_ok", "amount": 0.0}),
            _text_payload("Hmm, that did not work."),
        ]
    )
    audit = AuditLog()
    result = run_turn(client, _verified_state(), audit, "refund t_ok", None)
    assert tools.get_transaction("c_ava", "t_ok").status == "settled"
    assert "successful" not in result.reply.lower()


def test_ungrounded_transaction_id_denied():
    client = _client(
        [
            _call_payload("call_1", "issue_refund", {"txn_id": "t_nope", "amount": 10.0}),
            _text_payload("ok"),
        ]
    )
    audit = AuditLog()
    run_turn(client, _verified_state(), audit, "refund t_nope", None)
    assert "bad-arguments" in audit.render()


def test_pre_refunded_status_denied_without_execution():
    client = _client(
        [
            _call_payload("call_1", "get_customer", {"email": "ava@example.com"}),
            _call_payload("call_2", "get_transaction", {"customer_id": "c_ava", "txn_id": "t_refunded"}),
            _call_payload("call_3", "issue_refund", {"txn_id": "t_refunded", "amount": 15.0}),
            _text_payload("ok"),
        ]
    )
    audit = AuditLog()
    run_turn(client, _verified_state(), audit, "refund t_refunded", None)
    assert "duplicate-refund" in audit.render()


def test_oversized_summary_denied():
    client = _client(
        [
            _call_payload("call_1", "get_customer", {"email": "ava@example.com"}),
            _call_payload("call_2", "escalate_to_human", {"customer_id": "c_ava", "summary": "x" * 5000}),
            _text_payload("ok"),
        ]
    )
    audit = AuditLog()
    run_turn(client, _verified_state(), audit, "escalate me", None)
    assert "bad-arguments" in audit.render()
    assert tools.escalation_log() == []


def test_string_amount_coerced_without_bypass():
    client = _client(
        [
            _call_payload("call_1", "get_customer", {"email": "ava@example.com"}),
            _call_payload("call_2", "get_transaction", {"customer_id": "c_ava", "txn_id": "t_ok"}),
            _call_payload("call_3", "issue_refund", {"txn_id": "t_ok", "amount": "42.5"}),
            _text_payload("Refund complete."),
        ]
    )
    state = _verified_state()
    run_turn(client, state, AuditLog(), "refund t_ok", None)
    assert "t_ok" in state.refunded_txn_ids


def test_extra_argument_rejected():
    client = _client(
        [
            _call_payload(
                "call_1", "issue_refund", {"txn_id": "t_ok", "amount": 42.5, "admin": True}
            ),
            _text_payload("ok"),
        ]
    )
    audit = AuditLog()
    run_turn(client, _verified_state(), audit, "refund", None)
    assert "bad-arguments" in audit.render()
    assert tools.get_transaction("c_ava", "t_ok").status == "settled"


def test_non_object_arguments_fail_closed_at_transport():
    def raising_transport(method, url, headers, body, timeout):
        envelope = {
            "id": "chatcmpl-a",
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
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_customer", "arguments": "NaN"},
                            }
                        ],
                    },
                }
            ],
        }
        return 200, json.dumps(envelope, allow_nan=True).encode("utf-8")

    client = GroqClient(
        api_key="groq-test-fake-key-1", base_url=BASE_URL, model=MODEL, request_fn=raising_transport
    )
    result = _run(client, _verified_state(), "hello", None)
    assert "temporarily unavailable" in result.reply


def test_labeled_last4_verifies():
    from app.state import ConversationState

    client = _client([])
    state = ConversationState(clock=lambda: NOW)
    history = _run(client, state, "hello").history
    history = _run(client, state, "My email is ava@example.com thanks", history).history
    result = _run(client, state, "my last 4 are 4242", history)
    assert state.verified is True
    assert "4242" not in json.dumps(result.history)


def test_uppercase_email_verifies():
    from app.state import ConversationState

    client = _client([])
    state = ConversationState(clock=lambda: NOW)
    history = _run(client, state, "hi").history
    history = _run(client, state, "AVA@EXAMPLE.COM", history).history
    result = _run(client, state, "4242", history)
    assert state.verified is True
    assert "AVA@EXAMPLE.COM" not in json.dumps(result.history)
