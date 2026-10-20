-- TwinSpark community reports: MySQL 8.0 or MariaDB 10.6+ (tested with MariaDB 11.8).
-- Import once, e.g. in phpMyAdmin: select the database, "Import", this file.
-- Nothing here identifies a person: no IP addresses (only a keyed hash for the daily rate limit, kept two
-- days), no host names, and the reporter id is stored as a keyed hash of a random number.

CREATE TABLE IF NOT EXISTS reports (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at      DATETIME        NOT NULL,
  updated_at       DATETIME        NOT NULL,
  reporter_hash    CHAR(64)        NULL,          -- HMAC of the random reporter id; NULL = anonymous
  recipe           VARCHAR(81)     NOT NULL,
  recipe_sha256    CHAR(64)        NOT NULL,
  mode             VARCHAR(31)     NOT NULL DEFAULT '',
  outcome          VARCHAR(20)     NOT NULL,      -- works | works-with-changes | fails
  failure_stage    VARCHAR(10)     NULL,
  twinspark        VARCHAR(32)     NOT NULL,
  engine           VARCHAR(10)     NOT NULL,
  engine_version   VARCHAR(32)     NULL,
  topology         VARCHAR(48)     NULL,
  tok_s            DOUBLE          NULL,          -- peak output tok/s of the max-throughput benchmark, if any
  benchmarks       TEXT            NOT NULL,      -- JSON, numbers only
  changes          TEXT            NOT NULL,      -- JSON [{field, value}]
  comment          VARCHAR(500)    NOT NULL DEFAULT '',
  hidden           TINYINT(1)      NOT NULL DEFAULT 0,   -- moderation: UPDATE reports SET hidden = 1 WHERE id = …
  PRIMARY KEY (id),
  UNIQUE KEY one_per_reporter (reporter_hash, recipe, recipe_sha256, mode),
  KEY by_recipe (recipe, hidden)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS rate_limits (
  ip_hash  CHAR(64) NOT NULL,                     -- HMAC of the address and the day; deleted after two days
  day      DATE     NOT NULL,
  hits     INT      NOT NULL DEFAULT 0,
  PRIMARY KEY (ip_hash, day)
) ENGINE=InnoDB DEFAULT CHARSET=ascii;
