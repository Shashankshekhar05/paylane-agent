"""Structured, redacted, in-memory audit events for Phase 3b.

Design: the allowlist is the guarantee, patterns are the backstop.

- Events carry closed enums and validated identifiers only. There is no
  field for message content, summaries, arguments, results, emails, or
  account details, so those values cannot be stored by construction.
- Every string crossing the boundary is additionally scanned for blocked
  patterns (emails, card-digit runs, API-key-like tokens, Bearer tokens,
  key markers). Violations are rejected; rejected values are never
  interpolated into exceptions, reprs, or rendered output.
- Pattern detection is NOT exhaustive: it cannot catch novel, obfuscated,
  split, or encoded secrets, nor secrets in shapes nobody anticipated.
  The closed enums and the absence of free-text fields are what make this
  log safe; the patterns are defense in depth only.
- In-memory only. No file or network I/O happens here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    TURN_START = "turn_start"
    TOOL_PROPOSED = "tool_proposed"
    TOOL_ALLOWED = "tool_allowed"
    TOOL_DENIED = "tool_denied"
    TOOL_RESULT = "tool_result"
    POLICY_DECISION = "policy_decision"
    ESCALATION = "escalation"
    TURN_END = "turn_end"
    PROVIDER_ERROR = "provider_error"


class ToolName(str, Enum):
    GET_CUSTOMER = "get_customer"
    GET_TRANSACTION = "get_transaction"
    ISSUE_REFUND = "issue_refund"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class Outcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"
    NEEDS_CLARIFICATION = "needs_clarification"


class ReasonCode(str, Enum):
    ELIGIBLE = "eligible"
    UNVERIFIED_CUSTOMER = "unverified-customer"
    DUPLICATE_REFUND = "duplicate-refund"
    PENDING_UNRESOLVED = "pending-unresolved"
    OUTSIDE_REFUND_WINDOW = "outside-refund-window"
    AMOUNT_OVER_LIMIT = "amount-over-limit"
    UNKNOWN_TOOL = "unknown-tool"
    BAD_ARGUMENTS = "bad-arguments"
    UNKNOWN_CUSTOMER = "unknown-customer"
    TOOL_FAILED = "tool-failed"
    PROVIDER_ERROR = "provider-error"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget-exceeded"
    VERIFICATION_MISMATCH = "verification-mismatch"
    VERIFICATION_PENDING = "verification-pending"
    UNSUPPORTED_BATCH = "unsupported-batch"
    NOT_OWNER = "not-owner"
    IDENTITY_MISMATCH = "identity-mismatch"


_REF_ID_RE = re.compile(r"^[a-z]+_[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_CARD_RUN_RE = re.compile(r"\b\d{4,19}\b")
_KEY_LIKE_RE = re.compile(r"\b(gsk_|sk-|xai-)[A-Za-z0-9_-]+\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_KEY_MARKER_RE = re.compile(r"\bapi[_-]?key\s*[:=]", re.IGNORECASE)


def contains_blocked_pattern(text: str) -> bool:
    """Backstop check for secret-like shapes. Not exhaustive (see above)."""
    return any(
        pattern.search(text) is not None
        for pattern in (
            _EMAIL_RE,
            _CARD_RUN_RE,
            _KEY_LIKE_RE,
            _BEARER_RE,
            _KEY_MARKER_RE,
        )
    )


def _check_ref_id(ref_id: str | None) -> None:
    if ref_id is None:
        return
    if not _REF_ID_RE.match(ref_id) or contains_blocked_pattern(ref_id):
        # Field name only: the rejected value is never echoed.
        raise ValueError("rejected audit value for field 'ref_id'")


@dataclass(frozen=True, kw_only=True)
class AuditEvent:
    seq: int
    kind: Kind
    tool_name: ToolName | None = None
    outcome: Outcome | None = None
    reason: ReasonCode | None = None
    ref_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, Kind):
            raise TypeError("AuditEvent.kind must be a Kind member")
        if self.tool_name is not None and not isinstance(self.tool_name, ToolName):
            raise TypeError("AuditEvent.tool_name must be a ToolName member")
        if self.outcome is not None and not isinstance(self.outcome, Outcome):
            raise TypeError("AuditEvent.outcome must be an Outcome member")
        if self.reason is not None and not isinstance(self.reason, ReasonCode):
            raise TypeError("AuditEvent.reason must be a ReasonCode member")
        _check_ref_id(self.ref_id)


class AuditLog:
    """Append-only in-memory audit log (IDs and codes only)."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._seq = 0

    def record(
        self,
        kind: Kind,
        *,
        tool_name: ToolName | None = None,
        outcome: Outcome | None = None,
        reason: ReasonCode | None = None,
        ref_id: str | None = None,
    ) -> AuditEvent:
        """Append one event. Unsupported fields raise TypeError (no **kwargs)."""
        self._seq += 1
        event = AuditEvent(
            seq=self._seq,
            kind=kind,
            tool_name=tool_name,
            outcome=outcome,
            reason=reason,
            ref_id=ref_id,
        )
        self._events.append(event)
        return event

    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def render(self) -> str:
        """Compact ID/code-only rendering for evaluation traces."""
        lines = []
        for event in self._events:
            lines.append(
                " ".join(
                    [
                        str(event.seq),
                        event.kind.value,
                        event.tool_name.value if event.tool_name else "-",
                        event.outcome.value if event.outcome else "-",
                        event.reason.value if event.reason else "-",
                        event.ref_id if event.ref_id else "-",
                    ]
                )
            )
        return "\n".join(lines)

    def clear(self) -> None:
        self._events.clear()
        self._seq = 0
