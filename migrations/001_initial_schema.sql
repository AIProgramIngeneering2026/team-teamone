-- 001_initial_schema.sql
-- Начальная схема БД CampusSpot: помещения, заявки, история проверок и ключи идемпотентности.

CREATE TABLE spaces (
    space_id TEXT PRIMARY KEY,
    category TEXT NOT NULL CHECK (
        category IN ('coworking', 'project_zone', 'classroom', 'lab', 'conference_hall')
    ),
    name TEXT NOT NULL
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('student', 'group_lead', 'teacher', 'special_staff', 'admin')
    ),
    space_id TEXT NOT NULL,
    space_category TEXT NOT NULL CHECK (
        space_category IN ('coworking', 'project_zone', 'classroom', 'lab', 'conference_hall')
    ),
    start_time_iso TEXT NOT NULL,
    end_time_iso TEXT NOT NULL,
    start_time_utc TEXT NOT NULL,
    end_time_utc TEXT NOT NULL,
    actual_end_time_iso TEXT,
    actual_end_time_utc TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('REQUESTED', 'CONFIRMED', 'USED', 'CANCELLED', 'EXPIRED')
    ),
    extra_json TEXT NOT NULL,
    required_checks_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_jobs_space_status_time
    ON jobs (space_id, status, start_time_utc, end_time_utc);

CREATE INDEX idx_jobs_user_status
    ON jobs (user_id, status);

CREATE TABLE job_check_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    tool_name TEXT NOT NULL,
    error_code TEXT,
    message TEXT,
    details_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (job_id, seq)
);

CREATE TABLE idempotency_keys (
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_resource_id TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    job_id TEXT,
    response_status INTEGER NOT NULL,
    response_body_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
);
