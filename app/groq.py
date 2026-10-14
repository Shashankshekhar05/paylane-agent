"""Thin Groq transport for Phase 3a.

Sends OpenAI-compatible `POST {base_url}/chat/completions` requests over
the standard library only (no new dependencies) and parses the response
envelope into typed structures. Deliberately narrow:

- No business tools are executed here (`app/tools.py` is never imported).
- No policy decisions are made here (`app/policy.py` is never imported).
- The API key travels only in the `Authorization` header and never appears
  in exceptions, reprs, logs, or status strings.
- HTTP failures, timeouts, and malformed payloads map to typed errors so
  callers (Phase 3c orchestrator) can fail closed.

Official basis: Groq local tool calling over OpenAI-compatible chat
completions (https://console.groq.com/docs/tool-use/overview),
parameters in https://console.groq.com/docs/api-reference.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from app.config import Settings, require_api_key

CHAT_COMPLETIONS_PATH = "chat/completions"
DEFAULT_TOOL_CHOICE = "auto"

# (method, url, headers, body, timeout) -> (http_status, raw_body).
RequestFn = Callable[[str, str, Mapping[str, str], bytes, float], tuple[int, bytes]]

_REDACTED = "***"


class GroqError(Exception):
    """Base transport error. Messages never carry secrets."""


class GroqHTTPError(GroqError):
    """Non-2xx response. Carries the status, never the body or key."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GroqTimeoutError(GroqError):
    """The request exceeded the configured timeout."""


class GroqMalformedError(GroqError):
    """The response was not the expected JSON envelope."""


def _safe_http_message(status: int) -> str:
    """Human-readable HTTP failure without response details."""
    if status == 401:
        return "Groq request unauthorized (HTTP 401). Check GROQ_API_KEY."
    if status == 429:
        return "Groq request rate-limited (HTTP 429). Retry later."
    if 500 <= status < 600:
        return f"Groq server error (HTTP {status}). Retry later."
    return f"Groq request failed (HTTP {status})."


def _join_url(base_url: str, path: str) -> str:
    """Join without producing `//` when the base URL has trailing slashes."""
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _auth_header(api_key: str) -> str:
    return f"Bearer {api_key}"


def _default_request_fn(
    method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    """Standard-library HTTPS transport. Raises typed errors only."""
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # Status is safe to keep; the body may hold sensitive details.
        raise GroqHTTPError(exc.code, _safe_http_message(exc.code)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise GroqTimeoutError(
            f"Groq request timed out after {timeout:g}s."
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise GroqTimeoutError(
                f"Groq request timed out after {timeout:g}s."
            ) from exc
        # Class name only: reason text/URLs are never echoed.
        raise GroqError(
            f"Groq request failed ({type(exc).__name__})."
        ) from exc


@dataclass(frozen=True)
class ToolCallProposal:
    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(frozen=True)
class AssistantMessage:
    role: str
    content: Optional[str]
    tool_calls: tuple[ToolCallProposal, ...]


@dataclass(frozen=True)
class ChatCompletion:
    id: str
    model: str
    message: AssistantMessage


def build_tool_result_message(call_id: str, content: str) -> dict[str, str]:
    """Build a continuation message for one executed tool call.

    The `name` field is deliberately omitted: Groq rejects `messages[].name`
    (see OpenAI-compatibility docs), and `tool_call_id` is sufficient for
    correlation.
    """
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _parse_tool_call(item: Any) -> ToolCallProposal:
    if not isinstance(item, dict):
        raise GroqMalformedError("Groq tool call was not a JSON object.")
    call_id = item.get("id")
    function = item.get("function")
    if not isinstance(call_id, str) or not call_id:
        raise GroqMalformedError("Groq tool call is missing its id.")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise GroqMalformedError("Groq tool call is missing its function name.")
    raw_arguments = function.get("arguments", "{}")
    if not isinstance(raw_arguments, str):
        raise GroqMalformedError("Groq tool arguments were not a JSON string.")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise GroqMalformedError(
            "Groq tool arguments were not valid JSON."
        ) from exc
    if not isinstance(arguments, dict):
        raise GroqMalformedError("Groq tool arguments were not a JSON object.")
    return ToolCallProposal(
        call_id=call_id,
        name=function["name"],
        arguments=arguments,
        raw_arguments=raw_arguments,
    )


def _parse_chat_completion(data: Any) -> ChatCompletion:
    if not isinstance(data, dict):
        raise GroqMalformedError("Groq response was not a JSON object.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GroqMalformedError("Groq response contained no choices.")
    first = choices[0]
    if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
        raise GroqMalformedError("Groq response choice had no message.")
    message = first["message"]
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise GroqMalformedError("Groq message content was not text.")
    raw_calls = message.get("tool_calls", [])
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        raise GroqMalformedError("Groq tool_calls was not a list.")
    tool_calls = tuple(_parse_tool_call(item) for item in raw_calls)
    response_id = data.get("id")
    model = data.get("model")
    return ChatCompletion(
        id=response_id if isinstance(response_id, str) else "",
        model=model if isinstance(model, str) else "",
        message=AssistantMessage(
            role=message.get("role", "assistant"),
            content=content,
            tool_calls=tool_calls,
        ),
    )


@dataclass(frozen=True)
class GroqClient:
    """Configured Groq chat-completions client (transport only)."""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout_s: float = 30.0
    request_fn: RequestFn = field(default=_default_request_fn, repr=False, compare=False)

    @classmethod
    def from_settings(
        cls, settings: Settings, request_fn: Optional[RequestFn] = None
    ) -> "GroqClient":
        """Build from validated settings; fails closed without a usable key."""
        return cls(
            api_key=require_api_key(settings),
            base_url=settings.base_url,
            model=settings.model,
            timeout_s=settings.timeout_s,
            request_fn=request_fn or _default_request_fn,
        )

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        tool_choice: str = DEFAULT_TOOL_CHOICE,
    ) -> ChatCompletion:
        """POST one chat-completions request and parse the envelope."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
        }
        if tools is not None:
            payload["tools"] = [dict(tool) for tool in tools]
        url = _join_url(self.base_url, CHAT_COMPLETIONS_PATH)
        headers = {
            "Content-Type": "application/json",
            "Authorization": _auth_header(self.api_key),
            # Stdlib's default `Python-urllib/3.x` UA is blocked by the
            # Cloudflare shield in front of api.groq.com (error 1010) on
            # some networks, surfacing as HTTP 403 before Groq auth runs.
            "User-Agent": "paylane-agent/0.1",
        }
        body = json.dumps(payload).encode("utf-8")
        try:
            status, raw = self.request_fn("POST", url, headers, body, self.timeout_s)
        except GroqError:
            raise
        except urllib.error.HTTPError as exc:
            # Status is safe to keep; the body may hold sensitive details.
            raise GroqHTTPError(exc.code, _safe_http_message(exc.code)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise GroqTimeoutError(
                f"Groq request timed out after {self.timeout_s:g}s."
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise GroqTimeoutError(
                    f"Groq request timed out after {self.timeout_s:g}s."
                ) from exc
            # Class name only: reason text/URLs are never echoed.
            raise GroqError(f"Groq request failed ({type(exc).__name__}).") from exc
        if isinstance(status, bool) or not isinstance(status, int):
            raise GroqError("Groq transport returned a malformed status.")
        if not 200 <= status < 300:
            # Custom transports report status directly; stdlib raises first.
            raise GroqHTTPError(status, _safe_http_message(status))
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)):
            raise GroqMalformedError("Groq transport returned a malformed body.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GroqMalformedError(
                "Groq response was not valid JSON."
            ) from exc
        return _parse_chat_completion(data)
