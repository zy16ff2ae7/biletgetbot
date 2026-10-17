CREATE TABLE payment_incidents (
  update_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, payment TEXT NOT NULL,
  reason TEXT NOT NULL, created INTEGER NOT NULL
);
PRAGMA user_version=2;
