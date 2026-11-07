# Driftwatch — Scope Boundaries

This document records limitations that are intentional and must not be interpreted as
completed production behavior.

## Implementation language decision

Provider-specific ingestion, validation, parsing, and indexing stay inside the
existing Python application. Any future implementation must use the existing Python
domain contracts, PostgreSQL persistence, queue services, and fake GCP/gcloud boundaries.

## Provider source adapters

The OpenAI and Razorpay adapters currently poll and watermark provider sources. They do not
yet perform provider-specific payload indexing.

Provider-specific payload indexing remains explicitly deferred because:

- OpenAI requires OpenAPI and SDK parsing decisions.
- Razorpay has no OpenAPI source and requires SDK-source parsing rules.
- Narrative changelog sources must not be treated as ground truth.

The adapters may identify that a source changed, but a changed source must not enter the
surface-diff pipeline until its payload has been parsed by a source-specific indexer that
produces the shared `SurfaceIndex` contract.

The first Python indexer slice now supports OpenAI OpenAPI JSON and provable Razorpay Python
SDK HTTP call literals. It intentionally quarantines malformed payloads and does not infer
operations from arbitrary prose, HTML, or unrecognized source shapes.

Durable raw snapshot persistence and a queue-backed indexing worker now connect this slice.
The worker marks malformed payloads as quarantined and retains the original bytes; provider
catalog setup and production object storage wiring remain separate deployment tasks.

Polling results are now connected through `SourcePollingCoordinator`: changed payloads are
persisted and enqueue an idempotent indexing job, while unchanged and failed attempts are
recorded separately. Full provider-catalog migration provisioning is still a deployment
task.

The provider catalog is now seeded through `migrations/003ProviderCatalog.sql`, and CI
provisions the catalog plus source-ingestion/index tables before integration tests. Real
object-storage offload and provider-specific production fixtures remain deferred.

## Ground-truth rule

Only sources marked as `ground_truth` may produce authoritative surface data. Narrative
documentation, changelogs, blog posts, and general API documentation may provide supporting
evidence, but they cannot independently create or confirm a detected API change.

## Fake cloud boundary

The current production wiring uses fake GCP/gcloud-compatible interfaces for local
development and CI. Real GCP services, credentials, service accounts, secret stores, and
deployment infrastructure are not included in the default test path.

## Deferred production integrations

The following remain outside the current implementation scope:

- Real provider-specific OpenAI and Razorpay indexers.
- Production OpenAI credentials and model selection.
- FastAPI deployment infrastructure.
- GitHub App installation-token flow.
- Real Slack, email, and webhook delivery.
- Stripe Billing checkout and webhook processing.
- Container or OS-level production sandbox isolation.
- Complete database-backed dashboard and billing workflows.

##MVCC Rules
- MVCC rules should be counter checked after the desgin of the database.