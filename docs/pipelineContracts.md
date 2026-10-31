# Driftwatch — Pipeline Contracts

This document defines the interfaces and trust boundaries between pipeline stages. The
payloads exchanged at those boundaries are documented in
[dataContracts.md](./dataContracts.md).

## Source to surface index

```python
class SourceAdapter(Protocol):
    kind: str

    def poll(self, state: SourceState) -> PollResult:
        """Check a watermark without downloading an unchanged payload."""

    def index(self, raw: bytes) -> SurfaceIndex:
        """Normalise ground-truth input or raise NotGroundTruthError."""
```

PayPal has two ground-truth adapters:

1. OpenAPI documents from `paypal/paypal-rest-api-specifications`.
2. Generated Python controllers from `paypal-server-sdk`.

Both produce the same `SurfaceIndex`; the SDK map is used to connect API surfaces to code
symbols, while the OpenAPI index is used for version-to-version change detection.

## Surface index to change record

```python
class IndexDiffer(Protocol):
    def diff(self, before: SurfaceIndex, after: SurfaceIndex) -> list[RawDelta]: ...

class EvidenceRetriever(Protocol):
    def retrieve(self, delta: RawDelta) -> list[Evidence]: ...

class ChangeClassifier(Protocol):
    def classify(
        self, deltas: list[RawDelta], evidence: list[Evidence]
    ) -> list[ChangeRecord]: ...
```

Detection and interpretation are separate: the differ is deterministic, and the LLM only
classifies already-detected deltas. A record without valid evidence is quarantined.

The classifier validates structured output with the closed change-type and recipe enums.
Every cited quote must be contained in the retrieved evidence record, every surface must
belong to the deterministic delta, and the model cannot override the delta's type or
severity. Invalid output is quarantined explicitly.

The evidence retriever always emits the deterministic delta as `raw_delta` evidence and may
add verbatim line-window quotes from configured provider sources. Provider quotes retain
their source URL and locator; empty quotes, missing source URLs, oversized responses, and
HTTP failures are rejected rather than silently converted into successful evidence.

## Change record to finding

```python
class DependencyScanner(Protocol):
    def scan(self, repo_root: Path) -> list[RepoDependency]: ...

class CandidateGenerator(Protocol):
    def generate(self, repo_root: Path, probes: list[str]) -> list[CandidateFile]: ...

class SymbolResolver(Protocol):
    language: str

    def resolve(self, src: bytes, surface_map: SurfaceMapView) -> list[ResolvedUsage]: ...

class ConfidenceScorer(Protocol):
    def score(
        self, usage: ResolvedUsage, change: ChangeRecord,
        dependency: RepoDependency | None,
    ) -> ScoredFinding: ...
```

Python files use the Python tree-sitter resolver. TypeScript, TSX, JavaScript, JSX, and MJS
files use the TypeScript resolver, which resolves mapped member-expression calls and known
request-object keys without treating similarly named in-house objects as provider usage.

For PayPal, dependency prefiltering recognises `paypal-server-sdk` and the
`paypalserversdk` import namespace. Raw HTTP matching also recognises PayPal API hosts and
paths, so repositories that do not use the SDK are not silently excluded.

## Finding to published artifact

```python
class Codemod(Protocol):
    def plan(self, finding: Finding, recipe: MigrationRecipe, src: bytes) -> list[Edit]: ...

class SandboxRunner(Protocol):
    def run(
        self, repo_root: Path, phase: str, command: list[str], allow_network: bool
    ) -> SandboxResult: ...

class Publisher(Protocol):
    def publish(
        self, decision: GateDecision, patch: Patch | None
    ) -> PublishedArtifact: ...
```

Codemods return byte-range edits and never reformat a file. The sandbox verifies syntax,
types, lint, and tests in a disposable checkout. It captures command output, enforces a
timeout, and records whether the command was allowed network access. The publisher is
idempotent on `(repo_id, change_id, artifact_kind)`.

The GitHub publisher uses a stable hidden marker to find existing issues or pull requests
before creating anything. Issues are created directly; pull requests create a short-lived
Driftwatch branch, update the patched file through the Contents API, and open a draft PR.

## LLM boundary

The `understand` call receives only deterministic deltas, source descriptors, and retrieved
evidence. The `fix` call receives one target file, one finding, and one recipe. Neither call
has tools, network, filesystem, or database access. Structured validation and verbatim-quote
checks run before any result enters the deterministic pipeline.
