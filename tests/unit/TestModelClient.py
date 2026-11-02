from __future__ import annotations

import json

import httpx
import pytest

from Driftwatch.Services.ModelClient import (
    ModelClientConfig,
    ModelResponseInvalid,
    ModelUnavailable,
    OpenAIModelClient,
    ScriptedModelClient,
)
from Driftwatch.Watch.Differ import Delta
from Driftwatch.Watch.Evidence import Evidence


def delta() -> Delta:
    return Delta(
        surface_ref="api:stripe/op:post_v1_charges",
        change_type="param_renamed",
        severity="breaking",
        op="modified",
        before=None,
        after=None,
        detail="source was renamed",
    )


def evidence() -> list[Evidence]:
    return [Evidence("raw_delta", "delta-1", "source was renamed")]


def config(**overrides: object) -> ModelClientConfig:
    values: dict[str, object] = {"api_key": "test-key", "model": "test-model"}
    values.update(overrides)
    return ModelClientConfig(**values)


def test_openai_client_sends_bounded_structured_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"title":"renamed"}'}}]},
        )

    client = OpenAIModelClient(
        config(),
        client=httpx.Client(
            base_url="https://example.test/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    assert client.classify(delta(), evidence()) == {"title": "renamed"}
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    body = json.loads(requests[0].content)
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][1]["content"].find("api:stripe/op:post_v1_charges") >= 0


def test_openai_client_retries_rate_limits() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = OpenAIModelClient(
        config(max_retries=2),
        client=httpx.Client(
            base_url="https://example.test/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    assert client.classify(delta(), evidence()) == {}
    assert calls == 3


def test_openai_client_retries_provider_unavailable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = OpenAIModelClient(
        config(max_retries=2),
        client=httpx.Client(
            base_url="https://example.test/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(ModelUnavailable):
        client.classify(delta(), evidence())
    assert calls == 3


def test_openai_client_rejects_oversized_request() -> None:
    client = OpenAIModelClient(
        config(max_input_bytes=1),
        client=httpx.Client(base_url="https://example.test/v1"),
    )

    with pytest.raises(ModelResponseInvalid, match="input limit"):
        client.classify(delta(), evidence())


def test_openai_client_maps_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = OpenAIModelClient(
        config(max_retries=0),
        client=httpx.Client(
            base_url="https://example.test/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(ModelUnavailable):
        client.classify(delta(), evidence())


def test_scripted_client_requires_explicit_behavior() -> None:
    with pytest.raises(ValueError, match="requires"):
        ScriptedModelClient()
