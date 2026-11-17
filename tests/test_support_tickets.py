"""Support conversations stay durable, private and separate from PDF jobs."""
from contextlib import ExitStack
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_processing import bot
from subscriptions import SubscriptionStore


class SupportButtonTests(unittest.TestCase):
    def test_button_opens_internal_support(self):
        button = bot.support_markup()["inline_keyboard"][0][0]
        self.assertEqual("support:menu", button.get("callback_data"))
        self.assertNotIn("url", button)


class SupportStoreTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("support_tickets")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "support.sqlite3"
        self.store = self.module.SupportStore(self.path)

    def create(self, update=10, user=501):
        return self.store.create(user, user, "Не получается обработать PDF", update, [900])

    def test_create_persists_history_and_admin_notification(self):
        ticket = self.create()
        reopened = self.module.SupportStore(self.path)
        self.assertEqual("open", reopened.ticket(ticket["id"], 501)["status"])
        self.assertEqual(1, len(reopened.messages(ticket["id"], 501)))
        self.assertEqual(900, reopened.claim_notifications()[0]["recipient_id"])

    def test_create_replay_is_idempotent(self):
        first = self.create()
        self.assertEqual(first["id"], self.create()["id"])
        self.assertEqual(1, len(self.store.list_tickets(501)))
        self.assertEqual(1, len(self.store.messages(first["id"], 501)))
        self.assertTrue(self.store.handled_update(10, 501))
        self.assertFalse(self.store.handled_update(10, 502))

    def test_other_user_cannot_read_reply_close_or_get_attachment(self):
        ticket = self.store.create(501, 501, "Не открывается PDF", 10, [900],
                                   attachment={"kind": "document", "file_id": "private-pdf"})
        mid = self.store.messages(ticket["id"], 501)[0]["id"]
        self.assertIsNone(self.store.ticket(ticket["id"], 502))
        self.assertIsNone(self.store.message(mid, 502))
        with self.assertRaises(ValueError):
            self.store.add_message(ticket["id"], 502, "Чужой ответ", 11, [900])
        with self.assertRaises(ValueError):
            self.store.close(ticket["id"], 502, [900])

    def test_staff_reply_and_user_followup_update_status(self):
        ticket = self.create()
        self.store.add_message(ticket["id"], 900, "Проверили, попробуйте снова", 11, [900], staff=True)
        self.assertEqual("answered", self.store.ticket(ticket["id"], 501)["status"])
        self.store.add_message(ticket["id"], 501, "Спасибо, сейчас проверю", 12, [900])
        self.assertEqual("open", self.store.ticket(ticket["id"], 501)["status"])
        self.assertEqual(3, len(self.store.messages(ticket["id"], 501)))

    def test_reply_replay_does_not_duplicate_history(self):
        ticket = self.create()
        for _ in range(2):
            self.store.add_message(ticket["id"], 501, "Дополнение", 11, [900])
        self.assertEqual(2, len(self.store.messages(ticket["id"], 501)))

    def test_closed_ticket_refuses_new_messages(self):
        ticket = self.create()
        self.store.close(ticket["id"], 501, [900])
        self.assertEqual("closed", self.store.ticket(ticket["id"], 501)["status"])
        with self.assertRaises(ValueError):
            self.store.add_message(ticket["id"], 501, "Дополнение", 11, [900])

    def test_open_ticket_limit_and_text_limit(self):
        for update in range(3):
            self.create(update)
        with self.assertRaises(ValueError):
            self.create(4)
        with self.assertRaises(ValueError):
            self.store.create(502, 502, "x" * 3001, 5, [900])
        self.assertEqual([], self.store.list_tickets(502))

    def test_pending_notifications_survive_failure_and_restart(self):
        self.create()
        pending = self.store.claim_notifications(now=10**10)
        self.assertEqual(1, len(pending))
        self.store.finish_notification(pending[0]["id"], False, now=10**10)
        reopened = self.module.SupportStore(self.path)
        pending = reopened.claim_notifications(now=10**10 + 4000)
        self.assertEqual(1, len(pending))
        reopened.finish_notification(pending[0]["id"], True, now=10**10 + 4000)
        self.assertEqual([], reopened.claim_notifications(now=10**10 + 8000))


class SupportFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("support_tickets")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(bot, "STATE_DIR", root))
        self.store = self.module.SupportStore(root / "support.sqlite3")
        self.states = SubscriptionStore(root / "subscriptions.sqlite3")
        self.send = Mock(return_value=True)
        self.desk = self.module.SupportDesk(self.store, self.states, self.send,
                                          lambda uid, username=None: uid == 900, [900])

    def msg(self, uid=501, text=None, **extra):
        result = {"chat": {"id": uid, "type": "private"}, "from": {"id": uid}}
        if text is not None:
            result["text"] = text
        result.update(extra)
        return result

    def cb(self, data, uid=501):
        return self.desk.callback(data, uid, uid)

    def create(self):
        self.cb("support:new")
        self.desk.handle_message(self.msg(text="Не обрабатывается документ"), 10)
        return self.store.list_tickets(501)[0]

    def test_new_request_keeps_support_conversation_and_queues_notification(self):
        self.create()
        self.assertEqual("support:reply:1", self.states.get_user_state(501))
        self.assertIn("#1", self.send.call_args.args[1])
        self.desk.flush_notifications()
        self.assertTrue(any(call.args[0] == 900 for call in self.send.call_args_list))

    def test_screenshot_caption_is_a_support_attachment(self):
        self.cb("support:new")
        self.desk.handle_message(self.msg(caption="Вот ошибка", photo=[{"file_id": "screen"}]), 10)
        ticket = self.store.list_tickets(501)[0]
        message = self.store.messages(ticket["id"], 501)[0]
        self.assertEqual("photo", message["attachment_kind"])
        self.assertEqual("Вот ошибка", message["body"])

    def test_staff_reply_is_delivered_to_owner_inside_bot(self):
        ticket = self.create()
        self.cb("support:reply:" + str(ticket["id"]), 900)
        self.desk.handle_message(self.msg(900, "Исправлено, проверьте"), 11)
        self.desk.flush_notifications()
        self.assertTrue(any(call.args[0] == 501 and "Исправлено" in call.args[1]
                            for call in self.send.call_args_list))

    def test_nonstaff_cannot_open_queue_or_reply_to_other_user(self):
        ticket = self.create()
        self.send.reset_mock()
        self.cb("support:queue", 502)
        self.cb("support:reply:" + str(ticket["id"]), 502)
        self.assertIsNone(self.states.get_user_state(502))
        self.assertTrue(all("Не обрабатывается" not in str(call) for call in self.send.call_args_list))

    def test_cancel_does_not_create_ticket(self):
        self.cb("support:new")
        self.desk.handle_message(self.msg(text="отмена"), 10)
        self.assertIsNone(self.states.get_user_state(501))
        self.assertEqual([], self.store.list_tickets(501))

    def test_unknown_slash_command_exits_support_input(self):
        self.cb("support:new")
        self.assertFalse(self.desk.handle_message(self.msg(text="/start"), 10))
        self.assertIsNone(self.states.get_user_state(501))

    def test_invalid_input_keeps_composer_open(self):
        self.cb("support:new")
        self.desk.handle_message(self.msg(text="x" * 3001), 10)
        self.assertEqual("support:new", self.states.get_user_state(501))
        self.assertEqual([], self.store.list_tickets(501))

    def test_pdf_in_support_never_enters_pdf_processing(self):
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.states))
        self.stack.enter_context(patch.object(bot, "get_support_desk", return_value=self.desk))
        process = self.stack.enter_context(patch.object(bot, "process_incoming_document"))
        self.cb("support:new")
        bot.dispatch_update({"update_id": 10, "message": self.msg(
            caption="Проблемный файл", document={"file_id": "source", "file_name": "ticket.pdf"})})
        ident, payload = self.states.claim()
        bot.handle(payload["message"], update_id=ident)
        self.states.finish(ident)
        process.assert_not_called()
        self.assertEqual(1, len(self.store.list_tickets(501)))
        # Recovery of the same update must not turn the support PDF into a ticket-edit job.
        bot.handle(payload["message"], update_id=ident)
        process.assert_not_called()
        self.assertEqual(1, len(self.store.list_tickets(501)))

    def test_two_queued_support_pdfs_stay_in_one_conversation(self):
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.states))
        self.stack.enter_context(patch.object(bot, "get_support_desk", return_value=self.desk))
        process = self.stack.enter_context(patch.object(bot, "process_incoming_document"))
        self.cb("support:new")
        for ident in (10, 11):
            bot.dispatch_update({"update_id": ident, "message": self.msg(
                document={"file_id": f"source-{ident}", "file_name": "ticket.pdf"})})
        for _ in range(2):
            ident, payload = self.states.claim()
            bot.handle(payload["message"], update_id=ident)
            self.states.finish(ident)
        process.assert_not_called()
        ticket = self.store.list_tickets(501)[0]
        self.assertEqual(2, len(self.store.messages(ticket["id"], 501)))

    def test_support_callback_is_a_barrier_for_auto_batch(self):
        self.states.enqueue(10, {"kind": "callback_query", "callback": {
            "from": {"id": 501}, "message": {"chat": {"id": 501}}, "data": "support:new"}})
        self.states.enqueue(11, {"kind": "message", "message": self.msg(
            document={"file_id": "support-file", "file_name": "ticket.pdf"})})
        self.assertEqual([], self.states.claim_pending_documents(501, 501))

    def test_support_command_is_a_barrier_for_auto_batch(self):
        self.states.enqueue(10, {"kind": "message", "message": self.msg(text="/support")})
        self.states.enqueue(11, {"kind": "message", "message": self.msg(
            document={"file_id": "support-file", "file_name": "ticket.pdf"})})
        self.assertEqual([], self.states.claim_pending_documents(501, 501))

    def test_other_users_support_does_not_block_auto_batch(self):
        self.states.enqueue(10, {"kind": "message", "message": self.msg(502, "/support")})
        self.states.enqueue(11, {"kind": "message", "message": self.msg(
            document={"file_id": "normal-file", "file_name": "ticket.pdf"})})
        self.assertEqual(1, len(self.states.claim_pending_documents(501, 501)))

    def test_main_menu_exits_support_conversation(self):
        self.create()
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.states))
        self.stack.enter_context(patch.object(bot, "send_welcome"))
        bot.handle_callback_query({"from": {"id": 501}, "message": {"chat": {"id": 501}},
                                   "data": "welcome:menu"})
        self.assertIsNone(self.states.get_user_state(501))

    def test_exit_support_button_returns_to_document_mode(self):
        self.create()
        self.cb("support:exit")
        self.assertIsNone(self.states.get_user_state(501))

    def test_replay_after_commit_restores_conversation_state(self):
        self.create()
        self.states.set_user_state(501, "support:new")
        self.desk.handle_message(self.msg(text="Повтор того же сообщения"), 10)
        self.assertEqual("support:reply:1", self.states.get_user_state(501))

    def test_revoked_staff_does_not_receive_private_notifications(self):
        self.create()
        self.desk.is_staff = lambda uid, username=None: False
        self.send.reset_mock()
        self.desk.flush_notifications()
        self.send.assert_not_called()

    def test_attachment_opens_only_for_owner_or_staff(self):
        self.cb("support:new")
        self.desk.handle_message(self.msg(photo=[{"file_id": "screen"}]), 10)
        message = self.store.messages(1, 501)[0]
        self.cb(f"support:file:{message['id']}")
        self.assertEqual({"kind": "photo", "file_id": "screen"}, self.send.call_args.kwargs["attachment"])
        self.send.reset_mock()
        self.cb(f"support:file:{message['id']}", 502)
        self.assertTrue(all("attachment" not in call.kwargs for call in self.send.call_args_list))

    def test_queue_pagination_history_and_message_view(self):
        for uid in range(501, 512):
            self.store.create(uid, uid, "Нужна помощь", uid, [900])
        self.cb("support:queue", 900)
        buttons = [b for row in self.send.call_args.args[2]["inline_keyboard"] for b in row]
        self.assertTrue(any(b["callback_data"] == "support:queue:10" for b in buttons))
        self.cb("support:queue:10", 900)
        for ident in range(20, 26):
            self.store.add_message(1, 501, "Дополнение", ident, [900])
        self.cb("support:view:1")
        self.cb("support:history:1:0")
        mid = self.store.messages(1, 501)[0]["id"]
        self.cb(f"support:message:{mid}")
        self.assertIn("Нужна помощь", self.send.call_args.args[1])

    def test_support_remains_available_with_exhausted_free_quota(self):
        self.states.daily_consume(501, count=self.states.DAILY_LIMIT)
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.states))
        self.stack.enter_context(patch.object(bot, "get_support_desk", return_value=self.desk))
        bot.handle(self.msg(text="/support"), update_id=10)
        self.cb("support:new")
        bot.handle(self.msg(text="Нужна помощь с ошибкой"), update_id=11)
        self.assertEqual(1, len(self.store.list_tickets(501)))
        self.assertEqual(0, self.states.daily_left(501))

    def test_failed_notification_send_remains_pending(self):
        self.create()
        self.send.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            self.desk.flush_notifications()
        self.assertEqual(1, len(self.store.claim_notifications(now=10**10)))

    def test_view_activates_the_selected_conversation(self):
        first = self.create()
        second = self.store.create(502, 502, "Другой вопрос", 11, [900])
        self.cb(f"support:reply:{first['id']}", 900)
        self.cb(f"support:view:{second['id']}", 900)
        self.assertEqual(f"support:reply:{second['id']}", self.states.get_user_state(900))

    def test_message_view_cannot_leave_staff_reply_on_another_ticket(self):
        first = self.create()
        second = self.store.create(502, 502, "Другой вопрос", 11, [900])
        mid = self.store.messages(second["id"], 502)[0]["id"]
        self.cb(f"support:reply:{first['id']}", 900)
        self.cb(f"support:message:{mid}", 900)
        self.desk.handle_message(self.msg(900, "Ответ на второй вопрос"), 12)
        self.assertEqual("open", self.store.ticket(first["id"], 501)["status"])
        self.assertEqual("answered", self.store.ticket(second["id"], 502)["status"])
