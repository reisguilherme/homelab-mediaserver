CREATE TABLE IF NOT EXISTS event_cursors (
  consumer_id TEXT PRIMARY KEY,
  acknowledged_id INTEGER NOT NULL CHECK(acknowledged_id >= 0),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_state_available ON outbox(state, available_at, id);
