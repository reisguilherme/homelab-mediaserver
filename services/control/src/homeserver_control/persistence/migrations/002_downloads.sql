CREATE TABLE IF NOT EXISTS downloads (
  id TEXT PRIMARY KEY,
  request_id TEXT REFERENCES requests(id),
  reservation_id TEXT REFERENCES reservations(id),
  operation_id TEXT REFERENCES operations(id),
  infohash TEXT NOT NULL UNIQUE,
  metadata_sha256 TEXT NOT NULL,
  destination TEXT NOT NULL,
  category TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS download_files (
  download_id TEXT NOT NULL REFERENCES downloads(id),
  relative_path TEXT NOT NULL,
  expected_bytes INTEGER NOT NULL CHECK(expected_bytes > 0),
  selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
  PRIMARY KEY(download_id, relative_path)
);

CREATE TABLE IF NOT EXISTS permits (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  reservation_id TEXT NOT NULL REFERENCES reservations(id),
  infohash TEXT NOT NULL,
  metadata_sha256 TEXT NOT NULL,
  destination TEXT NOT NULL,
  budget_bytes INTEGER NOT NULL CHECK(budget_bytes >= 0),
  state TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_downloads_state ON downloads(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_download_files_selected ON download_files(download_id, selected);
CREATE INDEX IF NOT EXISTS idx_permits_state ON permits(state, expires_at);
