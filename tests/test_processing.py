"""Offline checks: PDF processing and replies never call Telegram."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
import requests

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ticket_bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def ticket(kind, name_parts=None):
    doc = fitz.open()
    page = doc.new_page(width=842 if kind == "ticketland" else 595, height=595 if kind == "ticketland" else 842)
    font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

    def text(x, y, value, size=8):
        page.insert_text((x, y), value, fontname="F0", fontfile=font, fontsize=size)

    text(40, 90, "Ряд 6 Место 23")
    text(40, 110, "1 100 рублей")
    if kind == "kassir":
        text(211, 307, "Имя, фамилия")
        text(301, 307, "Name")
        text(345, 307, "Иванова Анна Александровна")
        text(42, 325, 'Билет в рамках программы "Пушкинская Карта".', 7)
        text(42, 335, "При входе на мероприятие могут попросить предъявить ваш документ, удостоверяющий личность.", 7)
        text(42, 345, "При нарушении правил программы Пушкинская карта вам могут отказать в посещении мероприятия.", 7)
    elif kind == "ticketland":
        text(10, 580, "TICKETLAND.RU")
        text(425, 560, name_parts if isinstance(name_parts, str) else (name_parts[0] if name_parts else "ИВАНОВ АЛЕКСАНДР СЕРГЕЕВИЧ"), 10)
    elif kind == "mos":
        text(42, 680, "Управляйте своим заказом")
        text(42, 694, "Для этого отсканируйте QR-код")
        text(42, 708, "bilet.mos.ru")
    elif kind == "intickets":
        for x, label in [(50, "Номер заказа:"), (155, "Номер билета:"),
                         (260, "Дата покупки:"), (365, "Стоимость:"), (453, "ФИО:")]:
            text(x, 120, label, 11)
        parts = name_parts or ("Константин Витальевич", "Астафьев")
        for index, part in enumerate(parts):
            text(453, 140 + index * 14, part, 10)
        text(365, 170, "В рамках программы «ПУШКИНСКАЯ КАРТА»", 8)
        text(50, 190, 'Уполномоченное лицо: ООО "ИНТИКЕТС", ИНН 7720312951')
    data = doc.tobytes()
    doc.close()
    return data


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def text_of(self, data):
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc[0].get_text().replace("\xa0", " ")

    def test_kassir_entire_name_row_and_program_removed(self):
        ok, result = bot.clean_ticket(ticket("kassir"))
        self.assertTrue(ok, result)
        text = self.text_of(result)
        for unwanted in ["Иванова", "Имя, фамилия", "Name", "Пушкин", "При входе"]:
            self.assertNotIn(unwanted, text)
        self.assertIn("Ряд 6 Место 23", text)

    def test_ticketland_already_cleaned_program_still_removes_uppercase_name(self):
        ok, result = bot.clean_ticket(ticket("ticketland"))
        self.assertTrue(ok, result)
        self.assertNotIn("ИВАНОВ", self.text_of(result))
        self.assertIn("Ряд 6 Место 23", self.text_of(result))

    def test_ticketland_removes_lowercase_name(self):
        ok, result = bot.clean_ticket(ticket("ticketland", "клочко Сергей  Васильевич"))
        self.assertTrue(ok, result)
        self.assertNotIn("клочко", self.text_of(result))
        self.assertNotIn("Васильевич", self.text_of(result))
        self.assertIn("Ряд 6 Место 23", self.text_of(result))

    def test_full_autoclean_removes_mos_promotional_text(self):
        ok, result = bot.clean_ticket(ticket("mos"))
        self.assertTrue(ok, result)
        self.assertNotIn("Управляйте", self.text_of(result))
        self.assertIn("Ряд 6 Место 23", self.text_of(result))

    def test_intickets_multiline_name_and_program_removed(self):
        ok, result = bot.clean_ticket(ticket("intickets"))
        self.assertTrue(ok, result)
        text = self.text_of(result)
        for unwanted in ["ФИО:", "Константин", "Витальевич", "Астафьев", "ПУШКИНСКАЯ"]:
            self.assertNotIn(unwanted, text)
        self.assertIn("Ряд 6 Место 23", text)

    def test_intickets_name_cell_cleared_with_apostrophes_and_accents(self):
        for parts in [("John David", "O'Connor"), ("José María", "García"),
                      ("Константин Витальевич", "О’Коннор")]:
            with self.subTest(parts=parts):
                ok, result = bot.clean_ticket(ticket("intickets", parts))
                self.assertTrue(ok, result)
                text = self.text_of(result)
                for part in parts:
                    self.assertNotIn(part, text)
                self.assertNotIn("ФИО:", text)
                self.assertIn("Ряд 6 Место 23", text)

    def test_plain_pdf_returned_unchanged(self):
        source = ticket("plain")
        ok, result = bot.clean_ticket(source)
        self.assertTrue(ok, result)
        self.assertEqual(source, result)

    def test_invalid_pdf_rejected(self):
        ok, _ = bot.clean_ticket(b"not a PDF")
        self.assertFalse(ok)

    def test_multiple_pages_processed(self):
        with fitz.open(stream=ticket("kassir"), filetype="pdf") as first, \
                fitz.open(stream=ticket("ticketland"), filetype="pdf") as second:
            first.insert_pdf(second)
            source = first.tobytes()
        ok, result = bot.clean_ticket(source)
        self.assertTrue(ok, result)
        with fitz.open(stream=result, filetype="pdf") as doc:
            self.assertEqual(2, len(doc))
            self.assertNotIn("Иванова", doc[0].get_text())
            self.assertNotIn("ИВАНОВ", doc[1].get_text())

    def test_telegram_errors_do_not_expose_token(self):
        with patch.object(bot, "BOT_TOKEN", "test-secret"):
            self.assertNotIn("test-secret", bot._safe_error(Exception("URL/bottest-secret/getFile")))

    def test_file_upload_retry_rewinds_pdf_bytes(self):
        sent = []
        class Response:
            def json(self):
                return {"ok": True}
        def post(_url, **kwargs):
            sent.append(kwargs["files"]["document"][1].read())
            if len(sent) == 1:
                raise requests.Timeout("test timeout")
            return Response()
        with patch.object(bot, "http_session") as session, patch.object(bot.time, "sleep"):
            session.return_value.post.side_effect = post
            self.assertTrue(bot.send_cleaned(111, b"pdf-bytes", "a.pdf", "test"))
        self.assertEqual([b"pdf-bytes", b"pdf-bytes"], sent)

    def test_password_pdf_rejected(self):
        with fitz.open(stream=ticket("plain"), filetype="pdf") as doc:
            data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                              owner_pw="owner", user_pw="user")
        self.assertFalse(bot.clean_ticket(data)[0])

    def run_handler(self, source, delivered=True):
        with tempfile.TemporaryDirectory() as td:
            legacy = {"111": {"debt": 150, "uses": 3}}
            legacy_path = Path(td) / "debts.json"
            legacy_path.write_text(json.dumps(legacy))
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            config = dict(bot.CONFIG, admin_ids=[111])
            with patch.object(bot, "STATE_DIR", Path(td)), \
                patch.object(bot, "_store", store), patch.object(bot, "CONFIG", config), \
                patch.object(bot, "download_file", return_value=source), \
                patch.object(bot, "send_text") as send_text, \
                patch.object(bot, "send_cleaned", return_value=delivered) as send_file:
                bot.handle({"chat": {"id": 111}, "from": {"id": 111},
                            "document": {"file_id": "test", "file_name": "TICKET.PDF"}})
                return json.loads(legacy_path.read_text()), send_file.call_args, send_text.call_args_list

    def test_admin_delivery_without_per_file_charge(self):
        debt, call, _ = self.run_handler(ticket("kassir"))
        self.assertIsNotNone(call)
        self.assertEqual({"111": {"debt": 150, "uses": 3}}, debt)
        self.assertTrue(call.args[2].endswith(".pdf"))
        self.assertIn("р6", call.args[2])

    def test_no_charge_for_unchanged_file(self):
        debt, call, _ = self.run_handler(ticket("plain"))
        self.assertIsNotNone(call)
        self.assertEqual({"111": {"debt": 150, "uses": 3}}, debt)

    def test_no_charge_for_failed_delivery(self):
        debt, _, _ = self.run_handler(ticket("kassir"), delivered=False)
        self.assertEqual({"111": {"debt": 150, "uses": 3}}, debt)

    def test_engine_warning_returns_explanation_without_sending_pdf(self):
        def warning(doc):
            out = fitz.open(stream=doc.tobytes(), filetype="pdf")
            return out, [{"page": 0, "warn": ["test failed reflow"]}]

        with patch.object(bot.srv, "autoclean_doc", side_effect=warning):
            ok, result = bot.clean_ticket(ticket("kassir"))
        self.assertFalse(ok)
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
