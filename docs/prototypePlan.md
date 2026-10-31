# Driftwatch — Build Plan

Two horizons: a de-risking spike to run immediately, and the 6–8 week MVP.

---

## Part 1 — The spike (today, ~2 hours)

### Principle: spike the unknowns, not the knowns

A tempting first session builds the GitHub App, Postgres schema, and a dashboard. That would
be a mistake. Those are *known-solvable* — thousands of people have built them, and none of
them can fail in a way that kills the product.

Four things genuinely can:

| # | Risky assumption | If false |
| --- | --- | --- |
| A1 | The endpoint→SDK-symbol→field map is derivable from SDK source | The Surface Map needs hand-curation per API per language. Breadth moat gone, scope explodes |
| A2 | Semantic surface-index diffing of an 8.3 MB spec is fast and low-noise | Daily diffs drown in phantom changes; alert quality collapses |
| A3 | tree-sitter resolution of real-world call chains has usable precision | Findings are mostly false positives; nobody trusts a PR |
| A4 | Byte-splice codemods produce reviewably-small diffs | PRs get closed unread |

So the spike builds exactly one vertical slice through those four, and **stubs everything
else**: no database (SQLite or JSON on disk), no GitHub App, no LLM, no sandbox, no web UI.
`ChangeRecord`s are hand-written fixtures where the LLM would produce them — which also gives
us the golden-test corpus we will need later anyway.

### Target slice

```
stripe-python source  ──(tree-sitter)──>  surface_map.json          [A1]
spec3.json @ T1, T2   ──(indexer+differ)──>  deltas.json            [A2]
fixture repo + deltas ──(resolver)──────>  findings.json            [A3]
findings + recipe     ──(byte-splice)───>  patch.diff               [A4]
```

### Steps

**S0 — Scaffold (10 min).** `uv` project, Python 3.12. Deps:
`tree-sitter`, `tree-sitter-python`, `tree-sitter-typescript`, `pydantic`, `httpx`, `typer`,
`orjson`, `pytest`. Package layout mirroring the design seams:

```
Driftwatch/
  surfaces/     SurfaceRef, SurfaceDescriptor, SurfaceIndex
  Watch/        OpenapiIndexer.py, Differ.py, adapters/
  Mapping/      SdkPython.py, SdkTypescript.py
  Match/        Prefilter.py, Candidates.py, Resolver.py, Scorer.py
  fix/          codemods/, splice.py
  cli.py
tests/fixtures/
```

**S1 — Surface Map deriver [A1] (~30 min).** Parse `stripe-python` with tree-sitter. For each
`class *Service`, walk methods, find the `self._request("post", "/v1/charges")` literal pair,
emit `api:stripe/op:post_v1_charges ↔ py:stripe.ChargeService.create`. Then parse
`stripe/params/_*_params.py` `TypedDict`s for the param-level surface. Also emit legacy
`py:stripe.Charge.create` aliases, since plenty of real code still uses the static style.

> **Gate A1:** ≥ 300 operation bindings derived, and 10 hand-checked bindings are correct.
> Below ~100, the deriver is missing a code pattern and needs a look before continuing.

**S2 — Index + differ [A2] (~30 min).** Download `spec3.json` at two commits from
`stripe/openapi` history. Build the flattened index ($ref resolution, `allOf` flattening,
enum sorting, `oneOf` ordering normalised). Set-diff. Classify each delta into the
`change_type` taxonomy mechanically.

> **Gate A2:** index build < 10 s for 8.3 MB; diff of two adjacent commits yields a
> *reviewably small* delta set with **zero** cosmetic/phantom entries. Phantom diffs mean
> normalisation is incomplete — fix before moving on, because every downstream stage
> amplifies this noise.

**S3 — Matcher [A3] (~35 min).** Fixture repo with ~8 files covering the real shapes:
`import stripe`, `import stripe as pay`, `client = StripeClient(...)`, legacy
`stripe.Charge.create`, a response-attribute read, a raw `requests.post` to
`api.stripe.com`, a `model="gpt-4-..."` literal, plus **two deliberate near-miss decoys**
(a local variable named `charges`, an unrelated `source=` kwarg on a different function).
Implement prefilter → ripgrep candidates → tree-sitter resolver → scorer.

> **Gate A3:** all true positives found, **both decoys rejected**. The decoys are the whole
> point of this gate: a matcher that finds everything including the wrong things is worse
> than no matcher.

**S4 — Codemod [A4] (~25 min).** Implement `rename_param` and `replace_model_id` as
byte-splice edits. Apply, reparse, assert zero `ERROR` nodes, emit a unified diff.

> **Gate A4:** diff touches only the intended lines, byte-identical elsewhere (whitespace,
> comments, import order all untouched), and the patched fixture still parses.

**S5 — CLI + report (10 min).**

```
driftwatch derive-map  --sdk stripe-python --out surface_map.json
driftwatch diff-spec   --provider stripe --from <sha> --to <sha> --out deltas.json
driftwatch scan        --repo ./tests/fixtures/sample_repo --changes deltas.json
driftwatch fix         --finding <id> --out patch.diff
```

### Spike exit criteria

All four gates green, with the numbers written down. Then we know whether the architecture in
`systemDesign.md` survives contact with real data — and if a gate fails, we have learned the
single most valuable thing available today, cheaply.

Explicitly **not** in the spike: Postgres, FastAPI, GitHub App, LLM calls, Docker sandbox,
dashboard, auth, billing.

---

## Part 2 — MVP (6–8 weeks)

Each week ends with something demonstrable. Ordering is driven by risk and by the fact that
distribution (public-repo scanning) must come *before* billing.

**Week 1 — Foundations.** Spike code hardened into the package layout. Postgres +
`dataModel.sql` via Alembic. Postgres-backed job queue (`FOR UPDATE SKIP LOCKED`). FastAPI
skeleton with `/healthz`, `/readyz`, `/v1/providers`. Docker Compose for local dev.
*Demo:* `poll_source` → `build_index` → `diff_snapshots` runs on a schedule and persists.

**Week 2 — Watch, for real, all three providers.** Source adapters: Stripe OpenAPI (GA spec
only), OpenAI OpenAPI + `ChatModel` literal diffing, Razorpay SDK-source + changelog HTML
with canary assertions. 15-minute SHA-watermark polling.
*Demo:* a real upstream change detected within 15 minutes of the provider pushing it.

**Week 3 — Understand.** Evidence retriever (trigram search over `source_document`).
LLM classifier with the §2.1 contract, Pydantic validation, verbatim-quote guard,
quarantine path. `llm_call` cost tracking. Build the **golden corpus**: ~40 historical real
changes with hand-labelled expected `ChangeRecord`s, as a regression suite.
*Demo:* `/v1/changes` serving real, evidence-cited records; classifier scored against golden.

**Week 4 — Match at scale.** Full matcher with the TypeScript resolver. Surface Map derivation
jobs per SDK release, with derived-vs-curated disagreement alerts. Scan fan-out.
*Demo:* scan 20 real public GitHub repos using Stripe; hand-audit precision. **This is the
most important number in the project** — if precision on real code is below ~0.8, stop and fix
the matcher rather than building the fix engine on sand.

**Week 5 — Fix + sandbox.** Codemod suite (6 kinds). Two-phase Docker sandbox with the
network rules from §7.3. Verification ladder. LLM patch agent as fallback. Gate decision
function with exhaustive unit tests.
*Demo:* a verified patch with a passing test run, on a real repo.

**Week 6 — Act.** GitHub App: install flow, per-job tokens, branch/commit/PR, issue fallback,
chat-ops, `pull_request` webhook → `pr_outcome`. PR body template with evidence links.
Idempotent publishing.
*Demo:* a real PR on a real public repo, merged by a stranger.

**Week 7 — Dashboard + distribution.** FastAPI + Jinja2 + HTMX (no build step, fastest path):
upcoming-changes timeline, per-repo findings, finding detail with diff, settings, the public
`/v1/precision` page. RSS feed. Generate the first *"Stripe API changes this month that affect
you"* post from the pipeline.
*Demo:* public change feed live; first content post published.

**Week 8 — Trust, then billing.** Confidence recalibration from `pr_outcome`. Trust-ladder
auto-promotion with consent. Alert channels (Slack, email). Rate limits, audit log, tenant
isolation tests. Stripe billing (pleasingly recursive). Security pass on the sandbox.
*Demo:* a paying private repo at `pr_ready`, with its measured precision on screen.

### Sequencing rules

1. **Public repos before billing.** Free public scanning is both distribution and the only
   honest test of whether the PRs are good. Charge after the merge rate is known.
2. **Golden corpus before the LLM is load-bearing.** Without it, prompt changes are
   superstition.
3. **Precision gate before the fix engine.** A fix engine on a bad matcher is wasted work.
4. **`alert_only`/`issue_with_diff` as the default** until measured precision earns promotion.

### Pre-code task

The competitor scan from §12 of the design — `oasdiff`, Optic, codemod.com, OpenRewrite,
Semgrep, Grit, Sourcegraph Batch Changes. Specifically: does anything already do the *join*
(spec delta → surface map → your repo → verified patch)? And is Semgrep a better matching
backend than a hand-built tree-sitter resolver? That second question could remove a week of
Week 4, so it is worth 30 minutes before Week 4 starts — though not before the spike, since
the spike answers whether the hard parts work at all.
