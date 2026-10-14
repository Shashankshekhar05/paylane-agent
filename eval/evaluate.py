"""Deterministic offline evaluator for the 12 Phase 4 scenarios.

No network access, no secrets, no live model. Each scenario replays its
scripted provider responses through `app.agent.run_turn` against the real
mock tools and policy gates, then checks the recorded expectations.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app import tools
from app.agent import run_turn
from app.audit import AuditLog
from app.groq import GroqClient
from app.state import ConversationState

BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "openai/gpt-oss-20b"

TXN_CUSTOMER = {
    "t_ok": "c_ava",
    "t_big": "c_ava",
    "t_old": "c_ava",
    "t_pending": "c_ava",
    "t_refunded": "c_ava",
    "t_bob1": "c_bob",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _envelope(spec: dict) -> dict:
    if "text" in spec:
        message: dict = {"role": "assistant", "content": spec["text"]}
        finish = "stop"
        calls: list = []
    else:
        raw_calls = [spec["call"]] if "call" in spec else spec["calls"]
        calls = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for call_id, name, args in raw_calls
        ]
        message = {"role": "assistant", "content": None, "tool_calls": calls}
        finish = "tool_calls"
    return {
        "id": "chatcmpl-eval",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
    }


class ScriptedTransport:
    """Pop scripted envelopes in order; any extra request fails loudly."""

    def __init__(self, specs: list[dict]) -> None:
        self._specs = list(specs)
        self.calls = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls += 1
        assert self._specs, "unexpected extra provider request"
        return 200, json.dumps(_envelope(self._specs.pop(0))).encode("utf-8")


def _check_fragments(container: str, fragments: list[str], kind: str, label: str) -> list[dict]:
    checks = []
    for fragment in fragments:
        found = fragment in container
        ok = found if kind == "contains" else not found
        checks.append(
            {
                "name": f"{label} {'contains' if kind == 'contains' else 'absent'} {fragment!r}",
                "pass": ok,
                "detail": "ok" if ok else f"{'missing' if kind == 'contains' else 'unexpectedly present'}: {fragment!r}",
            }
        )
    return checks


def run_scenario(scenario: dict) -> dict:
    tools.reset_mock_data()
    clock = datetime.fromisoformat(scenario["clock"])
    state = ConversationState(clock=lambda clock=clock: clock)
    audit = AuditLog()
    specs: list[dict] = []
    for turn in scenario["turns"]:
        specs.extend(turn.get("provider", []))
    transport = ScriptedTransport(specs)
    client = GroqClient(
        api_key="groq-test-fake-key-1",
        base_url=BASE_URL,
        model=MODEL,
        timeout_s=5.0,
        request_fn=transport,
    )
    history = None
    replies: list[str] = []
    checks: list[dict] = []
    for index, turn in enumerate(scenario["turns"], start=1):
        result = run_turn(client, state, audit, turn["user"], history)
        history = result.history
        replies.append(result.reply)
        rendered = audit.render()
        expect = turn.get("expect", {})
        checks.extend(_check_fragments(result.reply, expect.get("reply_contains", []), "contains", f"turn{index} reply"))
        checks.extend(_check_fragments(result.reply, expect.get("reply_absent", []), "absent", f"turn{index} reply"))
        checks.extend(_check_fragments(rendered, expect.get("audit_contains", []), "contains", f"turn{index} audit"))
        checks.extend(_check_fragments(rendered, expect.get("audit_absent", []), "absent", f"turn{index} audit"))
    checks.append(
        {
            "name": "provider specs fully consumed",
            "pass": not transport._specs,
            "detail": "ok" if not transport._specs else f"unused specs: {len(transport._specs)}",
        }
    )

    expect = scenario.get("expect", {})
    for txn_id in expect.get("refunded", []):
        record = tools.get_transaction(TXN_CUSTOMER[txn_id], txn_id)
        ok = (
            record is not None
            and str(record.status) == "TransactionStatus.REFUNDED"
            and txn_id in state.refunded_txn_ids
        )
        checks.append({"name": f"refunded {txn_id}", "pass": ok, "detail": "ok" if ok else "not refunded"})
    tickets = len(tools.escalation_log())
    if "tickets" in expect:
        checks.append(
            {
                "name": f"tickets == {expect['tickets']}",
                "pass": tickets == expect["tickets"],
                "detail": "ok" if tickets == expect["tickets"] else f"got {tickets}",
            }
        )
    if "verified" in expect:
        checks.append(
            {
                "name": f"verified == {expect['verified']}",
                "pass": state.verified == expect["verified"],
                "detail": "ok" if state.verified == expect["verified"] else f"got {state.verified}",
            }
        )
    rendered = audit.render()
    checks.extend(_check_fragments(rendered, expect.get("audit_contains", []), "contains", "audit"))
    checks.extend(_check_fragments(rendered, expect.get("audit_absent", []), "absent", "audit"))

    passed = all(check["pass"] for check in checks)
    return {
        "id": scenario["id"],
        "title": scenario["title"],
        "category": scenario.get("category", ""),
        "pass": passed,
        "checks": checks,
        "evidence": {"replies": replies, "audit": rendered},
    }


def _redact(text: str) -> str:
    return _EMAIL_RE.sub("[email withheld]", text)


def evaluate_all(write: bool = False) -> dict:
    root = Path(__file__).resolve().parent.parent
    with open(root / "scenarios" / "conversations.json", encoding="utf-8") as handle:
        scenarios = json.load(handle)["scenarios"]
    results = [run_scenario(scenario) for scenario in scenarios]
    report = {
        "scenarios": results,
        "passed": sum(1 for item in results if item["pass"]),
        "failed": sum(1 for item in results if not item["pass"]),
    }
    if write:
        out_dir = root / "eval"
        out_dir.mkdir(exist_ok=True)
        with open(out_dir / "results.json", "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        lines = ["# Scenario transcripts (offline evaluation)", ""]
        for item, scenario in zip(results, scenarios):
            lines.append(f"## {item['id']}: {item['title']}")
            lines.append(f"Verdict: {'PASS' if item['pass'] else 'FAIL'}")
            lines.append("")
            for turn, reply in zip(scenario["turns"], item["evidence"]["replies"]):
                lines.append(f"Customer: {_redact(turn['user'])}")
                lines.append(f"Agent: {_redact(reply)}")
                lines.append("")
            lines.append("Audit trace:")
            lines.append("```")
            lines.append(item["evidence"]["audit"])
            lines.append("```")
            lines.append("")
        transcripts = root / "transcripts"
        transcripts.mkdir(exist_ok=True)
        with open(transcripts / "conversations.md", "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    tools.reset_mock_data()
    return report


if __name__ == "__main__":
    summary = evaluate_all(write=True)
    print(f"passed={summary['passed']} failed={summary['failed']}")
    raise SystemExit(0 if summary["failed"] == 0 else 1)
