CREATE TABLE IF NOT EXISTS expiry_reminders (
  user_id INTEGER PRIMARY KEY, reminded_at INTEGER NOT NULL
);
PRAGMA user_version=4;
