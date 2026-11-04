"""Provider-specific parsing of persisted, ground-truth source payloads."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

import orjson

from Driftwatch.Surfaces import SurfaceDescriptor, SurfaceIndex, op_ref
from Driftwatch.Watch.OpenapiIndexer import build_index

_API_PATH_RE = re.compile(r"^/v\d+(?:/|$)")


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
    """Index provider OpenAPI JSON using the canonical shared indexer."""

    source_kind = "openapi"

    def __init__(self, provider: str = "openai") -> None:
        if not provider.strip():
            raise ValueError("provider must not be empty")
        self.provider = provider

    def index(self, raw: bytes, *, revision: str) -> IndexedPayload:
        try:
            document = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise PayloadIndexingError(
                f"{self.provider} payload is not valid JSON"
            ) from error
        if not isinstance(document, dict) or not document.get("openapi"):
            raise PayloadIndexingError(
                f"{self.provider} payload is not an OpenAPI document"
            )
        try:
            index = build_index(document, self.provider, revision)
        except (TypeError, ValueError, KeyError) as error:
            raise PayloadIndexingError(
                f"{self.provider} OpenAPI payload could not be indexed"
            ) from error
        return IndexedPayload(self.provider, revision, _sha256(raw), index)


class PythonSdkPayloadIndexer:
    """Index provable HTTP operations from a provider Python source payload.

    The payload is a UTF-8 source file or a concatenation produced by the source adapter.
    Only string literals containing an API path and an adjacent HTTP method argument are
    emitted; unknown source shapes are ignored rather than guessed.
    """

    source_kind = "sdk_source"

    def __init__(self, provider: str) -> None:
        if not provider.strip():
            raise ValueError("provider must not be empty")
        self.provider = provider

    def index(self, raw: bytes, *, revision: str) -> IndexedPayload:
        try:
            tree = ast.parse(raw.decode("utf-8"))
        except (UnicodeDecodeError, SyntaxError) as error:
            raise PayloadIndexingError(
                f"{self.provider} payload is not valid Python source"
            ) from error
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        index = SurfaceIndex(self.provider, revision)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            path = _string_value(node.args[0])
            if path is None or not _API_PATH_RE.match(path):
                continue
            method = _method_name(node, parents)
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
            raise PayloadIndexingError(
                f"{self.provider} source contained no provable API operations"
            )
        return IndexedPayload(self.provider, revision, _sha256(raw), index)


class RazorpaySdkPayloadIndexer(PythonSdkPayloadIndexer):
    """Index provable HTTP operations from a Razorpay Python source payload."""

    def __init__(self) -> None:
        super().__init__("razorpay")


def _string_value(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _method_name(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str | None:
    for keyword in call.keywords:
        if keyword.arg in {"method", "http_method"}:
            value = _string_value(keyword.value)
            if value:
                return value
    if len(call.args) > 1:
        value = _string_value(call.args[1])
        if value:
            return value
    value = call.func.value if isinstance(call.func, ast.Attribute) else None
    while isinstance(value, ast.Call):
        if isinstance(value.func, ast.Attribute) and value.func.attr in {
            "http_method",
            "method",
        }:
            if value.args:
                method = value.args[0]
                if isinstance(method, ast.Attribute):
                    return method.attr
                literal = _string_value(method)
                if literal:
                    return literal
        value = value.func.value if isinstance(value.func, ast.Attribute) else None
    current: ast.AST = call
    while current in parents:
        current = parents[current]
        if not isinstance(current, ast.Call) or not isinstance(current.func, ast.Attribute):
            continue
        if current.func.attr not in {"http_method", "method"} or not current.args:
            continue
        method = current.args[0]
        if isinstance(method, ast.Attribute):
            return method.attr
        literal = _string_value(method)
        if literal:
            return literal
    return None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
