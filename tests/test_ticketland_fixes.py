import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

sys.modules.setdefault("zxingcpp", MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class TicketlandFixesTests(unittest.TestCase):

    def test_ticketland_social_resource_and_clustered_price(self):
        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BILETNAYA": "/Users/mac/Билетная"
        }):
            import fitz
            import bot
            import server

            doc = fitz.open()
            page = doc.new_page(width=842, height=595)
            font_path = "/Users/mac/Билетная/vendor/Arial.ttf"

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


if __name__ == "__main__":
    unittest.main()
