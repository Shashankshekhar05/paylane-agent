"""Groq tool-calling orchestrator for Phase 3c (one turn per call).

`run_turn()` advances a single customer message against a caller-supplied
`ConversationState`. All safety properties are enforced here and in
`app/policy.py`; the model only proposes tool calls and prose.

Mandatory safeguards implemented here:
1.  Solicited verification replies are parsed locally; raw replies never
    enter Groq history (a value-free placeholder is appended instead).
2.  Email alone never verifies: both the email and the last four digits
    must match via `verify_identity`. Model-proposed emails are never
    treated as identity proof.
3.  Unverified account data stays out of model context and replies.
4.  Only the four registry tools run, after Pydantic argument validation.
5.  Multi-call batches execute zero tools (`UNSUPPORTED_BATCH`).
6.  Every proposal received (valid or not) counts to MAX_TOOL_CALLS=5.
7.  Provider requests cap at MAX_PROVIDER_REQUESTS=6.
8.  Refund eligibility + duplicates are rechecked immediately before
    executing `issue_refund`.
9.  `mark_refunded()` runs only after a validated `RefundSuccess`.
10. Success is never claimed without confirmed success in-trace.
11. `escalate_to_human` requires a customer ID seen this run; unknown IDs
     create no ticket.
12. Audit events use closed enums/IDs only; no keys, messages, emails,
     digits, or payloads are ever logged.
13. Ownership binding: verification records one `verified_customer_id`;
     `get_transaction` proposals naming any other customer are denied before
     the tool runs, foreign transaction results are never rendered to the
     model, and `refund_decision` denies non-owned transactions (`not-owner`).
     `get_customer` lookups for other known customers still run (documented
     cross-customer escalation needs the ID for routing), but their
     sensitive fields are redacted from model context; foreign names/digits
     in final replies are blocked even when verified.
14. Tool exceptions fail closed: any unexpected exception from a mock tool
     records `tool-failed` and returns a safe error, never a success.
15. False cross-identity claims are blocked: model text affirming
     verification as a different known customer is replaced
     (`identity-mismatch`); the binding is authoritative, not model prose.
16. Refund success must name the requested transaction (`refund_id_matches`);
     mismatched IDs fail closed without marking anything refunded.
17. Unexpected transport exceptions fail closed like provider errors
      (`temporarily unavailable`), never leaking secrets or internals.
      (`get_customer` lookups and `escalate_to_human` for other known
      customers remain allowed: cross-customer escalation is documented
      intentional behavior routed to human review.)
18. Verified sessions add a trusted system-level context note derived only
      from `state.verified` + `state.verified_customer_id` (no PII, no
      hardcoded IDs), rebuilt every turn and never persisted into history,
      so the model stops re-asking for email/digits. Guidance only:
      authorization stays in `app/policy.py`.

Unresolved items ($75 wording, pending, partials, frozen interaction) are
preserved exactly as `app/policy.py` + `docs/POLICY_DECISIONS.md` define.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from pydantic import ValidationError

from app import tools
from app.audit import AuditLog, Kind, Outcome, ReasonCode, ToolName
from app.groq import GroqClient, GroqError, GroqTimeoutError, build_tool_result_message
from app.policy import (
    can_disclose,
    is_duplicate,
    is_owner,
    is_refund_success,
    refund_decision,
    refund_id_matches,
    verify_identity,
)
from app.prompts import SYSTEM_PROMPT
from app.schemas import (
    CustomerRecord,
    EscalateArgs,
    GetCustomerArgs,
    GetTransactionArgs,
    IssueRefundArgs,
    StrictModel,
    TransactionRecord,
)
from app.state import ConversationState, VerificationStatus

MAX_PROVIDER_REQUESTS_PER_TURN = 6
MAX_TOOL_CALLS_PER_TURN = 5
MAX_VERIFICATION_ATTEMPTS = 3

ASK_EMAIL_MESSAGE = (
    "To help with your account, please reply with the email address on file."
)
ASK_LAST4_MESSAGE = (
    "Thanks. Now please reply with the last 4 digits of your card."
)
VERIFIED_MESSAGE = "Thanks — you're verified. How can I help?"
MISMATCH_MESSAGE = (
    "Those details didn't match our records. Please check and try again."
)
UNRECOGNIZED_MESSAGE = (
    "I couldn't find that account. Please check the email address and try again, "
    "or contact Paylane support directly for help with verification."
)
BUDGET_MESSAGE = (
    "I hit a processing limit on that turn. Please try again, or ask for a "
    "human specialist."
)
UNAVAILABLE_MESSAGE = (
    "The support service is temporarily unavailable. Please try again shortly."
)
ONE_STEP_MESSAGE = (
    "I can only handle one step at a time. Could you please restate your request?"
)
UNCONFIRMED_MESSAGE = (
    "I couldn't confirm that action. Please try again or ask for a human specialist."
)
IDENTITY_MISMATCH_MESSAGE = (
    "I can only help with the account verified in this conversation. "
    "If you need help with a different account, please verify it first "
    "or ask for a human specialist."
)
FALLBACK_MESSAGE = "I couldn't prepare a response. Please try again shortly."
VERIFICATION_PLACEHOLDER_VERIFIED = "[verification details received — outcome: verified]"
VERIFICATION_PLACEHOLDER_PENDING = "[verification details received — outcome: not verified]"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_EMAIL_LABELED_RE = re.compile(r"(?i)\bemail\s*[:=]\s*([\w.+-]+@[\w-]+\.[\w.]+)")
_LAST4_FULL_RE = re.compile(r"\d{4}")
_LAST4_LABELED_RE = re.compile(r"(?i)(?:last\s*-?\s*4|last4|card)[^0-9]{0,20}(\d{4})")
_CARD_RUN_RE = re.compile(r"\b\d{13,19}\b")
# Narrow identity-affirmation detector for the foreign-identity guard in
# _finalize. It fires only together with a *different known customer's*
# name or ID (see _claims_foreign_identity), so ordinary conversation —
# including identity words about the verified customer — passes through.
_IDENTITY_CLAIM_RE = re.compile(r"\bverif\w*|\bauthenticat\w*", re.IGNORECASE)
# Defense in depth only: this pattern backs the final-response guard in
# _finalize. Refund authorization itself happens in _handle_refund via
# is_refund_success; this regex never approves anything. It is not a
# complete natural-language detector: novel phrasings can still evade it.
_SUCCESS_CLAIM_RE = re.compile(
    r"\brefund\b.{0,40}\b(success|successful|succeeded|complete|completed"
    r"|completing|completion|done|issued|approved|processed)\b"
    r"|\bmoney\b.{0,30}\b(on its way back|on the way back|refunded|sent back)\b",
    re.IGNORECASE,
)

_TOOL_DESCRIPTIONS = {
    "get_customer": "Look up a customer record by email address.",
    "get_transaction": "Look up one transaction for a known customer.",
    "issue_refund": "Request a mock refund for a known transaction.",
    "escalate_to_human": "Open a human-support ticket with a short summary.",
}

_ARGS_MODELS: dict[str, type[StrictModel]] = {
    "get_customer": GetCustomerArgs,
    "get_transaction": GetTransactionArgs,
    "issue_refund": IssueRefundArgs,
    "escalate_to_human": EscalateArgs,
}

_TOOL_NAMES: dict[str, ToolName] = {
    "get_customer": ToolName.GET_CUSTOMER,
    "get_transaction": ToolName.GET_TRANSACTION,
    "issue_refund": ToolName.ISSUE_REFUND,
    "escalate_to_human": ToolName.ESCALATE_TO_HUMAN,
}


@dataclass
class TurnResult:
    reply: str
    history: list[dict[str, Any]]


@dataclass
class _Seen:
    """Records and emails observed this turn (Python-side only)."""

    customers: dict[str, tuple[CustomerRecord, str]]
    txns: dict[str, TransactionRecord]

    def __init__(self) -> None:
        self.customers = {}
        self.txns = {}


def build_tool_definitions() -> list[dict[str, Any]]:
    """OpenAI-compatible function specs for the four approved tools."""
    definitions = []
    for name, model in _ARGS_MODELS.items():
        parameters = model.model_json_schema()
        parameters.pop("title", None)
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": _TOOL_DESCRIPTIONS[name],
                    "parameters": parameters,
                },
            }
        )
    return definitions


def build_verified_context_note(state: ConversationState) -> Optional[str]:
    """Trusted system-level note for an already-verified session.

    Derived exclusively from application state (`state.verified` plus the
    bound `state.verified_customer_id`) — never from customer claims or
    conversation history. It carries no PII: only the opaque internal
    customer binding, no email, no card digits, no names, no secrets.
    Returns None unless the session is bound, so unverified and
    pending-verification flows keep their existing behavior. The note is
    guidance only; authorization stays in `app/policy.py`.
    """
    if not state.verified or state.verified_customer_id is None:
        return None
    return (
        "TRUSTED APPLICATION CONTEXT (not customer text): identity "
        "verification already succeeded for this session, bound to customer "
        f"'{state.verified_customer_id}'. Do not ask the customer to repeat "
        "their email address or card digits. For account requests, propose "
        "the appropriate approved tool using this customer binding "
        "(get_transaction, issue_refund, or escalate_to_human). Every "
        "proposal is validated by deterministic policy code; a denial is final."
    )


def _extract_email(text: str) -> Optional[str]:
    stripped = text.strip()
    full = _EMAIL_RE.fullmatch(stripped)
    if full:
        return full.group(0)
    labeled = _EMAIL_LABELED_RE.search(stripped)
    if labeled:
        return labeled.group(1)
    hits = _EMAIL_RE.findall(stripped)
    if len(hits) == 1:
        return hits[0]
    return None


def _extract_last4(text: str) -> Optional[str]:
    stripped = text.strip()
    if _LAST4_FULL_RE.fullmatch(stripped):
        return stripped
    labeled = _LAST4_LABELED_RE.search(stripped)
    if labeled:
        return labeled.group(1)
    return None


def _bump_attempts(state: ConversationState) -> bool:
    """Count a well-formed mismatch; True when the cap is reached."""
    state.verification_attempts += 1
    return state.verification_attempts >= MAX_VERIFICATION_ATTEMPTS


def _clear_pending(state: ConversationState) -> None:
    state.awaiting_verification_reply = False
    state.pending_customer_id = None
    state.pending_record = None


def _process_verification_reply(
    state: ConversationState, audit: AuditLog, text: str
) -> str:
    """Handle one solicited verification reply locally. Returns the reply.

    Never appends raw text to history; the caller appends a placeholder.
    """
    if state.email_on_file is None:
        email = _extract_email(text)
        if email is None:
            audit.record(Kind.POLICY_DECISION, reason=ReasonCode.VERIFICATION_PENDING)
            return ASK_EMAIL_MESSAGE
        record = _safe_tool_call(audit, ToolName.GET_CUSTOMER, tools.get_customer, email)
        if record is _TOOL_FAILED:
            return UNAVAILABLE_MESSAGE
        if record is None:
            audit.record(Kind.POLICY_DECISION, reason=ReasonCode.VERIFICATION_MISMATCH)
            if _bump_attempts(state):
                _clear_pending(state)
                state.verification = VerificationStatus.UNVERIFIED
                return UNRECOGNIZED_MESSAGE
            return ASK_EMAIL_MESSAGE
        state.email_on_file = email.strip()
        state.pending_customer_id = record.id
        state.pending_record = record
        state.known_customer_ids.add(record.id)
        state.verification = VerificationStatus.PENDING
        audit.record(
            Kind.POLICY_DECISION,
            reason=ReasonCode.VERIFICATION_PENDING,
            ref_id=record.id,
        )
        return ASK_LAST4_MESSAGE
    last4 = _extract_last4(text)
    if last4 is None:
        audit.record(Kind.POLICY_DECISION, reason=ReasonCode.VERIFICATION_PENDING)
        return ASK_LAST4_MESSAGE
    record = state.pending_record
    email_on_file = state.email_on_file
    # Both halves are user-supplied across the two solicited steps: the
    # email produced a live record lookup in step one, the digits arrive
    # now. verify_identity enforces both halves regardless.
    if (
        record is not None
        and email_on_file is not None
        and state.pending_customer_id is not None
        and verify_identity(record, email_on_file, email_on_file, last4)
    ):
        state.mark_verified(state.pending_customer_id)
        state.verification_attempts = 0
        _clear_pending(state)
        audit.record(Kind.POLICY_DECISION, reason=ReasonCode.ELIGIBLE)
        return VERIFIED_MESSAGE
    audit.record(Kind.POLICY_DECISION, reason=ReasonCode.VERIFICATION_MISMATCH)
    if _bump_attempts(state):
        customer_id = state.pending_customer_id
        _clear_pending(state)
        state.verification = VerificationStatus.UNVERIFIED
        if customer_id is not None and customer_id in state.known_customer_ids:
            ticket = _safe_tool_call(
                audit, ToolName.ESCALATE_TO_HUMAN, tools.escalate_to_human,
                customer_id,
                "Verification attempts exhausted; manual identity check needed.",
            )
            if ticket is _TOOL_FAILED:
                return UNAVAILABLE_MESSAGE
            audit.record(
                Kind.TOOL_ALLOWED,
                tool_name=ToolName.ESCALATE_TO_HUMAN,
                ref_id=ticket.ticket_id,
            )
            audit.record(
                Kind.ESCALATION,
                tool_name=ToolName.ESCALATE_TO_HUMAN,
                outcome=Outcome.ESCALATE,
                ref_id=ticket.ticket_id,
            )
            return (
                "I couldn't verify those details, so I've escalated this to a "
                "human specialist for a manual identity check."
            )
        return UNRECOGNIZED_MESSAGE
    return MISMATCH_MESSAGE


def present_result(
    state: ConversationState, tool_key: str, result: Any
) -> dict[str, Any]:
    """Render a tool result for model context (redacted until verified)."""
    if tool_key == "get_customer":
        if result is None:
            return {"status": "not-found"}
        if not can_disclose(state):
            return {"status": "found", "customer_id": result.id, "verification": "pending"}
        if result.id != state.verified_customer_id:
            # Foreign record: route-only redaction. The ID stays available
            # for documented cross-customer escalation; name, status, and
            # card digits never enter model context.
            return {"status": "found", "customer_id": result.id, "verification": "pending"}
        return result.model_dump(mode="json")
    if tool_key == "get_transaction":
        if result is None:
            return {"status": "not-found"}
        if not can_disclose(state):
            return {"status": "found", "txn_id": result.txn_id, "verification": "pending"}
        if not is_owner(state, result):
            return {"status": "denied", "reason": "not-owner"}
        return result.model_dump(mode="json")
    if tool_key == "issue_refund":
        if is_refund_success(result):
            return {"status": "refunded", "refund_id": result.refund_id}
        reason = result.reason if hasattr(result, "reason") else "failed"
        return {"status": "failed", "reason": reason}
    if tool_key == "escalate_to_human":
        return {"status": "escalated", "ticket_id": result.ticket_id}
    return {"status": "error", "reason": "unknown-tool"}


def _forbidden_hit(state: ConversationState, seen: _Seen, text: str) -> bool:
    """True when customer-facing text carries data it must not.

    Pattern detection is not exhaustive: obfuscated, split, or novel
    encodings of emails and digits can evade these checks. The primary
    guarantees are structural (redacted tool outputs, placeholder history,
    verified-only disclosure); this scan is defense in depth.
    """
    lowered = text.lower()
    emails: set[str] = set()
    if state.email_on_file:
        emails.add(state.email_on_file.lower())
    for _, address in seen.customers.values():
        emails.add(address.lower())
    for address in emails:
        if address and address in lowered:
            return True
    if _CARD_RUN_RE.search(text) is not None:
        return True
    if not can_disclose(state):
        for record, _ in seen.customers.values():
            if record.card_last4 in text or record.name in text:
                return True
        for txn in seen.txns.values():
            if txn.txn_id in text:
                return True
    else:
        # Verified sessions may discuss their own account, but never another
        # customer's name or card digits observed this turn.
        for record, _ in seen.customers.values():
            if record.id != state.verified_customer_id and (
                record.card_last4 in text or record.name in text
            ):
                return True
    return False


def _claims_foreign_identity(state: ConversationState, seen: _Seen, text: str) -> bool:
    """True when text affirms verification as a different known customer.

    All three must hold: the session is bound to one customer, the text
    uses identity-affirming language, and it names a *different* customer
    observed this run (record name/ID from this turn, or a known ID).
    Anything less specific is left alone to avoid blocking normal
    conversation. A cold claim with no known foreign identifier cannot be
    tied to a customer deterministically and is out of scope here.
    """
    if not state.verified or state.verified_customer_id is None:
        return False
    if _IDENTITY_CLAIM_RE.search(text) is None:
        return False
    lowered = text.lower()
    for record, _ in seen.customers.values():
        if record.id != state.verified_customer_id and (
            record.name.lower() in lowered or record.id.lower() in lowered
        ):
            return True
    for customer_id in state.known_customer_ids:
        if customer_id != state.verified_customer_id and customer_id.lower() in lowered:
            return True
    return False


_TOOL_FAILED = object()


def _safe_tool_call(
    audit: AuditLog, tool_name: ToolName, func: Any, *args: Any
) -> Any:
    """Run one mock tool; unexpected exceptions fail closed.

    Returns the tool result, or the `_TOOL_FAILED` sentinel after recording
    a `tool-failed` denial (IDs/codes only — exception text, tracebacks, and
    arguments are never logged). Callers must check for the sentinel and
    must not treat it as success, mark refunds, or open tickets.
    """
    try:
        return func(*args)
    except Exception:
        audit.record(
            Kind.TOOL_DENIED, tool_name=tool_name, outcome=Outcome.DENY,
            reason=ReasonCode.TOOL_FAILED,
        )
        return _TOOL_FAILED


def _claims_success(text: str) -> bool:
    return _SUCCESS_CLAIM_RE.search(text) is not None


def run_turn(
    client: GroqClient,
    state: ConversationState,
    audit: AuditLog,
    user_message: str,
    history: Optional[Sequence[Mapping[str, Any]]] = None,
) -> TurnResult:
    """Advance one customer turn. Caller owns `state` across turns."""
    conversation = [dict(message) for message in history] if history else []
    audit.record(Kind.TURN_START)
    try:
        return _run_turn_inner(client, state, audit, user_message, conversation)
    finally:
        audit.record(Kind.TURN_END)


def _run_turn_inner(
    client: GroqClient,
    state: ConversationState,
    audit: AuditLog,
    user_message: str,
    conversation: list[dict[str, Any]],
) -> TurnResult:
    seen = _Seen()

    if state.awaiting_verification_reply:
        reply = _process_verification_reply(state, audit, user_message)
        conversation.append(
            {"role": "user", "content": VERIFICATION_PLACEHOLDER_VERIFIED}
            if state.verified
            else {"role": "user", "content": VERIFICATION_PLACEHOLDER_PENDING}
        )
        conversation.append({"role": "assistant", "content": reply})
        return TurnResult(reply=reply, history=conversation)

    if not state.verified:
        state.awaiting_verification_reply = True
        state.verification = VerificationStatus.PENDING
        audit.record(Kind.POLICY_DECISION, reason=ReasonCode.VERIFICATION_PENDING)
        conversation.append({"role": "user", "content": "[message withheld pending verification]"})
        conversation.append({"role": "assistant", "content": ASK_EMAIL_MESSAGE})
        return TurnResult(reply=ASK_EMAIL_MESSAGE, history=conversation)

    system_content = SYSTEM_PROMPT
    verified_note = build_verified_context_note(state)
    if verified_note is not None:
        # Provider-bound only: rebuilt from live state every turn, never
        # persisted into caller-owned history (which must not be trusted).
        system_content = system_content + "\n\n" + verified_note
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    messages.extend(conversation)
    messages.append({"role": "user", "content": user_message})
    conversation.append({"role": "user", "content": user_message})

    definitions = build_tool_definitions()
    provider_requests = 0
    proposed_calls = 0
    refund_successes = 0

    while True:
        if provider_requests >= MAX_PROVIDER_REQUESTS_PER_TURN:
            audit.record(Kind.TOOL_DENIED, reason=ReasonCode.BUDGET_EXCEEDED)
            reply = BUDGET_MESSAGE
            conversation.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, history=conversation)
        try:
            completion = client.chat(messages, definitions)
        except GroqError as exc:
            reason = (
                ReasonCode.TIMEOUT
                if isinstance(exc, GroqTimeoutError)
                else ReasonCode.PROVIDER_ERROR
            )
            audit.record(Kind.PROVIDER_ERROR, reason=reason)
            reply = UNAVAILABLE_MESSAGE
            conversation.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, history=conversation)
        except Exception:
            # Unexpected transport failure: same fail-closed path, with no
            # secret, header, traceback, or payload reaching the customer.
            audit.record(Kind.PROVIDER_ERROR, reason=ReasonCode.PROVIDER_ERROR)
            reply = UNAVAILABLE_MESSAGE
            conversation.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, history=conversation)
        provider_requests += 1

        calls = list(completion.message.tool_calls)
        if len(calls) == 0:
            reply = _finalize(state, seen, completion.message.content, refund_successes, audit)
            conversation.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, history=conversation)

        proposed_calls += len(calls)
        if len(calls) != 1:
            audit.record(
                Kind.TOOL_DENIED,
                outcome=Outcome.DENY,
                reason=ReasonCode.UNSUPPORTED_BATCH,
            )
            reply = ONE_STEP_MESSAGE
            conversation.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, history=conversation)
        if proposed_calls > MAX_TOOL_CALLS_PER_TURN:
            audit.record(
                Kind.TOOL_DENIED, outcome=Outcome.DENY, reason=ReasonCode.BUDGET_EXCEEDED
            )
            reply = BUDGET_MESSAGE
            conversation.append({"role": "assistant", "content": reply})
            return TurnResult(reply=reply, history=conversation)

        refund_successes += _handle_proposal(state, audit, seen, messages, conversation, calls[0])


def _handle_proposal(
    state: ConversationState,
    audit: AuditLog,
    seen: _Seen,
    messages: list[dict[str, Any]],
    conversation: list[dict[str, Any]],
    proposal: Any,
) -> int:
    """Validate, gate, and execute one proposal. Returns successes added."""
    audit.record(
        Kind.TOOL_PROPOSED,
        tool_name=_TOOL_NAMES.get(proposal.name) if proposal.name in _TOOL_NAMES else None,
    )
    if proposal.name not in _ARGS_MODELS:
        audit.record(
            Kind.TOOL_DENIED, outcome=Outcome.DENY, reason=ReasonCode.UNKNOWN_TOOL
        )
        output: dict[str, Any] = {"status": "error", "reason": "unknown-tool"}
        _append_exchange(messages, conversation, proposal, output)
        return 0
    try:
        args = _ARGS_MODELS[proposal.name](**proposal.arguments)
    except (ValidationError, TypeError, AttributeError):
        audit.record(
            Kind.TOOL_DENIED,
            tool_name=_TOOL_NAMES[proposal.name],
            outcome=Outcome.DENY,
            reason=ReasonCode.BAD_ARGUMENTS,
        )
        _append_exchange(
            messages, conversation, proposal, {"status": "error", "reason": "bad-arguments"}
        )
        return 0

    if proposal.name == "get_customer":
        result = _safe_tool_call(audit, ToolName.GET_CUSTOMER, tools.get_customer, args.email)
        if result is _TOOL_FAILED:
            _append_exchange(
                messages, conversation, proposal, {"status": "error", "reason": "tool-failed"}
            )
            return 0
        if result is not None:
            seen.customers[result.id] = (result, args.email.strip())
            state.known_customer_ids.add(result.id)
            audit.record(
                Kind.TOOL_ALLOWED, tool_name=ToolName.GET_CUSTOMER, ref_id=result.id
            )
            audit.record(
                Kind.TOOL_RESULT, tool_name=ToolName.GET_CUSTOMER, ref_id=result.id
            )
        else:
            audit.record(Kind.TOOL_RESULT, tool_name=ToolName.GET_CUSTOMER)
        _append_exchange(messages, conversation, proposal, present_result(state, "get_customer", result))
        return 0

    if proposal.name == "get_transaction":
        # Ownership gate: verification binds to one customer ID. A proposal
        # naming any other customer is denied before the tool runs, so a
        # foreign transaction never enters model context or `seen`.
        if args.customer_id != state.verified_customer_id:
            audit.record(
                Kind.POLICY_DECISION, outcome=Outcome.DENY, reason=ReasonCode.NOT_OWNER
            )
            audit.record(
                Kind.TOOL_DENIED,
                tool_name=ToolName.GET_TRANSACTION,
                outcome=Outcome.DENY,
                reason=ReasonCode.NOT_OWNER,
            )
            _append_exchange(
                messages,
                conversation,
                proposal,
                {"status": "denied", "reason": "not-owner"},
            )
            return 0
        result = _safe_tool_call(
            audit, ToolName.GET_TRANSACTION, tools.get_transaction,
            args.customer_id, args.txn_id,
        )
        if result is _TOOL_FAILED:
            _append_exchange(
                messages, conversation, proposal, {"status": "error", "reason": "tool-failed"}
            )
            return 0
        if result is not None:
            seen.txns[result.txn_id] = result
            audit.record(
                Kind.TOOL_ALLOWED, tool_name=ToolName.GET_TRANSACTION, ref_id=result.txn_id
            )
            audit.record(
                Kind.TOOL_RESULT, tool_name=ToolName.GET_TRANSACTION, ref_id=result.txn_id
            )
        else:
            audit.record(Kind.TOOL_RESULT, tool_name=ToolName.GET_TRANSACTION)
        _append_exchange(
            messages, conversation, proposal, present_result(state, "get_transaction", result)
        )
        return 0

    if proposal.name == "issue_refund":
        return _handle_refund(state, audit, seen, messages, conversation, proposal, args)

    if proposal.name == "escalate_to_human":
        known = args.customer_id in seen.customers or args.customer_id in state.known_customer_ids
        if not known:
            audit.record(
                Kind.TOOL_DENIED,
                tool_name=ToolName.ESCALATE_TO_HUMAN,
                outcome=Outcome.DENY,
                reason=ReasonCode.UNKNOWN_CUSTOMER,
            )
            _append_exchange(
                messages, conversation, proposal, {"status": "error", "reason": "unknown-customer"}
            )
            return 0
        ticket = _safe_tool_call(
            audit, ToolName.ESCALATE_TO_HUMAN, tools.escalate_to_human,
            args.customer_id, args.summary,
        )
        if ticket is _TOOL_FAILED:
            _append_exchange(
                messages, conversation, proposal, {"status": "error", "reason": "tool-failed"}
            )
            return 0
        audit.record(
            Kind.TOOL_ALLOWED, tool_name=ToolName.ESCALATE_TO_HUMAN, ref_id=ticket.ticket_id
        )
        audit.record(
            Kind.ESCALATION,
            tool_name=ToolName.ESCALATE_TO_HUMAN,
            outcome=Outcome.ESCALATE,
            ref_id=ticket.ticket_id,
        )
        _append_exchange(
            messages, conversation, proposal, present_result(state, "escalate_to_human", ticket)
        )
        return 0

    return 0


def _handle_refund(
    state: ConversationState,
    audit: AuditLog,
    seen: _Seen,
    messages: list[dict[str, Any]],
    conversation: list[dict[str, Any]],
    proposal: Any,
    args: Any,
) -> int:
    audit.record(Kind.TOOL_PROPOSED, tool_name=ToolName.ISSUE_REFUND)
    if not state.verified:
        audit.record(
            Kind.TOOL_DENIED,
            tool_name=ToolName.ISSUE_REFUND,
            outcome=Outcome.DENY,
            reason=ReasonCode.UNVERIFIED_CUSTOMER,
        )
        _append_exchange(
            messages, conversation, proposal, {"status": "denied", "reason": "unverified-customer"}
        )
        return 0
    txn = seen.txns.get(args.txn_id)
    if txn is None:
        audit.record(
            Kind.TOOL_DENIED,
            tool_name=ToolName.ISSUE_REFUND,
            outcome=Outcome.DENY,
            reason=ReasonCode.BAD_ARGUMENTS,
        )
        _append_exchange(
            messages, conversation, proposal, {"status": "error", "reason": "unknown-transaction"}
        )
        return 0
    # Recheck eligibility and duplicates immediately before execution.
    if is_duplicate(state, txn):
        audit.record(
            Kind.POLICY_DECISION, outcome=Outcome.DENY, reason=ReasonCode.DUPLICATE_REFUND
        )
        audit.record(
            Kind.TOOL_DENIED,
            tool_name=ToolName.ISSUE_REFUND,
            outcome=Outcome.DENY,
            reason=ReasonCode.DUPLICATE_REFUND,
        )
        _append_exchange(
            messages, conversation, proposal, {"status": "denied", "reason": "duplicate-refund"}
        )
        return 0
    decision = refund_decision(state, txn, args.amount, state.now())
    outcome = {
        "allow": Outcome.ALLOW, "deny": Outcome.DENY,
        "escalate": Outcome.ESCALATE, "needs_clarification": Outcome.NEEDS_CLARIFICATION,
    }[decision.outcome]
    audit.record(Kind.POLICY_DECISION, outcome=outcome, reason=ReasonCode(decision.reason))
    if decision.outcome != "allow":
        audit.record(
            Kind.TOOL_DENIED, tool_name=ToolName.ISSUE_REFUND, outcome=outcome,
            reason=ReasonCode(decision.reason),
        )
        status = {"deny": "denied", "escalate": "escalation_required"}.get(
            decision.outcome, "needs_clarification"
        )
        _append_exchange(
            messages, conversation, proposal, {"status": status, "reason": decision.reason}
        )
        return 0
    if is_duplicate(state, txn):
        audit.record(
            Kind.TOOL_DENIED,
            tool_name=ToolName.ISSUE_REFUND,
            outcome=Outcome.DENY,
            reason=ReasonCode.DUPLICATE_REFUND,
        )
        _append_exchange(
            messages, conversation, proposal, {"status": "denied", "reason": "duplicate-refund"}
        )
        return 0
    result = _safe_tool_call(
        audit, ToolName.ISSUE_REFUND, tools.issue_refund, args.txn_id, args.amount
    )
    if result is _TOOL_FAILED:
        _append_exchange(
            messages, conversation, proposal, {"status": "failed", "reason": "tool-failed"}
        )
        return 0
    if not refund_id_matches(result, args.txn_id):
        audit.record(
            Kind.TOOL_DENIED,
            tool_name=ToolName.ISSUE_REFUND,
            outcome=Outcome.DENY,
            reason=ReasonCode.TOOL_FAILED,
        )
        _append_exchange(
            messages, conversation, proposal, present_result(state, "issue_refund", result)
        )
        return 0
    state.mark_refunded(args.txn_id)
    audit.record(
        Kind.TOOL_ALLOWED, tool_name=ToolName.ISSUE_REFUND, ref_id=result.refund_id
    )
    audit.record(
        Kind.TOOL_RESULT, tool_name=ToolName.ISSUE_REFUND, ref_id=result.refund_id
    )
    _append_exchange(
        messages, conversation, proposal, present_result(state, "issue_refund", result)
    )
    return 1


def _append_exchange(
    messages: list[dict[str, Any]],
    conversation: list[dict[str, Any]],
    proposal: Any,
    output: dict[str, Any],
) -> None:
    """Append a complete call/result pair to both histories."""
    assistant_echo = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": proposal.call_id,
                "type": "function",
                # Pairing uses call_id; arguments are intentionally blanked so
                # model-proposed values (which may contain emails) are not
                # replayed into subsequent model context.
                "function": {"name": proposal.name, "arguments": "{}"},
            }
        ],
    }
    tool_message = build_tool_result_message(proposal.call_id, json.dumps(output))
    messages.extend([assistant_echo, tool_message])
    conversation.extend([assistant_echo, tool_message])


def _finalize(
    state: ConversationState,
    seen: _Seen,
    content: Any,
    refund_successes: int,
    audit: AuditLog,
) -> str:
    """Apply disclosure and success-claim guards to final text."""
    text = content if isinstance(content, str) and content.strip() else FALLBACK_MESSAGE
    if _claims_foreign_identity(state, seen, text):
        audit.record(
            Kind.POLICY_DECISION, outcome=Outcome.DENY, reason=ReasonCode.IDENTITY_MISMATCH
        )
        return IDENTITY_MISMATCH_MESSAGE
    if _forbidden_hit(state, seen, text):
        audit.record(
            Kind.POLICY_DECISION, outcome=Outcome.DENY, reason=ReasonCode.UNVERIFIED_CUSTOMER
        )
        return ASK_EMAIL_MESSAGE
    if _claims_success(text) and refund_successes == 0:
        audit.record(
            Kind.TOOL_DENIED, tool_name=ToolName.ISSUE_REFUND,
            outcome=Outcome.DENY, reason=ReasonCode.TOOL_FAILED,
        )
        return UNCONFIRMED_MESSAGE
    return text
