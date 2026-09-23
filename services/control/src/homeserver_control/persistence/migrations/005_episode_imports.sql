CREATE TABLE IF NOT EXISTS episode_imports (
  permit_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  command_id TEXT,
  updated_at TEXT NOT NULL
);
