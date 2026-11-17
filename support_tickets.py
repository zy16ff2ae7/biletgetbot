"""Private support conversations and a durable notification outbox."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


class SupportStore:
    MAX_OPEN = 3
    MAX_MESSAGES = 100
    MAX_TEXT = 3000

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise RuntimeError("Unsupported support schema")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','answered','closed')),
                    created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    closed_by INTEGER, source_update INTEGER UNIQUE
                );
                CREATE INDEX IF NOT EXISTS ticket_owner ON tickets(user_id, updated);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    author_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','staff')),
                    body TEXT NOT NULL, attachment_kind TEXT, file_id TEXT,
                    created INTEGER NOT NULL, source_update INTEGER UNIQUE
                );
                CREATE INDEX IF NOT EXISTS message_ticket ON messages(ticket_id, id);
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    recipient_id INTEGER NOT NULL, event TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL,
                    UNIQUE(ticket_id, message_id, recipient_id, event)
                );
                PRAGMA user_version=1;
                COMMIT;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _access(db, ident, actor, staff=False):
        return db.execute("SELECT * FROM tickets WHERE id=? AND (user_id=? OR ?)",
                          (ident, actor, bool(staff))).fetchone()

    @classmethod
    def _validate(cls, text, attachment, update_id):
        text = (text or "").strip()
        if not text or len(text) > cls.MAX_TEXT:
            raise ValueError("Опишите проблему сообщением от 1 до 3000 символов.")
        if update_id is not None and (type(update_id) is not int or update_id < 0):
            raise ValueError("Некорректное сообщение.")
        if attachment:
            file_id = attachment.get("file_id")
            if (attachment.get("kind") not in {"photo", "document"}
                    or not isinstance(file_id, str) or not 1 <= len(file_id) <= 1024):
                raise ValueError("Не удалось распознать вложение.")
        return text

    @staticmethod
    def _notify(db, ticket, message_id, recipients, event):
        for recipient in set(recipients):
            if type(recipient) is int and recipient > 0:
                db.execute("INSERT OR IGNORE INTO notifications "
                           "(ticket_id,message_id,recipient_id,event,next_attempt) VALUES(?,?,?,?,?)",
                           (ticket, message_id, recipient, event, int(time.time())))

    @staticmethod
    def _insert_message(db, ticket, actor, text, update_id, staff, attachment):
        attachment = attachment or {}
        return db.execute("INSERT INTO messages "
                          "(ticket_id,author_id,role,body,attachment_kind,file_id,created,source_update) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                          (ticket, actor, "staff" if staff else "user", text,
                           attachment.get("kind"), attachment.get("file_id"),
                           int(time.time()), update_id)).lastrowid

    def create(self, user_id, chat_id, text, update_id, staff_ids, attachment=None):
        text = self._validate(text, attachment, update_id)
        if type(user_id) is not int or user_id <= 0 or chat_id != user_id:
            raise ValueError("Поддержка доступна в личном чате с ботом.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if update_id is not None:
                old = db.execute("SELECT * FROM tickets WHERE source_update=?", (update_id,)).fetchone()
                if old:
                    if old["user_id"] != user_id:
                        raise ValueError("Обращение не найдено.")
                    return dict(old)
            count = db.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND status!='closed'",
                               (user_id,)).fetchone()[0]
            if count >= self.MAX_OPEN:
                raise ValueError("У вас уже три открытых обращения. Дополните существующее или закройте решённое.")
            now = int(time.time())
            ident = db.execute("INSERT INTO tickets "
                               "(user_id,chat_id,status,created,updated,source_update) VALUES(?,?,'open',?,?,?)",
                               (user_id, chat_id, now, now, update_id)).lastrowid
            mid = self._insert_message(db, ident, user_id, text, update_id, False, attachment)
            self._notify(db, ident, mid, [uid for uid in staff_ids if uid != user_id], "created")
            return dict(db.execute("SELECT * FROM tickets WHERE id=?", (ident,)).fetchone())

    def ticket(self, ident, actor, staff=False):
        with self.db() as db:
            row = self._access(db, ident, actor, staff)
            return dict(row) if row else None

    def processed_ticket(self, update_id, actor):
        if update_id is None:
            return None
        with self.db() as db:
            row = db.execute("SELECT ticket_id FROM messages WHERE source_update=? AND author_id=?",
                             (update_id, actor)).fetchone()
            return row[0] if row else None

    def handled_update(self, update_id, actor):
        return self.processed_ticket(update_id, actor) is not None

    def list_tickets(self, user_id=None, offset=0):
        with self.db() as db:
            rows = db.execute("SELECT t.*, (SELECT body FROM messages m WHERE m.ticket_id=t.id "
                              "ORDER BY m.id LIMIT 1) AS subject FROM tickets t "
                              "WHERE (user_id=? OR ? IS NULL) AND (status!='closed' OR ? IS NOT NULL) "
                              "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END, "
                              "updated DESC, id DESC LIMIT 10 OFFSET ?",
                              (user_id, user_id, user_id, offset)).fetchall()
            return [dict(row) for row in rows]

    def messages(self, ident, actor, staff=False):
        with self.db() as db:
            if not self._access(db, ident, actor, staff):
                raise ValueError("Обращение не найдено.")
            return [dict(row) for row in db.execute("SELECT * FROM messages WHERE ticket_id=? ORDER BY id",
                                                  (ident,))]

    def message(self, ident, actor, staff=False):
        with self.db() as db:
            row = db.execute("SELECT m.* FROM messages m JOIN tickets t ON t.id=m.ticket_id "
                             "WHERE m.id=? AND (t.user_id=? OR ?)", (ident, actor, bool(staff))).fetchone()
            return dict(row) if row else None

    def add_message(self, ident, actor, text, update_id, staff_ids, staff=False, attachment=None):
        text = self._validate(text, attachment, update_id)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            ticket = self._access(db, ident, actor, staff)
            if not ticket:
                raise ValueError("Обращение не найдено.")
            if update_id is not None:
                old = db.execute("SELECT * FROM messages WHERE source_update=?", (update_id,)).fetchone()
                if old:
                    if old["ticket_id"] != ident or old["author_id"] != actor:
                        raise ValueError("Сообщение уже обработано.")
                    return dict(old)
            if ticket["status"] == "closed":
                raise ValueError("Обращение закрыто. Создайте новое, если вопрос остался.")
            count = db.execute("SELECT COUNT(*) FROM messages WHERE ticket_id=?", (ident,)).fetchone()[0]
            if count >= self.MAX_MESSAGES:
                raise ValueError("В обращении достигнут лимит сообщений. Создайте новое обращение.")
            mid = self._insert_message(db, ident, actor, text, update_id, staff, attachment)
            db.execute("UPDATE tickets SET status=?,updated=? WHERE id=?",
                       ("answered" if staff else "open", int(time.time()), ident))
            recipients = [ticket["chat_id"]] if staff else staff_ids
            self._notify(db, ident, mid, [uid for uid in recipients if uid != actor], "reply")
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

    def close(self, ident, actor, staff_ids, staff=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            ticket = self._access(db, ident, actor, staff)
            if not ticket:
                raise ValueError("Обращение не найдено.")
            if ticket["status"] == "closed":
                return
            db.execute("UPDATE tickets SET status='closed',closed_by=?,updated=? WHERE id=?",
                       (actor, int(time.time()), ident))
            mid = db.execute("SELECT id FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
                             (ident,)).fetchone()[0]
            recipients = [ticket["chat_id"]] if staff else staff_ids
            self._notify(db, ident, mid, [uid for uid in recipients if uid != actor], "closed")

    def claim_notifications(self, now=None):
        now = int(time.time()) if now is None else now
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM notifications WHERE delivered=0 AND next_attempt<=? "
                              "ORDER BY id LIMIT 5", (now,)).fetchall()
            for row in rows:
                db.execute("UPDATE notifications SET attempts=attempts+1,next_attempt=? WHERE id=?",
                           (now + 300, row["id"]))
            return [dict(row) for row in rows]

    def finish_notification(self, ident, delivered, now=None):
        now = int(time.time()) if now is None else now
        with self.db() as db:
            row = db.execute("SELECT attempts FROM notifications WHERE id=?", (ident,)).fetchone()
            if row:
                delay = min(3600, 10 * 2 ** min(row[0], 8))
                db.execute("UPDATE notifications SET delivered=?,next_attempt=? WHERE id=?",
                           (int(delivered), now + delay, ident))


class SupportDesk:
    STATUS = {"open": "Открыто", "answered": "Есть ответ поддержки", "closed": "Закрыто"}

    def __init__(self, store, states, send, is_staff, staff_ids):
        self.store, self.states, self.send = store, states, send
        self.is_staff, self.staff_ids = is_staff, staff_ids

    @staticmethod
    def _button(label, action):
        return {"text": label, "callback_data": "support:" + action}

    @staticmethod
    def _number(value, minimum=1):
        if not value.isascii() or not value.isdecimal() or len(value) > 10 or int(value) < minimum:
            raise ValueError("Некорректный номер обращения.")
        return int(value)

    def _focus(self, ticket, uid):
        state = f"support:reply:{ticket['id']}" if ticket["status"] != "closed" else None
        self.states.set_user_state(uid, state)

    def menu(self, uid, chat, username=None):
        rows = [[self._button("✍️ Создать обращение", "new")],
                [self._button("📨 Мои обращения", "list")]]
        if self.is_staff(uid, username):
            rows.append([self._button("🛠 Все обращения", "queue")])
        rows.append([{"text": "🏠 В главное меню", "callback_data": "welcome:menu"}])
        return self.send(chat, "🎫 Поддержка\n\nОпишите проблему в обращении. "
                         "Ответ придёт сюда, в бота. Подписка для обращения не требуется.",
                         {"inline_keyboard": rows})

    def view(self, ident, uid, chat, staff=False):
        ticket = self.store.ticket(ident, uid, staff)
        if not ticket:
            raise ValueError("Обращение не найдено.")
        self._focus(ticket, uid)
        messages = self.store.messages(ident, uid, staff)
        created = datetime.fromtimestamp(ticket["created"], ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
        text = f"🎫 Обращение #{ident}\nСтатус: {self.STATUS[ticket['status']]}\nСоздано: {created} МСК"
        if staff:
            text += f"\nПользователь: ID {ticket['user_id']}"
        rows = []
        for message in messages[-5:]:
            author = "Поддержка" if message["role"] == "staff" else "Пользователь"
            text += f"\n\n{author}: {message['body'][:400]}"
            rows.append([self._button(f"📨 Сообщение #{message['id']}", f"message:{message['id']}")])
        if len(messages) > 5:
            rows.append([self._button("📜 Ранее в переписке", f"history:{ident}:0")])
        if ticket["status"] != "closed":
            rows.append([self._button("✍️ Ответить", f"reply:{ident}"),
                         self._button("✅ Закрыть обращение", f"close:{ident}")])
            if self.states.get_user_state(uid) == f"support:reply:{ident}":
                text += "\n\nСледующие сообщения и файлы добавятся сюда. Для обработки билетов нажмите «Вернуться к билетам»."
        rows.append([self._button("🎟 Вернуться к билетам", "exit")])
        rows.append([self._button("⬅️ Поддержка", "menu")])
        return self.send(chat, text, {"inline_keyboard": rows})

    def callback(self, data, uid, chat, username=None):
        if chat != uid:
            return self.send(chat, "Обращения доступны в личном чате с ботом.")
        staff = self.is_staff(uid, username)
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else "menu"
        try:
            if action == "exit":
                self.states.set_user_state(uid, None)
                return self.send(chat, "Вы вышли из переписки с поддержкой. Можно отправлять PDF на обработку.",
                                 {"inline_keyboard": [[{"text": "🏠 Главное меню", "callback_data": "welcome:menu"}]]})
            if action in {"menu", "cancel"}:
                self.states.set_user_state(uid, None)
                return self.menu(uid, chat, username)
            if action == "new":
                if len([t for t in self.store.list_tickets(uid) if t["status"] != "closed"]) >= self.store.MAX_OPEN:
                    raise ValueError("У вас уже три открытых обращения. Дополните существующее.")
                self.states.set_user_state(uid, "support:new")
                return self.send(chat, "✍️ Опишите проблему одним сообщением. "
                                 "Можно приложить PDF или скриншот с подписью.\nДля отмены: отмена.",
                                 {"inline_keyboard": [[self._button("Отмена", "cancel")]]})
            if action in {"list", "queue"}:
                if action == "queue" and not staff:
                    raise ValueError("Этот раздел доступен только поддержке.")
                self.states.set_user_state(uid, None)
                offset = min(self._number(parts[2], 0), 10000) if len(parts) > 2 else 0
                tickets = self.store.list_tickets(None if action == "queue" else uid, offset)
                rows = [[self._button(f"#{t['id']} · {self.STATUS[t['status']]} · {' '.join(t['subject'].split())[:35]}",
                                      f"view:{t['id']}")] for t in tickets]
                if offset:
                    rows.append([self._button("← Назад", f"{action}:{max(0, offset-10)}")])
                if len(tickets) == 10:
                    rows.append([self._button("Далее →", f"{action}:{offset+10}")])
                rows.append([self._button("⬅️ Поддержка", "menu")])
                return self.send(chat, "🎫 Обращения" if tickets else "Обращений пока нет.", {"inline_keyboard": rows})
            if action in {"message", "file"}:
                message = self.store.message(self._number(parts[2]), uid, staff)
                if not message:
                    raise ValueError("Обращение не найдено.")
                self._focus(self.store.ticket(message["ticket_id"], uid, staff), uid)
                markup = {"inline_keyboard": [[self._button("⬅️ К обращению", f"view:{message['ticket_id']}")]]}
                if action == "file":
                    if not message["file_id"]:
                        raise ValueError("Вложения нет.")
                    return self.send(chat, f"Вложение к обращению #{message['ticket_id']}", markup,
                                     attachment={"kind": message["attachment_kind"], "file_id": message["file_id"]})
                if message["file_id"]:
                    markup["inline_keyboard"].insert(0, [self._button("📎 Вложение", f"file:{message['id']}")])
                return self.send(chat, f"Обращение #{message['ticket_id']} · Сообщение #{message['id']}\n\n{message['body']}", markup)
            ident = self._number(parts[2])
            ticket = self.store.ticket(ident, uid, staff)
            if not ticket:
                raise ValueError("Обращение не найдено.")
            if action == "history":
                self._focus(ticket, uid)
                offset = min(self._number(parts[3], 0), 100)
                messages = self.store.messages(ident, uid, staff)[offset:offset+10]
                rows = [[self._button(f"Сообщение #{m['id']}", f"message:{m['id']}")] for m in messages]
                if len(messages) == 10:
                    rows.append([self._button("Далее →", f"history:{ident}:{offset+10}")])
                rows.append([self._button("⬅️ К обращению", f"view:{ident}")])
                return self.send(chat, f"Переписка обращения #{ident}", {"inline_keyboard": rows})
            if action == "reply":
                if ticket["status"] == "closed":
                    raise ValueError("Обращение закрыто.")
                self.states.set_user_state(uid, f"support:reply:{ident}")
                return self.send(chat, f"✍️ Напишите ответ в обращение #{ident}. Можно приложить файл.\nДля отмены: отмена.",
                                 {"inline_keyboard": [[self._button("Отмена", "cancel")]]})
            self.states.set_user_state(uid, None)
            if action == "close":
                self.store.close(ident, uid, self.staff_ids, staff)
            elif action != "view":
                raise ValueError("Неизвестное действие поддержки.")
            return self.view(ident, uid, chat, staff)
        except (ValueError, IndexError) as error:
            return self.send(chat, str(error) if isinstance(error, ValueError) else "Некорректная кнопка поддержки.")

    @staticmethod
    def _attachment(msg):
        doc = msg.get("document")
        photos = msg.get("photo") or []
        info = doc or (photos[-1] if photos else None)
        if not info:
            return None
        if (info.get("file_size") or 0) > 20 * 1024 * 1024:
            raise ValueError("Вложение должно быть не больше 20 МБ.")
        return {"kind": "document" if doc else "photo", "file_id": info.get("file_id")}

    def handle_message(self, msg, update_id=None):
        uid, chat = msg["from"]["id"], msg["chat"]["id"]
        if chat != uid or msg["chat"].get("type", "private") != "private":
            self.send(chat, "Обращения доступны в личном чате с ботом.")
            return True
        username = msg["from"].get("username")
        text = (msg.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        staff = self.is_staff(uid, username)
        old = self.store.processed_ticket(update_id, uid)
        if old:
            if self.states.get_user_state(uid) in {"support:new", f"support:reply:{old}"}:
                ticket = self.store.ticket(old, uid, staff)
                self.states.set_user_state(uid, f"support:reply:{old}" if ticket and ticket["status"] != "closed" else None)
            self.view(old, uid, chat, staff)
            return True
        if command in {"/support", "/tickets", "/support_queue"}:
            action = {"/support": "menu", "/tickets": "list", "/support_queue": "queue"}[command]
            self.callback("support:" + action, uid, chat, username)
            return True
        state = self.states.get_user_state(uid)
        if not isinstance(state, str) or not state.startswith("support:"):
            return False
        if text.lower() in {"отмена", "cancel", "/cancel"}:
            self.callback("support:cancel", uid, chat, username)
            return True
        if command.startswith("/"):
            self.states.set_user_state(uid, None)
            return False
        try:
            attachment = self._attachment(msg)
            body = text or (msg.get("caption") or "").strip()
            if not body and attachment:
                body = "Вложение к обращению."
            if state == "support:new":
                ticket = self.store.create(uid, chat, body, update_id, self.staff_ids, attachment)
                ident = ticket["id"]
            else:
                ident = self._number(state.split(":")[2])
                self.store.add_message(ident, uid, body, update_id, self.staff_ids, staff, attachment)
            self.states.set_user_state(uid, f"support:reply:{ident}")
            self.view(ident, uid, chat, staff)
        except (ValueError, IndexError) as error:
            self.send(chat, str(error) if isinstance(error, ValueError) else "Откройте обращение заново.")
        return True

    def flush_notifications(self):
        for notice in self.store.claim_notifications():
            delivered = False
            try:
                recipient = notice["recipient_id"]
                ticket = self.store.ticket(notice["ticket_id"], recipient, staff=True)
                if recipient != ticket["user_id"] and not self.is_staff(recipient):
                    delivered = True  # A removed staff member must not receive private history.
                    continue
                message = self.store.message(notice["message_id"], recipient, staff=True)
                title = {"created": "Новое обращение", "reply": "Новое сообщение", "closed": "Обращение закрыто"}[notice["event"]]
                text = f"🎫 {title} #{ticket['id']}\nСтатус: {self.STATUS[ticket['status']]}"
                if recipient != ticket["user_id"]:
                    text += f"\nПользователь: ID {ticket['user_id']}"
                if notice["event"] != "closed":
                    author = "Поддержка" if message["role"] == "staff" else "Пользователь"
                    text += f"\n\n{author}: {message['body']}"
                rows = [[self._button("📨 Открыть обращение", f"view:{ticket['id']}")]]
                if message["file_id"] and notice["event"] != "closed":
                    rows.append([self._button("📎 Вложение", f"file:{message['id']}")])
                delivered = bool(self.send(recipient, text, {"inline_keyboard": rows}))
            finally:
                self.store.finish_notification(notice["id"], delivered)
