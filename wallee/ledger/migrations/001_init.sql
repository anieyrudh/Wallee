-- Ledger schema: action state machine, approvals, and event log.
-- Applied once on first boot. SQLite WAL mode for crash safety.

PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    params_json TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    device_group TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PROPOSED','WAITING_APPROVAL','DISPATCHED',
        'DONE','FAILED','REJECTED','UNKNOWN'
    )),
    reason TEXT,
    result_json TEXT,
    error_json TEXT,
    created_ts REAL NOT NULL,
    created_mono REAL NOT NULL,
    updated_ts REAL NOT NULL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    max_proposal_age_ms INTEGER NOT NULL DEFAULT 30000
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_device ON actions(device_group, status);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    params_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT')),
    approved_by TEXT NOT NULL,
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('DEBUG','INFO','WARN','ERROR','CRITICAL')),
    action_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT
);
