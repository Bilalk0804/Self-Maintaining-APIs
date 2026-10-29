# Spike Results — the four gates

Run 2026-10-10 against live upstream data: `stripe-python` at `master`, and Stripe's GA
OpenAPI document at three real commits of `stripe/openapi`.

**All four gates passed.** The architecture in [`SYSTEM_DESIGN.md`](./SYSTEM_DESIGN.md)
survived contact with real data, and the single assumption it most depends on — that the
endpoint-to-code map is derivable rather than hand-curated — held.

Reproduce with:

```bash
./scripts/fetch_fixtures.sh
uv run pytest                     # 71 tests, the gates included as regressions
uv run driftwatch derive-map --sdk-root .cache/stripe-python --out .cache/surface_map.json \
                             --spec .cache/spec_T2.json
uv run driftwatch diff-spec  --before .cache/spec_T0.json --after .cache/spec_T2.json \
                             --out .cache/deltas.json
uv run driftwatch scan       --repo tests/fixtures/sample_repo --changes .cache/deltas.json \
                             --surface-map .cache/surface_map.json --out .cache/findings.json
uv run driftwatch fix        --repo tests/fixtures/sample_repo \
                             --findings .cache/findings.json --out .cache/patch.diff
```

---

## A1 — The Surface Map is derivable from SDK source

**Target:** ≥300 operations derived, 10 bindings correct by hand.
**Result: 585 operations, 3,888 bindings, in 1.5 s — and validated mechanically, not by hand.**

| Metric | Value |
| --- | --- |
| Operation bindings | 3,888 |
| Distinct operations | 585 |
| Operations with parameter sets | 546 |
| Operations with response types | 318 |
| Derived operations present in the GA spec | **94.5%** |
| GA spec operations covered by the SDK | 90.4% |
| Unexplained mismatches | **0** |
| Runtime | 1.5 s over 1,546 files |

Rather than hand-checking ten bindings, every derived operation is cross-checked against
Stripe's own spec. The remaining 5.5% is fully accounted for: 32 operations are Stripe's
`/v2/` API, which `spec3.json` does not describe at all (it contains zero `v2` paths). That
is a coverage finding — v2 needs its own source — not an error.

All four call styles that appear in real code resolve to the same operation:

```
api:stripe/op:post_v1_charges
    py:stripe.Charge.create            # legacy static resource
    py:stripe.ChargeService.create     # service class
    py:stripe.v1.charges.create        # namespaced client accessor
    py:stripe.charges.create           # top-level client alias
```

### What cross-validation caught

Running the check was worth more than the gate itself. It exposed three defects that would
each have produced *confidently wrong* patches — the worst possible failure mode:

1. **Bare class names collide.** `stripe-python` has three classes named `SessionService`
   (checkout, billing_portal, financial_connections) and two named `Transaction`. Keying on
   the bare name cross-bound them, so `POST /v1/checkout/sessions` resolved to
   `financial_connections.sessions.create` and inherited the wrong parameter set. Fixed by
   keying everything on `(module, class_name)` and reading the SDK's own generated
   `_subservices` map, which names the owning module and so is exact rather than inferred.
2. **`class_url` overrides were ignored**, producing paths like `/v1/balance_settingss`.
   Now such classes derive nothing: a missing binding costs recall, a wrong one costs trust.
3. **v2 resources were nested under `/v1`**, yielding `/v1/v2/core/accounts`.

This is the "derived entries cross-validate curated ones" mechanism from §5.1 of the design,
working on its first run.

---

## A2 — Semantic spec diffing is fast and quiet

**Target:** index in <10 s, zero phantom changes between adjacent revisions.
**Result: 0.17 s to index 7.9 MB; one real change across four days; zero phantoms.**

| Check | Result |
| --- | --- |
| Index build, 7.9 MB → 12,924 surfaces | **0.17 s** (target <10 s) |
| Diff of two 12.9k-surface indexes | 0.02 s |
| Self-diff control (same revision) | **0 deltas** |
| Adjacent revisions, 4 days apart | **1 material change, 0 phantom** |
| One month apart, material | 63 changes |
| One month apart, including additive | 676 changes |
| Additive suppression | **90.7%** |

The 90.7% suppression rate confirms the design's estimate that additive changes are ~90% of
volume. Alerting on them is what would make the product feel like spam, so they are routed
to the content pipeline instead.

The single change detected across four days is itself the argument for this product:

```
[behavioral] enum_value_added
  obj:subscriptions_resource_paused_status_details_subscription/field:type
  gained ['final_payment_failure', 'first_payment_failure']
```

An enum gaining values breaks exhaustive matches and changes behaviour silently. Nothing
4xxs. No changelog entry is required for it. Only a spec diff finds it.

The month-apart diff surfaced genuine breaking changes, including a real rename that the rest
of the pipeline is then tested against:

```
param_renamed  op:post_v1_checkout_sessions/param:payment_method_types
            -> op:post_v1_checkout_sessions/param:allowed_payment_method_types
```

Normalisation is what makes this quiet: `$ref` resolution, `allOf` flattening, sorted enums
and sorted `oneOf` members, and documentation hashed separately from shape. The self-diff and
union-reordering tests guard each of those.

---

## A3 — The matcher finds true positives and rejects near-misses

**Target:** find all true positives, reject both decoys.
**Result: all true positives found; all five decoys rejected; zero false positives.**

The fixture repository contains five real usage shapes and five decoys engineered to fool a
text-based matcher. Running the **real** 63-delta diff against it produces exactly one
finding — the real rename — and nothing else.

| Usage shape | Found |
| --- | --- |
| `client.charges.create(...)` via `StripeClient` | yes |
| `pay.Charge.create(...)` via aliased legacy import | yes |
| `client.checkout.sessions.create(...)` nested accessor | yes |
| `requests.post("https://api.stripe.com/v1/charges", data={...})` raw HTTP | yes |
| `model="gpt-4-vision-preview"` string literal (both occurrences) | yes |
| `charge.source` response field read via assignment tracking | yes |

| Decoy | Rejected |
| --- | --- |
| `send_email(source=...)` — matching kwarg on an unrelated function | yes |
| `billing.charges.create(source=...)` — identical attribute path, in-house class | yes |
| `"gpt-4-vision-preview"` inside a comment | yes |
| local `charge = {"source": ...}` dict, unrelated to any response | yes |
| a function *named* `payment_method_types` | yes |

The decoys matter more than the true positives. Every one of those shapes is more common in
real repositories than the genuine usage is, and a matcher that flags them is unsafe to put
behind a pull request. The root check that rejects all of them is one line: a call's root
identifier must resolve through an import or a client binding before the attribute chain is
even looked up.

Prefilter and scoring also work end to end: the pinned range `>=11.0,<13.0` is recovered, the
`pytest` runner is detected, raw-HTTP usage is detected with no SDK import present, and a
repository depending only on Flask is skipped before any file is parsed.

### Two defects this gate caught

1. **Filtering kwargs by the SDK's parameter list discarded the findings that matter most.**
   A renamed or removed parameter is precisely the one absent from a newer SDK, so the filter
   suppressed every `param_renamed` finding. Membership is now a confidence signal, not a
   filter — and when the change under consideration is that parameter's own rename, the
   absence is treated as expected rather than suspicious (0.838 → 0.931, draft → ready).
2. **One operation-level change fanned out into nine findings**, one per keyword argument. A
   call passing five arguments is one problem, not six. Operation-level changes now match
   call sites only.

---

## A4 — Byte-splice patches are minimal and reviewable

**Target:** diff touches only intended lines; patched file parses.
**Result: +1 / −1 lines, byte-identical elsewhere, valid under `git apply`.**

```diff
--- a/app/checkout.py
+++ b/app/checkout.py
@@ -8,7 +8,7 @@
 def start_checkout(price_id):
     session = client.checkout.sessions.create(
         mode="payment",
-        payment_method_types=["card", "link"],
+        allowed_payment_method_types=["card", "link"],
         line_items=[{"price": price_id, "quantity": 1}],
         success_url="https://example.test/ok",
     )
```

| Check | Result |
| --- | --- |
| Lines changed | +1 / −1 |
| Argument value `["card", "link"]` preserved | byte-identical |
| Whitespace, comments, import order | untouched |
| Patched file reparses with zero `ERROR` nodes | yes |
| `git apply --check` | valid |
| Decoy file referenced in patch | no |

This is the whole argument for splicing bytes instead of re-printing a syntax tree. A reviewer
reads this diff in about five seconds. An AST round-trip would have reformatted the file and
buried the same one-token change in hundreds of lines, which is the fastest way for an
automated pull request to be closed unread.

Also verified: a `param_removed` finding yields **no** codemod and degrades to an issue, which
is the correct outcome — there is no safe mechanical fix for a removal, and the gating function
refuses to invent one.

---

## Test suite

71 tests, 2.5 s, lint and format clean. The gates are encoded as regressions so a later
refactor cannot quietly undo them, and the gating table is tested exhaustively because it is
the component that protects user trust: every confidence threshold, every missing
precondition, the `default_changed` veto, and the repository-mode ceiling.

---

## What this does and does not prove

**Proven on real data.** Surface Map derivation works and is self-validating. Spec diffing is
fast, quiet, and finds real breaking *and* silent behavioural changes. The matcher
distinguishes genuine SDK calls from look-alike code. Codemods produce reviewable diffs.

**Not yet proven.** Everything the spike deliberately stubbed: the LLM understanding layer,
the Docker sandbox, the GitHub App, the database, the dashboard. And three measurements that
only real repositories can give:

1. **Matcher precision on real public repositories.** The fixture decoys are the shapes I
   thought of. Week 4's hand-audit of ~20 real repositories is the honest test, and the
   number to stop on if it comes in below ~0.8.
2. **Pull request merge rate**, the north-star metric.
3. **Coverage for a provider with no spec.** Razorpay exercises the SDK-source-diff adapter,
   which this spike did not touch.

**Known limitations, by choice.**

- The Surface Map is derived from SDK `master`, not from the version each repository pins.
  Production must derive per pinned version; the scorer currently compensates for the gap.
- Response-field resolution is intra-procedural and assignment-only. A field reached through
  a helper's return value is missed — a known recall gap, preferred over silent false positives.
- TypeScript parsers are wired up but the TS resolver is not written; Python only so far.
- `/v2/` operations derive correctly but have no spec to diff against.
