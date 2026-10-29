# Driftwatch

> Working name. "Dependabot for API changes": detect upstream API drift, find the code it
> breaks, and ship a verified patch.

Dependabot answers *"is there a newer version of this library?"* Driftwatch answers the
question that actually causes incidents: *"upstream changed their API — does my code care,
and what exactly do I change?"*

**Status: de-risking spike complete, all four gates passed.** See
[`docs/GATE_RESULTS.md`](docs/GATE_RESULTS.md) for measurements against live Stripe data.

## Pipeline

```
WATCH      poll sources -> flatten to a surface index -> semantic set-diff   (deterministic)
UNDERSTAND classify severity, deadline, migration recipe                     (LLM, constrained)
MATCH      dependency prefilter -> ripgrep candidates -> tree-sitter resolve (deterministic)
FIX        codemod first, LLM fallback, verify in a sandbox
ACT        pull request, issue, or alert — gated by measured confidence
```

Detection is deterministic; the LLM only narrates and proposes. It is never asked *"did
something change?"* — that is computed by set-diffing surface indexes. One hallucinated
breaking change costs more trust than ten correct ones earn.

## Quick start

```bash
uv sync
./scripts/fetch_fixtures.sh    # stripe-python + three real spec revisions (~60 MB, cached)
uv run pytest                  # 71 tests
```

Run the pipeline end to end:

```bash
# 1. Derive the endpoint -> code-symbol map from SDK source, cross-checked against the spec
uv run driftwatch derive-map --sdk-root .cache/stripe-python \
                             --out .cache/surface_map.json --spec .cache/spec_T2.json

# 2. Diff two real revisions of Stripe's OpenAPI document into classified changes
uv run driftwatch diff-spec --before .cache/spec_T0.json --after .cache/spec_T2.json \
                            --out .cache/deltas.json

# 3. Find affected code
uv run driftwatch scan --repo tests/fixtures/sample_repo --changes .cache/deltas.json \
                       --surface-map .cache/surface_map.json --out .cache/findings.json

# 4. Generate a minimal patch
uv run driftwatch fix --repo tests/fixtures/sample_repo \
                      --findings .cache/findings.json --out .cache/patch.diff
```

Step 2 finds 63 material changes across a month of real Stripe releases. Step 3 narrows them
to the one line in the fixture repository that actually breaks. Step 4 emits a +1/−1 diff.

## Layout

```
src/driftwatch/
  surfaces.py        SurfaceRef / SurfaceDescriptor / SurfaceIndex — the shared vocabulary
  tsutil.py          tree-sitter helpers
  watch/             openapi_indexer.py (normalise), differ.py (set-diff + classify)
  mapping/           sdk_python.py — derive endpoint <-> symbol bindings from SDK source
  match/             prefilter, candidates, resolver, scorer, engine
  fix/               splice.py (byte-range edits), codemods.py
  cli.py
  services/          application use cases, independent of presentation
  views/             Rich console presentation adapters
docs/
  SYSTEM_DESIGN.md   architecture, change taxonomy, trust ladder, security, cost
  DATA_MODEL.sql     30 tables, verified against Postgres 16
  API_CONTRACT.md    pipeline interfaces, LLM schemas, HTTP surface, job kinds
  PROTOTYPE_PLAN.md  the spike, then the 6-8 week MVP
  GATE_RESULTS.md    spike measurements
```

## Design decisions worth knowing

- **The Surface Map is derived, not curated.** Provider SDKs are generated from their specs,
  so the HTTP path sits in the source beside each method. Parsing it removes the hand-curation
  bottleneck, which is what makes broad API coverage affordable.
- **Surface index, not document diff.** OpenAPI, GraphQL SDL and SDK source all flatten to
  `{SurfaceRef: SurfaceDescriptor}`, so one differ and one matcher serve every provider.
- **Codemods splice bytes; they never re-print a syntax tree.** An AST round-trip reformats
  the file and buries a two-line change in hundreds, which is how automated pull requests get
  closed unread.
- **Silent behavioural changes are first-class, and never auto-fixed.** A changed default is
  the most valuable thing to detect and the least safe thing to patch automatically.
- **Trust is earned.** New installations open issues, not pull requests, and promotion
  requires measured precision.

## Application structure

The command-line entry point is a controller: it validates options and delegates to
`PipelineService`. Domain models and deterministic pipeline stages remain framework-neutral,
while `ConsoleView` owns Rich formatting. This keeps the MVC boundaries explicit:

- **Models:** surface indexes, deltas, maps, findings, and scan results.
- **Services:** orchestration of indexing, diffing, matching, and fixing.
- **Controllers:** CLI commands (and future HTTP handlers) that call services.
- **Views:** console tables and status output.

New functionality should depend inward on domain models and service protocols. Services should
not import Typer or Rich, and controllers should not implement matching or diffing logic.
Python code uses four-space indentation, a 100-character line limit, and Ruff rules `E`, `F`,
`I`, `UP`, and `B`; format and lint checks run against `src/` and `tests/` only.
