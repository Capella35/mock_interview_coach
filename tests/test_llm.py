"""Step 4 tests: LLM client, send guard, metadata-only logging, settings. No network."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import groq
import httpx
import pytest

from interview_agent.config import ConfigError, Settings, load_settings, read_env_file
from interview_agent.llm import (
    FakeLLM,
    GroqClient,
    LLMCallError,
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    make_client,
    parse_json,
)
from interview_agent.redact import PIILeakError

SECRET_LINE = "Rebuilt the nightly ETL pipeline at Acme Logistics"  # stands in for document text


def _req(user: str = SECRET_LINE, **kw) -> LLMRequest:
    return LLMRequest(purpose="extract_candidate", prompt_id="extract_candidate.v1",
                      system="Return JSON.", user=user, **kw)


# --------------------------------------------------------------------------- #
# Send guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["user", "system"])
def test_guard_blocks_contact_details_before_provider_is_called(field):
    fake = FakeLLM(['{"ok": true}'])
    leaky = "Contact alex.rivera@example.com or +44 20 7946 0958"
    req = _req(user=leaky) if field == "user" else LLMRequest("p", "p.v1", system=leaky, user="hi")
    with pytest.raises(PIILeakError):
        fake.complete(req)
    assert fake.calls == []  # never reached the provider


def test_redacted_placeholders_pass_the_guard():
    fake = FakeLLM(['{"ok": true}'])
    fake.complete(_req(user="[NAME] | [EMAIL] | [PHONE]\n" + SECRET_LINE))
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# Logging: metadata only
# --------------------------------------------------------------------------- #


def test_logs_metadata_but_never_text(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeLLM(['{"facts": ["' + "RESPONSE-ONLY-TEXT" + '"]}'])
    fake.complete(_req())
    assert "llm_call_ok" in caplog.text and "purpose=extract_candidate" in caplog.text
    assert "prompt_id=extract_candidate.v1" in caplog.text
    assert SECRET_LINE not in caplog.text and "RESPONSE-ONLY-TEXT" not in caplog.text


def test_failures_logged_without_text(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(LLMCallError):
        FakeLLM([]).complete(_req())
    assert "llm_call_failed" in caplog.text and SECRET_LINE not in caplog.text


def test_reprs_hide_text():
    assert SECRET_LINE not in repr(_req())
    assert "secret-response" not in repr(LLMResponse(text="secret-response", model="m"))


# --------------------------------------------------------------------------- #
# FakeLLM
# --------------------------------------------------------------------------- #


def test_fake_by_prompt_id_sequence_and_callable():
    by_id = FakeLLM({"extract_candidate.v1": '{"a": 1}'})
    assert by_id.complete_json(_req()) == {"a": 1}
    with pytest.raises(LLMCallError, match="no response for prompt_id"):
        by_id.complete(LLMRequest("match", "match_gap.v1", system="s", user="u"))

    seq = FakeLLM(['{"n": 1}', '{"n": 2}'])
    assert [seq.complete_json(_req())["n"] for _ in range(2)] == [1, 2]
    with pytest.raises(LLMCallError, match="no more queued"):
        seq.complete(_req())

    echo = FakeLLM(lambda r: '{"len": %d}' % len(r.user))
    assert echo.complete_json(_req(user="abc")) == {"len": 3}
    assert echo.calls[0].user == "abc"


def test_model_required():
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        FakeLLM(["x"], model="")


# --------------------------------------------------------------------------- #
# parse_json
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', '  ```\n{"a": 1}\n```  '])
def test_parse_json_tolerates_fences(text):
    assert parse_json(text) == {"a": 1}


def test_parse_json_error_does_not_quote_reply():
    with pytest.raises(LLMOutputError) as exc:
        parse_json("Sure! Here is " + SECRET_LINE, "extract_candidate")
    assert SECRET_LINE not in str(exc.value) and "extract_candidate" in str(exc.value)


# --------------------------------------------------------------------------- #
# GroqClient (SDK replaced by a stub — no network)
# --------------------------------------------------------------------------- #


class _StubSDK:
    def __init__(self, reply=None, error=None, finish_reason="stop"):
        self.kwargs = None
        self._reply, self._error, self._finish = reply, error, finish_reason
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self._error:
            raise self._error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._reply), finish_reason=self._finish)],
            model="served-model",
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        )


def _groq(stub) -> GroqClient:
    return GroqClient(api_key="", model="configured-model", sdk_client=stub)


def test_groq_request_shape_and_response():
    stub = _StubSDK(reply='{"facts": []}')
    resp = _groq(stub).complete(_req(max_tokens=2000))
    assert stub.kwargs == {
        "model": "configured-model",
        "messages": [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": SECRET_LINE}],
        "temperature": 0.0,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    assert (resp.text, resp.model, resp.prompt_tokens, resp.completion_tokens) == ('{"facts": []}', "served-model", 120, 30)


def test_groq_plain_text_mode_omits_response_format():
    stub = _StubSDK(reply="hello")
    _groq(stub).complete(_req(json_mode=False))
    assert "response_format" not in stub.kwargs


def test_groq_truncated_reply_rejected():
    with pytest.raises(LLMOutputError, match="cut off"):
        _groq(_StubSDK(reply='{"facts": [', finish_reason="length")).complete(_req())


_REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


@pytest.mark.parametrize(
    "error, match",
    [
        (groq.RateLimitError("rate limited", response=httpx.Response(429, request=_REQ), body=None), "HTTP 429"),
        (groq.AuthenticationError("bad key", response=httpx.Response(401, request=_REQ), body=None), "HTTP 401"),
        (groq.APIConnectionError(request=_REQ), "APIConnectionError"),
    ],
)
def test_groq_errors_wrapped_without_text(error, match):
    with pytest.raises(LLMCallError, match=match) as exc:
        _groq(_StubSDK(error=error)).complete(_req())
    assert SECRET_LINE not in str(exc.value)


def test_real_sdk_configured_with_retries_and_timeout():
    client = GroqClient(api_key="test-key-not-real", model="m")  # constructing makes no network call
    assert client._sdk.max_retries == 5
    assert client._sdk.timeout == 60.0
    assert "test-key" not in repr(client)


def test_groq_requires_key():
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        GroqClient(api_key="", model="m")


def test_groq_refuses_sdk_debug_logging(monkeypatch):
    monkeypatch.setenv("GROQ_LOG", "debug")
    with pytest.raises(ConfigError, match="GROQ_LOG"):
        GroqClient(api_key="k", model="m")


# --------------------------------------------------------------------------- #
# Settings + factory
# --------------------------------------------------------------------------- #


def test_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nGROQ_API_KEY="gsk_abc"\nexport LLM_MODEL=some-model\n\nBROKEN LINE\nEMPTY=\n')
    assert read_env_file(p) == {"GROQ_API_KEY": "gsk_abc", "LLM_MODEL": "some-model", "EMPTY": ""}
    assert read_env_file(tmp_path / "missing.env") == {}


def test_environment_overrides_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("LLM_MODEL=from-file\nGROQ_API_KEY=gsk_file\n")
    s = load_settings(p, environ={"LLM_MODEL": "from-env"})
    assert (s.llm_provider, s.llm_model, s.groq_api_key) == ("groq", "from-env", "gsk_file")


def test_settings_repr_masks_keys():
    r = repr(Settings(llm_model="m", groq_api_key="gsk_supersecret"))
    assert "gsk_supersecret" not in r and "groq_api_key=set" in r


@pytest.mark.parametrize(
    "settings, match",
    [
        (Settings(llm_model="m"), "GROQ_API_KEY"),
        (Settings(groq_api_key="k"), "LLM_MODEL"),
        (Settings(llm_provider="anthropic", llm_model="m"), "not wired"),
        (Settings(llm_provider="openai", llm_model="m"), "unknown LLM_PROVIDER"),
    ],
)
def test_make_client_errors(settings, match):
    with pytest.raises(ConfigError, match=match):
        make_client(settings)


def test_make_client_groq():
    client = make_client(Settings(llm_model="m", groq_api_key="test-key-not-real"))
    assert isinstance(client, GroqClient) and client.model == "m"
