"""Surface Map data structures."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Self

Provenance = Literal["sdk_source_derived", "llm_proposed", "human_curated"]

#: Confidence assigned per provenance. Derived entries are read straight out of generated
#: SDK source, so they are near-certain; LLM-proposed entries are capped well below the
#: threshold that permits a ready-for-review pull request.
PROVENANCE_CONFIDENCE: dict[Provenance, float] = {
    "sdk_source_derived": 0.95,
    "human_curated": 1.0,
    "llm_proposed": 0.6,
}


@dataclass(frozen=True, slots=True)
class SurfaceMapEntry:
    """A binding between one API surface and one code symbol."""

    surface_ref: str
    symbol_ref: str
    provenance: Provenance = "sdk_source_derived"
    confidence: float = 0.95
    source_locator: str | None = None
    #: Name of the generated params TypedDict/interface, when the binding came from a
    #: method whose request body is a typed parameter object.
    params_type: str | None = None


@dataclass(slots=True)
class SurfaceMap:
    """All bindings derived from one SDK at one version.

    ``entries`` holds operation-level bindings. Parameter names live in ``op_params`` keyed by
    operation surface ref rather than as one entry per parameter: an operation has ~20
    parameters and ~3 symbol aliases, so entry-per-parameter would inflate the map by two
    orders of magnitude for information the matcher can reconstruct on demand.
    """

    provider: str
    language: str
    sdk_package: str
    sdk_version: str
    entries: list[SurfaceMapEntry] = field(default_factory=list)
    op_params: dict[str, list[str]] = field(default_factory=dict)
    #: Operation surface ref -> response object name, so that an attribute read on a value
    #: returned by a matched call can be resolved to an ``obj:.../field:...`` surface.
    op_returns: dict[str, str] = field(default_factory=dict)

    def add(self, entry: SurfaceMapEntry) -> None:
        """Append a binding."""
        self.entries.append(entry)

    def params_for(self, op_surface_ref: str) -> frozenset[str]:
        """Return the known request parameter names for an operation."""
        return frozenset(self.op_params.get(op_surface_ref, ()))

    def returns_for(self, op_surface_ref: str) -> str | None:
        """Return the response object name for an operation, if known."""
        return self.op_returns.get(op_surface_ref)

    def by_symbol(self) -> dict[str, list[SurfaceMapEntry]]:
        """Index bindings by code symbol, for the matcher's resolution step."""
        out: dict[str, list[SurfaceMapEntry]] = defaultdict(list)
        for e in self.entries:
            out[e.symbol_ref].append(e)
        return dict(out)

    def by_surface(self) -> dict[str, list[SurfaceMapEntry]]:
        """Index bindings by surface ref, for change fan-out."""
        out: dict[str, list[SurfaceMapEntry]] = defaultdict(list)
        for e in self.entries:
            out[e.surface_ref].append(e)
        return dict(out)

    @property
    def operation_count(self) -> int:
        """Number of distinct operation-level surfaces bound."""
        return len({e.surface_ref for e in self.entries if "/param:" not in e.surface_ref})

    def save(self, path: Path) -> None:
        """Write the map to a JSON file."""
        payload = {
            "provider": self.provider,
            "language": self.language,
            "sdk_package": self.sdk_package,
            "sdk_version": self.sdk_version,
            "entries": [asdict(e) for e in self.entries],
            "op_params": self.op_params,
            "op_returns": self.op_returns,
        }
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read a map previously written by :meth:`save`."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            provider=raw["provider"],
            language=raw["language"],
            sdk_package=raw["sdk_package"],
            sdk_version=raw["sdk_version"],
            entries=[SurfaceMapEntry(**e) for e in raw["entries"]],
            op_params=raw.get("op_params", {}),
            op_returns=raw.get("op_returns", {}),
        )
