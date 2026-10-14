"""Static prompt-content tests (no model-behavior evaluation).

These tests assert what the prompt text says. Whether a model follows the
prompt is a behavior question for Phase 5 evaluation, explicitly out of
scope here: Python policy code — not this text — is the authorization
boundary.
"""

import re

from app import prompts

FULL_TEXT = prompts.SYSTEM_PROMPT + "\n" + prompts.RULEBOOK


def test_authority_disclaimer_present():
    assert "not" in prompts.SYSTEM_PROMPT.lower()
    assert "authorization" in FULL_TEXT.lower() or "authorization boundary" in FULL_TEXT.lower()
    assert "app/policy.py" in prompts.SYSTEM_PROMPT


def test_all_four_tool_names_present():
    for name in ("get_customer", "get_transaction", "issue_refund", "escalate_to_human"):
        assert name in prompts.SYSTEM_PROMPT
        assert name in prompts.RULEBOOK


def test_required_safety_instructions_present():
    lowered = FULL_TEXT.lower()
    assert "untrusted" in lowered
    assert "never" in lowered and "successful" in lowered
    assert "unfreeze" in lowered
    assert "verif" in lowered
    for trigger in ("legal", "$500", "closure", "$75"):
        assert trigger in FULL_TEXT


def test_unresolved_areas_not_converted_to_rules():
    lowered = FULL_TEXT.lower()
    assert "partial refunds are allowed" not in lowered
    assert "partial refunds are forbidden" not in lowered
    assert "partial refunds are prohibited" not in lowered
    assert "may still be refunded" not in lowered
    assert "requested amount" not in lowered
    assert "must equal" not in lowered
    # Unresolved areas direct the model to ask instead.
    assert "clarif" in lowered


def test_prompt_contains_no_sensitive_or_secret_material():
    assert "GROQ_API_KEY" not in FULL_TEXT
    assert "your-key-here" not in FULL_TEXT.lower()
    assert "@" not in FULL_TEXT
    assert re.search(r"\b\d{4}\b", FULL_TEXT) is None
    assert "refund_id" not in FULL_TEXT
    assert "ticket_id" not in FULL_TEXT


def test_rulebook_is_concise_guidance():
    assert len(prompts.RULEBOOK.splitlines()) <= 40
    assert len(prompts.SYSTEM_PROMPT) > len(prompts.RULEBOOK)
