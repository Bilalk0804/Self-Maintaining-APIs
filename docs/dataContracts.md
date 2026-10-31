# Driftwatch — Data Contracts

This document defines the data that moves through Driftwatch. It is intentionally separate
from [pipelineContracts.md](./pipelineContracts.md), which defines who produces and
consumes each value.

## End-to-end data flow

```text
ProviderSource
  -> SourceSnapshot
  -> SurfaceIndex { SurfaceRef: SurfaceDescriptor }
  -> RawDelta
  -> Evidence
  -> ChangeRecord + MigrationRecipe
  -> SurfaceMap
  -> ResolvedUsage
  -> Finding
  -> Patch
  -> VerificationResult
  -> PublishedArtifact
```

## Canonical identifiers

Every provider uses the same surface-reference grammar:

```text
api:<provider>/<kind>:<id>[/<subkind>:<subid>]
```

Examples:

```text
api:paypal/op:post_v2_checkout_orders
api:paypal/op:post_v2_checkout_orders/param:paypal_request_id
api:paypal/obj:order/field:status
api:stripe/op:post_v1_charges/param:source
```

The provider slug is part of every identifier. A PayPal `order` can therefore never collide
with a Stripe `order`.

## Contract records

| Record | Producer | Consumer | Required invariants |
|---|---|---|---|
| `ProviderSource` | provider registry | poller | `kind`, URL, and ground-truth flag are explicit |
| `SurfaceDescriptor` | source indexer | differ | only caller-visible shape is compared |
| `RawDelta` | differ | evidence/classifier | deterministic fingerprint; no LLM fields |
| `Evidence` | retriever | classifier/publisher | non-empty verbatim quote, source URL for provider evidence |
| `ChangeRecord` | classifier | matcher/API | deterministic type/severity, valid surface, and grounded evidence |
| `MigrationRecipe` | classifier/human | codemod | closed kind plus JSON parameters |
| `SurfaceMapEntry` | SDK adapter | resolver | provenance and confidence are recorded |
| `Finding` | matcher | fix/gate | repository location, surface, score, and fingerprint |
| `Patch` | codemod/LLM fallback | verifier/publisher | bounded edits and diff hash |
| `VerificationResult` | sandbox | gate | phase, command, exit status, and network policy |
| `PublishedArtifact` | publisher | dashboard/API | idempotent `(repo, change, kind)` key and GitHub URL |

## PayPal provider contract

Evidence records contain a deterministic `raw_delta` quote plus zero or more
`provider_source` quotes. Provider quotes are bounded line windows copied from the fetched
source, with the source URL and line locator retained for review. Retrieval failures and
oversized responses fail explicitly; they are not represented as empty evidence.

The classifier returns a Pydantic-validated `ChangeRecordOut`. It quarantines output when a
quote is not contained in the cited evidence, a surface is not part of the detected delta,
or the model attempts to change the deterministic change type or severity. Expired or
implausibly distant deadlines are removed rather than trusted.

The hackathon integration uses the official:

- Python package: `paypal-server-sdk`
- SDK repository: `paypal/PayPal-Python-Server-SDK`
- Generated import namespace: `paypalserversdk`
- API specifications: `paypal/paypal-rest-api-specifications`

The first supported PayPal surfaces are the generated Orders, Payments, Vault, Transaction
Search, and Subscriptions controllers. The SDK adapter reads controller method paths such as
`/v2/checkout/orders` and their HTTP verbs, then emits the same `SurfaceMap` format used for
Stripe. API changes are indexed from PayPal's REST OpenAPI documents, not inferred from SDK
prose.

## Example PayPal records

```json
{
  "surface_ref": "api:paypal/op:post_v2_checkout_orders",
  "change_type": "param_added_required",
  "severity": "breaking",
  "code_probes": ["/v2/checkout/orders", "create_order", "paypal_request_id"],
  "evidence": [{"kind": "raw_delta", "ref": "delta-sha", "quote": "required changed"}],
  "recipe": {"kind": "insert_required_param", "params": {"name": "paypal_request_id"}}
}
```

```json
{
  "provider": "paypal",
  "language": "python",
  "sdk_package": "paypal-server-sdk",
  "symbol_ref": "py:paypalserversdk.controllers.orders_controller.OrdersController.create_order",
  "surface_ref": "api:paypal/op:post_v2_checkout_orders",
  "provenance": "sdk_source_derived",
  "confidence": 0.95
}
```
