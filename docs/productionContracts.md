# Driftwatch — Production Wiring Contracts

This document is the fillable contract for capabilities that are intentionally deferred
until the product research phase is complete. It defines what each implementation must
provide without committing the project to a provider, deployment topology, or billing
design prematurely.

See [Scope Boundries.md](./Scope%20Boundries.md) for the current implementation limitations
and intentionally deferred behavior.

The local development and CI deployment target is **fake GCP/gcloud**: interfaces must be
usable with deterministic in-memory or local emulators first, and no implementation may
require a real GCP project, paid API, or production credential during tests.

The backend implementation language is Python. Provider-specific ingestion, parsing, and
indexing will extend the existing Python ports-and-adapters boundaries rather than
introducing a separate .NET service.

## Contract status

Each section has four states:

- `research`: requirements or provider details are still being confirmed.
- `stubbed`: the interface and fake implementation exist.
- `integrated`: the real adapter is connected to the application boundary.
- `verified`: integration tests and operational checks pass.

No section may be marked `integrated` or `verified` by tests that only exercise a mock.

## Shared implementation rules

Every production adapter MUST:

1. expose a typed Python protocol at the application boundary;
2. have a deterministic fake implementation for unit and integration tests;
3. validate configuration at startup and fail closed for missing security settings;
4. apply explicit timeouts, retry limits, and error categories;
5. avoid logging credentials, access tokens, request bodies containing secrets, or
   customer source code;
6. propagate a correlation ID and tenant/account ID from the authenticated request or job;
7. be idempotent where an operation can be retried;
8. document the external API version and credential ownership;
9. provide a health/readiness check that does not expose secrets;
10. include a contract test that can run with fake GCP/gcloud only.

## 1. OpenAI model client

**Status:** `stubbed`

The fake-first client boundary is implemented in
[`src/Driftwatch/Services/ModelClient.py`](../src/Driftwatch/Services/ModelClient.py).
It supports bounded OpenAI-compatible JSON requests and a deterministic scripted client;
the real API key, selected model, and production endpoint remain deployment configuration.

### Boundary

```python
class ModelClient(Protocol):
    def classify(
        self,
        *,
        prompt: str,
        evidence: list[Evidence],
        deadline: datetime | None,
        correlation_id: str,
    ) -> ModelClassification:
        """Return schema-validated, evidence-grounded model output."""
```

### Required behavior

- Use structured output validated by the existing classifier models.
- Preserve the rule that deterministic detection controls change type and severity.
- Reject unsupported citations and quarantine malformed output.
- Enforce token, input-size, timeout, and retry budgets.
- Prevent customer code or evidence from being sent when the account policy disallows it.
- Record model name, API version, latency, token usage, and an opaque request ID.
- Keep provider credentials outside source control and outside job payloads.

### Fake contract

The fake client returns scripted classifications and scripted failures. Tests MUST be able
to assert timeout, malformed output, rate limit, and provider-unavailable behavior without
network access.

## 2. Provider-specific OpenAI and Razorpay adapters

**Status:** `stubbed`

OpenAI and Razorpay polling adapters are implemented in
[`src/Driftwatch/Watch/ProviderAdapters.py`](../src/Driftwatch/Watch/ProviderAdapters.py).
They bind provider metadata to the shared conditional HTTP poller and do not yet parse
provider payloads into surface indexes; source-specific indexing remains the next research
step for each provider.

The first indexing slice is implemented in
[`src/Driftwatch/Watch/PayloadIndexers.py`](../src/Driftwatch/Watch/PayloadIndexers.py):
OpenAI OpenAPI JSON is normalized through the canonical indexer, and Razorpay Python source
emits only statically provable HTTP operations. Raw payload persistence and index status are
defined in `migrations/003ProviderPayloads.sql`; applying that migration requires the
provider catalog tables from `docs/dataModel.sql`.

Durable persistence and queue processing are implemented in
[`src/Driftwatch/Services/SourceIngestion.py`](../src/Driftwatch/Services/SourceIngestion.py).
`SourceSnapshotRepository.ingest()` stores bytes before parsing and deduplicates by
`(source_id, content_sha256)`. `SourceIndexWorker` claims `index_source_snapshot` jobs,
persists normalized entries in `source_snapshot_index`, marks successful snapshots as
`built`, and quarantines parser failures without deleting the raw payload. The additional
index table is defined in `migrations/004SourceIndex.sql`.

`SourcePollingCoordinator.poll_once()` now connects adapters to this durable path. It loads
the persisted watermark, records changed/unchanged/failed poll attempts, ingests changed
bytes, and enqueues an idempotent `index_source_snapshot` job. The provider catalog tables
from `docs/dataModel.sql` must be installed before migrations `003ProviderPayloads.sql` and
`004SourceIndex.sql`; the current CI queue-only database does not yet provision that full
catalog.

CI now provisions the catalog and ingestion schema in this order:
`001JobQueue.sql`, `002NormalizeJobQueue.sql`, `003ProviderCatalog.sql`,
`003ProviderPayloads.sql`, and `004SourceIndex.sql`. The PostgreSQL integration test covers
polling a seeded Razorpay source, deduplicating the raw snapshot, enqueueing the indexing
job, and persisting a built normalized index.

### Boundary

```python
class ProviderAdapter(Protocol):
    provider: str

    def poll(self, state: SourceState) -> PollResult:
        """Use provider-specific source rules and watermarks."""

    def index(self, raw: bytes) -> SurfaceIndex:
        """Produce the shared normalized surface contract."""
```

OpenAI and Razorpay adapters MUST preserve the common `SourceAdapter` output. Provider
quirks belong inside the adapter and must not leak into differ, evidence, matcher, or
publisher code.

### Required behavior

- Declare source URLs, SDK repositories, package names, authentication requirements, and
  rate-limit policy.
- Use ETag/Last-Modified and content hashes where supported.
- Distinguish ground-truth sources from narrative-only sources.
- Keep raw provider responses bounded and retain source locators for evidence.
- Fail explicitly on authentication, throttling, malformed payload, and source drift.

## 3. FastAPI HTTP application

**Status:** `stubbed`

The dependency-injected application factory is implemented in
[`src/Driftwatch/Http/Application.py`](../src/Driftwatch/Http/Application.py). It is
deliberately not a production server entry point yet: deployment configuration, database
readiness, rate limits, public change-feed routes, and webhook handling remain open.

### Boundary

```python
class HttpApplication(Protocol):
    """Thin HTTP layer around authenticated controllers."""
```

The HTTP layer MUST only deserialize requests, authenticate them, call controllers, and map
typed errors to HTTP responses. It MUST NOT perform provider polling, matching, patching,
publishing, or direct queue SQL.

### Required routes to define

| Route | Contract |
| --- | --- |
| `POST /v1/scans` | authenticated, tenant-authorized enqueue |
| `GET /v1/dashboard/impact` | authenticated account-scoped read |
| `GET /healthz` | process health only; no dependency secrets |
| `GET /readyz` | dependency readiness with bounded checks |
| `POST /v1/webhooks/github` | raw-body signature verification before parsing |

The final request and response schemas, CORS policy, trusted hosts, body limits, and rate
limits remain research decisions.

## 4. Authentication and authorization

**Status:** `stubbed`

The current Auth0 boundary is the reference contract:

- validate RS256, exact issuer, exact audience, expiration, issued-at, subject, and JWKS;
- derive account identity from verified claims;
- resolve repositories by authenticated account ownership;
- never accept account IDs or filesystem paths as trusted client input;
- map authentication failures to `401` and authorization failures to `403` without
  disclosing tenant existence.

Remaining research fields:

| Field | Decision |
| --- | --- |
| Auth0 issuer | `____________________________` |
| API audience | `____________________________` |
| Account claim / organization strategy | `____________________________` |
| Required scopes | `____________________________` |
| Role model | `____________________________` |

## 5. GitHub App installation-token flow

**Status:** `research`

### Boundary

```python
class GitHubTokenProvider(Protocol):
    def installation_token(
        self, installation_id: str, correlation_id: str
    ) -> GitHubInstallationToken:
        """Mint a short-lived installation token; never persist the token."""
```

The implementation MUST verify installation and repository ownership before use, request
least-privilege permissions, cache tokens only until shortly before expiry, and store the
App private key through a secret manager abstraction.

## 6. Slack, email, and webhook delivery

**Status:** `research`

### Boundary

```python
class AlertDelivery(Protocol):
    def deliver(self, alert: Alert, destination: Destination) -> DeliveryResult:
        """Deliver once or return a typed retryable/permanent failure."""
```

Delivery MUST be deduplicated by the existing alert key, bounded by retry/backoff policy,
and isolated from scan workers. Webhook delivery requires a signing secret, timestamped
signatures, replay protection, and delivery status tracking.

## 7. Stripe Billing checkout and webhooks

**Status:** `research`

### Boundary

```python
class BillingProvider(Protocol):
    def create_checkout(self, request: CheckoutRequest) -> CheckoutSession: ...
    def consume_webhook(self, raw_body: bytes, signature: str) -> BillingEvent: ...
```

Billing MUST verify Stripe signatures over the exact raw body, make webhook processing
idempotent, update usage limits transactionally, and never trust plan or entitlement values
from the browser.

## 8. Production sandbox isolation

**Status:** `research`

The current disposable-directory runner is a development boundary, not a production
security boundary. A production runner MUST define:

- container or OS isolation technology;
- non-root execution and read-only base filesystem;
- CPU, memory, process, disk, and wall-clock limits;
- default-deny network policy;
- workspace and secret mounts;
- cleanup behavior after timeout or crash;
- an audit record for command, policy, result, and artifact hashes.

The fake runner MUST simulate timeout, non-zero exit, network denial, and cleanup.

## 9. Database-backed dashboard and billing workflows

**Status:** `research`

Every workflow MUST specify its owner tables, transaction boundary, indexes, retention
policy, tenant predicate, and retry behavior. Read models may be denormalized, but account
scope and entitlement changes MUST remain derived from authoritative records.

Required unresolved decisions:

| Workflow | Authoritative tables | Retention | Owner |
| --- | --- | --- | --- |
| Dashboard impact | `________________` | `________________` | `________________` |
| Alert delivery | `________________` | `________________` | `________________` |
| Usage metering | `________________` | `________________` | `________________` |
| Billing entitlements | `________________` | `________________` | `________________` |

## 10. Fake GCP/gcloud deployment contract

**Status:** `stubbed`

Local development MUST use fake implementations for:

- Secret Manager;
- Cloud Run or container execution;
- Pub/Sub or job dispatch;
- Cloud Storage;
- service-account identity;
- logging and tracing export.

The fake interfaces MUST preserve authentication, permissions, retries, resource names,
and failure semantics. They MUST NOT silently bypass authorization or turn unavailable
services into successful operations.

Deployment configuration placeholders:

```text
GCP_PROJECT_ID=fake-driftwatch
GCP_REGION=fake-region
GCP_SERVICE_ACCOUNT=fake-driftwatch-runtime
GCP_SECRET_BACKEND=fake
GCP_QUEUE_BACKEND=fake
GCP_STORAGE_BACKEND=fake
```

When real gcloud is introduced, it is a separate implementation of the same protocols.
Production credentials, project IDs, service-account keys, and secrets MUST NOT be added to
the repository or required by the default test command.

## Definition of ready

This contract is complete for a capability only when:

- its research fields are filled;
- its typed protocol and fake implementation exist;
- unit tests cover success, malformed input, timeout, retry, and authorization failure;
- an integration test runs without real external credentials;
- the real adapter is explicitly enabled by configuration;
- operational ownership, secret storage, observability, and rollback behavior are documented.
