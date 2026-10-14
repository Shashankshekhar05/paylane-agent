"""Config validation tests (technical controls only)."""

import pytest

from app.config import (
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    format_config_status,
    is_usable_api_key,
    load_settings,
    require_api_key,
)

REAL_KEY = "groq-test-key-12345678"


def test_missing_key_raises_setup_error_without_leaking():
    settings = load_settings(env={}, load_dotenv=False)
    with pytest.raises(RuntimeError) as excinfo:
        require_api_key(settings)
    message = str(excinfo.value)
    assert API_KEY_ENV_VAR in message
    assert settings.api_key is None or settings.api_key not in message


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "your-key-here", "YOUR-KEY-HERE", "placeholder", "changeme", "xxx", "test", "my-example-key"],
)
def test_placeholder_and_empty_keys_rejected(bad):
    assert not is_usable_api_key(bad)
    settings = load_settings(env={API_KEY_ENV_VAR: bad}, load_dotenv=False)
    assert not settings.has_usable_key
    with pytest.raises(RuntimeError):
        require_api_key(settings)


def test_usable_key_accepted_and_never_rendered():
    settings = load_settings(env={API_KEY_ENV_VAR: REAL_KEY}, load_dotenv=False)
    assert settings.has_usable_key
    assert require_api_key(settings) == REAL_KEY
    assert REAL_KEY not in format_config_status(settings)
    assert REAL_KEY not in repr(settings)


def test_status_and_error_hide_key_value(monkeypatch, tmp_path):
    monkeypatch.setenv(API_KEY_ENV_VAR, REAL_KEY)
    settings = load_settings(load_dotenv=False)
    assert REAL_KEY not in format_config_status(settings)
    # A dotenv file must not override an existing environment value.
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(f"{API_KEY_ENV_VAR}=dotenv-key-12345678\n", encoding="utf-8")
    reloaded = load_settings(dotenv_path=dotenv_file)
    assert reloaded.api_key == REAL_KEY


def test_base_url_defaults_to_groq_https():
    settings = load_settings(env={}, load_dotenv=False)
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.base_url.startswith("https://")


@pytest.mark.parametrize(
    "bad_url",
    ["http://api.groq.com/openai/v1", "ftp://example.com/v1", "not-a-url", "http://"],
)
def test_non_https_base_url_rejected_without_echoing(bad_url):
    with pytest.raises(ValueError) as excinfo:
        load_settings(env={BASE_URL_ENV_VAR: bad_url}, load_dotenv=False)
    message = str(excinfo.value)
    assert BASE_URL_ENV_VAR in message
    assert bad_url not in message


def test_status_reports_host_only():
    settings = load_settings(env={API_KEY_ENV_VAR: REAL_KEY}, load_dotenv=False)
    status = format_config_status(settings)
    assert "api.groq.com" in status
    assert "https://api.groq.com/openai/v1" not in status
    assert REAL_KEY not in status


@pytest.mark.parametrize("bad_timeout", ["nan", "inf", "-inf", "0", "-1", "not-a-number"])
def test_invalid_timeout_falls_back_to_safe_default(bad_timeout):
    settings = load_settings(env={"GROQ_TIMEOUT_S": bad_timeout}, load_dotenv=False)
    assert settings.timeout_s == DEFAULT_TIMEOUT_S


def test_valid_timeout_is_preserved():
    settings = load_settings(env={"GROQ_TIMEOUT_S": "12.5"}, load_dotenv=False)
    assert settings.timeout_s == 12.5


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://user:password@example.com/openai/v1",
        "https://api.groq.com/openai/v1?token=secret",
        "https://api.groq.com/openai/v1#fragment",
        "https://api.groq.com:invalid/openai/v1",
    ],
)
def test_base_url_rejects_credentials_query_fragment_and_bad_port(bad_url):
    with pytest.raises(ValueError) as excinfo:
        load_settings(env={BASE_URL_ENV_VAR: bad_url}, load_dotenv=False)
    assert BASE_URL_ENV_VAR in str(excinfo.value)
    assert bad_url not in str(excinfo.value)


def test_default_model_matches_documented_model():
    settings = load_settings(env={}, load_dotenv=False)
    assert settings.model == DEFAULT_MODEL == "openai/gpt-oss-20b"
