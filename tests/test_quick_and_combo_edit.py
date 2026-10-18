"""Tests for quick 1-line ticket editing and batch combo editor."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import fitz

from test_processing import bot
import seat_tools
import ticket_meta


class QuickAndComboEditTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

        self.store = bot.SubscriptionStore(Path(self.td.name) / "subscriptions.sqlite3")
        store_patch = patch.object(bot, "_store", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)

    def _sample_ticket_pdf(self, sector="Партер", row="6", seat="23", price="1 100", fio="Иванов Иван Иванович"):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        def text(x, y, val, size=8):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

        text(50, 50, "МХТ им. Чехова")
        text(50, 70, "Спектакль: Вишнёвый сад")
        text(50, 90, "15.11.2026 19:00")
        text(50, 110, f"{sector} Ряд {row} Место {seat}")
        text(50, 130, f"{price} рублей")
        text(211, 307, "Имя, фамилия")
        text(301, 307, "Name")
        text(345, 307, fio)
        data = doc.tobytes()
        doc.close()
        return data

    def test_parse_quick_edit_variations(self):
        # Full combo
        q = seat_tools.parse_quick_edit("Иванов И.И. 2500 ряд 3 место 12")
        self.assertEqual(q["fio"], "Иванов И.И.")
        self.assertEqual(q["custom_price"], 2500)
        self.assertEqual(q["seat_info"], {"row": "3", "seat": "12"})
        self.assertFalse(q["reset_all"])

        # Price only with currency
        q = seat_tools.parse_quick_edit("3500 руб")
        self.assertIsNone(q["fio"])
        self.assertEqual(q["custom_price"], 3500)
        self.assertIsNone(q["seat_info"])

        # Price only plain number
        q = seat_tools.parse_quick_edit("4000")
        self.assertEqual(q["custom_price"], 4000)

        # FIO only
        q = seat_tools.parse_quick_edit("Петров Сергей Викторович")
        self.assertEqual(q["fio"], "Петров Сергей Викторович")
        self.assertIsNone(q["custom_price"])
        self.assertIsNone(q["seat_info"])

        # Seats only
        q = seat_tools.parse_quick_edit("Партер, ряд 5, место 10")
        self.assertEqual(q["seat_info"], {"sector": "Партер", "row": "5", "seat": "10"})

        # Hide price
        q = seat_tools.parse_quick_edit("скрыть цену")
        self.assertTrue(q["hide_price"])

        # Show price
        q = seat_tools.parse_quick_edit("показать цену 5000")
        self.assertFalse(q["hide_price"])
        self.assertEqual(q["custom_price"], 5000)

        # Resets
        self.assertTrue(seat_tools.parse_quick_edit("сброс")["reset_all"])
        self.assertTrue(seat_tools.parse_quick_edit("сброс фио")["reset_fio"])
        self.assertTrue(seat_tools.parse_quick_edit("сброс цены")["reset_price"])
        self.assertTrue(seat_tools.parse_quick_edit("сброс мест")["reset_seat"])

    def test_markup_buttons(self):
        token = "testtok123"
        markup = bot._single_ticket_markup(token, pages=1)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"quick:{token}")
        self.assertIn("Быстрая правка", markup["inline_keyboard"][0][0]["text"])

        # Batch token in markup when pages > 1
        markup_multi = bot._single_ticket_markup(token, pages=3, batch_token="batch999")
        callbacks = [btn["callback_data"] for row in markup_multi["inline_keyboard"] for btn in row]
        self.assertIn("bmass:combo:batch999", callbacks)

    def test_batch_dashboard_and_item_card_markup(self):
        batch_token = "btok_sample"
        # Mock session with 2 items
        items = [
            {"idx": 0, "orig_name": "t1.pdf", "clean_path": "/fake/1", "meta": {}, "fio": None},
            {"idx": 1, "orig_name": "t2.pdf", "clean_path": "/fake/2", "meta": {}, "fio": None},
        ]
        self.store.save_batch_session(batch_token, 12345, 12345, items)

        text, kb = bot._build_batch_dashboard(batch_token)
        callbacks = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn(f"bmass:combo:{batch_token}", callbacks)

        card_text, card_kb = bot._build_batch_item_card(batch_token, 0)
        card_callbacks = [btn["callback_data"] for row in card_kb["inline_keyboard"] for btn in row]
        self.assertIn(f"bset:quick:{batch_token}:0", card_callbacks)

    def test_apply_mass_combo_to_batch_multiline(self):
        # Prepare actual dummy files for items
        t1_bytes = self._sample_ticket_pdf(row="1", seat="1", price="1000", fio="Old 1")
        t2_bytes = self._sample_ticket_pdf(row="1", seat="2", price="1000", fio="Old 2")
        p1 = Path(self.td.name) / "t1.pdf"
        p2 = Path(self.td.name) / "t2.pdf"
        p1.write_bytes(t1_bytes)
        p2.write_bytes(t2_bytes)

        batch_token = "btok_multiline"
        items = [
            {"idx": 0, "orig_name": "t1.pdf", "orig_path": str(p1), "clean_path": str(p1), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
            {"idx": 1, "orig_name": "t2.pdf", "orig_path": str(p2), "clean_path": str(p2), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
        ]
        self.store.save_batch_session(batch_token, 12345, 12345, items)

        raw_input = (
            "1. Иванов Иван — ряд 10 место 15 — 3500 руб\n"
            "2. Петров Петр — ряд 10 место 16 — скрыть цену"
        )
        count, reports = bot._apply_mass_combo_to_batch(batch_token, raw_input)
        self.assertEqual(count, 2)
        self.assertEqual(len(reports), 2)

        session = self.store.get_batch_session(batch_token)
        updated_items = session["items"]
        self.assertEqual(updated_items[0]["fio"], "Иванов Иван")
        self.assertEqual(updated_items[0]["custom_price"], 3500)
        self.assertEqual(updated_items[0]["seat_info"], "ряд 10 · место 15")
        self.assertFalse(updated_items[0]["hide_price"])

        self.assertEqual(updated_items[1]["fio"], "Петров Петр")
        self.assertEqual(updated_items[1]["seat_info"], "ряд 10 · место 16")
        self.assertTrue(updated_items[1]["hide_price"])

    def test_apply_mass_combo_to_batch_single_line_sequence(self):
        t1_bytes = self._sample_ticket_pdf(row="1", seat="1", price="1000")
        t2_bytes = self._sample_ticket_pdf(row="1", seat="2", price="1000")
        p1 = Path(self.td.name) / "t1_seq.pdf"
        p2 = Path(self.td.name) / "t2_seq.pdf"
        p1.write_bytes(t1_bytes)
        p2.write_bytes(t2_bytes)

        batch_token = "btok_seq"
        items = [
            {"idx": 0, "orig_name": "t1.pdf", "orig_path": str(p1), "clean_path": str(p1), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
            {"idx": 1, "orig_name": "t2.pdf", "orig_path": str(p2), "clean_path": str(p2), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
        ]
        self.store.save_batch_session(batch_token, 12345, 12345, items)

        raw_input = "Партер, ряд 5 место 10, 4500 руб"
        count, reports = bot._apply_mass_combo_to_batch(batch_token, raw_input)
        self.assertEqual(count, 2)

        session = self.store.get_batch_session(batch_token)
        updated_items = session["items"]
        self.assertEqual(updated_items[0]["seat_info"], "сектор Партер, ряд 5, место 10")
        self.assertEqual(updated_items[0]["custom_price"], 4500)
        self.assertEqual(updated_items[1]["seat_info"], "сектор Партер, ряд 5, место 11")
        self.assertEqual(updated_items[1]["custom_price"], 4500)

    def test_quick_edit_single_ticket_reissue(self):
        pdf_bytes = self._sample_ticket_pdf(row="2", seat="5", price="1500")
        tok = "quick_reissue_tok"
        bot._save_doc_token(tok, "file_id_mock", custom_price=1500, hide_price=False, fio="Old Name", seat_info=None, uid=123)

        with patch.object(bot, "download_file", return_value=pdf_bytes), \
             patch.object(bot, "send_cleaned", return_value=True) as mock_send:
            bot._reissue_ticket_with_quick(123, 123, tok, "Сидоров 3000 ряд 8 место 20")
            self.assertTrue(mock_send.called)
            args, kwargs = mock_send.call_args
            markup = kwargs.get("reply_markup") or {}
            self.assertIn("quick:", markup.get("inline_keyboard", [[]])[0][0]["callback_data"])

    def test_quick_edit_reset_does_not_revive_settings(self):
        # User has saved custom price and saved FIO in settings
        self.store.set_user_custom_price(123, 5000)
        self.store.set_user_fio(123, "Default User FIO")

        pdf_bytes = self._sample_ticket_pdf(row="2", seat="5", price="1500")
        tok = "quick_reset_tok"
        bot._save_doc_token(tok, "file_id_mock", custom_price=2000, hide_price=False, fio="Custom Name", seat_info="ряд 2, место 5", uid=123)

        with patch.object(bot, "download_file", return_value=pdf_bytes), \
             patch.object(bot, "clean_ticket_with_report", wraps=bot.clean_ticket_with_report) as mock_clean, \
             patch.object(bot, "send_cleaned", return_value=True):
            bot._reissue_ticket_with_quick(123, 123, tok, "сброс")
            self.assertTrue(mock_clean.called)
            args, kwargs = mock_clean.call_args
            # When reset, spectator_fio, custom_price, and seat_info should be None
            self.assertIsNone(kwargs.get("spectator_fio"))
            self.assertIsNone(kwargs.get("custom_price"))
            self.assertIsNone(kwargs.get("seat_info"))
            self.assertFalse(kwargs.get("hide_price"))

    def test_apply_mass_combo_explicit_indices(self):
        t1_bytes = self._sample_ticket_pdf(row="1", seat="1", price="1000")
        t2_bytes = self._sample_ticket_pdf(row="1", seat="2", price="1000")
        t3_bytes = self._sample_ticket_pdf(row="1", seat="3", price="1000")
        p1 = Path(self.td.name) / "t1_gap.pdf"
        p2 = Path(self.td.name) / "t2_gap.pdf"
        p3 = Path(self.td.name) / "t3_gap.pdf"
        p1.write_bytes(t1_bytes)
        p2.write_bytes(t2_bytes)
        p3.write_bytes(t3_bytes)

        batch_token = "btok_gaps"
        items = [
            {"idx": 0, "orig_name": "t1.pdf", "orig_path": str(p1), "clean_path": str(p1), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
            {"idx": 1, "orig_name": "t2.pdf", "orig_path": str(p2), "clean_path": str(p2), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
            {"idx": 2, "orig_name": "t3.pdf", "orig_path": str(p3), "clean_path": str(p3), "meta": {}, "fio": None, "custom_price": None, "hide_price": False, "seat_info": None},
        ]
        self.store.save_batch_session(batch_token, 12345, 12345, items)

        # Update ticket 1 and ticket 3, skipping ticket 2
        raw_input = (
            "1. Иванов 2000\n"
            "3. Сидоров 4000"
        )
        count, reports = bot._apply_mass_combo_to_batch(batch_token, raw_input)
        self.assertEqual(count, 2)

        session = self.store.get_batch_session(batch_token)
        updated_items = session["items"]
        self.assertEqual(updated_items[0]["fio"], "Иванов")
        self.assertEqual(updated_items[0]["custom_price"], 2000)
        self.assertIsNone(updated_items[1]["fio"])  # Unchanged!
        self.assertEqual(updated_items[2]["fio"], "Сидоров")
        self.assertEqual(updated_items[2]["custom_price"], 4000)

    def test_parse_seat_edge_cases(self):
        p = seat_tools.parse_seat_input("балкон 1 ряд 3 место 5")
        self.assertEqual(p.get("sector"), "балкон 1")
        self.assertEqual(p.get("row"), "3")
        self.assertEqual(p.get("seat"), "5")

        p = seat_tools.parse_seat_input("р 5 м 12")
        self.assertEqual(p.get("row"), "5")
        self.assertEqual(p.get("seat"), "12")

        q = seat_tools.parse_quick_edit("0 руб")
        self.assertEqual(q.get("custom_price"), 0)


if __name__ == "__main__":
    unittest.main()
