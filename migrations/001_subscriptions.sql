CREATE TABLE invoices (
  payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL, price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 10000),
  created INTEGER NOT NULL, url TEXT, initial_charge TEXT, canceled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE payments (
  charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL REFERENCES invoices(payload),
  user_id INTEGER NOT NULL, expires INTEGER NOT NULL, amount INTEGER NOT NULL,
  refunded INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL
);
CREATE INDEX payments_user ON payments(user_id, expires);
CREATE TABLE grants (user_id INTEGER PRIMARY KEY, expires INTEGER NOT NULL, admin_id INTEGER NOT NULL);
CREATE TABLE access_actions (id TEXT PRIMARY KEY, expires INTEGER NOT NULL);
CREATE TABLE gift_invites (
  username TEXT PRIMARY KEY, days INTEGER NOT NULL, admin_id INTEGER NOT NULL,
  expires INTEGER NOT NULL, claimed_user INTEGER
);
CREATE TABLE jobs (
  update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
  created INTEGER NOT NULL, finished INTEGER
);
CREATE TABLE collections (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'open', created INTEGER NOT NULL
);
CREATE TABLE collection_files (
  collection_id TEXT NOT NULL REFERENCES collections(id), ordinal INTEGER NOT NULL,
  name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
  PRIMARY KEY(collection_id, ordinal)
);
CREATE TABLE seen_updates (update_id INTEGER PRIMARY KEY, created INTEGER NOT NULL);
PRAGMA user_version=1;
