"""CLI entry point: config check, one-shot turns, and the live demo.

`--check-config` and single `--message` turns never persist state; use
`--live-demo` (same as `python -m app.live_demo`) for the shared-state
multi-turn live Groq run. Every message avoids printing secrets.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.agent import run_turn
from app.audit import AuditLog
from app.config import format_config_status, load_settings, require_api_key
from app.groq import GroqClient
from app.state import ConversationState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paylane-agent",
        description="Paylane AI Support Agent (Phase 1 skeleton: config check only).",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without calling any external API.",
    )
    parser.add_argument(
        "--message",
        default=None,
        help="Handle one customer message (one turn, no persistence).",
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="Use a deterministic stub provider instead of the network.",
    )
    parser.add_argument(
        "--live-demo",
        action="store_true",
        help="Run the multi-turn live Groq demo (see app/live_demo.py).",
    )
    return parser


def _stub_request_fn(method, url, headers, body, timeout):
    """Deterministic offline stand-in (never touches the network)."""
    payload = {
        "id": "stub-1",
        "object": "chat.completion",
        "created": 0,
        "model": "stub",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "[deterministic-test-mode] Live provider disabled; no action taken.",
                },
            }
        ],
    }
    return 200, json.dumps(payload).encode("utf-8")


def _run_message(text, no_live):
    settings = load_settings()
    try:
        if no_live:
            client = GroqClient(
                api_key="stub",
                base_url=settings.base_url,
                model=settings.model,
                timeout_s=settings.timeout_s,
                request_fn=_stub_request_fn,
            )
        else:
            client = GroqClient.from_settings(settings)
    except (RuntimeError, ValueError) as exc:
        print(f"config INVALID: {exc}", file=sys.stderr)
        return 2
    log = AuditLog()
    try:
        result = run_turn(client, ConversationState(), log, text, None)
    except Exception as exc:
        print(f"agent error: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(result.reply)
    print("--- audit ---")
    print(log.render())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_config:
        settings = load_settings()
        try:
            require_api_key(settings)
        except RuntimeError as exc:
            print(f"config INVALID: {exc}", file=sys.stderr)
            return 2
        print(f"config OK: {format_config_status(settings)}")
        return 0

    if args.live_demo:
        from app.live_demo import main as live_demo_main

        return live_demo_main()

    if args.message is not None:
        return _run_message(args.message, args.no_live)

    print("Paylane agent skeleton (Phase 1): no conversation logic yet.")
    print("Use --help or --check-config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
