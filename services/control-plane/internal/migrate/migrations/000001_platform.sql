CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL,
    email_normalized TEXT GENERATED ALWAYS AS (lower(email)) STORED,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'expired', 'deleted')),
    provider TEXT NOT NULL DEFAULT 'openai',
    secret_ref TEXT,
    encrypted_credentials BYTEA,
    credential_key_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS accounts_email_normalized_uq ON accounts (email_normalized);
CREATE INDEX IF NOT EXISTS accounts_status_created_idx ON accounts (status, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS registration_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'partial')),
    requested_count INTEGER NOT NULL DEFAULT 0 CHECK (requested_count >= 0),
    queued_count INTEGER NOT NULL DEFAULT 0 CHECK (queued_count >= 0),
    running_count INTEGER NOT NULL DEFAULT 0 CHECK (running_count >= 0),
    succeeded_count INTEGER NOT NULL DEFAULT 0 CHECK (succeeded_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS registration_batches_status_created_idx
    ON registration_batches (status, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS registration_batches_completed_idx
    ON registration_batches (completed_at DESC, id DESC)
    WHERE completed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'draining', 'offline')),
    version TEXT,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(capabilities) = 'object'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS workers_status_heartbeat_idx ON workers (status, last_seen_at DESC, id);

CREATE TABLE IF NOT EXISTS registration_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID REFERENCES registration_batches(id) ON DELETE SET NULL,
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'reserved', 'running', 'stopping', 'succeeded', 'failed', 'cancelled')),
    priority SMALLINT NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 1 CHECK (max_attempts >= 1),
    request JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(request) = 'object'),
    result JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result) = 'object'),
    error_code TEXT,
    error_message TEXT,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS registration_jobs_idempotency_uq
    ON registration_jobs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS registration_jobs_batch_status_idx
    ON registration_jobs (batch_id, status, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS registration_jobs_status_priority_idx
    ON registration_jobs (status, priority DESC, created_at ASC, id ASC)
    WHERE status IN ('queued', 'reserved', 'running', 'stopping');
CREATE INDEX IF NOT EXISTS registration_jobs_worker_status_idx
    ON registration_jobs (worker_id, status, updated_at DESC)
    WHERE worker_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS registration_jobs_terminal_retention_idx
    ON registration_jobs (completed_at DESC, id DESC)
    WHERE status IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS job_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES registration_jobs(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence >= 0),
    level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('debug', 'info', 'success', 'warning', 'error')),
    stage TEXT,
    message TEXT NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(attributes) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS job_events_job_sequence_uq ON job_events (job_id, sequence);
CREATE INDEX IF NOT EXISTS job_events_job_created_idx ON job_events (job_id, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS job_events_created_idx ON job_events (created_at DESC, id DESC);

CREATE OR REPLACE FUNCTION set_platform_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accounts_set_updated_at ON accounts;
CREATE TRIGGER accounts_set_updated_at BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION set_platform_updated_at();
DROP TRIGGER IF EXISTS registration_batches_set_updated_at ON registration_batches;
CREATE TRIGGER registration_batches_set_updated_at BEFORE UPDATE ON registration_batches
    FOR EACH ROW EXECUTE FUNCTION set_platform_updated_at();
DROP TRIGGER IF EXISTS workers_set_updated_at ON workers;
CREATE TRIGGER workers_set_updated_at BEFORE UPDATE ON workers
    FOR EACH ROW EXECUTE FUNCTION set_platform_updated_at();
DROP TRIGGER IF EXISTS registration_jobs_set_updated_at ON registration_jobs;
CREATE TRIGGER registration_jobs_set_updated_at BEFORE UPDATE ON registration_jobs
    FOR EACH ROW EXECUTE FUNCTION set_platform_updated_at();
