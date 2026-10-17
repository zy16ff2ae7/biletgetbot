"""Tests for seat, row, and sector replacement functionality."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import fitz

from test_processing import bot
import seat_tools
import ticket_meta


class SeatReplacementTests(unittest.TestCase):
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

    def _sample_ticket_pdf(self, sector="Партер", row="6", seat="23", price="1 100"):
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
        text(345, 307, "Иванов Иван Иванович")
        text(42, 325, 'Билет в рамках программы "Пушкинская Карта".', 7)
        data = doc.tobytes()
        doc.close()
        return data

    def test_parse_seat_input(self):
        # Combined row and seat
        p = seat_tools.parse_seat_input("Ряд 3 Место 12")
        self.assertEqual(p, {"row": "3", "seat": "12"})

        # Combined sector, row, seat
        p = seat_tools.parse_seat_input("Партер, ряд 5, место 10")
        self.assertEqual(p, {"sector": "Партер", "row": "5", "seat": "10"})

        # Pure numbers "3 12"
        p = seat_tools.parse_seat_input("3 12")
        self.assertEqual(p, {"row": "3", "seat": "12"})

        # Row only
        p = seat_tools.parse_seat_input("ряд 7")
        self.assertEqual(p, {"row": "7"})

        # Seat only
        p = seat_tools.parse_seat_input("место 15")
        self.assertEqual(p, {"seat": "15"})

        # Sector only
        p = seat_tools.parse_seat_input("сектор Балкон")
        self.assertEqual(p, {"sector": "Балкон"})

        # Sector + row
        p = seat_tools.parse_seat_input("Балкон, ряд 2")
        self.assertEqual(p, {"sector": "Балкон", "row": "2"})

    def test_format_seat_summary(self):
        s = seat_tools.format_seat_summary({"row": "3", "seat": "12"})
        self.assertEqual(s, "ряд 3 · место 12")

        s = seat_tools.format_seat_summary({"sector": "Балкон", "row": "5", "seat": "10"})
        self.assertEqual(s, "сектор Балкон · ряд 5 · место 10")

    def test_new_text_for_line(self):
        # Combined line
        line = {"text": "Партер Ряд 6 Место 23", "tags": ["sector", "row", "seat"]}
        new_t = seat_tools.new_text_for_line(line, {"row": "3", "seat": "12", "sector": "Балкон"})
        self.assertEqual(new_t, "Балкон Ряд 3 Место 12")

        # Separate row line
        line_row = {"text": "Ряд: 6", "tags": ["row"]}
        new_row = seat_tools.new_text_for_line(line_row, {"row": "3"})
        self.assertEqual(new_row, "Ряд: 3")

        # Separate seat line
        line_seat = {"text": "Место 23", "tags": ["seat"]}
        new_seat = seat_tools.new_text_for_line(line_seat, {"seat": "12"})
        self.assertEqual(new_seat, "Место 12")

    def test_clean_ticket_with_seat_replacement(self):
        data = self._sample_ticket_pdf(sector="Партер", row="6", seat="23")
        ok, result, info = bot.clean_ticket_with_report(data, seat_info="Балкон, ряд 3, место 12")
        self.assertTrue(ok)
        self.assertTrue(info.get("changed"))
        self.assertEqual(info.get("seat_info"), "Балкон, ряд 3, место 12")

        # Verify new text exists and old text does not exist
        with fitz.open(stream=result, filetype="pdf") as doc:
            page_text = doc[0].get_text()
            self.assertIn("3", page_text)
            self.assertIn("12", page_text)
            self.assertIn("Балкон", page_text)
            self.assertNotIn("Партер", page_text)
            self.assertNotIn("Ряд 6", page_text)

    def test_single_ticket_markup_includes_seat_button(self):
        markup = bot._single_ticket_markup("tok123")
        buttons = [btn for row in markup["inline_keyboard"] for btn in row]
        seat_btns = [b for b in buttons if b["callback_data"] == "seat:tok123"]
        self.assertEqual(len(seat_btns), 1)
        self.assertEqual(seat_btns[0]["text"], "🪑 Изменить место/ряд")

    def test_seat_callback_query(self):
        store = bot.get_store()
        store.save_doc_token("tok_test", "file_abc")
        cb = {
            "data": "seat:tok_test",
            "from": {"id": 1464797266, "username": "admin"},
            "message": {"chat": {"id": 100}, "message_id": 200},
        }
        with patch.object(bot, "send_text") as mock_send:
            bot.handle_callback_query(cb)
            state = store.get_user_state(1464797266)
            self.assertEqual(state, "waiting_seat:tok_test")
            self.assertTrue(mock_send.called)
            self.assertIn("🪑 Введите новый ряд, место или сектор", mock_send.call_args[0][1])

    def test_reissue_ticket_via_waiting_seat_state(self):
        store = bot.get_store()
        pdf_data = self._sample_ticket_pdf(sector="Партер", row="6", seat="23")
        store.save_doc_token("tok_reissue", "file_sample_reissue")
        store.set_user_state(1464797266, "waiting_seat:tok_reissue")

        msg = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1464797266, "username": "admin"},
            "text": "Ряд 3 Место 12",
        }

        with patch.object(bot, "send_text"), \
             patch.object(bot, "download_file", return_value=pdf_data), \
             patch.object(bot, "send_cleaned", return_value={"message_id": 555}) as mock_send_cleaned:
            bot.handle(msg)

            # State should be reset
            self.assertIsNone(store.get_user_state(1464797266))
            self.assertTrue(mock_send_cleaned.called)

            # Check that reply_markup has new token with seat_info
            call_kwargs = mock_send_cleaned.call_args[1]
            markup = call_kwargs.get("reply_markup") or {}
            self.assertIn("inline_keyboard", markup)

            # Check caption includes seating info
            caption = mock_send_cleaned.call_args[0][3]
            self.assertIn("места 🪑", caption)

    def test_seat_command(self):
        store = bot.get_store()
        pdf_data = self._sample_ticket_pdf(sector="Партер", row="6", seat="23")
        store.save_doc_token("tok_cmd", "file_sample_cmd")
        store.set_user_last_doc_token(1464797266, "tok_cmd")

        msg = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1464797266, "username": "admin"},
            "text": "/seat Партер, ряд 5, место 10",
        }

        with patch.object(bot, "send_text"), \
             patch.object(bot, "download_file", return_value=pdf_data), \
             patch.object(bot, "send_cleaned", return_value={"message_id": 777}) as mock_send_cleaned:
            bot.handle(msg)
            self.assertTrue(mock_send_cleaned.called)
            sent_pdf = mock_send_cleaned.call_args[0][1]

            # Inspect modified pdf
            with fitz.open(stream=sent_pdf, filetype="pdf") as doc:
                text = doc[0].get_text()
                self.assertIn("5", text)
                self.assertIn("10", text)


if __name__ == "__main__":
    unittest.main()
