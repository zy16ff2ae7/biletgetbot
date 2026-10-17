"""Bounded, private metadata cache of successfully delivered Telegram files."""
import hashlib
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time

TTL = 86400
MAX_ENTRIES = 2000


def key_for(version, user, chat, name, identity):
    # Cache scope includes owner, filename and exact processing version.
    import json
    value = json.dumps([version, user, chat, name, identity], ensure_ascii=True)
    return hashlib.sha256(value.encode()).hexdigest()


class DeliveryCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, "
                       "file_id TEXT NOT NULL, caption TEXT NOT NULL, created INTEGER NOT NULL)")
        os.chmod(self.path, 0o600)

    def get(self, key):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            row = db.execute("SELECT file_id,caption FROM files WHERE key=? AND created>?",
                             (key, int(time.time()) - TTL)).fetchone()
            return {"file_id": row[0], "caption": row[1]} if row else None
        finally:
            db.close()

    def put(self, keys, file_id, caption):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                now = int(time.time())
                db.execute("DELETE FROM files WHERE created<=?", (now - TTL,))
                for key in set(keys):
                    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)", (key, file_id, caption, now))
                db.execute("DELETE FROM files WHERE key IN (SELECT key FROM files "
                           "ORDER BY created DESC, key LIMIT -1 OFFSET ?)", (MAX_ENTRIES,))
        finally:
            db.close()

    def forget(self, keys):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                db.executemany("DELETE FROM files WHERE key=?", ((key,) for key in keys))
        finally:
            db.close()
