CREATE TABLE IF NOT EXISTS subtitle_artifacts (
  reservation_id TEXT NOT NULL REFERENCES reservations(id),
  scope_key TEXT NOT NULL,
  infohash TEXT NOT NULL,
  source TEXT NOT NULL,
  language TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  content BLOB NOT NULL,
  PRIMARY KEY (reservation_id, scope_key, infohash)
);
