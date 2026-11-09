"""Orchestrates the four match stages for one repository against a set of changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from Driftwatch.Mapping.Model import SurfaceMap
from Driftwatch.Match import Candidates as candidates
from Driftwatch.Match import Prefilter as prefilter
from Driftwatch.Match.Resolver import Usage, resolve_file
from Driftwatch.Match.Scorer import Finding, score
from Driftwatch.Match.TypescriptResolver import resolve_file as resolve_typescript_file
from Driftwatch.Surfaces import SurfaceRef
from Driftwatch.Watch.Differ import Delta

_SCRIPT_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".ts", ".tsx"})


@dataclass(slots=True)
class ScanResult:
    """Outcome of scanning one repository."""

    repo_root: str
    skipped_reason: str | None = None
    dependencies: list[prefilter.RepoDependency] = field(default_factory=list)
    test_runner: str | None = None
    files_considered: int = 0
    files_parsed: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_tests(self) -> bool:
        """Whether a test runner was detected."""
        return self.test_runner is not None


def scan_repo(repo_root: Path, deltas: list[Delta], smap: SurfaceMap) -> ScanResult:
    """Scan a repository for code affected by a set of upstream changes.

    Args:
        repo_root: Repository checkout root.
        deltas: Classified upstream changes to match against.
        smap: Surface Map for the provider those changes belong to.

    Returns:
        A :class:`ScanResult`, with ``skipped_reason`` set when stage 0 rules the repository
        out before any file is parsed.
    """
    all_sources = candidates.source_files(repo_root)
    deps = prefilter.scan_dependencies(repo_root)
    deps += prefilter.detect_raw_http(repo_root, all_sources)
    result = ScanResult(
        repo_root=str(repo_root),
        dependencies=deps,
        test_runner=prefilter.detect_test_runner(repo_root),
    )

    providers = {d.provider for d in deps}
    if smap.provider not in providers:
        result.skipped_reason = f"no dependency on {smap.provider}"
        return result

    probes: list[str] = []
    for delta in deltas:
        probes.extend(delta.code_probes())
    literal_probes = _literal_probes(deltas)
    probes.extend(literal_probes)
    probes.extend(sdk_call_probes(smap))

    candidate_files = candidates.find_candidates(repo_root, probes)
    result.files_considered = len(candidate_files)

    literal_map = {value: ref for ref, value in _literal_pairs(deltas)}
    deltas_by_surface: dict[str, list[Delta]] = {}
    for delta in deltas:
        deltas_by_surface.setdefault(delta.surface_ref, []).append(delta)

    seen: set[str] = set()
    for path in candidate_files:
        usages = _usages_for(path, repo_root, smap, literal_map)
        if usages is None:
            continue
        result.files_parsed += 1
        for usage in usages:
            for delta in _relevant_deltas(usage.surface_ref, deltas_by_surface):
                finding = score(usage, delta, surface_map_confidence=0.95)
                if finding is None or finding.fingerprint in seen:
                    continue
                seen.add(finding.fingerprint)
                result.findings.append(finding)

    result.findings.sort(key=lambda f: (-f.confidence, f.usage.file_path, f.usage.start_line))
    return result


def sdk_call_probes(smap: SurfaceMap) -> list[str]:
    """Return dotted SDK call spellings worth grepping for.

    A change's own probes are parameter names and URL paths. A file that only
    calls ``stripe.paymentIntents.create`` contains neither, so it is never
    opened. The Python spelling (``payment_intents.create``) and the Node
    spelling (``paymentIntents.create``) are both included. This pass is
    broader than the change probes: any file that mentions an SDK call is parsed.

    Args:
        smap: Surface map whose symbol tails become the probes.

    Returns:
        Sorted, de-duplicated probe strings of at least three characters.
    """
    prefix = f"{smap.sdk_package}."
    probes: set[str] = set()
    for entry in smap.entries:
        body = entry.symbol_ref.split(":", 1)[-1]
        if not body.startswith(prefix):
            continue
        tail = body[len(prefix) :]
        if tail.startswith(("v1.", "v2.")):
            tail = tail.split(".", 1)[1]
        if tail.endswith("_async"):
            tail = tail[: -len("_async")]
        if "." not in tail:
            continue
        probes.add(tail)
        camel = ".".join(_camel(part) for part in tail.split("."))
        if camel != tail:
            probes.add(camel)
    return sorted(probe for probe in probes if len(probe) >= 3)


def _camel(name: str) -> str:
    """Convert one snake_case identifier to camelCase."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest if part)


def _usages_for(
    path: Path,
    repo_root: Path,
    smap: SurfaceMap,
    literal_map: dict[str, str],
) -> list[Usage] | None:
    """Parse one candidate file, or return ``None`` when this scan cannot read it."""
    if path.suffix != ".py" and path.suffix not in _SCRIPT_SUFFIXES:
        return None
    try:
        src = path.read_bytes()
    except OSError:
        return None
    rel = path.relative_to(repo_root).as_posix()
    if path.suffix == ".py":
        return resolve_file(src, rel, smap, literal_probes=literal_map)
    return resolve_typescript_file(src, rel, smap)


def _relevant_deltas(usage_ref: str, by_surface: dict[str, list[Delta]]) -> list[Delta]:
    """Return changes that could concern a usage: its own surface or its parent operation."""
    out = list(by_surface.get(usage_ref, ()))
    parent = usage_ref.split("/param:")[0].split("/field:")[0]
    if parent != usage_ref:
        out.extend(by_surface.get(parent, ()))
    return out


def _literal_pairs(deltas: list[Delta]) -> list[tuple[str, str]]:
    """Return ``(surface_ref, literal_value)`` for string-valued surfaces and dropped enums."""
    out: list[tuple[str, str]] = []
    for delta in deltas:
        ref = SurfaceRef.parse(delta.surface_ref)
        if ref.kind == "model":
            out.append((delta.surface_ref, ref.ident))
        if delta.change_type == "enum_value_removed" and delta.before and delta.after:
            dropped = set(delta.before.enum_values or ()) - set(delta.after.enum_values or ())
            out.extend((delta.surface_ref, value) for value in sorted(dropped))
    return out


def _literal_probes(deltas: list[Delta]) -> list[str]:
    """Return the literal strings worth grepping for."""
    return [value for _ref, value in _literal_pairs(deltas)]
