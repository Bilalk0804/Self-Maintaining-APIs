"""Provider-specific parsing of persisted, ground-truth source payloads."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Protocol

import orjson

from Driftwatch.Surfaces import SurfaceDescriptor, SurfaceIndex, op_ref
from Driftwatch.Watch.OpenapiIndexer import build_index


class PayloadIndexingError(ValueError):
    """Raised when a ground-truth payload cannot be safely indexed."""


@dataclass(frozen=True, slots=True)
class IndexedPayload:
    """Immutable result of indexing one raw payload."""

    provider: str
    revision: str
    content_sha256: str
    index: SurfaceIndex


class PayloadIndexer(Protocol):
    """Index one provider-specific ground-truth payload."""

    provider: str
    source_kind: str

    def index(self, raw: bytes, *, revision: str) -> IndexedPayload:
        """Return a normalized surface index or raise without partial output."""


class OpenApiPayloadIndexer:
    """Index OpenAPI JSON using the canonical shared OpenAPI indexer."""

    provider = "openai"
    source_kind = "openapi"

    def index(self, raw: bytes, *, revision: str) -> IndexedPayload:
        try:
            document = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise PayloadIndexingError("OpenAI payload is not valid JSON") from error
        if not isinstance(document, dict) or not document.get("openapi"):
            raise PayloadIndexingError("OpenAI payload is not an OpenAPI document")
        try:
            index = build_index(document, self.provider, revision)
        except (TypeError, ValueError, KeyError) as error:
            raise PayloadIndexingError("OpenAI OpenAPI payload could not be indexed") from error
        return IndexedPayload(self.provider, revision, _sha256(raw), index)


class RazorpaySdkPayloadIndexer:
    """Index provable HTTP operations from a Razorpay Python source payload.

    The payload is a UTF-8 source file or a concatenation produced by the source adapter.
    Only string literals containing an API path and an adjacent HTTP method argument are
    emitted; unknown source shapes are ignored rather than guessed.
    """

    provider = "razorpay"
    source_kind = "sdk_source"

    def index(self, raw: bytes, *, revision: str) -> IndexedPayload:
        try:
            tree = ast.parse(raw.decode("utf-8"))
        except (UnicodeDecodeError, SyntaxError) as error:
            raise PayloadIndexingError("Razorpay payload is not valid Python source") from error
        index = SurfaceIndex(self.provider, revision)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            path = _string_value(node.args[0])
            if path is None or not path.startswith("/v1/"):
                continue
            method = _method_name(node)
            if method is None:
                continue
            index.add(
                op_ref(self.provider, method, path),
                SurfaceDescriptor(
                    kind="op",
                    type_sig=f"{method.lower()} {path}",
                    source_locator=f"line {node.lineno}",
                ),
            )
        if not index.entries:
            raise PayloadIndexingError("Razorpay source contained no provable API operations")
        return IndexedPayload(self.provider, revision, _sha256(raw), index)


def _string_value(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _method_name(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg in {"method", "http_method"}:
            value = _string_value(keyword.value)
            if value:
                return value
    if len(call.args) > 1:
        value = _string_value(call.args[1])
        if value:
            return value
    return None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
