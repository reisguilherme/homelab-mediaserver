CREATE TABLE IF NOT EXISTS movie_imports (
  reservation_id TEXT PRIMARY KEY REFERENCES reservations(id),
  state TEXT NOT NULL,
  command_id TEXT,
  updated_at TEXT NOT NULL
);
