CREATE TABLE IF NOT EXISTS daily_usage (
  user_id INTEGER NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day)
);
PRAGMA user_version=5;
