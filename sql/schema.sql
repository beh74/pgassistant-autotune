-- Initial schema for new installations; no conversion of previous schemas.
CREATE SCHEMA IF NOT EXISTS autotune;
CREATE TABLE IF NOT EXISTS autotune.runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_id text NOT NULL,
    request jsonb NOT NULL,
    cutoff timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    snapshot_run_id uuid,
    snapshot_collected_at timestamptz,
    error text
);
CREATE INDEX IF NOT EXISTS runs_queue ON autotune.runs(status, created_at);
CREATE TABLE IF NOT EXISTS autotune.actions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES autotune.runs(id),
    target_id text NOT NULL,
    recommendation_id bigint NOT NULL,
    action_fingerprint text NOT NULL,
    advisor_id text,
    recommendation jsonb NOT NULL,
    proposed_sql text,
    executed_sql text,
    target_before jsonb,
    action_type text,
    execution_backend text NOT NULL DEFAULT 'postgresql',
    external_operation_id text,
    execution_key text NOT NULL,
    status text NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    execution_ms double precision,
    error text
);
CREATE INDEX IF NOT EXISTS actions_history ON autotune.actions(target_id, started_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS actions_execution_key_once
ON autotune.actions(target_id, execution_key)
WHERE status IN ('running', 'succeeded', 'failed', 'unknown');

CREATE TABLE IF NOT EXISTS autotune.action_steps (
    action_id bigint NOT NULL REFERENCES autotune.actions(id),
    step_number integer NOT NULL,
    sql text NOT NULL,
    status text NOT NULL DEFAULT 'planned',
    started_at timestamptz,
    finished_at timestamptz,
    execution_ms double precision,
    error text,
    PRIMARY KEY (action_id, step_number)
);
