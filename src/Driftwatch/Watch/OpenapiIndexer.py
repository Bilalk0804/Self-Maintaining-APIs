"""Flatten an OpenAPI document into a :class:`SurfaceIndex`.

Normalisation is the whole job. A regenerated spec reorders keys, renames inline schemas and
rewrites descriptions constantly; if any of that reaches the differ it manufactures phantom
changes, and every downstream stage amplifies the noise into alerts nobody trusts. So we
resolve ``$ref``, flatten ``allOf``, sort ``enum`` and ``oneOf`` members, and keep
documentation text in a separate hash from the shape.
"""

from __future__ import annotations

import hashlib
from typing import Any

import orjson

from Driftwatch.Surfaces import SurfaceDescriptor, SurfaceIndex, operation_id

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options"})

#: Guards against cyclic ``$ref`` chains, which real specs do contain.
_MAX_REF_DEPTH = 24
_MAX_SPEC_BYTES = 15 * 1024 * 1024  # 15 MB


def load_openapi(raw: bytes, *, max_bytes: int = _MAX_SPEC_BYTES) -> dict[str, Any]:
    """Parse an OpenAPI JSON document with a strict byte ceiling."""
    if len(raw) > max_bytes:
        raise ValueError(f"OpenAPI document exceeds maximum size of {max_bytes} bytes")
    data = orjson.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("OpenAPI root must be a JSON object")
    return data


def build_index(doc: dict[str, Any], provider: str, revision: str) -> SurfaceIndex:
    """Flatten an OpenAPI document into a surface index.

    Emits an entry per operation, per request parameter (query/path/body property), and per
    response object field, plus enum-value entries where a schema is a closed set.

    Args:
        doc: Parsed OpenAPI document.
        provider: Provider slug for minting surface refs.
        revision: Revision label (commit sha, release tag) recorded on the index.
    """
    index = SurfaceIndex(provider=provider, revision=revision)
    resolver = _Resolver(doc)

    for path, path_item in (doc.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            _index_operation(index, resolver, provider, method, path, path_item, operation)

    for name, webhook in (doc.get("webhooks") or {}).items():
        if isinstance(name, str) and isinstance(webhook, dict):
            _index_webhook(index, resolver, provider, name, webhook)

    for name, schema in (doc.get("components", {}).get("schemas") or {}).items():
        _index_object(index, resolver, provider, name, schema)

    return index


def _index_webhook(
    index: SurfaceIndex,
    resolver: _Resolver,
    provider: str,
    name: str,
    path_item: dict[str, Any],
) -> None:
    """Index an OpenAPI 3.1 webhook and its request payload fields.

    Stripe's unified OpenAPI files include webhook events alongside v1 and v2 paths.
    Webhooks are keyed by event name rather than URL, so they use the existing ``event``
    surface kind and retain the same parameter descriptors as request bodies.
    """
    event_ref = f"api:{provider}/event:{name}"
    operation = next(
        (
            candidate
            for method, candidate in path_item.items()
            if method.lower() in _HTTP_METHODS and isinstance(candidate, dict)
        ),
        None,
    )
    if operation is None:
        return

    index.add(
        event_ref,
        SurfaceDescriptor(
            kind="event",
            type_sig="webhook",
            deprecated=bool(operation.get("deprecated")),
            doc_hash=_doc_hash(operation.get("description") or operation.get("summary")),
            source_locator=f"#/webhooks/{name}",
        ),
    )

    body = resolver.resolve(operation.get("requestBody") or {})
    for media_type, media in (body.get("content") or {}).items():
        if not isinstance(media, dict):
            continue
        schema = resolver.flatten(media.get("schema") or {})
        required = set(schema.get("required") or [])
        for prop_name, prop_schema in (schema.get("properties") or {}).items():
            flat = resolver.flatten(prop_schema)
            index.add(
                f"{event_ref}/param:{prop_name}",
                SurfaceDescriptor(
                    kind="param",
                    type_sig=_type_sig(flat),
                    required=prop_name in required,
                    nullable=_nullable(flat),
                    default=flat.get("default"),
                    enum_values=_enum(flat),
                    deprecated=bool(flat.get("deprecated")),
                    doc_hash=_doc_hash(flat.get("description")),
                    source_locator=f"#/webhooks/{name}/requestBody/{media_type}/{prop_name}",
                ),
            )
        break


def _index_operation(
    index: SurfaceIndex,
    resolver: _Resolver,
    provider: str,
    method: str,
    path: str,
    path_item: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    """Add one operation and its request parameters to the index."""
    op_id = operation_id(method, path)
    ref = f"api:{provider}/op:{op_id}"
    locator = f"#/paths/{path}/{method}"
    index.add(
        ref,
        SurfaceDescriptor(
            kind="op",
            type_sig=f"{method.lower()} {path}",
            deprecated=bool(operation.get("deprecated")),
            doc_hash=_doc_hash(operation.get("description") or operation.get("summary")),
            source_locator=locator,
        ),
    )

    # Path-level parameters apply to every operation on that path.
    params = [*(path_item.get("parameters") or []), *(operation.get("parameters") or [])]
    for param in params:
        resolved = resolver.resolve(param)
        name = resolved.get("name")
        if not isinstance(name, str):
            continue
        schema = resolver.flatten(resolved.get("schema") or {})
        index.add(
            f"{ref}/param:{name}",
            SurfaceDescriptor(
                kind="param",
                type_sig=_type_sig(schema),
                required=bool(resolved.get("required")),
                nullable=_nullable(schema),
                default=schema.get("default"),
                enum_values=_enum(schema),
                deprecated=bool(resolved.get("deprecated") or schema.get("deprecated")),
                doc_hash=_doc_hash(resolved.get("description")),
                source_locator=f"{locator}/parameters/{name}",
            ),
        )

    body = resolver.resolve(operation.get("requestBody") or {})
    for media_type, media in (body.get("content") or {}).items():
        if not isinstance(media, dict):
            continue
        schema = resolver.flatten(media.get("schema") or {})
        required = set(schema.get("required") or [])
        for prop_name, prop_schema in (schema.get("properties") or {}).items():
            flat = resolver.flatten(prop_schema)
            index.add(
                f"{ref}/param:{prop_name}",
                SurfaceDescriptor(
                    kind="param",
                    type_sig=_type_sig(flat),
                    required=prop_name in required,
                    nullable=_nullable(flat),
                    default=flat.get("default"),
                    enum_values=_enum(flat),
                    deprecated=bool(flat.get("deprecated")),
                    doc_hash=_doc_hash(flat.get("description")),
                    source_locator=f"{locator}/requestBody/{media_type}/{prop_name}",
                ),
            )
        break  # one media type is enough; Stripe uses form-encoding only


def _index_object(
    index: SurfaceIndex,
    resolver: _Resolver,
    provider: str,
    name: str,
    schema: dict[str, Any],
) -> None:
    """Add a response object and its fields to the index."""
    flat = resolver.flatten(schema)
    obj_ref = f"api:{provider}/obj:{name}"
    index.add(
        obj_ref,
        SurfaceDescriptor(
            kind="obj",
            type_sig=_type_sig(flat),
            deprecated=bool(flat.get("deprecated")),
            doc_hash=_doc_hash(flat.get("description")),
            source_locator=f"#/components/schemas/{name}",
        ),
    )
    required = set(flat.get("required") or [])
    for prop_name, prop_schema in (flat.get("properties") or {}).items():
        prop_flat = resolver.flatten(prop_schema)
        index.add(
            f"{obj_ref}/field:{prop_name}",
            SurfaceDescriptor(
                kind="field",
                type_sig=_type_sig(prop_flat),
                required=prop_name in required,
                nullable=_nullable(prop_flat),
                default=prop_flat.get("default"),
                enum_values=_enum(prop_flat),
                deprecated=bool(prop_flat.get("deprecated")),
                doc_hash=_doc_hash(prop_flat.get("description")),
                source_locator=f"#/components/schemas/{name}/properties/{prop_name}",
            ),
        )


class _Resolver:
    """Resolves ``$ref`` pointers and flattens ``allOf`` composition, with memoisation."""

    __slots__ = ("_doc", "_ref_cache", "_flat_cache")

    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc
        self._ref_cache: dict[str, dict[str, Any]] = {}
        self._flat_cache: dict[int, dict[str, Any]] = {}

    def resolve(self, node: Any, depth: int = 0) -> dict[str, Any]:
        """Follow a ``$ref`` chain to the underlying object."""
        if not isinstance(node, dict) or depth > _MAX_REF_DEPTH:
            return node if isinstance(node, dict) else {}
        ref = node.get("$ref")
        if not isinstance(ref, str):
            return node
        if not ref.startswith("#/"):
            return {}
        if ref in self._ref_cache:
            return self._ref_cache[ref]
        target: Any = self._doc
        for part in ref[2:].split("/"):
            if not isinstance(target, dict):
                return {}
            target = target.get(part.replace("~1", "/").replace("~0", "~"))
            if target is None:
                return {}
        # Cache before recursing so a cycle terminates rather than recursing forever.
        self._ref_cache[ref] = target if isinstance(target, dict) else {}
        resolved = self.resolve(target, depth + 1)
        self._ref_cache[ref] = resolved
        return resolved

    def flatten(self, schema: Any, depth: int = 0) -> dict[str, Any]:
        """Resolve refs and merge ``allOf`` members into a single schema object."""
        if not isinstance(schema, dict) or depth > _MAX_REF_DEPTH:
            return schema if isinstance(schema, dict) else {}
        key = id(schema)
        if depth == 0 and key in self._flat_cache:
            return self._flat_cache[key]

        resolved = self.resolve(schema, depth)
        all_of = resolved.get("allOf")
        if not isinstance(all_of, list):
            if depth == 0:
                self._flat_cache[key] = resolved
            return resolved

        merged: dict[str, Any] = {k: v for k, v in resolved.items() if k != "allOf"}
        properties: dict[str, Any] = dict(merged.get("properties") or {})
        required: list[str] = list(merged.get("required") or [])
        for member in all_of:
            flat_member = self.flatten(member, depth + 1)
            properties.update(flat_member.get("properties") or {})
            required.extend(flat_member.get("required") or [])
            for k, v in flat_member.items():
                if k not in ("properties", "required", "allOf"):
                    merged.setdefault(k, v)
        if properties:
            merged["properties"] = properties
        if required:
            merged["required"] = sorted(set(required))
        if depth == 0:
            self._flat_cache[key] = merged
        return merged


def _type_sig(schema: dict[str, Any], depth: int = 0) -> str | None:
    """Build an order-stable type signature.

    ``oneOf``/``anyOf`` members are sorted so that a regenerated spec listing the same union
    in a different order produces an identical signature.
    """
    if not schema or depth > _MAX_REF_DEPTH:
        return None
    for union_key in ("oneOf", "anyOf"):
        members = schema.get(union_key)
        if isinstance(members, list) and members:
            parts = sorted(
                filter(
                    None,
                    (_type_sig(m, depth + 1) if isinstance(m, dict) else None for m in members),
                )
            )
            return f"{union_key}[{','.join(parts)}]"
    type_name = schema.get("type")
    if isinstance(type_name, list):
        type_name = "|".join(sorted(str(t) for t in type_name))
    if type_name == "array":
        items = schema.get("items")
        inner = _type_sig(items, depth + 1) if isinstance(items, dict) else None
        return f"array[{inner or 'any'}]"
    fmt = schema.get("format")
    if type_name and fmt:
        return f"{type_name}:{fmt}"
    if type_name:
        return str(type_name)
    if schema.get("properties"):
        return "object"
    return None


def _enum(schema: dict[str, Any]) -> tuple[str, ...] | None:
    """Return sorted enum values, or ``None`` when the schema is not a closed set."""
    values = schema.get("enum")
    if not isinstance(values, list) or not values:
        return None
    return tuple(sorted(str(v) for v in values))


def _nullable(schema: dict[str, Any]) -> bool | None:
    """Return the nullability flag, handling both OpenAPI 3.0 and 3.1 spellings."""
    if "nullable" in schema:
        return bool(schema["nullable"])
    type_name = schema.get("type")
    if isinstance(type_name, list):
        return "null" in type_name
    return None


def _doc_hash(text: Any) -> str | None:
    """Hash documentation text so prose edits are detectable but not shape-affecting."""
    if not isinstance(text, str) or not text:
        return None
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
