"""Offline transport tests for app/groq.py (no network access).

Every test injects a stub request_fn; nothing here performs real HTTP.
Fake credentials are obviously fake and only assert header mechanics.
"""

import json
import socket
import urllib.error

import pytest

from app.config import load_settings
from app.groq import (
    GroqClient,
    GroqError,
    GroqHTTPError,
    GroqMalformedError,
    GroqTimeoutError,
    build_tool_result_message,
)

FAKE_KEY = "groq-test-fake-key-12345678"
SECRET_KEY = "groq-test-secret-key-9999"
BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "openai/gpt-oss-20b"

MESSAGES = [{"role": "user", "content": "Help with my refund."}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_customer",
            "description": "Look up a customer by email.",
            "parameters": {
                "type": "object",
                "properties": {"email": {"type": "string"}},
                "required": ["email"],
            },
        },
    }
]


class Recorder:
    """Stub transport capturing the outgoing request."""

    def __init__(self, payload=None, status=200):
        self.calls = []
        self.status = status
        self.payload = payload if payload is not None else {"choices": []}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        raw = self.payload
        if not isinstance(raw, (bytes, bytearray)):
            raw = json.dumps(raw).encode("utf-8")
        return self.status, bytes(raw)


def _client(recorder=None, key=FAKE_KEY, base_url=BASE_URL, timeout=30.0):
    return GroqClient(
        api_key=key,
        base_url=base_url,
        model=MODEL,
        timeout_s=timeout,
        request_fn=recorder or Recorder(),
    )


def _completion_payload(message):
    return {
        "id": "chatcmpl-test-1",
        "object": "chat.completion",
        "created": 1730241104,
        "model": MODEL,
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }


def test_request_url_method_auth_and_body():
    recorder = Recorder(_completion_payload({"role": "assistant", "content": "Hi."}))
    client = _client(recorder)
    client.chat(MESSAGES, TOOLS)
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert call["headers"]["Content-Type"] == "application/json"
    body = json.loads(call["body"].decode("utf-8"))
    assert body["model"] == MODEL
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False
    assert "n" not in body


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.groq.com/openai/v1",
        "https://api.groq.com/openai/v1/",
        "https://api.groq.com/openai/v1///",
    ],
)
def test_url_joining_normalizes_trailing_slashes(base_url):
    recorder = Recorder(_completion_payload({"role": "assistant", "content": "Hi."}))
    _client(recorder, base_url=base_url).chat(MESSAGES)
    assert recorder.calls[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"


def test_tools_omitted_when_none():
    recorder = Recorder(_completion_payload({"role": "assistant", "content": "Hi."}))
    _client(recorder).chat(MESSAGES)
    body = json.loads(recorder.calls[0]["body"].decode("utf-8"))
    assert "tools" not in body


def test_messages_pass_through_without_name_fields():
    recorder = Recorder(_completion_payload({"role": "assistant", "content": "Hi."}))
    sent = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]
    _client(recorder).chat(sent, TOOLS)
    body = json.loads(recorder.calls[0]["body"].decode("utf-8"))
    assert all("name" not in message for message in body["messages"])


def test_parses_text_only_response():
    recorder = Recorder(_completion_payload({"role": "assistant", "content": "Hello."}))
    result = _client(recorder).chat(MESSAGES)
    assert result.id == "chatcmpl-test-1"
    assert result.message.content == "Hello."
    assert result.message.tool_calls == ()


def test_parses_single_tool_call():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_customer",
                    "arguments": '{"email": "ava@example.com"}',
                },
            }
        ],
    }
    result = _client(Recorder(_completion_payload(message))).chat(MESSAGES, TOOLS)
    assert len(result.message.tool_calls) == 1
    call = result.message.tool_calls[0]
    assert (call.call_id, call.name) == ("call_abc123", "get_customer")
    assert call.arguments == {"email": "ava@example.com"}


def test_preserves_multiple_tool_calls_in_order():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": "get_customer", "arguments": "{}"}}
            for i in range(3)
        ],
    }
    result = _client(Recorder(_completion_payload(message))).chat(MESSAGES, TOOLS)
    assert [call.call_id for call in result.message.tool_calls] == ["call_0", "call_1", "call_2"]


def test_build_tool_result_message_omits_name():
    message = build_tool_result_message("call_abc123", '{"id": "c_ava"}')
    assert message == {"role": "tool", "tool_call_id": "call_abc123", "content": '{"id": "c_ava"}'}
    assert "name" not in message


@pytest.mark.parametrize("bad_arguments", ["not-json", "[1, 2]", "42", "null"])
def test_malformed_tool_arguments_rejected(bad_arguments):
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_customer", "arguments": bad_arguments}}
        ],
    }
    with pytest.raises(GroqMalformedError):
        _client(Recorder(_completion_payload(message))).chat(MESSAGES, TOOLS)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"index": 0}]},
        {"choices": [{"index": 0, "message": {"role": "assistant", "content": 42}}]},
        {"choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": {}}}]},
        {"choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [{"id": "", "function": {"name": "x", "arguments": "{}"}}]}}]},
    ],
)
def test_malformed_envelopes_rejected(payload):
    with pytest.raises(GroqMalformedError):
        _client(Recorder(payload)).chat(MESSAGES)


def test_invalid_json_body_rejected():
    with pytest.raises(GroqMalformedError):
        _client(Recorder(b"not-json{{{")).chat(MESSAGES)


def test_urllib_http_error_preserves_status_without_details():
    def raising_http_error(method, url, headers, body, timeout):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    with pytest.raises(GroqHTTPError) as excinfo:
        _client(raising_http_error).chat(MESSAGES)
    assert excinfo.value.status == 429
    assert "429" in str(excinfo.value)
    assert FAKE_KEY not in str(excinfo.value)


def test_non_2xx_status_from_transport_preserves_status():
    recorder = Recorder(status=500, payload=b"secret-server-detail")
    with pytest.raises(GroqHTTPError) as excinfo:
        _client(recorder).chat(MESSAGES)
    assert excinfo.value.status == 500
    assert "secret-server-detail" not in str(excinfo.value)


@pytest.mark.parametrize("error", [socket.timeout("timed out"), TimeoutError()])
def test_timeouts_map_to_timeout_error(error):
    def raising(method, url, headers, body, timeout):
        raise error

    with pytest.raises(GroqTimeoutError):
        _client(raising, timeout=7.5).chat(MESSAGES)


def test_url_error_without_status_is_generic_and_safe():
    def raising(method, url, headers, body, timeout):
        raise urllib.error.URLError("connection refused by secret-host")

    with pytest.raises(GroqError) as excinfo:
        _client(raising).chat(MESSAGES)
    assert not isinstance(excinfo.value, (GroqHTTPError, GroqTimeoutError))
    assert "secret-host" not in str(excinfo.value)
    assert SECRET_KEY not in str(excinfo.value)


def test_timeout_reaches_transport():
    recorder = Recorder(_completion_payload({"role": "assistant", "content": "Hi."}))
    _client(recorder, timeout=12.5).chat(MESSAGES)
    assert recorder.calls[0]["timeout"] == 12.5


def test_key_never_in_repr_or_errors():
    client = _client(Recorder(b"bad"), key=SECRET_KEY)
    assert SECRET_KEY not in repr(client)
    with pytest.raises(GroqError) as excinfo:
        client.chat(MESSAGES)
    assert SECRET_KEY not in str(excinfo.value)


def test_from_settings_uses_config_and_rejects_placeholder():
    settings = load_settings(
        env={
            "GROQ_API_KEY": FAKE_KEY,
            "GROQ_MODEL": MODEL,
            "GROQ_BASE_URL": BASE_URL,
            "GROQ_TIMEOUT_S": "17",
        },
        load_dotenv=False,
    )
    client = GroqClient.from_settings(settings, request_fn=Recorder())
    assert (client.model, client.base_url, client.timeout_s) == (MODEL, BASE_URL, 17.0)
    bad = load_settings(env={"GROQ_API_KEY": "your-key-here"}, load_dotenv=False)
    with pytest.raises(RuntimeError):
        GroqClient.from_settings(bad, request_fn=Recorder())
