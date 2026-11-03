CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS job (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority        integer NOT NULL DEFAULT 100,
    status          text NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 3,
    run_after       timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    locked_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT job_status_ck CHECK (status IN (
        'pending', 'running', 'succeeded', 'failed', 'dead'
    ))
);

CREATE INDEX IF NOT EXISTS job_claim_ix
    ON job (priority ASC, run_after ASC)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS job_run (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      uuid NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    attempt_no  integer NOT NULL,
    worker_id   text NOT NULL,
    exit_status text NOT NULL,
    error       text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS job_run_job_ix ON job_run (job_id, attempt_no);
