import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

sys.modules.setdefault("zxingcpp", MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class TicketlandFixesTests(unittest.TestCase):

    def test_ticketland_social_resource_and_clustered_price(self):
        biletnaya_dir = os.environ.get("BILETNAYA", "/Users/mac/Билетная")
        font_path = str(Path(biletnaya_dir) / "vendor" / "Arial.ttf")
        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BILETNAYA": biletnaya_dir
        }):
            import fitz
            import bot
            import server

            doc = fitz.open()
            page = doc.new_page(width=842, height=595)

            def text(x, y, value, size=8):
                page.insert_text((x, y), value, fontname="F0", fontfile=font_path, fontsize=size)

            text(10, 580, "TICKETLAND.RU")
            text(230, 480, "Стоимость:")
            text(235, 501, "1 250 рублей", size=9)
            text(232, 505, "2500 руб", size=9)
            text(425, 552, "Ресурс Соц", size=10)
            text(425, 573, "srs.culture.elena@gmail.com", size=10)

            pdf_bytes = doc.tobytes()
            doc.close()

            # Clean ticket and change price to 3000
            ok, res, info = bot.clean_ticket_with_report(pdf_bytes, custom_price=3000)
            self.assertTrue(ok, f"Processing failed: {res}")

            out = fitz.open(stream=res, filetype="pdf")
            out_text = out[0].get_text().replace("\xa0", " ")

            # Check that Ресурс Соц and email were removed
            self.assertNotIn("Ресурс Соц", out_text)
            self.assertNotIn("srs.culture", out_text)

            # Check price replacement
            self.assertNotIn("1 250", out_text)
            self.assertNotIn("2500", out_text)
            self.assertIn("3 000", out_text)

            # Verify that only ONE price string is present in that slot
            words = [w[4].replace("\xa0", " ") for w in out[0].get_text("words") if 220 <= w[0] <= 320 and 485 <= w[1] <= 520]
            price_full = " ".join(words)
            self.assertEqual(price_full, "3 000 рублей")
            out.close()

    def test_ticketland_typography_consistency(self):
        biletnaya_dir = os.environ.get("BILETNAYA", "/Users/mac/Билетная")
        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BILETNAYA": biletnaya_dir
        }):
            import fitz
            import bot
            import server

            doc = fitz.open()
            page = doc.new_page(width=842, height=595)
            font_path = str(Path(biletnaya_dir) / "vendor" / "Arial.ttf")
            page.insert_text((235, 501), "1 250 рублей", fontname="ArialMT", fontfile=font_path, fontsize=10)
            cleaned, _ = server.autoclean_doc(doc)

            # Test price replacement runs
            style = {"font": "ArialMT", "bold": False, "serif": False}
            price_runs = bot._replacement_runs(cleaned, "test-doc", "2 500 рублей", style)
            self.assertEqual(len(price_runs), 1, f"Expected unified run, got: {price_runs}")
            self.assertIn("Arial", price_runs[0][0].name)
            self.assertEqual(price_runs[0][1], "2 500 рублей")

            # Test FIO replacement runs
            fio_runs = bot._replacement_runs(cleaned, "test-doc", "Иванов Иван Иванович", style)
            self.assertEqual(len(fio_runs), 1, f"Expected unified run, got: {fio_runs}")
            self.assertIn("Arial", fio_runs[0][0].name)
            self.assertEqual(fio_runs[0][1], "Иванов Иван Иванович")

            doc.close()

    def test_ticketland_long_spectator_name_clean(self):
        biletnaya_dir = os.environ.get("BILETNAYA", "/Users/mac/Билетная")
        font_path = str(Path(biletnaya_dir) / "vendor" / "Arial.ttf")
        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BILETNAYA": biletnaya_dir
        }):
            import fitz
            import bot

            doc = fitz.open()
            page = doc.new_page(width=842, height=595)

            def text(x, y, value, size=8):
                page.insert_text((x, y), value, fontname="F0", fontfile=font_path, fontsize=size)

            text(10, 580, "TICKETLAND.RU")
            text(230, 480, "Стоимость:")
            text(235, 501, "1 250 рублей", size=9)
            text(425, 521, 'В рамках программы "Пушкинская карта".', size=8)
            text(425, 529, 'Допускаются зрители строго от 14 до 22 лет.', size=8)
            long_name = "Константинопольская Александра Александровна"
            text(425, 552, long_name, size=10)
            text(425, 573, "bilety.longname@mail.ru", size=10)

            pdf_bytes = doc.tobytes()
            doc.close()

            ok, res, info = bot.clean_ticket_with_report(pdf_bytes)
            self.assertTrue(ok, f"Processing failed: {res}")

            out = fitz.open(stream=res, filetype="pdf")
            out_text = out[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Константинопольская", out_text)
            self.assertNotIn("bilety.longname", out_text)
            self.assertNotIn("Пушкинская карта", out_text)
            self.assertNotIn("строго от 14 до 22 лет", out_text)
            out.close()

    def test_ticketland_bdt_194102030_cleanup_and_editing(self):
        biletnaya_dir = os.environ.get("BILETNAYA", "/Users/mac/Билетная")
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "ticketland_bdt_194102030.pdf"
        self.assertTrue(fixture_path.exists(), f"Fixture missing: {fixture_path}")
        pdf_bytes = fixture_path.read_bytes()

        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BILETNAYA": biletnaya_dir
        }):
            import fitz
            import bot

            # 1. Clean ticket without arguments
            ok, res, info = bot.clean_ticket_with_report(pdf_bytes)
            self.assertTrue(ok, f"Processing failed: {res}")
            out = fitz.open(stream=res, filetype="pdf")
            page = out[0]
            out_text = page.get_text().replace("\xa0", " ")

            # Personal / Pushkin data removed
            self.assertNotIn("Соловьев", out_text)
            self.assertNotIn("zzgquqa", out_text)
            self.assertNotIn("Пушкинская карта", out_text)
            self.assertNotIn("от 14 до 22", out_text)
            self.assertNotIn("скачать фискальный чек", out_text)

            # Ticket identity and support info preserved
            self.assertIn("support@ticketland.ru", out_text)
            self.assertIn("БДТ им. Г.А. Товстоногова", out_text)
            self.assertIn("5 000 рублей", out_text)
            self.assertIn("658962123710", out_text)

            # Artwork and codes preserved (poster, MTS logo, 12+, QR code)
            self.assertEqual(len(page.get_image_info()), 5)
            out.close()

            # 2. Clean with spectator FIO and custom price
            ok_edit, res_edit, info_edit = bot.clean_ticket_with_report(
                pdf_bytes, spectator_fio="Иванов Иван Иванович", custom_price=3500
            )
            self.assertTrue(ok_edit, f"Editing failed: {res_edit}")
            out_edit = fitz.open(stream=res_edit, filetype="pdf")
            edit_text = " ".join(w[4].replace("\xa0", " ") for w in out_edit[0].get_text("words"))
            self.assertIn("Иванов Иван Иванович", edit_text)
            self.assertIn("3 500 рублей", edit_text)
            self.assertNotIn("5 000", edit_text)
            out_edit.close()


if __name__ == "__main__":
    unittest.main()
