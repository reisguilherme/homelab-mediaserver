PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL UNIQUE,
  media_key TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES requests(id),
  media_key TEXT NOT NULL UNIQUE,
  filesystem_id TEXT NOT NULL,
  budget_bytes INTEGER NOT NULL CHECK(budget_bytes >= 0),
  allocated_bytes INTEGER NOT NULL DEFAULT 0 CHECK(allocated_bytes >= 0),
  state TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tombstones (
  media_key TEXT PRIMARY KEY,
  deleted_at TEXT NOT NULL,
  source_generation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  generation TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  UNIQUE(event_type, object_id, generation)
);

CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_reservations_state ON reservations(state);
CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state, updated_at);
