-- Driftwatch data model (Postgres 15+)
-- Design reference: docs/systemDesign.md
--
-- Conventions:
--   * surrogate PKs are uuid, generated with gen_random_uuid() (pgcrypto)
--   * every tenant-scoped table carries account_id for row-level isolation
--   * natural dedup keys are content hashes, enforced with UNIQUE constraints
--   * timestamps are timestamptz, named *_at
--   * enums are CHECK constraints rather than pg types, so adding a variant is a
--     non-blocking migration instead of an ALTER TYPE

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- CATALOG: providers, sources, raw snapshots
-- ============================================================================

CREATE TABLE api_provider (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL UNIQUE,          -- 'stripe', 'openai', 'razorpay'
    display_name    text NOT NULL,
    homepage_url    text,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- One provider has many sources: a GA spec, a changelog, an SDK repo, ...
CREATE TABLE api_source (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES api_provider(id) ON DELETE CASCADE,
    kind            text NOT NULL,
    url             text NOT NULL,
    -- adapter-specific config: github repo/path/ref, css selectors, sdk language, ...
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- is this a ground-truth source we build a surface index from, or narrative only?
    is_ground_truth boolean NOT NULL DEFAULT false,
    poll_interval_s integer NOT NULL DEFAULT 900,  -- 15 min; see SYSTEM_DESIGN 4.2
    -- cheap-change-detection state: last seen commit sha / etag / last-modified
    watermark       jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_polled_at  timestamptz,
    last_ok_at      timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT api_source_kind_ck CHECK (kind IN (
        'openapi', 'graphql_sdl', 'sdk_source',
        'changelog_html', 'changelog_rss', 'deprecation_table'
    )),
    CONSTRAINT api_source_url_uq UNIQUE (provider_id, kind, url)
);

-- Immutable blob of a ground-truth source at a point in time. Body lives in S3.
CREATE TABLE spec_snapshot (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       uuid NOT NULL REFERENCES api_source(id) ON DELETE CASCADE,
    sha256          text NOT NULL,
    storage_uri     text NOT NULL,                 -- s3://bucket/key
    byte_size       bigint NOT NULL,
    upstream_ref    text,                          -- git sha, release tag, etag
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    -- flattened SurfaceIndex, also in S3 (can be large); kept separate from the raw blob
    index_uri       text,
    index_built_at  timestamptz,
    surface_count   integer,
    CONSTRAINT spec_snapshot_sha_uq UNIQUE (source_id, sha256)
);
CREATE INDEX spec_snapshot_source_time_ix ON spec_snapshot (source_id, fetched_at DESC);

-- Narrative sources: a changelog entry, a deprecation-notice page section.
CREATE TABLE source_document (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       uuid NOT NULL REFERENCES api_source(id) ON DELETE CASCADE,
    url             text NOT NULL,
    title           text,
    published_at    timestamptz,                   -- as stated upstream
    content_text    text NOT NULL,                 -- extracted plaintext/markdown
    content_sha256  text NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_document_sha_uq UNIQUE (source_id, content_sha256)
);
CREATE INDEX source_document_published_ix ON source_document (source_id, published_at DESC);
-- trigram index drives evidence retrieval: find changelog spans mentioning a surface name
CREATE INDEX source_document_text_trgm_ix ON source_document USING gin (content_text gin_trgm_ops);

-- Named upstream API versions: '2025-03-31.basil', 'v2', '2025-01' (Shopify).
CREATE TABLE api_version (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES api_provider(id) ON DELETE CASCADE,
    label           text NOT NULL,
    released_on     date,
    sunset_on       date,                           -- when it stops working
    is_breaking     boolean NOT NULL DEFAULT false, -- Stripe: twice-yearly named releases
    notes           text,
    CONSTRAINT api_version_uq UNIQUE (provider_id, label)
);

-- ============================================================================
-- CHANGES: deterministic deltas, then LLM-classified records
-- ============================================================================

-- Output of the index differ. Purely mechanical, no LLM involvement.
CREATE TABLE raw_delta (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES api_provider(id) ON DELETE CASCADE,
    from_snapshot_id uuid REFERENCES spec_snapshot(id) ON DELETE SET NULL,
    to_snapshot_id  uuid NOT NULL REFERENCES spec_snapshot(id) ON DELETE CASCADE,
    surface_ref     text NOT NULL,                 -- api:stripe/op:post_v1_charges/param:source
    op              text NOT NULL,                 -- added | removed | modified
    before          jsonb,                         -- SurfaceDescriptor
    after           jsonb,
    -- which descriptor attributes moved: ['required','default']
    changed_attrs   text[] NOT NULL DEFAULT '{}',
    source_locator  text,                          -- JSON pointer, for evidence citation
    fingerprint     text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT raw_delta_op_ck CHECK (op IN ('added', 'removed', 'modified')),
    CONSTRAINT raw_delta_fp_uq UNIQUE (fingerprint)
);
CREATE INDEX raw_delta_surface_ix ON raw_delta (provider_id, surface_ref);
CREATE INDEX raw_delta_to_snapshot_ix ON raw_delta (to_snapshot_id);

-- The structured, human-meaningful change. One per material delta (or per cluster).
CREATE TABLE change_record (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES api_provider(id) ON DELETE CASCADE,
    change_type     text NOT NULL,
    severity        text NOT NULL,
    -- primary surface plus every surface this change touches (denormalised for matching)
    surface_ref     text NOT NULL,
    surface_refs    text[] NOT NULL DEFAULT '{}',
    from_version_id uuid REFERENCES api_version(id) ON DELETE SET NULL,
    to_version_id   uuid REFERENCES api_version(id) ON DELETE SET NULL,
    title           text NOT NULL,                 -- one line, human
    summary         text NOT NULL,                 -- LLM narration, grounded in evidence
    -- deadlines
    announced_on    date,
    effective_on    date,
    deadline_on     date,                          -- hard cutoff, drives urgency
    enforcement     text,                          -- hard | soft | unknown
    -- literal probes for ripgrep candidate generation (SYSTEM_DESIGN 6, stage 1)
    code_probes     text[] NOT NULL DEFAULT '{}',
    -- review state: nothing without evidence is ever published
    review_state    text NOT NULL DEFAULT 'pending',
    reviewed_by     text,
    reviewed_at     timestamptz,
    is_published    boolean NOT NULL DEFAULT false,
    fingerprint     text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT change_record_type_ck CHECK (change_type IN (
        'operation_added', 'operation_removed', 'operation_moved', 'operation_deprecated',
        'param_added_optional', 'param_added_required', 'param_removed', 'param_renamed',
        'param_type_changed', 'param_deprecated',
        'response_field_removed', 'response_field_renamed', 'response_field_type_changed',
        'enum_value_added', 'enum_value_removed',
        'default_changed', 'model_deprecated', 'auth_scheme_changed',
        'error_code_changed', 'rate_limit_changed',
        'webhook_event_added', 'webhook_event_removed',
        'sdk_signature_changed', 'version_pin_required'
    )),
    CONSTRAINT change_record_severity_ck CHECK (severity IN (
        'breaking', 'behavioral', 'deprecating', 'deadline', 'additive'
    )),
    CONSTRAINT change_record_review_ck CHECK (review_state IN (
        'pending', 'auto_approved', 'human_approved', 'quarantined', 'rejected'
    )),
    CONSTRAINT change_record_enforcement_ck CHECK (
        enforcement IS NULL OR enforcement IN ('hard', 'soft', 'unknown')
    ),
    CONSTRAINT change_record_fp_uq UNIQUE (fingerprint)
);
CREATE INDEX change_record_feed_ix ON change_record (provider_id, is_published, deadline_on);
CREATE INDEX change_record_surfaces_ix ON change_record USING gin (surface_refs);
CREATE INDEX change_record_probes_ix ON change_record USING gin (code_probes);

-- Mandatory grounding. A change_record with zero evidence rows must be quarantined.
CREATE TABLE change_evidence (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id       uuid NOT NULL REFERENCES change_record(id) ON DELETE CASCADE,
    kind            text NOT NULL,                 -- raw_delta | document_span | api_version
    raw_delta_id    uuid REFERENCES raw_delta(id) ON DELETE SET NULL,
    document_id     uuid REFERENCES source_document(id) ON DELETE SET NULL,
    url             text,
    quote           text,                          -- exact upstream text, for the PR body
    span_start      integer,
    span_end        integer,
    CONSTRAINT change_evidence_kind_ck CHECK (kind IN ('raw_delta', 'document_span', 'api_version')),
    -- each evidence row must actually point at something
    CONSTRAINT change_evidence_target_ck CHECK (
        raw_delta_id IS NOT NULL OR document_id IS NOT NULL OR url IS NOT NULL
    )
);
CREATE INDEX change_evidence_change_ix ON change_evidence (change_id);

-- Machine-executable migration instruction. Drives codemod selection; prose lives in
-- change_record.summary so the two never get confused.
CREATE TABLE migration_recipe (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id       uuid NOT NULL REFERENCES change_record(id) ON DELETE CASCADE,
    kind            text NOT NULL,                 -- rename_param | replace_model_id | ...
    -- e.g. {"from":"source","to":"payment_method","op":"post_v1_charges"}
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    language        text,                          -- null = language-agnostic
    confidence      numeric(4,3) NOT NULL DEFAULT 0.500,
    provenance      text NOT NULL DEFAULT 'llm_proposed',
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT migration_recipe_prov_ck CHECK (provenance IN (
        'llm_proposed', 'human_curated', 'derived'
    )),
    CONSTRAINT migration_recipe_lang_ck CHECK (language IS NULL OR language IN ('python', 'typescript'))
);
CREATE INDEX migration_recipe_change_ix ON migration_recipe (change_id);

-- ============================================================================
-- MAPPING: spec surface <-> code symbol (SYSTEM_DESIGN 5)
-- ============================================================================

CREATE TABLE surface_map (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES api_provider(id) ON DELETE CASCADE,
    language        text NOT NULL,
    sdk_package     text NOT NULL,                 -- 'stripe', 'openai', 'razorpay'
    sdk_version     text NOT NULL,                 -- the SDK release this was derived from
    builder         text NOT NULL,
    entry_count     integer NOT NULL DEFAULT 0,
    built_at        timestamptz NOT NULL DEFAULT now(),
    is_active       boolean NOT NULL DEFAULT true,
    CONSTRAINT surface_map_lang_ck CHECK (language IN ('python', 'typescript')),
    CONSTRAINT surface_map_builder_ck CHECK (builder IN (
        'sdk_source_derived', 'llm_proposed', 'human_curated'
    )),
    CONSTRAINT surface_map_uq UNIQUE (provider_id, language, sdk_package, sdk_version, builder)
);

CREATE TABLE surface_map_entry (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    map_id          uuid NOT NULL REFERENCES surface_map(id) ON DELETE CASCADE,
    surface_ref     text NOT NULL,                 -- api:stripe/op:post_v1_charges
    symbol_ref      text NOT NULL,                 -- py:stripe.ChargeService.create
    -- semver range over the SDK package where this binding holds
    version_range   text NOT NULL DEFAULT '*',
    confidence      numeric(4,3) NOT NULL,
    -- where in the SDK source this was read from, for debuggability
    source_locator  text,
    reviewed_by     text,
    CONSTRAINT surface_map_entry_uq UNIQUE (map_id, surface_ref, symbol_ref)
);
CREATE INDEX surface_map_entry_surface_ix ON surface_map_entry (surface_ref);
CREATE INDEX surface_map_entry_symbol_ix ON surface_map_entry (symbol_ref);

-- ============================================================================
-- TENANCY: accounts, installations, repos
-- ============================================================================

CREATE TABLE account (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL UNIQUE,
    display_name    text NOT NULL,
    plan            text NOT NULL DEFAULT 'free',
    -- data-handling preferences surfaced in the dashboard (SYSTEM_DESIGN 10)
    retain_snippets boolean NOT NULL DEFAULT true,
    allow_llm_on_code boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_plan_ck CHECK (plan IN ('free', 'pro', 'team', 'agency'))
);

CREATE TABLE installation (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    provider        text NOT NULL DEFAULT 'github',
    -- we store only the installation id; tokens are minted per job, never persisted
    external_id     text NOT NULL,
    external_login  text,
    suspended_at    timestamptz,
    installed_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT installation_uq UNIQUE (provider, external_id)
);

CREATE TABLE repo (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    installation_id uuid NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    external_id     text NOT NULL,                 -- github repo node id
    full_name       text NOT NULL,                 -- 'acme/payments'
    default_branch  text NOT NULL DEFAULT 'main',
    is_private      boolean NOT NULL DEFAULT true,
    is_active       boolean NOT NULL DEFAULT true,
    -- cached from the last scan, drives the stage-0 prefilter
    languages       text[] NOT NULL DEFAULT '{}',
    has_tests       boolean,
    test_runner     text,                          -- pytest | jest | vitest | null
    last_scanned_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT repo_uq UNIQUE (installation_id, external_id)
);
CREATE INDEX repo_account_ix ON repo (account_id, is_active);

-- Stage-0 prefilter index: which repos depend on which provider SDK, at what version.
CREATE TABLE repo_dependency (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         uuid NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    provider_id     uuid REFERENCES api_provider(id) ON DELETE SET NULL,
    ecosystem       text NOT NULL,                 -- pypi | npm
    package_name    text NOT NULL,
    version_spec    text,                          -- as declared: '>=8,<13'
    resolved_version text,                         -- from the lockfile, if present
    manifest_path   text NOT NULL,
    -- true when the repo calls the API over raw HTTP with no SDK at all
    is_raw_http     boolean NOT NULL DEFAULT false,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT repo_dependency_uq UNIQUE (repo_id, manifest_path, package_name)
);
CREATE INDEX repo_dependency_provider_ix ON repo_dependency (provider_id, repo_id);

CREATE TABLE repo_setting (
    repo_id         uuid PRIMARY KEY REFERENCES repo(id) ON DELETE CASCADE,
    -- the trust ladder (SYSTEM_DESIGN 7.4)
    action_mode     text NOT NULL DEFAULT 'issue_with_diff',
    min_severity    text NOT NULL DEFAULT 'deprecating',
    auto_promote_at timestamptz,                   -- when the 2-week probation ends
    max_open_prs    integer NOT NULL DEFAULT 3,
    ignored_changes text[] NOT NULL DEFAULT '{}',  -- change fingerprints
    ignored_paths   text[] NOT NULL DEFAULT '{}',  -- globs: vendor/**, tests/fixtures/**
    CONSTRAINT repo_setting_mode_ck CHECK (action_mode IN (
        'off', 'alert_only', 'issue_with_diff', 'pr_draft', 'pr_ready'
    ))
);

CREATE TABLE notification_channel (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    kind            text NOT NULL,                 -- slack | email | webhook
    target          text NOT NULL,                 -- encrypted at rest
    min_severity    text NOT NULL DEFAULT 'breaking',
    is_active       boolean NOT NULL DEFAULT true,
    CONSTRAINT notification_channel_kind_ck CHECK (kind IN ('slack', 'email', 'webhook'))
);

-- ============================================================================
-- RESULTS: scans and findings
-- ============================================================================

CREATE TABLE repo_scan (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         uuid NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    commit_sha      text NOT NULL,
    trigger         text NOT NULL,                 -- scheduled | new_change | push | manual
    status          text NOT NULL DEFAULT 'queued',
    changes_considered integer NOT NULL DEFAULT 0,
    files_parsed    integer NOT NULL DEFAULT 0,
    findings_count  integer NOT NULL DEFAULT 0,
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    CONSTRAINT repo_scan_status_ck CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'skipped_no_dependency'
    ))
);
CREATE INDEX repo_scan_repo_ix ON repo_scan (repo_id, started_at DESC);

CREATE TABLE finding (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    repo_id         uuid NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    scan_id         uuid NOT NULL REFERENCES repo_scan(id) ON DELETE CASCADE,
    change_id       uuid NOT NULL REFERENCES change_record(id) ON DELETE CASCADE,
    -- location
    file_path       text NOT NULL,
    start_line      integer NOT NULL,
    end_line        integer NOT NULL,
    start_byte      integer,
    end_byte        integer,
    -- what matched
    match_kind      text NOT NULL,
    symbol_ref      text,                          -- resolved SymbolRef, if any
    snippet         text,                          -- nullable: account.retain_snippets
    -- hash of the NORMALISED snippet, so reformatting does not create a duplicate
    anchor_hash     text NOT NULL,
    -- scoring (SYSTEM_DESIGN 6, stage 3)
    confidence      numeric(4,3) NOT NULL,
    score_factors   jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'open',
    fingerprint     text NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    CONSTRAINT finding_match_kind_ck CHECK (match_kind IN (
        'operation_call', 'param_keyword', 'param_dict_key', 'response_attribute',
        'response_subscript', 'string_literal', 'raw_http_path', 'version_pin', 'text_only'
    )),
    CONSTRAINT finding_status_ck CHECK (status IN (
        'open', 'fixed', 'dismissed', 'snoozed', 'stale', 'false_positive'
    )),
    -- the anti-spam invariant (SYSTEM_DESIGN 8.2)
    CONSTRAINT finding_fp_uq UNIQUE (fingerprint)
);
CREATE INDEX finding_repo_status_ix ON finding (repo_id, status, confidence DESC);
CREATE INDEX finding_change_ix ON finding (change_id);
CREATE INDEX finding_account_ix ON finding (account_id, status);

-- Supporting matches that raised or lowered confidence, kept for debugging the matcher.
CREATE TABLE finding_evidence (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id      uuid NOT NULL REFERENCES finding(id) ON DELETE CASCADE,
    kind            text NOT NULL,                 -- import | alias | client_binding | assignment
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    file_path       text,
    line            integer
);
CREATE INDEX finding_evidence_finding_ix ON finding_evidence (finding_id);

-- ============================================================================
-- REMEDIATION: patches, sandbox runs, published artifacts
-- ============================================================================

CREATE TABLE fix_attempt (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         uuid NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    change_id       uuid NOT NULL REFERENCES change_record(id) ON DELETE CASCADE,
    strategy        text NOT NULL,                 -- codemod | llm_agent | hybrid
    codemod_name    text,
    status          text NOT NULL DEFAULT 'queued',
    -- gating decision and why (SYSTEM_DESIGN 7.4)
    decided_action  text,
    decision_reason text,
    confidence      numeric(4,3),
    attempt_no      integer NOT NULL DEFAULT 1,
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    CONSTRAINT fix_attempt_strategy_ck CHECK (strategy IN ('codemod', 'llm_agent', 'hybrid')),
    CONSTRAINT fix_attempt_status_ck CHECK (status IN (
        'queued', 'running', 'patched', 'verified', 'rejected', 'failed', 'abandoned'
    )),
    CONSTRAINT fix_attempt_action_ck CHECK (decided_action IS NULL OR decided_action IN (
        'alert_only', 'issue_with_diff', 'pr_draft', 'pr_ready'
    ))
);
CREATE INDEX fix_attempt_repo_change_ix ON fix_attempt (repo_id, change_id, attempt_no DESC);

CREATE TABLE patch (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fix_attempt_id  uuid NOT NULL REFERENCES fix_attempt(id) ON DELETE CASCADE,
    diff_uri        text NOT NULL,                 -- unified diff in S3
    diff_sha256     text NOT NULL,
    files_changed   integer NOT NULL,
    lines_added     integer NOT NULL,
    lines_removed   integer NOT NULL,
    -- byte-splice edits actually applied, for audit (SYSTEM_DESIGN 7.1)
    edits           jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- verification ladder results
    syntax_ok       boolean,
    typecheck_ok    boolean,
    lint_ok         boolean,
    tests_ok        boolean,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX patch_attempt_ix ON patch (fix_attempt_id);

CREATE TABLE sandbox_run (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patch_id        uuid NOT NULL REFERENCES patch(id) ON DELETE CASCADE,
    phase           text NOT NULL,                 -- install | test (SYSTEM_DESIGN 7.3)
    image           text NOT NULL,
    command         text NOT NULL,
    exit_code       integer,
    duration_ms     integer,
    network_enabled boolean NOT NULL,              -- must be false for phase='test'
    log_uri         text,
    tests_passed    integer,
    tests_failed    integer,
    timed_out       boolean NOT NULL DEFAULT false,
    ran_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sandbox_run_phase_ck CHECK (phase IN ('install', 'test', 'typecheck', 'lint')),
    -- enforce the isolation rule in the schema, not just in code
    CONSTRAINT sandbox_run_test_no_network_ck CHECK (
        phase <> 'test' OR network_enabled = false
    )
);
CREATE INDEX sandbox_run_patch_ix ON sandbox_run (patch_id);

-- A PR, issue, or alert we actually created upstream. Keyed for idempotent updates.
CREATE TABLE published_artifact (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id         uuid NOT NULL REFERENCES repo(id) ON DELETE CASCADE,
    change_id       uuid NOT NULL REFERENCES change_record(id) ON DELETE CASCADE,
    fix_attempt_id  uuid REFERENCES fix_attempt(id) ON DELETE SET NULL,
    kind            text NOT NULL,                 -- pull_request | issue
    external_number integer,
    external_url    text,
    branch_name     text,
    state           text NOT NULL DEFAULT 'open',  -- open | merged | closed | draft
    is_draft        boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT published_artifact_kind_ck CHECK (kind IN ('pull_request', 'issue')),
    -- one artifact per (repo, change): a re-scan updates it instead of opening another
    CONSTRAINT published_artifact_uq UNIQUE (repo_id, change_id, kind)
);
CREATE INDEX published_artifact_state_ix ON published_artifact (state, updated_at DESC);

-- ============================================================================
-- OPS AND LEARNING
-- ============================================================================

CREATE TABLE job (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,                 -- poll_source | diff_snapshot | ...
    -- dedup key: enqueueing the same logical job twice is a no-op
    idempotency_key text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT job_idempotency_uq UNIQUE (idempotency_key)
);

-- Hot queue state is isolated from the stable identity and large payload.
CREATE TABLE job_state (
    job_id          uuid PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    priority        integer NOT NULL DEFAULT 100,
    status          text NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 3,
    run_after       timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    locked_at       timestamptz,
    CONSTRAINT job_state_status_ck CHECK (status IN (
        'pending', 'running', 'succeeded', 'failed', 'dead'
    ))
);
-- supports the SELECT ... FOR UPDATE SKIP LOCKED claim query
CREATE INDEX job_state_claim_ix ON job_state (status, run_after, priority)
    WHERE status = 'pending';

CREATE TABLE job_payload (
    job_id          uuid PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE job_run (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    attempt_no      integer NOT NULL,
    worker_id       text NOT NULL,
    exit_status     text NOT NULL,
    duration_ms     integer,
    error           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX job_run_job_ix ON job_run (job_id, attempt_no);

-- Cost attribution: needed to know whether unit economics actually hold.
CREATE TABLE llm_call (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose         text NOT NULL,                 -- understand | fix | pr_body | map_propose
    model           text NOT NULL,
    account_id      uuid REFERENCES account(id) ON DELETE SET NULL,
    change_id       uuid REFERENCES change_record(id) ON DELETE SET NULL,
    fix_attempt_id  uuid REFERENCES fix_attempt(id) ON DELETE SET NULL,
    input_tokens    integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    cost_usd        numeric(10,6) NOT NULL DEFAULT 0,
    latency_ms      integer,
    schema_valid    boolean,                       -- did structured output validate?
    retries         integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_call_cost_ix ON llm_call (created_at DESC, purpose);
CREATE INDEX llm_call_account_ix ON llm_call (account_id, created_at DESC);

-- The feedback loop that turns `confidence` from a guess into a measurement.
CREATE TABLE pr_outcome (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id     uuid NOT NULL REFERENCES published_artifact(id) ON DELETE CASCADE,
    change_id       uuid NOT NULL REFERENCES change_record(id) ON DELETE CASCADE,
    change_type     text NOT NULL,                 -- denormalised for calibration queries
    predicted_confidence numeric(4,3) NOT NULL,
    outcome         text NOT NULL,
    human_edited    boolean NOT NULL DEFAULT false,
    edit_lines      integer,
    time_to_outcome_s integer,
    comment_text    text,                          -- why it was closed, if they said
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pr_outcome_ck CHECK (outcome IN (
        'merged_clean', 'merged_with_edits', 'closed_unwanted',
        'closed_wrong', 'superseded', 'stale'
    ))
);
CREATE INDEX pr_outcome_calibration_ix ON pr_outcome (change_type, predicted_confidence);

CREATE TABLE alert (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    channel_id      uuid REFERENCES notification_channel(id) ON DELETE SET NULL,
    change_id       uuid REFERENCES change_record(id) ON DELETE CASCADE,
    repo_id         uuid REFERENCES repo(id) ON DELETE CASCADE,
    severity        text NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    dedup_key       text NOT NULL,
    sent_at         timestamptz,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT alert_status_ck CHECK (status IN ('pending', 'sent', 'failed', 'suppressed')),
    CONSTRAINT alert_dedup_uq UNIQUE (dedup_key)
);
CREATE INDEX alert_pending_ix ON alert (status, created_at) WHERE status = 'pending';

CREATE TABLE audit_log (
    id              bigserial PRIMARY KEY,
    account_id      uuid REFERENCES account(id) ON DELETE SET NULL,
    actor           text NOT NULL,                 -- user email, 'system', 'github_webhook'
    action          text NOT NULL,
    target_type     text,
    target_id       text,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_account_ix ON audit_log (account_id, created_at DESC);

-- ============================================================================
-- VIEWS
-- ============================================================================

-- Dashboard: upcoming changes ordered by urgency, with blast radius per account.
CREATE VIEW v_upcoming_impact AS
SELECT
    f.account_id,
    cr.id                        AS change_id,
    p.slug                       AS provider,
    cr.change_type,
    cr.severity,
    cr.title,
    cr.deadline_on,
    cr.deadline_on - CURRENT_DATE AS days_remaining,
    count(DISTINCT f.repo_id)    AS repos_affected,
    count(*)                     AS findings_open,
    max(f.confidence)            AS best_confidence
FROM finding f
JOIN change_record cr ON cr.id = f.change_id
JOIN api_provider p   ON p.id = cr.provider_id
WHERE f.status = 'open'
  AND cr.is_published
GROUP BY f.account_id, cr.id, p.slug, cr.change_type, cr.severity, cr.title, cr.deadline_on;

-- Precision measurement per change_type: the input to confidence recalibration,
-- and the number we show a customer before asking them to enable pr_ready.
CREATE VIEW v_change_type_precision AS
SELECT
    change_type,
    count(*) AS outcomes,
    round(avg(CASE WHEN outcome IN ('merged_clean', 'merged_with_edits')
                   THEN 1.0 ELSE 0.0 END), 3) AS merge_rate,
    round(avg(CASE WHEN outcome = 'merged_clean' THEN 1.0 ELSE 0.0 END), 3) AS clean_merge_rate,
    round(avg(predicted_confidence), 3) AS avg_predicted
FROM pr_outcome
GROUP BY change_type;
