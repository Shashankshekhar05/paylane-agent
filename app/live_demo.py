"""Reproducible multi-turn LIVE demo against the real Groq provider.

Entry point for video evidence (reviewers: start here)::

    python -m app.live_demo

Unlike ``python -m app.runner --message ...`` (one stateless turn per
process), this demo holds ONE shared ``ConversationState`` plus its
``history`` across all turns in a single process, so local verification
(T1-T3) persists into the live tool-calling turns (T4-T5).

Flow (mock fixtures from ``app/tools.py`` — fictional data only):
  T1  "Hello, I need help with a refund"  -> agent asks for email (local)
  T2  "ava@example.com"                   -> agent asks for last 4 (local)
  T3  "4242"                              -> verified, bound to c_ava (local)
  T4  refund t_ok $42.50 (eligible)       -> LIVE: model should propose
      get_transaction + issue_refund; policy ALLOW; txn becomes REFUNDED
  T5  refund t_big $80.00 (over $75)      -> LIVE: policy DENY/ESCALATE
      (amount-over-limit); txn stays SETTLED

For every turn it prints the user message, the agent reply, a PII-free
verified-context indicator, and the full audit trace as JSON. It never
prints the API key, the customer email, or card digits outside the
scripted user-message echo. Exit code is 0 on a completed demo run even
if the provider fails closed (the audit trace then shows provider_error).
"""

from __future__ import annotations

import json
import sys

from app import tools
from app.agent import UNAVAILABLE_MESSAGE, build_verified_context_note, run_turn
from app.audit import AuditLog, Kind
from app.config import format_config_status, load_settings
from app.groq import GroqClient
from app.state import ConversationState

TURNS = [
    "Hello, I need help with a refund",
    "ava@example.com",
    "4242",
    "I want a refund for transaction t_ok, amount 42.50",
    "Now refund transaction t_big for $80.00",
]

MAX_PROVIDER_RETRIES = 1


def _had_provider_error(log: AuditLog, since: int) -> bool:
    """True when a provider_error was recorded after `since` events."""
    return any(
        event.kind is Kind.PROVIDER_ERROR for event in log.events()[since:]
    )


def _run_turn_with_retry(client, state, log, message, history):
    """Run one turn, retrying once on transient provider errors.

    Safe: `run_turn` never mutates the caller's `history` list (it works
    on a copy), and the provider_error path leaves `state` untouched, so
    a retry with the pre-turn history snapshot cannot duplicate messages
    or double-execute tools.
    """
    audit_mark = len(log.events())
    result = run_turn(client, state, log, message, history)
    if result.reply == UNAVAILABLE_MESSAGE and _had_provider_error(log, audit_mark):
        print("(transient provider error — retrying once)")
        result = run_turn(client, state, log, message, history)
    return result


def _audit_json(log: AuditLog) -> list[dict[str, object]]:
    """Serialize audit events to JSON-safe dicts (IDs/codes only, no PII)."""
    items = []
    for event in log.events():
        items.append(
            {
                "seq": event.seq,
                "kind": event.kind.value,
                "tool": event.tool_name.value if event.tool_name else None,
                "outcome": event.outcome.value if event.outcome else None,
                "reason": event.reason.value if event.reason else None,
                "ref_id": event.ref_id,
            }
        )
    return items


def main() -> int:
    settings = load_settings()
    # from_settings() fails closed without a usable GROQ_API_KEY.
    try:
        client = GroqClient.from_settings(settings)
    except (RuntimeError, ValueError) as exc:
        print(f"live_demo INVALID config: {exc}", file=sys.stderr)
        return 2
    print(f"live_demo {format_config_status(settings)}")

    tools.reset_mock_data()
    state = ConversationState()
    history: list[dict[str, object]] = []
    log = AuditLog()

    for index, message in enumerate(TURNS, start=1):
        result = _run_turn_with_retry(client, state, log, message, history)
        history = result.history
        # PII-free indicator: presence + length only, never the note text
        # (it carries the internal customer binding) nor any user secret.
        note = build_verified_context_note(state)
        print(f"--- T{index} user ---")
        print(message)
        print(f"--- T{index} agent ---")
        print(result.reply)
        print(f"--- T{index} state: verified={state.verified} "
              f"customer={state.verified_customer_id} "
              f"verified_note={'present' if note is not None else 'absent'} ---")

    print("--- full audit (JSON) ---")
    print(json.dumps(_audit_json(log), indent=2))
    print("--- mock transaction status ---")
    print(f"t_ok={tools.get_transaction('c_ava', 't_ok').status.value} "
          f"t_big={tools.get_transaction('c_ava', 't_big').status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
