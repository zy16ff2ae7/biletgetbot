"""Regression: mos.ru tickets — banner icon vs QR, FIO/price replacement fidelity.

Ticket order_21779455 (Театр Терезы Дуровой, 4 pages) failed delivery with
«Изменилось изображение QR-кода или штрихкода»: the checker treated the colored
document icon inside the «Управляйте своим заказом» banner as a raster code,
while the cleaner removes that whole banner by design.

The anonymized fixture carries a fabricated passenger name («Иванова Мария
Петровна») so name replacement is covered too: the old name must disappear
(it is drawn over otherwise), and replacement text must use the document's own
GolosText subsets (vendor Golos Text as the missing-glyph fallback) — not Arial.
"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import fitz

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
spec = importlib.util.spec_from_file_location("ticket_bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def _lines_of(page):
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 0:
            yield from block["lines"]


def _line_text(line):
    return "".join(span["text"] for span in line["spans"])


class MosBannerIconTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def test_promo_banner_icon_is_not_treated_as_code(self):
        data = (FIXTURES / "mos_tuz_aladdin_4page.pdf").read_bytes()
        ok, result, info = bot.clean_ticket_with_report(data)
        self.assertTrue(ok, msg=result if isinstance(result, str) else "cleanup failed")
        self.assertEqual(info["verification"]["readable_codes"], 4)
        with fitz.open(stream=result, filetype="pdf") as doc:
            self.assertEqual(len(doc), 4)
            self.assertNotIn("Управляйте своим заказом", doc[0].get_text())
            # Без запроса замены ФИО имя пассажира остаётся нетронутым
            self.assertIn("Иванова Мария Петровна", doc[0].get_text())

    def test_fio_and_price_replaced_with_matching_fonts(self):
        data = (FIXTURES / "mos_tuz_aladdin_4page.pdf").read_bytes()
        ok, result, info = bot.clean_ticket_with_report(
            data, spectator_fio="Морозова Елена Викторовна", custom_price="2500")
        self.assertTrue(ok, msg=result if isinstance(result, str) else "cleanup failed")
        self.assertEqual(info["verification"]["readable_codes"], 4)
        with fitz.open(stream=result, filetype="pdf") as doc:
            for pno, page in enumerate(doc):
                text = page.get_text()
                self.assertNotIn("Иванова Мария Петровна", text, f"p{pno}: старое ФИО не удалено")
                self.assertIn("Морозова Елена Викторовна", text, f"p{pno}: новое ФИО отсутствует")
                self.assertIn("2 500 руб. 00 коп.", text, f"p{pno}: новая цена отсутствует")

                name_lines = [l for l in _lines_of(page) if "Морозова" in _line_text(l)]
                self.assertEqual(len(name_lines), 1, f"p{pno}: ФИО напечатано не один раз")
                fonts = [s["font"] for s in name_lines[0]["spans"]]
                self.assertFalse(any("arial" in f.lower() for f in fonts),
                                 f"p{pno}: ФИО набрано фолбэком {fonts}")
                self.assertTrue(any("golos" in f.lower() for f in fonts),
                                f"p{pno}: нет вендорного Golos в {fonts}")

                price_lines = [l for l in _lines_of(page) if "2 500 руб." in _line_text(l)]
                self.assertEqual(len(price_lines), 1, f"p{pno}: цена напечатана не один раз")
                for span in price_lines[0]["spans"]:
                    self.assertNotIn("arial", span["font"].lower())
                    self.assertAlmostEqual(span["size"], 8.0, places=1)
                    self.assertEqual(span["color"], 0)

    def test_golos_documents_get_vendor_golos_fallback(self):
        srv = bot.srv
        cases = {
            "GolosText-SemiBold": "Golos Text SemiBold",
            "GolosText-Bold": "Golos Text Bold",
            "GolosText-Medium": "Golos Text Medium",
            "GolosText-Regular": "Golos Text Regular",
        }
        for doc_name, expected in cases.items():
            fonts = srv._fallbacks("Bold" in doc_name, False, doc_name)
            self.assertEqual(fonts[0].name, expected, doc_name)
            self.assertTrue(fonts[0].has_glyph(ord("Е")), doc_name)
        arial = srv._fallbacks(True, False, "ArialMT")
        self.assertNotIn("Golos", arial[0].name)


if __name__ == "__main__":
    unittest.main()
