# Driftwatch — Scope Boundaries

This document records limitations that are intentional and must not be interpreted as
completed production behavior.

## Implementation language decision

Provider-specific ingestion, validation, parsing, and indexing will remain inside the
existing Python application. A separate .NET service is not part of the current
architecture. Any future implementation must use the existing Python domain contracts,
PostgreSQL persistence, queue services, and fake GCP/gcloud boundaries.

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
