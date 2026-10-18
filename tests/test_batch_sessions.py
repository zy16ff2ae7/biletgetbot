import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import zipfile

import fitz

from test_processing import bot, ticket
import ticket_meta


class BatchSessionsTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def _sample_ticket_pdf(self, title="Чайка", row="3", seat="12", name=None, price=3000):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        def text(x, y, val, size=8):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

        text(50, 50, "МХТ им. Чехова")
        text(50, 70, f"Спектакль: {title}")
        text(50, 90, "04.11.2026 19:00")
        text(50, 110, f"Партер Ряд {row} Место {seat}")
        text(50, 130, f"{price} руб.")
        text(211, 307, "Имя, фамилия")
        text(301, 307, "Name")
        text(345, 307, name or "Иванова Анна Александровна")
        text(42, 325, 'Билет в рамках программы "Пушкинская Карта".', 7)
        data = doc.tobytes()
        doc.close()
        return data

    def test_batch_session_init_and_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(1001, 30, 1001)

            t1 = self._sample_ticket_pdf(title="Чайка", row="3", seat="12")
            t2 = self._sample_ticket_pdf(title="Чайка", row="3", seat="13")

            batch_res = {
                "ok": 2,
                "failed": 0,
                "changed": 2,
                "cleaned_items": [
                    {"name": "t1.pdf", "output_name": "t1.pdf", "orig_data": t1, "result": t1, "meta": {"title": "Чайка", "row": "3", "seats": ["12"]}},
                    {"name": "t2.pdf", "output_name": "t2.pdf", "orig_data": t2, "result": t2, "meta": {"title": "Чайка", "row": "3", "seats": ["13"]}},
                ]
            }

            with patch.object(bot, "STATE_DIR", td_path), patch.object(bot, "_store", store):
                token = bot._init_batch_session(1001, 1001, batch_res)
                self.assertIsNotNone(token)

                session = store.get_batch_session(token)
                self.assertIsNotNone(session)
                self.assertEqual(2, len(session["items"]))
                self.assertEqual("1001", str(session["user_id"]))

                # Check dashboard text and buttons
                text, markup = bot._build_batch_dashboard(token)
                self.assertIn("Управление билетами в пачке", text)
                self.assertIn("всего: 2 шт.", text)
                buttons = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
                self.assertIn("№ 1", buttons)
                self.assertIn("№ 2", buttons)
                self.assertIn("👥 Список ФИО", buttons)
                self.assertIn("🪑 Рассадить по порядку", buttons)
                self.assertIn("💰 Задать цену всем", buttons)
                self.assertIn("🎁 Скрыть все цены", buttons)

                # Check item card
                card_text, card_markup = bot._build_batch_item_card(token, 0)
                self.assertIn("Билет №1 из 2", card_text)
                self.assertIn("Чайка", card_text)
                card_buttons = [btn["text"] for row in card_markup["inline_keyboard"] for btn in row]
                self.assertIn("👤 Изменить ФИО", card_buttons)
                self.assertIn("🪑 Изменить место/ряд", card_buttons)
                self.assertIn("✏️ Изменить цену", card_buttons)

    def test_batch_individual_reprocess(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(1002, 30, 1002)

            t1 = self._sample_ticket_pdf(title="Гамлет", row="5", seat="10")
            batch_res = {
                "ok": 1,
                "failed": 0,
                "changed": 1,
                "cleaned_items": [
                    {"name": "t1.pdf", "output_name": "t1.pdf", "orig_data": t1, "result": t1, "meta": {"title": "Гамлет", "row": "5", "seats": ["10"]}},
                ]
            }

            with patch.object(bot, "STATE_DIR", td_path), patch.object(bot, "_store", store):
                token = bot._init_batch_session(1002, 1002, batch_res)

                # Edit FIO on ticket 0
                ok, item = bot._reprocess_batch_item(token, 0, fio="Петров Петр Петрович")
                self.assertTrue(ok)
                self.assertEqual("Петров Петр Петрович", item["fio"])

                clean_pdf = Path(item["clean_path"]).read_bytes()
                with fitz.open(stream=clean_pdf, filetype="pdf") as doc:
                    txt = doc[0].get_text().replace("\xa0", " ")
                    self.assertIn("Петров Петр Петрович", txt)

                # Edit Seat on ticket 0
                ok, item = bot._reprocess_batch_item(token, 0, seat_info="Партер, ряд 7, место 25")
                self.assertTrue(ok)
                self.assertEqual("Партер, ряд 7, место 25", item["seat_info"])

                # Edit Price on ticket 0
                ok, item = bot._reprocess_batch_item(token, 0, custom_price=5000)
                self.assertTrue(ok)
                self.assertEqual(5000, item["custom_price"])

                # Hide price on ticket 0
                ok, item = bot._reprocess_batch_item(token, 0, hide_price=True)
                self.assertTrue(ok)
                self.assertTrue(item["hide_price"])

    def test_mass_fio_assignment(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(1003, 30, 1003)

            t1 = self._sample_ticket_pdf(title="Ревизор", row="1", seat="1")
            t2 = self._sample_ticket_pdf(title="Ревизор", row="1", seat="2")
            t3 = self._sample_ticket_pdf(title="Ревизор", row="1", seat="3")

            batch_res = {
                "ok": 3,
                "failed": 0,
                "changed": 3,
                "cleaned_items": [
                    {"name": "t1.pdf", "orig_data": t1, "result": t1},
                    {"name": "t2.pdf", "orig_data": t2, "result": t2},
                    {"name": "t3.pdf", "orig_data": t3, "result": t3},
                ]
            }

            with patch.object(bot, "STATE_DIR", td_path), patch.object(bot, "_store", store):
                token = bot._init_batch_session(1003, 1003, batch_res)

                fios = [
                    "Иванов Иван Иванович",
                    "Петров Петр Петрович",
                    "Сидорова Анна Павловна"
                ]
                count = bot._apply_mass_fio_to_batch(token, fios)
                self.assertEqual(3, count)

                session = store.get_batch_session(token)
                self.assertEqual("Иванов Иван Иванович", session["items"][0]["fio"])
                self.assertEqual("Петров Петр Петрович", session["items"][1]["fio"])
                self.assertEqual("Сидорова Анна Павловна", session["items"][2]["fio"])

    def test_mass_sequential_seating(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(1004, 30, 1004)

            t1 = self._sample_ticket_pdf(title="Щелкунчик", row="1", seat="1")
            t2 = self._sample_ticket_pdf(title="Щелкунчик", row="1", seat="1")

            batch_res = {
                "ok": 2,
                "failed": 0,
                "changed": 2,
                "cleaned_items": [
                    {"name": "t1.pdf", "orig_data": t1, "result": t1},
                    {"name": "t2.pdf", "orig_data": t2, "result": t2},
                ]
            }

            with patch.object(bot, "STATE_DIR", td_path), patch.object(bot, "_store", store):
                token = bot._init_batch_session(1004, 1004, batch_res)

                count, summary = bot._apply_mass_seats_to_batch(token, "Партер, ряд 5, место 10")
                self.assertEqual(2, count)

                session = store.get_batch_session(token)
                self.assertIn("место 10", session["items"][0]["seat_info"])
                self.assertIn("место 11", session["items"][1]["seat_info"])

    def test_interactive_batch_states_in_handle(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(1005, 30, 1005)

            t1 = self._sample_ticket_pdf(title="Онегин", row="2", seat="5")
            t2 = self._sample_ticket_pdf(title="Онегин", row="2", seat="6")

            batch_res = {
                "ok": 2,
                "failed": 0,
                "changed": 2,
                "cleaned_items": [
                    {"name": "t1.pdf", "orig_data": t1, "result": t1},
                    {"name": "t2.pdf", "orig_data": t2, "result": t2},
                ]
            }

            sent_texts = []
            def fake_send_text(chat, txt, markup=None):
                sent_texts.append(txt)
                return True

            with patch.object(bot, "STATE_DIR", td_path), \
                 patch.object(bot, "_store", store), \
                 patch.object(bot, "send_text", side_effect=fake_send_text):

                token = bot._init_batch_session(1005, 1005, batch_res)

                # 1. Test waiting_batch_fio
                store.set_user_state(1005, f"waiting_batch_fio:{token}")
                bot.handle({
                    "chat": {"id": 1005, "type": "private"},
                    "from": {"id": 1005},
                    "text": "Пушкин Александр Сергеевич\nКрылов Иван Андреевич"
                })
                self.assertTrue(any("ФИО успешно назначены" in t for t in sent_texts))
                session = store.get_batch_session(token)
                self.assertEqual("Пушкин Александр Сергеевич", session["items"][0]["fio"])
                self.assertEqual("Крылов Иван Андреевич", session["items"][1]["fio"])

                # 2. Test waiting_batch_seat
                store.set_user_state(1005, f"waiting_batch_seat:{token}")
                bot.handle({
                    "chat": {"id": 1005, "type": "private"},
                    "from": {"id": 1005},
                    "text": "3 15"
                })
                self.assertTrue(any("Рассадка применена" in t for t in sent_texts))
                session = store.get_batch_session(token)
                self.assertIn("15", session["items"][0]["seat_info"])
                self.assertIn("16", session["items"][1]["seat_info"])

                # 3. Test waiting_batch_price
                store.set_user_state(1005, f"waiting_batch_price:{token}")
                bot.handle({
                    "chat": {"id": 1005, "type": "private"},
                    "from": {"id": 1005},
                    "text": "4000"
                })
                self.assertTrue(any("Цена 4000 ₽ установлена" in t for t in sent_texts))
                session = store.get_batch_session(token)
                self.assertEqual(4000, session["items"][0]["custom_price"])
                self.assertEqual(4000, session["items"][1]["custom_price"])

                # 4. Test cancel on waiting_batch_price
                store.set_user_state(1005, f"waiting_batch_price:{token}")
                bot.handle({
                    "chat": {"id": 1005, "type": "private"},
                    "from": {"id": 1005},
                    "text": "отмена"
                })
                self.assertTrue(any("отменено" in t for t in sent_texts))
                self.assertIsNone(store.get_user_state(1005))

    def test_batch_callbacks(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(1006, 30, 1006)

            t1 = self._sample_ticket_pdf(title="Мастер и Маргарита", row="4", seat="20")
            t2 = self._sample_ticket_pdf(title="Мастер и Маргарита", row="4", seat="21")

            batch_res = {
                "ok": 2,
                "failed": 0,
                "changed": 2,
                "cleaned_items": [
                    {"name": "t1.pdf", "orig_data": t1, "result": t1},
                    {"name": "t2.pdf", "orig_data": t2, "result": t2},
                ]
            }

            delivered_files = []
            def fake_send_cleaned(chat, data, name, caption, reply_markup=None, **kwargs):
                delivered_files.append((name, data, reply_markup))
                return True

            sent_texts = []
            def fake_send_text(chat, txt, reply_markup=None, **kwargs):
                sent_texts.append(txt)
                return True

            edited_texts = []
            def fake_tg(method, **kwargs):
                if method == "editMessageText":
                    edited_texts.append(kwargs.get("text"))
                return True

            with patch.object(bot, "STATE_DIR", td_path), \
                 patch.object(bot, "_store", store), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_text", side_effect=fake_send_text), \
                 patch.object(bot, "tg", side_effect=fake_tg):

                token = bot._init_batch_session(1006, 1006, batch_res)

                # 1. Callback bcfg
                bot.handle_callback_query({
                    "data": f"bcfg:{token}",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                all_texts = sent_texts + edited_texts
                self.assertTrue(any("Управление билетами в пачке" in t for t in all_texts))

                # 2. Callback bedit
                bot.handle_callback_query({
                    "data": f"bedit:{token}:0",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                all_texts = sent_texts + edited_texts
                self.assertTrue(any("Билет №1 из 2" in t for t in all_texts))

                # 3. Callback bset:fio
                bot.handle_callback_query({
                    "data": f"bset:fio:{token}:0",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                self.assertEqual(f"waiting_batch_item_fio:{token}:0", store.get_user_state(1006))

                # 4. Callback bset:seat
                bot.handle_callback_query({
                    "data": f"bset:seat:{token}:0",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                self.assertEqual(f"waiting_batch_item_seat:{token}:0", store.get_user_state(1006))

                # 5. Callback bzip
                bot.handle_callback_query({
                    "data": f"bzip:{token}",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                self.assertTrue(delivered_files[-1][0].endswith(".zip"))

                # 6. Callback bmerge
                bot.handle_callback_query({
                    "data": f"bmerge:{token}",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                self.assertTrue(delivered_files[-1][0].endswith(".pdf"))

                # 7. Callback bunpack
                bot.handle_callback_query({
                    "data": f"bunpack:{token}",
                    "from": {"id": 1006},
                    "message": {"chat": {"id": 1006}, "message_id": 999}
                })
                self.assertTrue(len(delivered_files) >= 4)

    def test_user_presets_store(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")

            # Initially empty
            self.assertEqual([], store.get_user_presets(2001))

            # Save preset
            pid1 = store.save_user_preset(2001, "Мой Большой", fio="Иванов И.И.", custom_price=5000, hide_price=0, batch_mode="zip", auto_preview=1)
            self.assertIsNotNone(pid1)

            presets = store.get_user_presets(2001)
            self.assertEqual(1, len(presets))
            self.assertEqual("Мой Большой", presets[0]["name"])
            self.assertEqual("Иванов И.И.", presets[0]["fio"])
            self.assertEqual(5000, presets[0]["custom_price"])
            self.assertEqual(0, presets[0]["hide_price"])
            self.assertEqual("zip", presets[0]["batch_mode"])
            self.assertEqual(1, presets[0]["auto_preview"])

            # Get single preset
            p = store.get_user_preset(pid1, 2001)
            self.assertIsNotNone(p)
            self.assertEqual("Мой Большой", p["name"])

            # Delete preset
            store.delete_user_preset(pid1, 2001)
            self.assertEqual([], store.get_user_presets(2001))

            # Limit: max 10
            for i in range(10):
                store.save_user_preset(2001, f"Пресет {i+1}")
            with self.assertRaises(ValueError):
                store.save_user_preset(2001, "Лишний пресет")

    def test_templates_card_and_application(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(2002, 30, 2002)

            with patch.object(bot, "STATE_DIR", td_path), patch.object(bot, "_store", store):
                # Check templates card text and buttons
                text, markup = bot.build_templates_card(2002)
                self.assertIn("Шаблоны настроек печати", text)
                self.assertIn("Подарочный", text)
                self.assertIn("Стандарт", text)
                self.assertIn("Для печати", text)
                buttons = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
                self.assertIn("🎁 Подарочный", buttons)
                self.assertIn("💼 Стандарт", buttons)
                self.assertIn("📑 Для печати", buttons)
                self.assertIn("💎 Своя цена 2500 ₽", buttons)
                self.assertIn("💾 Сохранить текущие как шаблон", buttons)

                # Apply gift preset to user
                bot._apply_preset_to_user(2002, bot.BUILTIN_PRESETS["gift"])
                u_set = store.get_user_settings(2002)
                self.assertEqual(1, u_set["hide_price"])
                self.assertEqual(1, u_set["auto_preview"])
                self.assertEqual("zip", u_set["batch_mode"])

                # Apply print preset to user
                bot._apply_preset_to_user(2002, bot.BUILTIN_PRESETS["print"])
                u_set = store.get_user_settings(2002)
                self.assertEqual(0, u_set["hide_price"])
                self.assertEqual("merge", u_set["batch_mode"])

                # Custom preset apply
                pid = store.save_user_preset(2002, "Тест VIP", custom_price=9900, hide_price=0, batch_mode="separate", auto_preview=1)
                custom_p = store.get_user_preset(pid, 2002)
                bot._apply_preset_to_user(2002, custom_p)
                u_set = store.get_user_settings(2002)
                self.assertEqual(9900, u_set["custom_price"])
                self.assertEqual("separate", u_set["batch_mode"])

    def test_templates_apply_to_batch(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            store.grant(2003, 30, 2003)

            t1 = self._sample_ticket_pdf(title="Ревизор", row="1", seat="1", price=1500)
            t2 = self._sample_ticket_pdf(title="Ревизор", row="1", seat="2", price=1500)

            batch_res = {
                "ok": 2,
                "failed": 0,
                "changed": 2,
                "cleaned_items": [
                    {"name": "t1.pdf", "output_name": "t1.pdf", "orig_data": t1, "result": t1, "meta": {"title": "Ревизор", "price": 1500}},
                    {"name": "t2.pdf", "output_name": "t2.pdf", "orig_data": t2, "result": t2, "meta": {"title": "Ревизор", "price": 1500}},
                ]
            }

            with patch.object(bot, "STATE_DIR", td_path), patch.object(bot, "_store", store):
                token = bot._init_batch_session(2003, 2003, batch_res)

                # Apply gift preset to batch
                count = bot._apply_preset_to_batch(token, bot.BUILTIN_PRESETS["gift"])
                self.assertEqual(2, count)
                session = store.get_batch_session(token)
                self.assertTrue(all(it["hide_price"] for it in session["items"]))

                # Apply price2500 preset to batch
                count = bot._apply_preset_to_batch(token, bot.BUILTIN_PRESETS["price2500"])
                self.assertEqual(2, count)
                session = store.get_batch_session(token)
                self.assertTrue(all(not it["hide_price"] for it in session["items"]))
                self.assertTrue(all(it["custom_price"] == 2500 for it in session["items"]))


if __name__ == "__main__":
    unittest.main()
