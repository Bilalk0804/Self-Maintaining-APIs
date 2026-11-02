-- Provider source ingestion: immutable raw payloads and explicit indexing state.
CREATE TABLE IF NOT EXISTS api_source_state (
    source_id              uuid PRIMARY KEY REFERENCES api_source(id) ON DELETE CASCADE,
    etag                   text,
    last_modified          text,
    content_sha256         text,
    upstream_ref           text,
    last_polled_at         timestamptz,
    last_changed_at        timestamptz,
    last_ok_at             timestamptz,
    consecutive_failures   integer NOT NULL DEFAULT 0,
    next_poll_at           timestamptz,
    lease_owner            text,
    lease_expires_at       timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT api_source_state_failures_ck CHECK (consecutive_failures >= 0)
);

CREATE TABLE IF NOT EXISTS api_source_poll (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           uuid NOT NULL REFERENCES api_source(id) ON DELETE CASCADE,
    started_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    status              text NOT NULL,
    http_status         integer,
    content_sha256      text,
    byte_size           bigint,
    error_code          text,
    error_message       text,
    correlation_id       text,
    CONSTRAINT api_source_poll_status_ck CHECK (
        status IN ('changed', 'unchanged', 'failed', 'rejected')
    ),
    CONSTRAINT api_source_poll_size_ck CHECK (byte_size IS NULL OR byte_size >= 0)
);
CREATE INDEX IF NOT EXISTS api_source_poll_source_time_ix
    ON api_source_poll (source_id, started_at DESC);

CREATE TABLE IF NOT EXISTS source_snapshot (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           uuid NOT NULL REFERENCES api_source(id) ON DELETE CASCADE,
    content_sha256      text NOT NULL,
    raw_payload         bytea NOT NULL,
    byte_size           bigint NOT NULL,
    content_type        text,
    upstream_ref        text,
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    index_status         text NOT NULL DEFAULT 'pending',
    index_error          text,
    indexed_at           timestamptz,
    index_entry_count    integer,
    CONSTRAINT source_snapshot_sha_uq UNIQUE (source_id, content_sha256),
    CONSTRAINT source_snapshot_size_ck CHECK (byte_size >= 0),
    CONSTRAINT source_snapshot_index_status_ck CHECK (
        index_status IN ('pending', 'built', 'failed', 'quarantined')
    )
);
CREATE INDEX IF NOT EXISTS source_snapshot_pending_ix
    ON source_snapshot (source_id, fetched_at)
    WHERE index_status = 'pending';
