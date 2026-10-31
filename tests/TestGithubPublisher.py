from __future__ import annotations

import httpx

from Driftwatch.Services.GithubPublisher import GithubPublisher, PublishRequest


def test_issue_publishing_is_idempotent() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "html_url": "https://github.com/acme/app/issues/7",
                        "body": "<!-- driftwatch:abc123:issue -->",
                    }
                ],
            )
        raise AssertionError("an existing issue should not be recreated")

    publisher = GithubPublisher(
        "test-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_base="https://github.test",
    )
    request = PublishRequest(
        repository="acme/app",
        change_fingerprint="abc123",
        kind="issue",
        title="API change",
        body="The API changed.",
    )

    result = publisher.publish(request)

    assert result.number == 7
    assert result.created is False
    assert calls == [("GET", "/repos/acme/app/issues")]


def test_draft_pull_request_publishes_one_file() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if request.method == "GET" and "/contents/" in request.url.path:
            return httpx.Response(200, json={"sha": "file-sha"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if request.method == "PUT":
            return httpx.Response(200, json={})
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"number": 8, "html_url": "https://github.com/acme/app/pull/8"},
            )
        raise AssertionError(request)

    publisher = GithubPublisher(
        "test-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_base="https://github.test",
    )
    result = publisher.publish(
        PublishRequest(
            repository="acme/app",
            change_fingerprint="deadbeef",
            kind="pull_request",
            title="Apply API migration",
            body="Verified patch.",
            file_path="src/payments.py",
            patched_content=b"new content",
        )
    )

    assert result.number == 8
    assert result.created is True
    assert ("POST", "/repos/acme/app/pulls") in calls
