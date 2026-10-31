-- Upgrade databases created by the original single-table queue migration.
CREATE TABLE IF NOT EXISTS job_state (
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

CREATE TABLE IF NOT EXISTS job_payload (
    job_id          uuid PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'job' AND column_name = 'payload'
    ) THEN
        INSERT INTO job_state (
            job_id, priority, status, attempts, max_attempts, run_after, locked_by, locked_at
        )
        SELECT id, priority, status, attempts, max_attempts, run_after, locked_by, locked_at
        FROM job
        ON CONFLICT (job_id) DO NOTHING;

        INSERT INTO job_payload (job_id, payload)
        SELECT id, payload FROM job
        ON CONFLICT (job_id) DO NOTHING;

        ALTER TABLE job DROP CONSTRAINT IF EXISTS job_status_ck;
        ALTER TABLE job DROP COLUMN IF EXISTS payload;
        ALTER TABLE job DROP COLUMN IF EXISTS priority;
        ALTER TABLE job DROP COLUMN IF EXISTS status;
        ALTER TABLE job DROP COLUMN IF EXISTS attempts;
        ALTER TABLE job DROP COLUMN IF EXISTS max_attempts;
        ALTER TABLE job DROP COLUMN IF EXISTS run_after;
        ALTER TABLE job DROP COLUMN IF EXISTS locked_by;
        ALTER TABLE job DROP COLUMN IF EXISTS locked_at;
    END IF;
END
$$;

DROP INDEX IF EXISTS job_claim_ix;
CREATE INDEX IF NOT EXISTS job_state_claim_ix
    ON job_state (status, run_after, priority)
    WHERE status = 'pending';
