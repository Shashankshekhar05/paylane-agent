"""CLI smoke tests (no external API calls)."""

import os
import subprocess
import sys

REAL_KEY = "groq-test-key-12345678"


def _run_cli(*args, env_override=None):
    env = dict(os.environ)
    # Hermetic default: an explicit placeholder takes precedence over any
    # real local .env value (environment wins over .env in load_settings),
    # so fail-closed assertions are deterministic on any machine.
    env["GROQ_API_KEY"] = "your-key-here"
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, "-m", "app.runner", *args],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        timeout=30,
    )


def test_help_exits_zero():
    proc = _run_cli("--help")
    assert proc.returncode == 0
    assert "check-config" in proc.stdout.lower()


def test_check_config_fails_closed_without_usable_key():
    proc = _run_cli("--check-config")
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "GROQ_API_KEY" in combined


def test_check_config_ok_with_key_and_never_prints_key():
    proc = _run_cli("--check-config", env_override={"GROQ_API_KEY": REAL_KEY})
    assert proc.returncode == 0
    combined = proc.stdout + proc.stderr
    assert "config OK" in combined
    assert REAL_KEY not in combined
