import os
import unittest
import fitz
import bot
srv = bot.srv

def _find_files_dir():
    candidates = [
        os.environ.get("PARTNER_FILES_DIR", ""),
        "/tmp/partner_test_files",
        os.path.join(os.path.dirname(__file__), "fixtures", "partner"),
        "/Users/mac/Downloads/ChatExport_UmaPalata ДОСТУПНЫЙ ДОСУГ СОЦРЕСУРС_2/files",
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return ""

FILES_DIR = _find_files_dir()


@unittest.skipUnless(os.path.isdir(FILES_DIR), "Chat export files not available")
class PartnerChatExportTests(unittest.TestCase):

    def test_promo_removal_gutarev(self):
        """32174_Гутарев.pdf: promo code must be cleaned, price replaced cleanly."""
        path = os.path.join(FILES_DIR, "32174_Гутарев.pdf")
        with open(path, "rb") as f:
            data = f.read()
        ok, res, info = bot.clean_ticket_with_report(data, custom_price=1500)
        self.assertTrue(ok)
        doc = fitz.open(stream=res, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
        self.assertNotIn("промо-код", text.lower())
        self.assertIn("1 500 руб.", text)

    def test_discount_warning_removal_ticketscloud(self):
        """32162_OYA-505770.pdf: discount legal notice must be removed, both prices updated."""
        path = os.path.join(FILES_DIR, "32162_OYA-505770.pdf")
        with open(path, "rb") as f:
            data = f.read()
        ok, res, info = bot.clean_ticket_with_report(data, custom_price=2000)
        self.assertTrue(ok)
        doc = fitz.open(stream=res, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
        self.assertNotIn("куплен со скидкой", text)
        self.assertNotIn("193", text)
        self.assertEqual(text.count("2000.00₽"), 2)

    def test_ticketland_discount_and_fiscal_removal_nikulina(self):
        """32168_Никулина1.pdf: discount and OrangeData fiscal check removed, price replaced."""
        path = os.path.join(FILES_DIR, "32168_Никулина1.pdf")
        with open(path, "rb") as f:
            data = f.read()
        ok, res, info = bot.clean_ticket_with_report(data, custom_price=2500)
        self.assertTrue(ok)
        doc = fitz.open(stream=res, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc).replace("\xa0", " ")
        self.assertNotIn("cheques-lk", text)
        self.assertNotIn("скачать фискальный чек", text)
        self.assertNotIn("Скидка", text)
        self.assertIn("2 500 рублей", text)
        self.assertIn("0 рублей", text)  # service fee intact

    def test_fiscal_qr_removal_dmitryeva(self):
        """32171_Дмитриева петсон.pdf: fiscal QR and receipt URL removed, price updated."""
        path = os.path.join(FILES_DIR, "32171_Дмитриева петсон.pdf")
        with open(path, "rb") as f:
            data = f.read()
        ok, res, info = bot.clean_ticket_with_report(data, custom_price=1500)
        self.assertTrue(ok)
        doc = fitz.open(stream=res, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc).replace("\xa0", " ")
        self.assertNotIn("cheques-lk", text)
        self.assertIn("1 500 рублей", text)


if __name__ == "__main__":
    unittest.main()
