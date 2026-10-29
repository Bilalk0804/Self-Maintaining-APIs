"""Use-case services used by controllers and other front ends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TypeVar

from driftwatch.mapping.model import SurfaceMap
from driftwatch.mapping.sdk_python import derive_stripe_python
from driftwatch.match.engine import ScanResult, scan_repo
from driftwatch.surfaces import SurfaceIndex
from driftwatch.watch.differ import Delta, diff_indexes
from driftwatch.watch.openapi_indexer import build_index, load_openapi

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class TimedResult[ResultT]:
    """A use-case result and the time spent producing it."""

    value: ResultT
    elapsed_seconds: float


class PipelineService:
    """Coordinate domain services without knowing about CLI or presentation concerns."""

    def derive_map(
        self,
        sdk_root: Path,
        *,
        provider: str = "stripe",
        sdk_version: str = "local",
    ) -> TimedResult[SurfaceMap]:
        """Derive a provider surface map from its SDK source."""
        started = perf_counter()
        result = derive_stripe_python(
            sdk_root,
            provider=provider,
            sdk_version=sdk_version,
        )
        return TimedResult(result, perf_counter() - started)

    def diff_specs(
        self,
        before: Path,
        after: Path,
        *,
        provider: str = "stripe",
        include_additive: bool = False,
    ) -> TimedResult[tuple[SurfaceIndex, SurfaceIndex, list[Delta]]]:
        """Build two indexes and return their classified semantic differences."""
        started = perf_counter()
        before_index = self.build_index(before, provider, before.stem)
        after_index = self.build_index(after, provider, after.stem)
        deltas = diff_indexes(
            before_index,
            after_index,
            include_additive=include_additive,
        )
        return TimedResult(
            (before_index, after_index, deltas),
            perf_counter() - started,
        )

    @staticmethod
    def build_index(path: Path, provider: str, revision: str) -> SurfaceIndex:
        """Load and index one OpenAPI document."""
        return build_index(load_openapi(path.read_bytes()), provider, revision)

    def scan(
        self,
        repo_root: Path,
        deltas: list[Delta],
        surface_map: SurfaceMap,
    ) -> TimedResult[ScanResult]:
        """Find affected repository usages."""
        started = perf_counter()
        result = scan_repo(repo_root, deltas, surface_map)
        return TimedResult(result, perf_counter() - started)
