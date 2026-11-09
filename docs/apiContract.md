# Driftwatch — API & LLM Contracts

> This is the compatibility index for the original combined contract document. Data payloads
> now live in [dataContracts.md](./dataContracts.md), and stage interfaces/data-flow
> boundaries live in [pipelineContracts.md](./pipelineContracts.md). The HTTP and LLM
> sections below remain here until the API surface is extracted into versioned schemas.

Companion to [`systemDesign.md`](./systemDesign.md) and [`dataModel.sql`](./dataModel.sql).

Three contracts live here: the internal pipeline interfaces (the seams that make the system
testable), the LLM I/O schemas (the trust boundary), and the HTTP surface.

---

## 1. Pipeline interfaces

These are the seams. Each is independently unit-testable against fixtures, which is what
makes a system with an LLM in it debuggable at all.

```python
from __future__ import annotations

from typing import Protocol

# --- Watch -----------------------------------------------------------------

class SourceAdapter(Protocol):
    """Fetch and normalise one upstream source."""

    kind: str
    def poll(self, state: SourceState) -> PollResult:
        """Cheap change detection. Must not download the payload unless it moved."""

    def index(self, raw: bytes) -> SurfaceIndex:
        """Flatten a ground-truth payload into {SurfaceRef: SurfaceDescriptor}.

        Raises:
            NotGroundTruthError: for narrative-only sources.
        """

class IndexDiffer(Protocol):
    def diff(self, before: SurfaceIndex, after: SurfaceIndex) -> list[RawDelta]: ...

# --- Understand ------------------------------------------------------------

class EvidenceRetriever(Protocol):
    def retrieve(self, delta: RawDelta, window_days: int = 30) -> list[Evidence]:
        """Find changelog/deprecation spans that mention this surface."""

class ChangeClassifier(Protocol):
    def classify(self, deltas: list[RawDelta], evidence: list[Evidence]) -> list[ChangeRecord]:
        """LLM-backed. Must return schema-valid records, each citing >=1 evidence item."""

# --- Match -----------------------------------------------------------------

class DependencyScanner(Protocol):
    def scan(self, repo_root: Path) -> list[RepoDependency]: ...

class CandidateGenerator(Protocol):
    def generate(self, repo_root: Path, probes: list[str]) -> list[CandidateFile]: ...

class SymbolResolver(Protocol):
    language: str
    def resolve(self, src: bytes, surface_map: SurfaceMapView) -> list[ResolvedUsage]:
        """Parse one file and resolve call chains to SurfaceRefs."""

class ConfidenceScorer(Protocol):
    def score(self, usage: ResolvedUsage, change: ChangeRecord,
              dep: RepoDependency | None) -> ScoredFinding: ...

# --- Fix -------------------------------------------------------------------

class Codemod(Protocol):
    name: str
    change_types: frozenset[str]
    languages: frozenset[str]
    def plan(self, finding: Finding, recipe: MigrationRecipe,
             src: bytes) -> list[Edit]:
        """Return byte-range splices. Never reformat. Never round-trip the AST."""

class SandboxRunner(Protocol):
    def run(self, repo_root: Path, phase: str, command: list[str],
            allow_network: bool) -> SandboxResult: ...

The sandbox runs each verification command in a disposable repository copy, captures stdout
and stderr, enforces a timeout, and records the explicit network policy in its result.
Verification never mutates the caller's checkout.

# --- Act -------------------------------------------------------------------

class Publisher(Protocol):
    def publish(self, decision: GateDecision, patch: Patch | None) -> PublishedArtifact:
        """Idempotent on (repo_id, change_id, kind): updates, never duplicates."""
```

### Controller draft

The first HTTP-facing controller is intentionally framework-neutral:

```python
class ScanController:
    def submit_scan(self, principal: AuthenticatedPrincipal, request: ScanRequest) -> ScanResponse:
        """Authorize the repository and enqueue work; do not run the scan inline."""

    def dashboard(self, principal: AuthenticatedPrincipal) -> list[ImpactSummary]:
        """Use the verified principal's account; never accept account_id from the client."""
```

The authentication dependency must validate Auth0 access tokens with RS256, the exact issuer,
the exact API audience, expiration, and a rotating JWKS key before constructing
`AuthenticatedPrincipal`. `account_id` and filesystem paths are not public request fields.
The repository authorization service resolves `repository_id` to a tenant-owned workspace and
rejects cross-account access. An eventual FastAPI handler should only deserialize the request,
invoke the authentication dependency and controller, and map `ScanResponse` or raised
validation errors to HTTP status codes. Queue workers, database queries, provider polling,
matching, and publishing remain outside the handler.

The repository authorization service queries `repo` by both repository ID and the
authenticated account, requires an active record, and derives the checkout workspace beneath
a server-configured workspace root. Client-supplied filesystem paths are rejected by design.

### 1.1 Gate decision

The single function that decides what we are allowed to do. Pure, table-driven, and
exhaustively unit-tested — it is the component that protects user trust.

```python
def decide(
    finding: Finding, change: ChangeRecord, patch: Patch | None, setting: RepoSetting
) -> GateDecision:
    """Map (confidence, change type, patch provenance, test result) to an action.

    Never returns a stronger action than `setting.action_mode` permits.
    `default_changed` never returns a PR action regardless of confidence, because a
    silent behavioural change requires a human to decide what the right behaviour is.
    """
```

---

## 2. LLM contracts

Two calls only. Both use structured output with a Pydantic v2 model, validate the response,
retry twice on validation failure, then quarantine. Neither has tools or network access.

### 2.1 `understand` — classify a delta cluster

**Inputs:** the deterministic `RawDelta` records, the before/after `SurfaceDescriptor`s, and
retrieved changelog spans. The prompt states plainly that the change has *already been
detected*; the model's job is interpretation, not discovery.

**Output schema:**

```python
class MigrationRecipeOut(BaseModel):
    kind: Literal[
        "rename_param",
        "rename_response_field",
        "replace_model_id",
        "replace_enum_value",
        "operation_moved",
        "insert_required_param",
        "manual_only",
    ]
    params: dict[str, Json]  # {"from": "source", "to": "payment_method"}
    language: Literal["python", "typescript"] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = None


class EvidenceCitation(BaseModel):
    kind: Literal["raw_delta", "document_span"]
    ref: str  # raw_delta fingerprint or document url
    quote: str = Field(min_length=8)  # must appear verbatim in the input


class ChangeRecordOut(BaseModel):
    change_type: ChangeType  # the closed enum from dataModel.sql
    severity: Literal["breaking", "behavioral", "deprecating", "deadline", "additive"]
    surface_refs: list[str] = Field(min_length=1)
    title: str = Field(max_length=120)
    summary: str  # 2-4 sentences, plain language, no marketing
    announced_on: date | None = None
    effective_on: date | None = None
    deadline_on: date | None = None
    enforcement: Literal["hard", "soft", "unknown"] = "unknown"
    code_probes: list[str] = Field(min_length=1)  # literal strings to ripgrep
    recipes: list[MigrationRecipeOut] = []
    evidence: list[EvidenceCitation] = Field(min_length=1)
```

**Post-validation guards, applied in code, not trusted to the model:**

1. Every `evidence[].quote` must appear verbatim in the provided input. A quote that does not
   is a fabrication → quarantine.
2. Every `surface_refs[]` entry must exist in the `after` or `before` index → quarantine.
3. `deadline_on` must be in the future and within 5 years → drop the field, keep the record.
4. `change_type` must be consistent with the delta's `changed_attrs` (a `param_renamed` with
   no removed+added param pair is rejected).

Guard 1 is the one that matters most: it is a cheap, mechanical hallucination detector that
needs no second model call.

### 2.2 `fix` — patch code a codemod cannot handle

**Inputs:** the change summary, the recipe, the target file (full text), and the specific
line range. **No other repo files**, to keep the context small and the blast radius bounded.

**Output schema:**

```python
class ProposedEdit(BaseModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    replacement: str
    rationale: str


class FixOut(BaseModel):
    edits: list[ProposedEdit] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)
    unresolved: list[str] = []  # what it could NOT fix; surfaced in the PR body
```

The model returns edits, never a rewritten file. A full-file rewrite cannot be reviewed and
quietly reformats everything. Patches from this path are capped at `pr_draft`.

### 2.3 Injection containment

Repo source and scraped HTML are attacker-influenceable. Containment:

- All untrusted text is passed in clearly delimited data blocks, and the system prompt states
  that content inside them is never an instruction.
- Neither call has tools, network, filesystem, or database access.
- Output is a closed-schema object, so the worst case is a bad diff, not an action.
- A bad diff still has to pass syntax, typecheck, and tests, and then a human merge.

---

## 3. HTTP surface (FastAPI)

Thin handlers, service layer below, Pydantic v2 models throughout. All tenant routes scoped
by `account_id` from the session; scoping is enforced in the data-access layer and tested.

### 3.1 Public change feed (unauthenticated — this is the marketing surface)

```
GET  /v1/changes                          ?provider=stripe&severity=breaking&since=2026-09-01
GET  /v1/changes/{change_id}              full record + evidence + recipes
GET  /v1/providers                        supported APIs and coverage status
GET  /v1/feed.rss                         severity>=deprecating, drives the content flywheel
```

```http
GET /v1/changes?provider=stripe&severity=breaking&limit=2
```
```json
{
  "items": [
    {
      "id": "3f2a...",
      "provider": "stripe",
      "change_type": "param_renamed",
      "severity": "breaking",
      "title": "charges.create: `source` replaced by `payment_method`",
      "summary": "Stripe removed the `source` parameter ...",
      "surface_refs": ["api:stripe/op:post_v1_charges/param:source"],
      "deadline_on": "2027-03-31",
      "enforcement": "hard",
      "evidence": [
        {"kind": "document_span", "url": "https://docs.stripe.com/changelog/...",
         "quote": "The source parameter is no longer supported"}
      ],
      "recipes": [{"kind": "rename_param", "params": {"from": "source", "to": "payment_method"}}]
    }
  ],
  "next_cursor": "eyJ..."
}
```

### 3.2 Dashboard (session auth)

```
GET    /v1/me                             account, plan, usage
GET    /v1/repos                          installed repos + scan status
GET    /v1/repos/{id}/findings            ?status=open&min_confidence=0.5
GET    /v1/repos/{id}/timeline            upcoming deadlines, from v_upcoming_impact
PATCH  /v1/repos/{id}/settings            action_mode, min_severity, ignores
POST   /v1/repos/{id}/scan                manual rescan  -> 202 + scan_id
GET    /v1/scans/{id}                     progress
GET    /v1/findings/{id}                  detail: snippet, evidence, proposed diff
POST   /v1/findings/{id}/dismiss          {reason} -> feeds calibration
POST   /v1/findings/{id}/fix              force a fix attempt -> 202
GET    /v1/precision                      measured precision per change_type
```

`GET /v1/precision` is deliberately public to the customer: we show the measured numbers
before asking anyone to let us open real PRs.

### 3.3 Webhooks and ops

```
POST /v1/webhooks/github                  HMAC-SHA256 verified, 10s budget, enqueue-and-ack
GET  /healthz                             liveness
GET  /readyz                              db + redis + queue depth
GET  /metrics                             prometheus
```

Webhook handling: verify signature, persist the raw event, enqueue, return 202 immediately.
Never do work in the request. Events consumed: `installation`,
`installation_repositories`, `push`, `pull_request` (feeds `pr_outcome`), `issue_comment`
(chat-ops).

### 3.4 Status codes

| Code | Use |
| --- | --- |
| 200 | read success |
| 202 | work enqueued, body carries the job/scan id |
| 400 | schema validation failure (FastAPI default shape) |
| 401 / 403 | unauthenticated / wrong account |
| 404 | not found, **or** found but not in caller's account (no existence leak) |
| 409 | conflicting state, e.g. scan already running |
| 422 | semantically invalid, e.g. unknown provider slug |
| 429 | rate limited, `Retry-After` set |

---

## 4. Job kinds

| `job.kind` | Payload | Idempotency key |
| --- | --- | --- |
| `poll_source` | `{source_id}` | `poll:{source_id}:{bucket_15min}` |
| `build_index` | `{snapshot_id}` | `index:{snapshot_id}` |
| `diff_snapshots` | `{from_id, to_id}` | `diff:{from_sha}:{to_sha}` |
| `understand_deltas` | `{provider_id, delta_ids}` | `understand:{cluster_hash}` |
| `build_surface_map` | `{provider_id, language, sdk_version}` | `map:{provider}:{lang}:{ver}` |
| `fanout_scans` | `{change_id}` | `fanout:{change_fingerprint}` |
| `scan_repo` | `{repo_id, change_ids, commit_sha}` | `scan:{repo_id}:{commit_sha}:{change_set_hash}` |
| `attempt_fix` | `{finding_ids, repo_id, change_id}` | `fix:{repo_id}:{change_fingerprint}:{attempt_no}` |
| `publish_artifact` | `{fix_attempt_id}` | `publish:{repo_id}:{change_fingerprint}` |
| `send_alert` | `{alert_id}` | `alert:{dedup_key}` |
| `recalibrate_confidence` | `{}` | `recal:{date}` |

Claim query (Postgres-backed queue, no Redis needed for the prototype):

```sql
UPDATE job SET status = 'running', locked_by = $1, locked_at = now(), attempts = attempts + 1
WHERE id IN (
    SELECT id FROM job
    WHERE status = 'pending' AND run_after <= now()
    ORDER BY priority, run_after
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

This is enough to run the whole pipeline on one box. The `Protocol`-shaped queue interface
means swapping to arq/Redis later is a single adapter, so the prototype does not pay for
scale it does not have.
