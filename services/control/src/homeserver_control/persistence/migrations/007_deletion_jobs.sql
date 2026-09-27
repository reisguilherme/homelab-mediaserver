CREATE TABLE IF NOT EXISTS deletion_jobs (
  item_id TEXT PRIMARY KEY,
  item_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'queued',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deletion_jobs_created_at
ON deletion_jobs(created_at);
