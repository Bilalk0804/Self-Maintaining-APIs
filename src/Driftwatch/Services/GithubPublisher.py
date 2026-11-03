"""Publish gated Driftwatch results to GitHub issues and draft pull requests."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

import httpx2

ArtifactKind = Literal["issue", "pull_request"]

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Data required to publish one idempotent GitHub artifact."""

    repository: str
    change_fingerprint: str
    kind: ArtifactKind
    title: str
    body: str
    base_branch: str = "main"
    file_path: str | None = None
    patched_content: bytes | None = None


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    """Reference to an existing or newly created GitHub artifact."""

    kind: ArtifactKind
    repository: str
    number: int
    url: str
    created: bool


def _validate_publish_request(request: PublishRequest) -> None:
    """Validate repository slug, fingerprint, branch, and file path against injection/traversal."""
    if not _REPO_SLUG_RE.fullmatch(request.repository) or ".." in request.repository.split("/"):
        raise ValueError(f"invalid GitHub repository slug: {request.repository!r}")
    if not _FINGERPRINT_RE.fullmatch(request.change_fingerprint):
        raise ValueError(f"invalid change_fingerprint: {request.change_fingerprint!r}")
    if (
        not _BRANCH_RE.fullmatch(request.base_branch)
        or ".." in request.base_branch
        or request.base_branch.startswith(("/", "-"))
    ):
        raise ValueError(f"invalid base_branch: {request.base_branch!r}")
    if request.file_path is not None:
        pure = PurePosixPath(request.file_path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or not pure.parts
            or pure.parts[0] in {".git", ".github"}
        ):
            raise ValueError(f"disallowed or unsafe file_path: {request.file_path!r}")


class GithubPublisher:
    """Publish issues and draft pull requests through the GitHub REST API."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx2.Client | None = None,
        api_base: str = "https://api.github.com",
    ) -> None:
        if not token.strip():
            raise ValueError("GitHub token must not be empty")
        if not api_base.startswith("https://"):
            raise ValueError("GitHub api_base must use HTTPS")
        self._client = client or httpx2.Client(
            timeout=30.0, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._api_base = api_base.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def publish(self, request: PublishRequest) -> PublishedArtifact:
        """Create or reuse an issue or draft pull request."""
        _validate_publish_request(request)
        marker = self._marker(request)
        if request.kind == "issue":
            return self._publish_issue(request, marker)
        if request.file_path is None or request.patched_content is None:
            raise ValueError("pull requests require file_path and patched_content")
        return self._publish_pull_request(request, marker)

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def _publish_issue(self, request: PublishRequest, marker: str) -> PublishedArtifact:
        existing = self._find_issue(request.repository, marker)
        if existing is not None:
            return self._artifact(request, existing, False)
        response = self._request(
            "POST",
            f"/repos/{quote(request.repository, safe='/')}/issues",
            json={"title": request.title, "body": f"{request.body}\n\n{marker}"},
        )
        return self._artifact(request, response, True)

    def _publish_pull_request(self, request: PublishRequest, marker: str) -> PublishedArtifact:
        branch = f"driftwatch/{request.change_fingerprint[:16]}"
        existing = self._find_pull_request(request.repository, marker)
        if existing is not None:
            return self._artifact(request, existing, False)

        base_ref = self._request(
            "GET",
            (
                f"/repos/{quote(request.repository, safe='/')}/git/ref/heads/"
                f"{quote(request.base_branch)}"
            ),
        )
        self._request(
            "POST",
            f"/repos/{quote(request.repository, safe='/')}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_ref["object"]["sha"]},
        )
        contents_path = quote(request.file_path or "", safe="/")
        current = self._request(
            "GET",
            f"/repos/{quote(request.repository, safe='/')}/contents/{contents_path}",
            params={"ref": branch},
        )
        self._request(
            "PUT",
            f"/repos/{quote(request.repository, safe='/')}/contents/{contents_path}",
            json={
                "message": request.title,
                "content": base64.b64encode(request.patched_content or b"").decode(),
                "branch": branch,
                "sha": current.get("sha"),
            },
        )
        response = self._request(
            "POST",
            f"/repos/{quote(request.repository, safe='/')}/pulls",
            json={
                "title": request.title,
                "body": f"{request.body}\n\n{marker}",
                "head": branch,
                "base": request.base_branch,
                "draft": True,
            },
        )
        return self._artifact(request, response, True)

    def _find_issue(self, repository: str, marker: str) -> dict | None:
        items = self._request(
            "GET",
            f"/repos/{quote(repository, safe='/')}/issues",
            params={"state": "all", "per_page": 100},
        )
        return next((item for item in items if marker in item.get("body", "")), None)

    def _find_pull_request(self, repository: str, marker: str) -> dict | None:
        items = self._request(
            "GET",
            f"/repos/{quote(repository, safe='/')}/pulls",
            params={"state": "all", "per_page": 100},
        )
        return next((item for item in items if marker in item.get("body", "")), None)

    def _request(self, method: str, path: str, **kwargs: object) -> dict | list:
        response = self._client.request(
            method,
            f"{self._api_base}{path}",
            headers=self._headers,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _marker(request: PublishRequest) -> str:
        """Build the stable marker used for idempotent publishing."""
        return f"<!-- driftwatch:{request.change_fingerprint}:{request.kind} -->"

    @staticmethod
    def _artifact(request: PublishRequest, response: dict, created: bool) -> PublishedArtifact:
        """Convert GitHub's response into the internal artifact contract."""
        return PublishedArtifact(
            kind=request.kind,
            repository=request.repository,
            number=response["number"],
            url=response["html_url"],
            created=created,
        )
