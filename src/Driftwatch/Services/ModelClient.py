"""Structured model clients for the evidence-grounded classifier."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from Driftwatch.Watch.Classifier import ModelClient
from Driftwatch.Watch.Differ import Delta
from Driftwatch.Watch.Evidence import Evidence


class ModelClientError(RuntimeError):
    """Base error for model transport and response failures."""


class ModelRateLimited(ModelClientError):
    """The model provider rejected the request due to rate limiting."""


class ModelUnavailable(ModelClientError):
    """The model provider could not process the request."""


class ModelResponseInvalid(ModelClientError):
    """The provider returned an unsupported response shape."""


@dataclass(frozen=True, slots=True)
class ModelClientConfig:
    """Bounded configuration for an OpenAI-compatible model endpoint."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 30.0
    max_input_bytes: int = 128_000
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("model API key must not be empty")
        if not self.model.strip():
            raise ValueError("model name must not be empty")
        if not self.base_url.startswith("https://"):
            raise ValueError("model base URL must use HTTPS")
        if self.timeout_seconds <= 0:
            raise ValueError("model timeout must be positive")
        if self.max_input_bytes <= 0:
            raise ValueError("model input limit must be positive")
        if self.max_retries < 0:
            raise ValueError("model retry count must not be negative")


class OpenAIModelClient(ModelClient):
    """Call an OpenAI-compatible chat-completions endpoint for JSON output."""

    def __init__(
        self,
        config: ModelClientConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def classify(self, delta: Delta, evidence: list[Evidence]) -> dict[str, Any]:
        """Return untrusted JSON for the existing grounding validator."""
        body = self._request_body(delta, evidence)
        encoded = json.dumps(body, separators=(",", ":")).encode()
        if len(encoded) > self._config.max_input_bytes:
            raise ModelResponseInvalid("model request exceeds configured input limit")

        last_error: ModelClientError | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.post(
                    "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                if response.status_code == 429:
                    raise ModelRateLimited("model provider rate limit")
                if response.status_code >= 500:
                    raise ModelUnavailable("model provider unavailable")
                response.raise_for_status()
                return _extract_json(response.json())
            except ModelRateLimited as error:
                last_error = error
            except ModelUnavailable as error:
                last_error = error
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                unavailable = ModelUnavailable("model provider unavailable")
                unavailable.__cause__ = error
                last_error = unavailable
            except (httpx.HTTPError, ValueError, ModelResponseInvalid) as error:
                raise ModelResponseInvalid("invalid model response") from error
            if attempt < self._config.max_retries:
                continue
        raise last_error or ModelUnavailable("model provider unavailable")

    def close(self) -> None:
        """Close the HTTP client when this instance owns it."""
        if self._owns_client:
            self._client.close()

    def _request_body(self, delta: Delta, evidence: list[Evidence]) -> dict[str, Any]:
        evidence_payload = [
            {"kind": item.kind, "ref": item.ref, "quote": item.quote} for item in evidence
        ]
        return {
            "model": self._config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only a JSON object matching the classifier schema. "
                        "The detected change type and severity are authoritative. "
                        "Cite only the supplied evidence; never invent quotes."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "delta": {
                                "surface_ref": delta.surface_ref,
                                "change_type": delta.change_type,
                                "severity": delta.severity,
                                "detail": delta.detail,
                                "renamed_to": delta.renamed_to,
                            },
                            "evidence": evidence_payload,
                        },
                        separators=(",", ":"),
                    ),
                },
            ],
        }


class ScriptedModelClient(ModelClient):
    """Deterministic fake model client for unit and fake-GCP integration tests."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        error: ModelClientError | None = None,
    ) -> None:
        if response is None and error is None:
            raise ValueError("scripted model requires a response or error")
        if response is not None and error is not None:
            raise ValueError("scripted model cannot contain both response and error")
        self._response = response
        self._error = error

    def classify(self, delta: Delta, evidence: list[Evidence]) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return dict(self._response or {})


def _extract_json(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ModelResponseInvalid("model response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelResponseInvalid("model response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ModelResponseInvalid("model choice must be an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ModelResponseInvalid("model response has no message")
    content = message.get("content")
    if isinstance(content, dict):
        result = content
    elif isinstance(content, str):
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise ModelResponseInvalid("model content is not valid JSON") from error
    else:
        raise ModelResponseInvalid("model content must be JSON")
    if not isinstance(result, dict):
        raise ModelResponseInvalid("model content must be a JSON object")
    return result
