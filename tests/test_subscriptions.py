from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from test_processing import bot, ticket
from subscriptions import SubscriptionStore, PERIOD


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SubscriptionStore(Path(self.tmp.name) / "store.sqlite3")
        self.invoice = self.store.invoice(222, 1000)
        self.payment = {"currency": "XTR", "total_amount": 1000, "invoice_payload": self.invoice["payload"],
                        "telegram_payment_charge_id": "charge-1", "is_recurring": True,
                        "subscription_expiration_date": int(time.time()) + PERIOD}

    def tearDown(self):
        self.tmp.cleanup()

    def test_payment_opens_monthly_access(self):
        self.assertFalse(self.store.status(222)["active"])
        self.assertTrue(self.store.apply_payment(222, self.payment))
        self.assertTrue(self.store.status(222)["active"])

    def test_payment_replay_does_not_extend_access(self):
        self.store.apply_payment(222, self.payment)
        expires = self.store.status(222)["expires"]
        self.assertFalse(self.store.apply_payment(222, self.payment))
        self.assertEqual(expires, self.store.status(222)["expires"])

    def test_old_receipt_replay_and_late_receipt_after_new_purchase(self):
        old = dict(self.payment, subscription_expiration_date=int(time.time()) - 1)
        self.store.apply_payment(222, old)
        newer = self.store.invoice(222, 1000)
        current = dict(self.payment, invoice_payload=newer["payload"], telegram_payment_charge_id="new-charge")
        self.store.apply_payment(222, current)
        self.assertFalse(self.store.apply_payment(222, old))
        late = dict(old, telegram_payment_charge_id="late-charge")
        self.assertTrue(self.store.apply_payment(222, late))
        self.assertEqual(current["subscription_expiration_date"], self.store.status(222)["expires"])

    def test_paid_invoice_cannot_start_another_subscription_but_can_renew(self):
        self.store.apply_payment(222, self.payment)
        self.assertFalse(self.store.checkout_valid(222, self.payment))
        renewal = dict(self.payment, telegram_payment_charge_id="renewal")
        self.assertTrue(self.store.apply_payment(222, renewal))

    def test_notifications_are_off_the_checkout_polling_path(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text") as send, \
                patch.object(bot, "tg", return_value=True) as tg:
            bot.dispatch_update({"update_id": 1, "message": {"chat": {"id": 222}, "from": {"id": 222}, "text": "/subscribe"}})
            bot.dispatch_update({"update_id": 2, "pre_checkout_query": dict(self.payment, id="checkout", **{"from": {"id": 222}})})
            send.assert_not_called()
            self.assertEqual("answerPreCheckoutQuery", tg.call_args.args[0])
        self.assertEqual("message", self.store.claim()[1]["kind"])

    def test_bad_paid_event_is_preserved_and_does_not_block_later_updates(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.dispatch_update({"update_id": 1, "message": {"chat": {"id": 222}, "from": {"id": 222},
                                "successful_payment": dict(self.payment, total_amount=1)}})
            bot.dispatch_update({"update_id": 2, "message": {"chat": {"id": 222}, "from": {"id": 222}, "text": "/id"}})
        self.assertEqual(1, len(self.store.payment_incidents()))
        self.assertFalse(self.store.status(222)["active"])
        self.assertEqual("notice", self.store.claim()[1]["kind"])
        self.assertEqual("message", self.store.claim()[1]["kind"])

    def test_dispatch_paid_event_activates_before_queued_notification(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text") as send:
            update = {"update_id": 1, "message": {"chat": {"id": 222}, "from": {"id": 222}, "successful_payment": self.payment}}
            bot.dispatch_update(update)
            bot.dispatch_update(update)
            send.assert_not_called()
        self.assertTrue(self.store.status(222)["active"])
        self.assertEqual("notice", self.store.claim()[1]["kind"])
        self.assertIsNone(self.store.claim())

    def test_wrong_owner_price_or_currency_rejected(self):
        for owner, change in [(223, {}), (222, {"total_amount": 1}), (222, {"currency": "RUB"})]:
            with self.subTest(change=change):
                self.assertFalse(self.store.checkout_valid(owner, dict(self.payment, **change)))
                with self.assertRaises(ValueError):
                    self.store.apply_payment(owner, dict(self.payment, **change))

    def test_expired_subscription_blocks_access(self):
        self.payment["subscription_expiration_date"] = int(time.time()) - 1
        self.store.apply_payment(222, self.payment)
        self.assertFalse(self.store.status(222)["active"])

    def test_refund_removes_paid_access(self):
        self.store.apply_payment(222, self.payment)
        self.store.refund(222, "charge-1")
        self.assertFalse(self.store.status(222)["active"])

    def test_reopening_database_keeps_access(self):
        self.store.apply_payment(222, self.payment)
        self.assertTrue(SubscriptionStore(self.store.path).status(222)["active"])

    def test_version_one_database_migrates_without_losing_access(self):
        self.store.apply_payment(222, self.payment)
        with sqlite3.connect(self.store.path) as db:
            db.execute("DROP TABLE payment_incidents")
            db.execute("PRAGMA user_version=1")
        upgraded = SubscriptionStore(self.store.path)
        self.assertTrue(upgraded.status(222)["active"])
        self.assertEqual([], upgraded.payment_incidents())

    def test_admin_grant_replay_is_idempotent(self):
        first = self.store.grant(222, 30, 111, "update-123")
        self.assertEqual(first, self.store.grant(222, 30, 111, "update-123"))

    def test_username_gift_binds_once_to_telegram_id(self):
        self.store.register_invite("freedemin", 30, 111)
        self.assertFalse(self.store.claim_invite("another", 222))
        self.assertTrue(self.store.claim_invite("FreeDemin", 222))
        self.assertTrue(self.store.status(222)["active"])
        self.assertFalse(self.store.claim_invite("freedemin", 223))
        self.assertFalse(self.store.status(223)["active"])

    def test_processing_requires_access_before_download(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "download_file") as download, \
                patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 222}, "from": {"id": 222},
                        "document": {"file_id": "x", "file_name": "a.pdf"}})
            download.assert_not_called()

    def test_invoice_price_currency_and_period(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"), \
                patch.object(bot, "tg", return_value="https://t.me/invoice/test") as tg:
            bot.subscribe(222, 222)
        kwargs = tg.call_args.kwargs
        self.assertEqual("XTR", kwargs["currency"])
        self.assertEqual(PERIOD, kwargs["subscription_period"])
        self.assertEqual(1000, kwargs["prices"][0]["amount"])

    def test_customer_cannot_grant_access(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 222}, "from": {"id": 222}, "text": "/grant 222 30"})
        self.assertFalse(self.store.status(222)["active"])

    def test_cancel_subscription_keeps_paid_access(self):
        self.store.apply_payment(222, self.payment)
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"), \
                patch.object(bot, "tg", return_value=True):
            bot.handle({"chat": {"id": 222}, "from": {"id": 222}, "text": "/cancel_subscription"})
        self.assertTrue(self.store.status(222)["active"])
        self.assertTrue(self.store.status(222)["invoice"]["canceled"])

    def test_queue_survives_restart_and_deduplicates_update(self):
        self.assertTrue(self.store.enqueue(555, {"kind": "document"}))
        self.assertFalse(self.store.enqueue(555, {"kind": "document"}))
        self.assertEqual(555, self.store.claim()[0])
        other = SubscriptionStore(self.store.path)
        other.recover_jobs()
        self.assertEqual(555, other.claim()[0])
        other.finish(555)
        self.assertIsNone(other.claim())

    def test_pdf_collection_returns_archive(self):
        with patch.object(bot, "STATE_DIR", Path(self.tmp.name)), patch.object(bot, "_store", self.store), \
                patch.object(bot, "CONFIG", dict(bot.CONFIG, admin_ids=[111])), \
                patch.object(bot, "download_file", return_value=ticket("kassir")), \
                patch.object(bot, "send_text"), patch.object(bot, "send_cleaned", return_value=True) as sent:
            message = {"chat": {"id": 111}, "from": {"id": 111}}
            bot.handle(dict(message, text="/batch"))
            bot.handle(dict(message, document={"file_id": "x", "file_name": "a.pdf"}))
            bot.handle(dict(message, document={"file_id": "y", "file_name": "b.pdf"}))
            bot.handle(dict(message, text="/done"))
            self.assertTrue(sent.call_args.args[2].endswith(".zip"))
            self.assertIn("р6 м23", sent.call_args.args[2])
            self.assertTrue(sent.call_args.args[1].startswith(b"PK"))
            spool = Path(self.tmp.name) / "spool"
            self.assertEqual([], [p for p in spool.glob("*/*") if p.is_file()])

    def test_collection_document_replay_keeps_one_spool_entry(self):
        with patch.object(bot, "STATE_DIR", Path(self.tmp.name)), patch.object(bot, "_store", self.store), \
                patch.object(bot, "CONFIG", dict(bot.CONFIG, admin_ids=[111])), \
                patch.object(bot, "download_file", return_value=ticket("kassir")), patch.object(bot, "send_text"):
            message = {"chat": {"id": 111}, "from": {"id": 111}}
            ident = self.store.create_collection(111, 111)
            document = dict(message, document={"file_id": "x", "file_name": "a.pdf"})
            bot.handle(document, update_id=123)
            bot.handle(document, update_id=123)
            files = self.store.collection_files(ident)
            self.assertEqual(1, len(files))
            self.assertEqual("123", Path(files[0]["path"]).name)

    def test_freedemin_configured_as_admin(self):
        self.assertIn(7868642254, bot.CONFIG["admin_ids"])
        self.assertTrue(bot.is_admin(7868642254))
        self.assertTrue(bot.is_admin(9999999999, "FreeDemin"))
        self.assertTrue(bot.has_access(7868642254))

    def test_freedemin_can_grant_and_revoke_access_by_id(self):
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 7868642254}, "from": {"id": 7868642254, "username": "FreeDemin"},
                        "text": "/grant 333 30"})
        self.assertTrue(self.store.status(333)["active"])
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 7868642254}, "from": {"id": 7868642254, "username": "FreeDemin"},
                        "text": "/revoke 333"})
        self.assertFalse(self.store.status(333)["active"])

    def test_admin_can_grant_access_by_username_invite_and_claimed(self):
        # 1. Unclaimed user: /grant @newuser 30 creates gift_invite
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 7868642254}, "from": {"id": 7868642254, "username": "FreeDemin"},
                        "text": "/grant @newuser 30"})
        # When newuser writes to bot, invite is claimed and access activated
        self.assertTrue(self.store.claim_invite("newuser", 444))
        self.assertTrue(self.store.status(444)["active"])

        # 2. Already claimed user: /grant @newuser 15 immediately extends
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 7868642254}, "from": {"id": 7868642254, "username": "FreeDemin"},
                        "text": "/grant @newuser 15"})
        self.assertTrue(self.store.status(444)["active"])

        # 3. Revoke by username
        with patch.object(bot, "_store", self.store), patch.object(bot, "send_text"):
            bot.handle({"chat": {"id": 7868642254}, "from": {"id": 7868642254, "username": "FreeDemin"},
                        "text": "/revoke @newuser"})
        self.assertFalse(self.store.status(444)["active"])

    def test_referral_system(self):
        self.store.touch_user(501, "referrer")
        self.store.touch_user(502, "newuser")

        ok, _ = self.store.register_referral(501, 501)
        self.assertFalse(ok)

        ok, ref_id = self.store.register_referral(501, 502)
        self.assertTrue(ok)
        self.assertEqual(501, ref_id)
        self.assertTrue(self.store.status(501)["active"])
        self.assertTrue(self.store.status(502)["active"])

        stats = self.store.get_referral_stats(501)
        self.assertEqual(1, stats["count"])
        self.assertEqual(3, stats["days_earned"])

        ok, _ = self.store.register_referral(501, 502)
        self.assertFalse(ok)

    def test_ticket_history(self):
        self.assertEqual([], self.store.get_ticket_history(601))

        self.store.add_ticket_history(601, "tok1", title="Чайка", date_str="04.11 19:00", seat_str="Ряд 3 Место 12", price=3000)
        self.store.add_ticket_history(601, "tok2", title="Гамлет", date_str="10.11 19:00", seat_str="Ряд 5 Место 1", price=2500)

        hist = self.store.get_ticket_history(601, limit=5)
        self.assertEqual(2, len(hist))
        self.assertEqual("tok2", hist[0]["token"])
        self.assertEqual("Гамлет", hist[0]["title"])
        self.assertEqual("tok1", hist[1]["token"])

    def test_soon_expiry_reminders(self):
        now = int(time.time())
        exp = now + 12 * 3600
        with self.store.db() as db:
            db.execute("INSERT INTO users(user_id, first_seen, last_seen) VALUES(701, ?, ?)", (now, now))
            db.execute("INSERT INTO grants(user_id, expires, admin_id) VALUES(701, ?, 0)", (exp,))

        due = self.store.due_soon_expiring_reminds(24)
        self.assertIn(701, due)

        self.store.mark_soon_expiry_reminded(701)
        due_after = self.store.due_soon_expiring_reminds(24)
        self.assertNotIn(701, due_after)

    def test_subscription_and_referral_cards(self):
        with patch.object(bot, "_store", self.store):
            text_inact, kb_inact = bot.build_subscription_card(801)
            self.assertIn("не активен", text_inact)
            self.assertIsNotNone(kb_inact)

            self.store.grant(801, 15, 0)
            text_act, kb_act = bot.build_subscription_card(801)
            self.assertIn("АКТИВЕН", text_act)
            self.assertIn("Осталось:", text_act)

            text_ref, kb_ref = bot.build_referral_card(801)
            self.assertIn("ПРИГЛАСИ КОЛЛЕГУ", text_ref)
            self.assertIn("ref_801", text_ref)

            text_hist, kb_hist = bot.build_history_card(801)
            self.assertIn("История", text_hist)


if __name__ == "__main__":
    unittest.main()
