"""Unit tests for advanced features:
1. Multi-page consecutive seat assignment
2. Force text eraser (/del <text> and waiting_del state)
3. Pushkin card stripping button (pushkin:<token>)
4. Quick sector selection (choose_sec:<token>:<sector>)
5. Avito marketplace preview (avito:<token> and make_avito_preview)
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import fitz

from test_processing import bot
import seat_tools
import eraser_tools
import ticket_meta


class AdvancedFeaturesTests(unittest.TestCase):
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

    def _sample_ticket_pdf(self, sector="Партер", row="6", seat="23", price="1 100", pages=1):
        doc = fitz.open()
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        for p in range(pages):
            page = doc.new_page(width=595, height=842)

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
            # Add a small barcode mock rect
            page.draw_rect(fitz.Rect(50, 400, 200, 450), color=(0, 0, 0), fill=(0, 0, 0))

        data = doc.tobytes()
        doc.close()
        return data

    def test_get_seats_for_pages(self):
        # Base single number
        s = seat_tools.get_seats_for_pages("12", 3)
        self.assertEqual(s, ["12", "13", "14"])

        # Range
        s = seat_tools.get_seats_for_pages("12-14", 3)
        self.assertEqual(s, ["12", "13", "14"])

        # Comma list
        s = seat_tools.get_seats_for_pages("12, 14, 16", 3)
        self.assertEqual(s, ["12", "14", "16"])

        # Number with letter suffix
        s = seat_tools.get_seats_for_pages("5А", 2)
        self.assertEqual(s, ["5А", "6А"])

        # Non-numeric string
        s = seat_tools.get_seats_for_pages("Входной", 2)
        self.assertEqual(s, ["Входной", "Входной"])

    def test_multi_page_consecutive_seats(self):
        # 2-page ticket: assigning "Ряд 3 Место 12" should make page 0 have seat 12 and page 1 have seat 13
        data = self._sample_ticket_pdf(sector="Партер", row="6", seat="23", pages=2)
        ok, result, info = bot.clean_ticket_with_report(data, seat_info="Ряд 3 Место 12")
        self.assertTrue(ok)
        self.assertTrue(info.get("changed"))

        with fitz.open(stream=result, filetype="pdf") as doc:
            self.assertEqual(len(doc), 2)
            # Page 0 has seat 12
            text_p0 = doc[0].get_text()
            self.assertIn("12", text_p0)
            self.assertIn("3", text_p0)

            # Page 1 has seat 13
            words_p1 = sorted([w for w in doc[1].get_text("words") if abs(w[1] - 102.76) < 2], key=lambda w: w[0])
            p1_joined = "".join(w[4] for w in words_p1)
            self.assertIn("3", p1_joined)
            self.assertIn("13", p1_joined)

    def test_sector_flexibility_parsing(self):
        # Various sectors
        cases = [
            ("Бельэтаж, ряд 2, место 5", "Бельэтаж", "2", "5"),
            ("Ложа 1, ряд 1, место 4", "Ложа 1", "1", "4"),
            ("Галерея, ряд 3, место 10", "Галерея", "3", "10"),
            ("Сектор B, ряд 4, место 22", "B", "4", "22"),
            ("Трибуна А, ряд 10, место 15", "Трибуна А", "10", "15"),
            ("Амфитеатр, ряд 7, место 8", "Амфитеатр", "7", "8"),
        ]
        for inp, exp_sec, exp_row, exp_seat in cases:
            p = seat_tools.parse_seat_input(inp)
            self.assertEqual(p.get("sector"), exp_sec, f"failed for {inp}")
            self.assertEqual(p.get("row"), exp_row, f"failed for {inp}")
            self.assertEqual(p.get("seat"), exp_seat, f"failed for {inp}")

    def test_choose_sec_callback_flow(self):
        store = bot.get_store()
        store.save_doc_token("tok_sec", "file_sec")
        cb = {
            "data": "choose_sec:tok_sec:Бельэтаж",
            "from": {"id": 1464797266, "username": "admin"},
            "message": {"chat": {"id": 100}, "message_id": 200},
        }
        with patch.object(bot, "send_text") as mock_send:
            bot.handle_callback_query(cb)
            state = store.get_user_state(1464797266)
            self.assertEqual(state, "waiting_seat_row:tok_sec:Бельэтаж")
            self.assertTrue(mock_send.called)
            self.assertIn("Бельэтаж", mock_send.call_args[0][1])

    def test_waiting_seat_row_input(self):
        store = bot.get_store()
        pdf_data = self._sample_ticket_pdf(sector="Партер", row="6", seat="23")
        store.save_doc_token("tok_sec_row", "file_sec_row")
        store.set_user_state(1464797266, "waiting_seat_row:tok_sec_row:Бельэтаж")

        msg = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1464797266, "username": "admin"},
            "text": "3 12",
        }

        with patch.object(bot, "send_text"), \
             patch.object(bot, "download_file", return_value=pdf_data), \
             patch.object(bot, "send_cleaned", return_value={"message_id": 888}) as mock_send_cleaned:
            bot.handle(msg)
            self.assertIsNone(store.get_user_state(1464797266))
            self.assertTrue(mock_send_cleaned.called)
            sent_pdf = mock_send_cleaned.call_args[0][1]

            with fitz.open(stream=sent_pdf, filetype="pdf") as doc:
                text = doc[0].get_text()
                self.assertIn("Бельэтаж", text)
                self.assertIn("3", text)
                self.assertIn("12", text)

    def test_del_command(self):
        store = bot.get_store()
        pdf_data = self._sample_ticket_pdf()
        store.save_doc_token("tok_del_cmd", "file_del_cmd")
        store.set_user_last_doc_token(1464797266, "tok_del_cmd")

        msg = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1464797266, "username": "admin"},
            "text": "/del МХТ им. Чехова",
        }

        with patch.object(bot, "send_text"), \
             patch.object(bot, "download_file", return_value=pdf_data), \
             patch.object(bot, "send_cleaned", return_value={"message_id": 999}) as mock_send_cleaned:
            bot.handle(msg)
            self.assertTrue(mock_send_cleaned.called)
            sent_pdf = mock_send_cleaned.call_args[0][1]

            with fitz.open(stream=sent_pdf, filetype="pdf") as doc:
                text = doc[0].get_text()
                self.assertNotIn("МХТ им. Чехова", text)

    def test_pushkin_callback(self):
        store = bot.get_store()
        pdf_data = self._sample_ticket_pdf()
        store.save_doc_token("tok_pk", "file_pk")

        cb = {
            "data": "pushkin:tok_pk",
            "from": {"id": 1464797266, "username": "admin"},
            "message": {"chat": {"id": 100}, "message_id": 200},
        }

        with patch.object(bot, "send_text"), \
             patch.object(bot, "download_file", return_value=pdf_data), \
             patch.object(bot, "send_cleaned", return_value={"message_id": 111}) as mock_send_cleaned:
            bot.handle_callback_query(cb)
            self.assertTrue(mock_send_cleaned.called)
            sent_pdf = mock_send_cleaned.call_args[0][1]

            with fitz.open(stream=sent_pdf, filetype="pdf") as doc:
                text = doc[0].get_text()
                self.assertNotIn("Пушкинская", text)

    def test_avito_preview_generation(self):
        pdf_data = self._sample_ticket_pdf()
        png_bytes = eraser_tools.make_avito_preview(pdf_data, srv=bot.srv)
        self.assertIsNotNone(png_bytes)
        self.assertTrue(len(png_bytes) > 1000)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_avito_callback(self):
        store = bot.get_store()
        pdf_data = self._sample_ticket_pdf()
        store.save_doc_token("tok_avito", "file_avito")

        cb = {
            "data": "avito:tok_avito",
            "from": {"id": 1464797266, "username": "admin"},
            "message": {"chat": {"id": 100}, "message_id": 200},
        }

        with patch.object(bot, "send_text"), \
             patch.object(bot, "download_file", return_value=pdf_data), \
             patch.object(bot, "send_photo") as mock_send_photo:
            bot.handle_callback_query(cb)
            self.assertTrue(mock_send_photo.called)
            photo_bytes = mock_send_photo.call_args[0][1]
            caption = mock_send_photo.call_args[1].get("caption", "")
            self.assertTrue(photo_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn("Авито", caption)


if __name__ == "__main__":
    unittest.main()
