"""Configuration validation for the Paylane support agent.

Technical controls only. No refund eligibility, pending-transaction,
partial-refund, or other business-policy decisions live here.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

API_KEY_ENV_VAR = "GROQ_API_KEY"
MODEL_ENV_VAR = "GROQ_MODEL"
BASE_URL_ENV_VAR = "GROQ_BASE_URL"
TIMEOUT_ENV_VAR = "GROQ_TIMEOUT_S"

DEFAULT_MODEL = "openai/gpt-oss-20b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_TIMEOUT_S = 30.0

# Obvious placeholder markers. Matching is case-insensitive and
# substring-based so values like "YOUR-KEY-HERE" are also rejected.
_PLACEHOLDER_SUBSTRINGS = (
    "your-key-here",
    "placeholder",
    "changeme",
    "example",
)

_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "xxx",
        "test",
        "none",
        "null",
        "nil",
        "changeme",
        "placeholder",
        "your-key-here",
    }
)

_MIN_KEY_LENGTH = 8


def is_usable_api_key(value: Optional[str]) -> bool:
    """Return True only for a non-empty, non-placeholder key.

    This is a technical guard so skeleton checks do not treat the
    `.env.example` placeholder as a real credential. It is not a
    business-policy decision.
    """
    if value is None:
        return False
    candidate = value.strip().strip("\"'")
    if len(candidate) < _MIN_KEY_LENGTH:
        return False
    lowered = candidate.lower()
    if lowered in _PLACEHOLDER_VALUES:
        return False
    return not any(marker in lowered for marker in _PLACEHOLDER_SUBSTRINGS)


def _normalize_base_url(value: Optional[str]) -> str:
    """Return a validated HTTPS base URL, or raise without echoing input.

    An empty value selects the default. A non-HTTPS or host-less value is
    a configuration error (fail closed); the error names the variable but
    never includes the supplied value.
    """
    candidate = (value or "").strip().strip("\"'") or DEFAULT_BASE_URL
    try:
        parsed = urlparse(candidate)
        # Accessing .port also validates malformed port values.
        _ = parsed.port
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            f"{BASE_URL_ENV_VAR} must be an HTTPS base URL with a valid host, "
            "no embedded credentials, and no query or fragment. "
            "Set a valid https:// base URL or leave it unset for the default."
        )
    return candidate.rstrip("/")


def _base_url_host(base_url: str) -> str:
    """Return only the host portion, for safe status reporting."""
    return urlparse(base_url).hostname or "unknown-host"


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE dotenv file without logging values."""
    parsed: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return parsed
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        val = raw_value.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            parsed[key] = val
    return parsed


def _default_dotenv_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class Settings:
    """Validated configuration. The raw key is never rendered."""

    api_key: Optional[str] = field(default=None, repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = DEFAULT_TIMEOUT_S

    @property
    def has_usable_key(self) -> bool:
        return is_usable_api_key(self.api_key)

    @property
    def key_status(self) -> str:
        return "set" if self.has_usable_key else "missing-or-placeholder"


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    load_dotenv: bool = True,
    dotenv_path: Optional[Path] = None,
) -> Settings:
    """Load settings from the environment (plus an optional `.env` file).

    Existing environment variables take precedence over `.env` values.
    No secret values are printed or logged by this function.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    merged = dict(source)

    if load_dotenv:
        path = dotenv_path if dotenv_path is not None else _default_dotenv_path()
        for key, val in _parse_dotenv_file(path).items():
            merged.setdefault(key, val)

    raw_key = merged.get(API_KEY_ENV_VAR)
    model = (merged.get(MODEL_ENV_VAR) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = _normalize_base_url(merged.get(BASE_URL_ENV_VAR))
    try:
        timeout_s = float(merged.get(TIMEOUT_ENV_VAR, DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    # urllib requires a meaningful timeout; NaN, infinity, zero, and negative
    # values are invalid or surprising and must not reach the transport.
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        timeout_s = DEFAULT_TIMEOUT_S

    return Settings(api_key=raw_key, model=model, base_url=base_url, timeout_s=timeout_s)


def require_api_key(settings: Settings) -> str:
    """Return the API key or raise a setup error without revealing it."""
    if settings.has_usable_key and settings.api_key is not None:
        return settings.api_key
    raise RuntimeError(
        f"{API_KEY_ENV_VAR} is missing or is still a placeholder. "
        "Set a real key in the environment (or a local untracked .env file) "
        "and retry. See .env.example for the expected variable name. "
        "Do not commit real keys."
    )


def format_config_status(settings: Settings) -> str:
    """Build a safe human-readable status line.

    Reports only the base-URL host (never the key or the complete URL).
    """
    return (
        f"{API_KEY_ENV_VAR}: {settings.key_status}; "
        f"model={settings.model}; host={_base_url_host(settings.base_url)}; "
        f"timeout_s={settings.timeout_s}"
    )
