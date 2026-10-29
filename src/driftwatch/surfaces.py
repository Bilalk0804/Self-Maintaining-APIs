"""Canonical identifiers for API surface, and the normalised index built from any source.

A ``SurfaceRef`` is the primary key of the whole system. Every layer -- the spec differ, the
surface map, the matcher, the codemods -- speaks in these strings, which is what lets one
pipeline serve OpenAPI, GraphQL SDL and SDK-source providers alike.

Grammar::

    api:<provider>/<kind>:<id>[/<subkind>:<subid>]

Examples::

    api:stripe/op:post_v1_charges
    api:stripe/op:post_v1_charges/param:source
    api:stripe/obj:charge/field:payment_method_details
    api:openai/model:gpt-4-vision-preview
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Self

SurfaceKind = Literal["op", "param", "obj", "field", "enum", "model", "event"]

_PATH_SANITISE_RE = re.compile(r"[^a-z0-9]+")
_PATH_PARAM_RE = re.compile(r"\{[^}]*\}")

#: Placeholder every path parameter collapses to. Spec and SDK disagree on parameter names
#: for the same route -- the spec writes ``/v1/charges/{charge}`` while the SDK's instance URL
#: builder yields ``/v1/charges/{id}`` -- so the name must not be part of an operation's
#: identity, or the same endpoint would get two different refs and never match.
_PATH_PARAM_TOKEN = "by_id"


def operation_id(method: str, path: str) -> str:
    """Build the stable operation component of a ``SurfaceRef``.

    Normalises an HTTP method and path into a lowercase, underscore-joined token. Path
    parameter names are collapsed to a single placeholder, so ``/v1/charges/{charge}`` and
    ``/v1/charges/{id}`` describe the same operation.

    Args:
        method: HTTP method, any case.
        path: URL path template, e.g. ``/v1/charges/{charge}/capture``.

    Returns:
        A token such as ``post_v1_charges_by_id_capture``.
    """
    normalised = _PATH_PARAM_RE.sub(_PATH_PARAM_TOKEN, path.lower())
    cleaned = _PATH_SANITISE_RE.sub("_", normalised).strip("_")
    return f"{method.lower()}_{cleaned}"


@dataclass(frozen=True, slots=True)
class SurfaceRef:
    """A parsed surface reference. Use :meth:`parse` / :func:`str` to cross the string boundary."""

    provider: str
    kind: SurfaceKind
    ident: str
    subkind: str | None = None
    subident: str | None = None

    def __str__(self) -> str:
        base = f"api:{self.provider}/{self.kind}:{self.ident}"
        if self.subkind is not None:
            return f"{base}/{self.subkind}:{self.subident}"
        return base

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Parse a surface reference string.

        Raises:
            ValueError: if ``raw`` does not match the documented grammar.
        """
        if not raw.startswith("api:"):
            raise ValueError(f"surface ref must start with 'api:': {raw!r}")
        parts = raw.split("/")
        if len(parts) not in (2, 3):
            raise ValueError(f"surface ref must have 2 or 3 segments: {raw!r}")
        provider = parts[0].removeprefix("api:")
        if not provider:
            raise ValueError(f"surface ref has empty provider: {raw!r}")
        kind, _, ident = parts[1].partition(":")
        if not ident:
            raise ValueError(f"surface ref segment missing identifier: {raw!r}")
        subkind: str | None = None
        subident: str | None = None
        if len(parts) == 3:
            subkind, _, subident = parts[2].partition(":")
            if not subident:
                raise ValueError(f"surface ref subsegment missing identifier: {raw!r}")
        return cls(provider, kind, ident, subkind, subident)  # type: ignore[arg-type]

    @property
    def parent(self) -> str:
        """The owning operation/object ref, dropping any param/field component."""
        return f"api:{self.provider}/{self.kind}:{self.ident}"


def op_ref(provider: str, method: str, path: str) -> str:
    """Return the ``SurfaceRef`` string for an HTTP operation."""
    return f"api:{provider}/op:{operation_id(method, path)}"


def param_ref(provider: str, method: str, path: str, name: str) -> str:
    """Return the ``SurfaceRef`` string for a request parameter of an operation."""
    return f"{op_ref(provider, method, path)}/param:{name}"


def field_ref(provider: str, obj: str, name: str) -> str:
    """Return the ``SurfaceRef`` string for a response object field."""
    return f"api:{provider}/obj:{obj}/field:{name}"


def model_ref(provider: str, model: str) -> str:
    """Return the ``SurfaceRef`` string for a named model identifier."""
    return f"api:{provider}/model:{model}"


@dataclass(frozen=True, slots=True)
class SurfaceDescriptor:
    """The semantic payload compared across two versions of a surface.

    Only fields that affect *callers* belong here. ``doc_hash`` is kept separate from the
    shape so a pure documentation edit never masquerades as a behavioural change.
    """

    kind: SurfaceKind
    type_sig: str | None = None
    required: bool | None = None
    nullable: bool | None = None
    default: Any = None
    enum_values: tuple[str, ...] | None = None
    deprecated: bool = False
    sunset_on: str | None = None
    doc_hash: str | None = None
    source_locator: str | None = None

    def shape(self) -> tuple[Any, ...]:
        """Return the comparison tuple, excluding documentation and provenance.

        Two descriptors with equal shapes are behaviourally identical to a caller even if
        their descriptions or spec locations differ.
        """
        return (
            self.kind,
            self.type_sig,
            self.required,
            self.nullable,
            _freeze(self.default),
            self.enum_values,
            self.deprecated,
            self.sunset_on,
        )


def _freeze(value: Any) -> Any:
    """Recursively convert JSON-ish values into hashable, order-stable equivalents."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(slots=True)
class SurfaceIndex:
    """A flattened ``{SurfaceRef: SurfaceDescriptor}`` view of one source at one revision."""

    provider: str
    revision: str
    entries: dict[str, SurfaceDescriptor] = field(default_factory=dict)

    def add(self, ref: str, descriptor: SurfaceDescriptor) -> None:
        """Insert a descriptor, keeping the first write for a duplicated ref."""
        self.entries.setdefault(ref, descriptor)

    def __len__(self) -> int:
        return len(self.entries)

    def operations(self) -> list[str]:
        """Return every operation-level ref in the index."""
        return [r for r in self.entries if "/param:" not in r and r.find("/op:") != -1]
