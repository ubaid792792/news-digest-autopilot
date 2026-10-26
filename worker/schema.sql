-- News Digest Autopilot - D1 schema (safe to re-run).

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- kind: rss | facebook (FetchRSS feed) | x | instagram
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  url TEXT NOT NULL,
  handle TEXT,
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_fetched_at TEXT,
  last_error TEXT,
  last_count INTEGER,
  UNIQUE (kind, url)
);

-- Research prompts: requests the AI turns into Google News searches each run.
CREATE TABLE IF NOT EXISTS research (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt TEXT NOT NULL,
  topic TEXT NOT NULL DEFAULT '',
  max_items INTEGER NOT NULL DEFAULT 8,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_run_at TEXT,
  last_error TEXT,
  last_count INTEGER,
  last_queries TEXT
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  emoji TEXT NOT NULL DEFAULT '',
  keywords TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1
);

-- One digest per delivery. kind: scheduled | manual | preview.
-- status: collecting -> ready -> sent | email_failed | skipped ; failed = collection failed.
CREATE TABLE IF NOT EXISTS digests (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  trigger TEXT,
  status TEXT NOT NULL,
  slot_date TEXT,
  slot_time TEXT,
  slot_at TEXT NOT NULL,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  send_email INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  ready_at TEXT,
  sent_at TEXT,
  run_id TEXT,
  item_count INTEGER,
  story_count INTEGER,
  data TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS digests_created ON digests (created_at);
CREATE INDEX IF NOT EXISTS digests_status ON digests (status, slot_at);

-- Posts already put in a digest, so overlapping windows never repeat a story.
CREATE TABLE IF NOT EXISTS seen (
  id TEXT PRIMARY KEY,
  seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_at ON seen (seen_at);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  digest_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT,
  trigger TEXT,
  status TEXT NOT NULL,
  summary TEXT,
  log TEXT,
  gh_run_url TEXT
);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at);

-- Daily counters (emails sent, AI calls) for the panel.
CREATE TABLE IF NOT EXISTS usage (
  day TEXT NOT NULL,
  key TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, key)
);
