CREATE TABLE IF NOT EXISTS series_subtitle_publication (
  tmdb_id INTEGER PRIMARY KEY CHECK (tmdb_id > 0),
  allowed_at TEXT NOT NULL
);
