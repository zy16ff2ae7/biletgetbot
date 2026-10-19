"""Tests for new templates (Afisha, Intickets, Kassa24) and multi-page batch FIO replacement."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import fitz

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ticket_bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
srv = bot.srv


def create_sample_kassa24_pdf():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")
    def text(x, y, val, size=8):
        page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)
    text(40, 50, "E-TICKET #12345 В РАМКАХ ПРОГРАММЫ", 9)
    text(40, 100, "Театр танца им. В.А. Елизарова", 12)
    text(40, 120, "ПОКУПАТЕЛЬСултанова Алиса Алексеевна", 10)
    text(40, 150, "РЯД 2", 10)
    text(40, 170, "МЕСТО 4", 10)
    text(40, 190, "ЦЕНА 1500 руб.", 10)
    text(40, 500, "ЛИНИЯ СГИБА", 8)
    data = doc.tobytes()
    doc.close()
    return data


def create_multipage_mxt_pdf():
    doc = fitz.open()
    font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")
    for pno in range(2):
        page = doc.new_page(width=600, height=852)
        def text(x, y, val, size=8):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)
        text(50, 50, "МХТ им. А.П. Чехова", 12)
        text(50, 70, "Чайка", 14)
        text(50, 90, "04.11.2026 19:00", 10)
        text(50, 110, f"Ряд {pno+1} Место {pno+1}", 10)
        text(50, 130, "3000 руб.", 10)
        text(50, 200, "ООО «Компания Афиша»", 8)
    data = doc.tobytes()
    doc.close()
    return data


class NewTemplatesAndFioTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def test_split_fio_list_variants(self):
        self.assertEqual(bot.split_fio_list("Иванов Иван\nПетров Петр"), ["Иванов Иван", "Петров Петр"])
        self.assertEqual(bot.split_fio_list("1. Иванов Иван\n2. Петров Петр"), ["Иванов Иван", "Петров Петр"])
        self.assertEqual(bot.split_fio_list("Иванов Иван; Петров Петр"), ["Иванов Иван", "Петров Петр"])
        self.assertEqual(bot.split_fio_list("Иванов Иван // Петров Петр"), ["Иванов Иван", "Петров Петр"])
        self.assertEqual(bot.split_fio_list("Иванов Иван"), ["Иванов Иван"])
        self.assertEqual(bot.split_fio_list(""), [])

    def test_kassa24_buyer_removed_line_of_fold_preserved(self):
        pdf_data = create_sample_kassa24_pdf()
        ok, res, info = bot.clean_ticket_with_report(pdf_data)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Султанова", text)
            self.assertNotIn("В РАМКАХ ПРОГРАММЫ", text)
            self.assertIn("ЛИНИЯ СГИБА", text)
            self.assertIn("РЯД 2", text)
            self.assertIn("МЕСТО 4", text)

    def test_multipage_sequential_fio_replacement(self):
        pdf_data = create_multipage_mxt_pdf()
        fios = "Астафьев Константин\nИванова Анна"
        ok, res, info = bot.clean_ticket_with_report(pdf_data, spectator_fio=fios)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            self.assertEqual(len(doc), 2)
            p0_text = doc[0].get_text().replace("\xa0", " ")
            p1_text = doc[1].get_text().replace("\xa0", " ")
            self.assertIn("Астафьев Константин", p0_text)
            self.assertNotIn("Иванова Анна", p0_text)
            self.assertIn("Иванова Анна", p1_text)
            self.assertNotIn("Астафьев Константин", p1_text)


    def test_real_kassa24_file(self):
        sample_path = Path("/Users/mac/Downloads/ticket_6ac63e7115b6b92c2c4c5930.pdf")
        if not sample_path.exists():
            return
        pdf_bytes = sample_path.read_bytes()
        ok, res, info = bot.clean_ticket_with_report(pdf_bytes)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Султанова", text)
            self.assertNotIn("В РАМКАХ ПРОГРАММЫ", text)
            self.assertIn("ЛИНИЯ СГИБА", text)

    def test_real_afisha_file(self):
        sample_path = Path("/Users/mac/Downloads/Afisha-Tickets-27717118.pdf")
        if not sample_path.exists():
            return
        pdf_bytes = sample_path.read_bytes()
        ok, res, info = bot.clean_ticket_with_report(pdf_bytes)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Филипушкина", text)
            self.assertNotIn("ПУШКИНСКАЯ", text)

    def test_real_intickets_file(self):
        sample_path = Path("/Users/mac/Downloads/Заказ № 29690245.pdf")
        if not sample_path.exists():
            return
        pdf_bytes = sample_path.read_bytes()
        ok, res, info = bot.clean_ticket_with_report(pdf_bytes)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Васильев", text)
            self.assertNotIn("ПУШКИНСКАЯ", text)
            self.assertIn("puppet.ru", text)


if __name__ == "__main__":
    unittest.main()
