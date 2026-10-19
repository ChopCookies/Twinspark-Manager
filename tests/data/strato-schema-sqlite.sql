-- community/strato/schema.sql in SQLite's dialect, for tests/test_community.py (php -S with pdo_sqlite)
CREATE TABLE reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  reporter_hash TEXT NULL, recipe TEXT NOT NULL, recipe_sha256 TEXT NOT NULL, mode TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL, failure_stage TEXT NULL, twinspark TEXT NOT NULL, engine TEXT NOT NULL,
  engine_version TEXT NULL, topology TEXT NULL, tok_s REAL NULL, benchmarks TEXT NOT NULL, changes TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
  UNIQUE (reporter_hash, recipe, recipe_sha256, mode)
);
CREATE TABLE rate_limits (ip_hash TEXT NOT NULL, day TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ip_hash, day));
