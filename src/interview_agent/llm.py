"""Step 4 — the only door to an LLM.

Every call goes through `LLMClient.complete()`, which:
  1. runs the send guard (`assert_no_pii`) on the system and user text and
     REFUSES to send if an email/phone pattern is present;
  2. calls the provider (`_call`);
  3. logs metadata only — purpose, prompt id, model, token counts, latency,
     outcome. Never prompt or response text.

Providers:
  GroqClient  - primary (DECISIONS.md). Retries on 429 / 5xx / timeouts are done
                by the official SDK (exponential back-off, honours retry-after).
  FakeLLM     - deterministic stand-in for tests; records every request it gets.
  Anthropic   - optional second provider, not wired yet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from interview_agent.config import ConfigError, Settings
from interview_agent.redact import assert_no_pii

log = logging.getLogger("interview_agent.llm")


class LLMCallError(RuntimeError):
    """Provider call failed. Message carries no prompt/response text."""


class LLMOutputError(ValueError):
    """Provider answered, but the output is unusable (e.g. not JSON)."""


@dataclass(frozen=True)
class LLMRequest:
    purpose: str  # e.g. "extract_candidate" — appears in logs
    prompt_id: str  # e.g. "extract_candidate.v1" — recorded with every output
    system: str = field(repr=False)
    user: str = field(repr=False)
    json_mode: bool = True
    temperature: float = 0.0
    max_tokens: int = 4096


@dataclass(frozen=True)
class LLMResponse:
    text: str = field(repr=False)
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0


class LLMClient(ABC):
    provider: str = "base"

    def __init__(self, model: str):
        if not model:
            raise ConfigError("no model configured (set LLM_MODEL)")
        self.model = model

    def complete(self, request: LLMRequest) -> LLMResponse:
        assert_no_pii(request.system)  # fail closed: nothing with contact details leaves
        assert_no_pii(request.user)
        start = time.monotonic()
        try:
            response = self._call(request)
        except Exception as e:
            log.warning(
                "llm_call_failed provider=%s model=%s purpose=%s prompt_id=%s error=%s latency_ms=%d",
                self.provider, self.model, request.purpose, request.prompt_id,
                type(e).__name__, int((time.monotonic() - start) * 1000),
            )
            raise
        latency = int((time.monotonic() - start) * 1000)
        response = LLMResponse(
            text=response.text, model=response.model, prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens, latency_ms=latency,
        )
        log.info(
            "llm_call_ok provider=%s model=%s purpose=%s prompt_id=%s prompt_tokens=%s completion_tokens=%s latency_ms=%d",
            self.provider, response.model, request.purpose, request.prompt_id,
            response.prompt_tokens, response.completion_tokens, latency,
        )
        return response

    def complete_json(self, request: LLMRequest) -> Any:
        return parse_json(self.complete(request).text, request.purpose)

    @abstractmethod
    def _call(self, request: LLMRequest) -> LLMResponse: ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"


@dataclass
class CallStats:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def call_json(
    client: LLMClient, request: LLMRequest, stats: Any, list_key: str, attempts: int = 2
) -> tuple[dict | None, str | None]:
    """Call and parse a JSON object that must hold a list under `list_key`.

    Unusable output — cut off, not JSON, or wrong shape — is retried (`attempts`
    total). Returns (data, None) or (None, failure_reason). Provider errors propagate.
    `stats` is anything with llm_calls / prompt_tokens / completion_tokens counters.
    """
    failure = ""
    for _ in range(attempts):
        stats.llm_calls += 1
        try:
            resp = client.complete(request)
        except LLMOutputError as e:
            failure = str(e)
            continue
        stats.prompt_tokens += resp.prompt_tokens or 0
        stats.completion_tokens += resp.completion_tokens or 0
        try:
            data = parse_json(resp.text, request.purpose)
        except LLMOutputError as e:
            failure = str(e)
            continue
        if isinstance(data, dict) and isinstance(data.get(list_key), list):
            return data, None
        failure = f"{request.purpose}: reply has no {list_key!r} list"
    return None, failure


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def parse_json(text: str, purpose: str = "llm") -> Any:
    """Parse a JSON reply, tolerating a ```json fence. Error message never quotes the reply."""
    m = _FENCE_RE.match(text)
    body = m.group(1) if m else text
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise LLMOutputError(f"{purpose}: reply is not valid JSON (line {e.lineno}, col {e.colno})") from None


# --------------------------------------------------------------------------- #
# Groq
# --------------------------------------------------------------------------- #


class GroqClient(LLMClient):
    provider = "groq"

    def __init__(self, api_key: str, model: str, *, max_retries: int = 5, timeout: float = 60.0, sdk_client: Any = None):
        super().__init__(model)
        if os.environ.get("GROQ_LOG", "").lower() == "debug":
            raise ConfigError("GROQ_LOG=debug makes the SDK log request bodies (document text); unset it")
        if sdk_client is None:
            if not api_key:
                raise ConfigError("GROQ_API_KEY is not set (copy .env.example to .env and fill it in)")
            from groq import Groq

            sdk_client = Groq(api_key=api_key, max_retries=max_retries, timeout=timeout)
        self._sdk = sdk_client

    def _call(self, request: LLMRequest) -> LLMResponse:
        import groq

        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        if request.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._sdk.chat.completions.create(**kwargs)
        except groq.APIStatusError as e:
            raise LLMCallError(f"{request.purpose}: Groq returned HTTP {e.status_code} ({type(e).__name__})") from e
        except groq.APIError as e:
            raise LLMCallError(f"{request.purpose}: Groq call failed ({type(e).__name__})") from e

        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMOutputError(f"{request.purpose}: reply cut off at max_tokens={request.max_tokens}")
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=choice.message.content or "",
            model=getattr(resp, "model", None) or self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )


# --------------------------------------------------------------------------- #
# Fake (tests)
# --------------------------------------------------------------------------- #

Responder = Callable[[LLMRequest], str] | Mapping[str, str] | Sequence[str]


class FakeLLM(LLMClient):
    """Deterministic test double. Answers by prompt_id (mapping), in order (sequence),
    or via a callable. Records every request it receives in `.calls`.
    It goes through the same send guard as a real provider."""

    provider = "fake"

    def __init__(self, responder: Responder, model: str = "fake-model"):
        super().__init__(model)
        self._responder = responder
        self._queue = list(responder) if isinstance(responder, (list, tuple)) else None
        self.calls: list[LLMRequest] = []

    def _call(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if callable(self._responder):
            text = self._responder(request)
        elif self._queue is not None:
            if not self._queue:
                raise LLMCallError("FakeLLM: no more queued responses")
            text = self._queue.pop(0)
        else:
            if request.prompt_id not in self._responder:
                raise LLMCallError(f"FakeLLM: no response for prompt_id {request.prompt_id!r}")
            text = self._responder[request.prompt_id]
        return LLMResponse(text=text, model=self.model)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def make_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "groq":
        return GroqClient(api_key=settings.groq_api_key, model=settings.llm_model)
    if settings.llm_provider == "anthropic":
        raise ConfigError("Anthropic is the optional second provider and is not wired yet (see DECISIONS.md)")
    raise ConfigError(f"unknown LLM_PROVIDER {settings.llm_provider!r} (expected 'groq')")
