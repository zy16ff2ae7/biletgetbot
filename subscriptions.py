"""Durable subscriptions, payment receipts and processing queue."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

PERIOD = 30 * 86400


class SubscriptionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Unsupported subscription schema")
            for target in range(version + 1, 3):
                migration = Path(__file__).parent / f"migrations/{target:03d}_{'subscriptions' if target == 1 else 'payment_incidents'}.sql"
                db.executescript("BEGIN IMMEDIATE;\n" + migration.read_text() + "\nCOMMIT;")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def invoice(self, user_id, price):
        if not 1 <= price <= 10000:
            raise ValueError("Invalid Stars price")
        with self.db() as db:
            row = db.execute("SELECT * FROM invoices WHERE user_id=? AND price=? AND initial_charge IS NULL "
                             "ORDER BY created DESC LIMIT 1", (user_id, price)).fetchone()
            if row:
                return dict(row)
            payload = "month:" + secrets.token_urlsafe(24)
            db.execute("INSERT INTO invoices(payload,user_id,price,created) VALUES(?,?,?,?)",
                       (payload, user_id, price, int(time.time())))
            return dict(db.execute("SELECT * FROM invoices WHERE payload=?", (payload,)).fetchone())

    def save_url(self, payload, url):
        with self.db() as db:
            db.execute("UPDATE invoices SET url=? WHERE payload=?", (url, payload))

    def checkout_valid(self, user_id, payment):
        with self.db() as db:
            row = db.execute("SELECT * FROM invoices WHERE payload=?", (payment.get("invoice_payload"),)).fetchone()
            if not self._payment_identity(row, user_id, payment) or row["initial_charge"] is not None:
                return False
            active = db.execute("SELECT 1 FROM payments WHERE user_id=? AND expires>? AND refunded=0 LIMIT 1",
                                (user_id, int(time.time()))).fetchone()
            gift = db.execute("SELECT 1 FROM grants WHERE user_id=? AND expires>?", (user_id, int(time.time()))).fetchone()
            return not active and not gift

    @staticmethod
    def _payment_identity(row, user_id, payment):
        return bool(row and row["user_id"] == user_id and payment.get("currency") == "XTR"
                    and type(payment.get("total_amount")) is int and payment["total_amount"] == row["price"])

    def apply_payment(self, user_id, payment):
        expires = payment.get("subscription_expiration_date")
        charge = payment.get("telegram_payment_charge_id")
        if (not payment.get("is_recurring") or not isinstance(expires, int) or isinstance(expires, bool)
                or not 0 < expires <= int(time.time()) + PERIOD + 86400
                or not isinstance(charge, str) or not 1 <= len(charge) <= 256):
            raise ValueError("Invalid subscription payment")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            invoice = db.execute("SELECT * FROM invoices WHERE payload=?", (payment.get("invoice_payload"),)).fetchone()
            if not self._payment_identity(invoice, user_id, payment):
                raise ValueError("Invalid payment identity")
            existing = db.execute("SELECT * FROM payments WHERE charge_id=?", (charge,)).fetchone()
            if existing:
                if existing["user_id"] != user_id or existing["payload"] != payment["invoice_payload"]:
                    raise ValueError("Payment identity mismatch")
                return False
            db.execute("INSERT INTO payments(charge_id,payload,user_id,expires,amount,created) VALUES(?,?,?,?,?,?)",
                       (charge, payment["invoice_payload"], user_id, expires, payment["total_amount"], int(time.time())))
            db.execute("UPDATE invoices SET initial_charge=COALESCE(initial_charge,?) WHERE payload=?",
                       (charge, payment["invoice_payload"]))
            return True

    def record_payment_incident(self, update_id, user_id, payment, reason):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO payment_incidents VALUES(?,?,?,?,?)",
                       (update_id, user_id, json.dumps(payment), reason, int(time.time())))

    def payment_incidents(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT update_id,user_id,reason,created FROM payment_incidents ORDER BY created DESC LIMIT 10")]

    def status(self, user_id):
        with self.db() as db:
            paid = db.execute("SELECT MAX(expires) FROM payments WHERE user_id=? AND refunded=0", (user_id,)).fetchone()[0] or 0
            grant = db.execute("SELECT expires FROM grants WHERE user_id=?", (user_id,)).fetchone()
            invoice = db.execute("SELECT * FROM invoices WHERE user_id=? AND initial_charge IS NOT NULL ORDER BY created DESC LIMIT 1",
                                 (user_id,)).fetchone()
        return {"expires": max(paid, grant[0] if grant else 0), "paid_expires": paid,
                "active": max(paid, grant[0] if grant else 0) > time.time(),
                "invoice": dict(invoice) if invoice else None}

    def grant(self, user_id, days, admin_id, action_id=None):
        if user_id <= 0 or not 1 <= days <= 3650:
            raise ValueError("Invalid access grant")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if action_id:
                old = db.execute("SELECT expires FROM access_actions WHERE id=?", (action_id,)).fetchone()
                if old:
                    return old[0]
            paid = db.execute("SELECT MAX(expires) FROM payments WHERE user_id=? AND refunded=0", (user_id,)).fetchone()[0] or 0
            gift = db.execute("SELECT expires FROM grants WHERE user_id=?", (user_id,)).fetchone()
            expires = max(int(time.time()), paid, gift[0] if gift else 0) + days * 86400
            db.execute("INSERT INTO grants(user_id,expires,admin_id) VALUES(?,?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires,admin_id=excluded.admin_id",
                       (user_id, expires, admin_id))
            if action_id:
                db.execute("INSERT INTO access_actions VALUES(?,?)", (action_id, expires))
        return expires

    def register_invite(self, username, days, admin_id):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO gift_invites(username,days,admin_id,expires) VALUES(?,?,?,?)",
                       (username.lower(), days, admin_id, int(time.time()) + 7 * 86400))

    def claim_invite(self, username, user_id):
        if not username:
            return False
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gift_invites WHERE username=?", (username.lower(),)).fetchone()
            if not row or row["claimed_user"] is not None or row["expires"] < time.time():
                return False
            expires = int(time.time()) + row["days"] * 86400
            db.execute("INSERT INTO grants(user_id,expires,admin_id) VALUES(?,?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET expires=MAX(grants.expires,excluded.expires)",
                       (user_id, expires, row["admin_id"]))
            db.execute("UPDATE gift_invites SET claimed_user=? WHERE username=?", (user_id, username.lower()))
            return True

    def revoke_grant(self, user_id):
        with self.db() as db:
            db.execute("DELETE FROM grants WHERE user_id=?", (user_id,))

    def grant_by_username(self, username, days, admin_id, action_id=None):
        if days < 1 or days > 3650:
            raise ValueError("days_out_of_range")
        username = username.lstrip("@").lower()
        with self.db() as db:
            row = db.execute("SELECT claimed_user FROM gift_invites WHERE username=?", (username,)).fetchone()
            if row and row["claimed_user"]:
                expires = self.grant(row["claimed_user"], days, admin_id, action_id)
                return expires, row["claimed_user"]
            db.execute("INSERT INTO gift_invites(username,days,admin_id,expires) VALUES(?,?,?,?) "
                       "ON CONFLICT(username) DO UPDATE SET days=excluded.days,admin_id=excluded.admin_id,expires=excluded.expires,claimed_user=NULL",
                       (username, days, admin_id, int(time.time()) + 7 * 86400))
            return None, None

    def revoke_grant_by_username(self, username):
        username = username.lstrip("@").lower()
        with self.db() as db:
            row = db.execute("SELECT claimed_user FROM gift_invites WHERE username=?", (username,)).fetchone()
            if row and row["claimed_user"]:
                db.execute("DELETE FROM grants WHERE user_id=?", (row["claimed_user"],))
            db.execute("DELETE FROM gift_invites WHERE username=?", (username,))

    def canceled(self, payload):
        with self.db() as db:
            db.execute("UPDATE invoices SET canceled=1 WHERE payload=?", (payload,))

    def payment(self, charge_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
            return dict(row) if row else None

    def refund(self, user_id, charge_id):
        with self.db() as db:
            db.execute("UPDATE payments SET refunded=1 WHERE charge_id=? AND user_id=?", (charge_id, user_id))

    def payments(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM payments ORDER BY created DESC LIMIT 10")]

    def enqueue(self, update_id, payload):
        with self.db() as db:
            cursor = db.execute("INSERT OR IGNORE INTO jobs(update_id,payload,created) VALUES(?,?,?)",
                                (update_id, json.dumps(payload, ensure_ascii=False), int(time.time())))
            return cursor.rowcount == 1

    def recover_jobs(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
            db.execute("DELETE FROM jobs WHERE finished<?", (int(time.time()) - 7 * 86400,))
            db.execute("DELETE FROM seen_updates WHERE created<?", (int(time.time()) - 7 * 86400,))

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY update_id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='processing' WHERE update_id=?", (row["update_id"],))
            return row["update_id"], json.loads(row["payload"])

    def claim_pending_documents(self, user_id, chat_id, limit=50):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT update_id, payload FROM jobs WHERE state='queued' ORDER BY update_id").fetchall()
            matching = []
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    kind = payload.get("kind")
                    msg = payload.get("message", {}) if kind in {"message", "document"} else None
                    if msg:
                        target_chat = msg.get("chat", {}).get("id")
                        target_uid = msg.get("from", {}).get("id")
                        if target_chat == chat_id and target_uid == user_id and msg.get("document"):
                            doc = msg["document"]
                            name = (doc.get("file_name") or "ticket.pdf").lower()
                            if name.endswith(".pdf"):
                                matching.append((row["update_id"], payload))
                                if len(matching) >= limit:
                                    break
                except Exception:
                    continue
            if matching:
                uids = [m[0] for m in matching]
                placeholders = ",".join("?" for _ in uids)
                db.execute(f"UPDATE jobs SET state='processing' WHERE update_id IN ({placeholders})", uids)
            return matching

    def finish(self, update_id):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='done',finished=?,payload='{}' WHERE update_id=?", (int(time.time()), update_id))

    def seen(self, update_id):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM seen_updates WHERE update_id=?", (update_id,)).fetchone()
                        or db.execute("SELECT 1 FROM jobs WHERE update_id=?", (update_id,)).fetchone())

    def mark_seen(self, update_id):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO seen_updates VALUES(?,?)", (update_id, int(time.time())))

    def open_collection(self, user_id, chat_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM collections WHERE user_id=? AND chat_id=? AND state='open' ORDER BY created DESC LIMIT 1",
                             (user_id, chat_id)).fetchone()
            return dict(row) if row else None

    def sealed_collection(self, user_id, chat_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM collections WHERE user_id=? AND chat_id=? AND state='sealed' ORDER BY created DESC LIMIT 1",
                             (user_id, chat_id)).fetchone()
            return dict(row) if row else None

    def seal_and_enqueue(self, update_id, ident, payload):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE collections SET state='sealed' WHERE id=?", (ident,))
            db.execute("INSERT OR IGNORE INTO jobs(update_id,payload,created) VALUES(?,?,?)",
                       (update_id, json.dumps(payload), int(time.time())))

    def create_collection(self, user_id, chat_id):
        current = self.open_collection(user_id, chat_id)
        if current:
            return current["id"]
        ident = secrets.token_hex(16)
        with self.db() as db:
            db.execute("INSERT INTO collections(id,user_id,chat_id,created) VALUES(?,?,?,?)",
                       (ident, user_id, chat_id, int(time.time())))
        return ident

    def collection(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM collections WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def collection_state(self, ident, state):
        if state not in {"sealed", "done", "canceled"}:
            raise ValueError("Invalid collection state")
        with self.db() as db:
            db.execute("UPDATE collections SET state=? WHERE id=?", (state, ident))

    def collection_files(self, ident):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM collection_files WHERE collection_id=? ORDER BY ordinal", (ident,))]

    def add_collection_file(self, ident, name, path, size):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT ordinal FROM collection_files WHERE path=?", (str(path),)).fetchone()
            if existing:
                return existing[0] + 1
            count, total = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM collection_files WHERE collection_id=?", (ident,)).fetchone()
            if count >= 50 or total + size > 100 * 1024 * 1024:
                raise ValueError("Collection limit exceeded")
            db.execute("INSERT INTO collection_files VALUES(?,?,?,?,?)", (ident, count, name, str(path), size))
            return count + 1

    def expired_collections(self):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT id FROM collections WHERE state IN ('done','canceled') OR created<?",
                                               (int(time.time()) - 900,))]

    def forget_collection(self, ident):
        with self.db() as db:
            db.execute("DELETE FROM collection_files WHERE collection_id=?", (ident,))
            db.execute("DELETE FROM collections WHERE id=?", (ident,))

    def _ensure_user_settings(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, fio TEXT, state TEXT)")
        cols = {row["name"] for row in db.execute("PRAGMA table_info(user_settings)")}
        if "hide_price" not in cols:
            db.execute("ALTER TABLE user_settings ADD COLUMN hide_price INTEGER DEFAULT 0")
        if "batch_mode" not in cols:
            db.execute("ALTER TABLE user_settings ADD COLUMN batch_mode TEXT DEFAULT 'zip'")
        if "auto_preview" not in cols:
            db.execute("ALTER TABLE user_settings ADD COLUMN auto_preview INTEGER DEFAULT 0")
        if "custom_price" not in cols:
            db.execute("ALTER TABLE user_settings ADD COLUMN custom_price INTEGER DEFAULT NULL")
        if "last_doc_token" not in cols:
            db.execute("ALTER TABLE user_settings ADD COLUMN last_doc_token TEXT DEFAULT NULL")
        db.execute("CREATE TABLE IF NOT EXISTS doc_tokens (token TEXT PRIMARY KEY, file_id TEXT, created INTEGER)")
        doc_cols = {row["name"] for row in db.execute("PRAGMA table_info(doc_tokens)")}
        if "custom_price" not in doc_cols:
            db.execute("ALTER TABLE doc_tokens ADD COLUMN custom_price INTEGER DEFAULT NULL")
        if "hide_price" not in doc_cols:
            db.execute("ALTER TABLE doc_tokens ADD COLUMN hide_price INTEGER DEFAULT NULL")
        if "fio" not in doc_cols:
            db.execute("ALTER TABLE doc_tokens ADD COLUMN fio TEXT DEFAULT NULL")
        if "seat_info" not in doc_cols:
            db.execute("ALTER TABLE doc_tokens ADD COLUMN seat_info TEXT DEFAULT NULL")
        db.execute("CREATE INDEX IF NOT EXISTS idx_doc_tokens_created ON doc_tokens(created)")

    def save_doc_token(self, token, file_id, custom_price=None, hide_price=None, fio=None, seat_info=None):
        with self.db() as db:
            self._ensure_user_settings(db)
            db.execute(
                "INSERT INTO doc_tokens(token, file_id, created, custom_price, hide_price, fio, seat_info) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET "
                "file_id=excluded.file_id, created=excluded.created, "
                "custom_price=excluded.custom_price, hide_price=excluded.hide_price, fio=excluded.fio, seat_info=excluded.seat_info",
                (token, file_id, int(time.time()), custom_price, (1 if hide_price else 0) if hide_price is not None else None, fio, seat_info)
            )
            db.execute("DELETE FROM doc_tokens WHERE created < ?", (int(time.time()) - 7 * 86400,))

    def get_doc_by_token(self, token):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT file_id FROM doc_tokens WHERE token=?", (token,)).fetchone()
            return row["file_id"] if row else None

    def get_doc_token_info(self, token):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT file_id, custom_price, hide_price, fio, seat_info FROM doc_tokens WHERE token=?", (token,)).fetchone()
            if not row:
                return None
            return {
                "file_id": row["file_id"],
                "custom_price": row["custom_price"],
                "hide_price": bool(row["hide_price"]) if row["hide_price"] is not None else None,
                "fio": row["fio"],
                "seat_info": row["seat_info"],
            }

    def set_user_last_doc_token(self, user_id, token):
        with self.db() as db:
            self._ensure_user_settings(db)
            db.execute("INSERT INTO user_settings(user_id, last_doc_token) VALUES(?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET last_doc_token=excluded.last_doc_token", (user_id, token))

    def get_user_last_doc_token(self, user_id):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT last_doc_token FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            return row["last_doc_token"] if row and row["last_doc_token"] else None

    def get_user_fio(self, user_id):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT fio FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            return row["fio"] if row and row["fio"] else None

    def set_user_fio(self, user_id, fio):
        with self.db() as db:
            self._ensure_user_settings(db)
            db.execute("INSERT INTO user_settings(user_id, fio) VALUES(?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET fio=excluded.fio", (user_id, fio))

    def get_user_custom_price(self, user_id):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT custom_price FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            return row["custom_price"] if row and row["custom_price"] is not None else None

    def set_user_custom_price(self, user_id, price):
        with self.db() as db:
            self._ensure_user_settings(db)
            db.execute("INSERT INTO user_settings(user_id, custom_price) VALUES(?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET custom_price=excluded.custom_price", (user_id, price))

    def get_user_state(self, user_id):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT state FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            return row["state"] if row and row["state"] else None

    def set_user_state(self, user_id, state):
        with self.db() as db:
            self._ensure_user_settings(db)
            db.execute("INSERT INTO user_settings(user_id, state) VALUES(?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state", (user_id, state))

    def get_user_settings(self, user_id):
        with self.db() as db:
            self._ensure_user_settings(db)
            row = db.execute("SELECT fio, state, hide_price, batch_mode, auto_preview, custom_price FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                return {"fio": None, "state": None, "hide_price": 0, "batch_mode": "zip", "auto_preview": 0, "custom_price": None}
            return {
                "fio": row["fio"] if row["fio"] else None,
                "state": row["state"] if row["state"] else None,
                "hide_price": int(row["hide_price"] or 0),
                "batch_mode": row["batch_mode"] or "zip",
                "auto_preview": int(row["auto_preview"] or 0),
                "custom_price": row["custom_price"] if row["custom_price"] is not None else None,
            }

    def update_user_setting(self, user_id, column, value):
        if column not in {"fio", "state", "hide_price", "batch_mode", "auto_preview", "custom_price"}:
            raise ValueError(f"Invalid setting column: {column}")
        with self.db() as db:
            self._ensure_user_settings(db)
            db.execute(f"INSERT INTO user_settings(user_id, {column}) VALUES(?, ?) "
                       f"ON CONFLICT(user_id) DO UPDATE SET {column}=excluded.{column}", (user_id, value))



