-- Persist normalized indexes separately from immutable raw source snapshots.
CREATE TABLE IF NOT EXISTS source_snapshot_index (
    snapshot_id      uuid PRIMARY KEY REFERENCES source_snapshot(id) ON DELETE CASCADE,
    provider         text NOT NULL,
    revision         text NOT NULL,
    entries          jsonb NOT NULL,
    entry_count      integer NOT NULL,
    built_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_snapshot_index_count_ck CHECK (entry_count >= 0)
);

CREATE INDEX IF NOT EXISTS source_snapshot_index_provider_ix
    ON source_snapshot_index (provider, built_at DESC);
