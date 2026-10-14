"""Audit redaction and event-design tests (offline, no network)."""

import pytest

from app.audit import (
    AuditEvent,
    AuditLog,
    Kind,
    Outcome,
    ReasonCode,
    ToolName,
    contains_blocked_pattern,
)


def _scripted_log() -> AuditLog:
    log = AuditLog()
    log.record(Kind.TURN_START)
    log.record(Kind.TOOL_PROPOSED, tool_name=ToolName.GET_CUSTOMER)
    log.record(
        Kind.TOOL_DENIED,
        tool_name=ToolName.ISSUE_REFUND,
        outcome=Outcome.DENY,
        reason=ReasonCode.UNVERIFIED_CUSTOMER,
    )
    log.record(
        Kind.ESCALATION,
        tool_name=ToolName.ESCALATE_TO_HUMAN,
        outcome=Outcome.ESCALATE,
        reason=ReasonCode.AMOUNT_OVER_LIMIT,
        ref_id="h_0001",
    )
    log.record(Kind.TURN_END)
    return log


def test_events_sequenced_and_rendered_as_codes_only():
    log = _scripted_log()
    assert [event.seq for event in log.events()] == [1, 2, 3, 4, 5]
    rendered = log.render()
    assert "1 turn_start - - - -" in rendered
    assert "h_0001" in rendered
    assert "@" not in rendered


def test_unsupported_fields_rejected_not_silently_accepted():
    log = AuditLog()
    with pytest.raises(TypeError):
        log.record(Kind.TURN_START, bogus_field="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AuditEvent(seq=1, kind=Kind.TURN_START, bogus="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        log.record("tool_denied")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        log.record(Kind.TOOL_DENIED, outcome="deny")  # type: ignore[arg-type]


def test_non_identifier_ref_ids_rejected():
    log = AuditLog()
    for bad in (
        "ava@example.com",
        "4242",
        "4111111111111111",
        "please refund my order now",
        '{"email": "x"}',
        "r t_ok",
        "",
    ):
        with pytest.raises(ValueError):
            log.record(Kind.TOOL_RESULT, ref_id=bad)
    assert log.events() == []


def test_blocked_values_never_echoed_in_errors_or_output():
    secrets = [
        "user@example.com",
        "4111111111111111",
        "gsk_testFakeSecretValue123",
        "Bearer secret-token-value",
        "api_key=super-secret-value",
    ]
    for secret in secrets:
        assert contains_blocked_pattern(secret) is True
        with pytest.raises(ValueError) as excinfo:
            AuditLog().record(Kind.TOOL_RESULT, ref_id=secret)
        assert secret not in str(excinfo.value)
        assert secret not in repr(excinfo.value)
    assert contains_blocked_pattern("h_0001") is False
    assert contains_blocked_pattern("amount-over-limit") is False


def test_render_never_contains_rejected_values():
    log = AuditLog()
    with pytest.raises(ValueError):
        log.record(Kind.ESCALATION, ref_id="contact user@example.com now")
    assert "user@example.com" not in log.render()
    assert "user@example.com" not in repr(log.events())


def test_clear_resets_log():
    log = _scripted_log()
    log.clear()
    assert log.events() == []
    assert log.render() == ""
    event = log.record(Kind.TURN_START)
    assert event.seq == 1


def test_unsupported_batch_reason_code_accepted_and_rendered():
    log = AuditLog()
    event = log.record(Kind.TOOL_DENIED, reason=ReasonCode.UNSUPPORTED_BATCH)
    assert event.reason is ReasonCode.UNSUPPORTED_BATCH
    assert "unsupported-batch" in log.render()
