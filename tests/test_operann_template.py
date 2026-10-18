import io
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from test_processing import bot
import ticket_meta
import seat_tools
import eraser_tools


class OperannTemplateTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def _make_operann_pdf(self, spectator_name="Аристова Анна", is_pushkin=False, row="1", seat="27", price=3000):
        doc = fitz.open()
        page = doc.new_page(width=595.28, height=841.89)
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        def txt(x, y, val, size=11.0, bold=False):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

        # Header right
        txt(402.6, 62.0, "Нижегородский государственный академический театр", size=6.0)
        txt(458.9, 70.4, "оперы и балета им. А.С.Пушкина", size=6.0)
        txt(408.6, 87.2, "603024, г. Нижний Новгород, ул. Белинского, дом 59", size=6.0)
        txt(413.7, 95.6, "Тел.: +7 (831) 234-05-34 (касса), +7 (831) 234-05-31", size=6.0)
        txt(500.0, 104.0, "ИНН 5262035539", size=6.0)
        txt(504.2, 112.4, "www.operann.ru", size=6.0)

        # Left column
        txt(54.5, 254.1, "22.11.2026 15:00", size=11.0)
        txt(54.5, 272.0, "Лебединое озеро", size=13.5, bold=True)
        txt(54.5, 287.0, "Основной зал", size=10.5)
        txt(54.5, 303.5, "Нижний Новгород, ул. Белинского, д. 59", size=10.5)
        txt(54.5, 321.3, f"Партер, ряд {row}, место {seat}", size=13.2, bold=True)
        txt(54.5, 337.3, "Вход в зрительный зал после третьего звонка запрещён", size=11.0)

        # Right column
        txt(428.6, 302.9, "Билет №5804867764138", size=11.0)
        txt(467.3, 319.4, f"Цена: {price} руб.", size=11.0)
        txt(412.2, 335.9, "Номер заказа: 39682878662", size=11.0)

        if is_pushkin:
            txt(385.5, 352.4, f"Билет на имя: {spectator_name}", size=11.0)
            txt(487.1, 368.9, "Евгеньевич", size=11.0)
            txt(378.0, 385.4, "Билет куплен в рамках программы", size=11.0)
            txt(410.9, 401.9, "«ПУШКИНСКАЯ КАРТА»", size=11.0)
        else:
            txt(487.1, 368.8, spectator_name, size=11.0)

        # Draw mock barcode bars so verification passes
        for i in range(15):
            bx = 420 + i * 8
            page.draw_rect(fitz.Rect(bx, 240, bx + 3, 285), color=(0, 0, 0), fill=(0, 0, 0))

        # Page 2: Box office hours
        page2 = doc.new_page(width=595.28, height=841.89)
        page2.insert_text((158.6, 64.2), "КАССА ТЕАТРА +7 (831) 234-05-34, режим работы: 9:30-19:00",
                          fontname="F0", fontfile=font, fontsize=7.5)

        data = doc.tobytes()
        doc.close()
        return data

    def test_metadata_extraction_operann(self):
        pdf_bytes = self._make_operann_pdf()
        meta = ticket_meta.extract_ticket_metadata(pdf_bytes, srv=bot.srv)
        self.assertEqual("Нижегородский театр оперы и балета", meta.get("theater"))
        self.assertEqual("Лебединое озеро", meta.get("title"))
        self.assertEqual("22.11.2026", meta.get("date"))
        self.assertEqual("15:00", meta.get("time"))
        self.assertEqual("Партер", meta.get("sector"))
        self.assertEqual("1", meta.get("row"))
        self.assertEqual(["27"], meta.get("seats"))
        self.assertEqual(3000, meta.get("price"))
        self.assertEqual(2, meta.get("pages"))

        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="operann.pdf")
        self.assertIn("Лебединое озеро", filename)
        self.assertIn("Нижегородский театр оперы и балета", filename)
        self.assertIn("Партер", filename)
        self.assertIn("р1", filename)
        self.assertIn("м27", filename)

    def test_autoclean_removes_standalone_fio(self):
        pdf_bytes = self._make_operann_pdf(spectator_name="Аристова Анна", is_pushkin=False)
        ok, cleaned_bytes, info = bot._clean_ticket_with_report_impl(pdf_bytes)
        self.assertTrue(ok)
        self.assertTrue(info["changed"])

        clean_doc = fitz.open(stream=cleaned_bytes, filetype="pdf")
        text = clean_doc[0].get_text().replace("\xa0", " ")
        self.assertNotIn("Аристова", text)
        self.assertNotIn("Анна", text)
        # Core metadata preserved
        self.assertIn("Лебединое озеро", text)
        self.assertIn("Партер, ряд 1, место 27", text)
        self.assertIn("Цена: 3000 руб.", text)
        clean_doc.close()

    def test_autoclean_removes_pushkin_card_and_name(self):
        pdf_bytes = self._make_operann_pdf(spectator_name="Замараев Дмитрий", is_pushkin=True)
        ok, cleaned_bytes, info = bot._clean_ticket_with_report_impl(pdf_bytes)
        self.assertTrue(ok)
        self.assertTrue(info["changed"])

        clean_doc = fitz.open(stream=cleaned_bytes, filetype="pdf")
        text = clean_doc[0].get_text().replace("\xa0", " ")
        self.assertNotIn("Замараев", text)
        self.assertNotIn("Дмитрий", text)
        self.assertNotIn("Евгеньевич", text)
        self.assertNotIn("ПУШКИНСКАЯ КАРТА", text)
        self.assertNotIn("в рамках программы", text)
        # Core metadata preserved
        self.assertIn("Лебединое озеро", text)
        self.assertIn("Партер, ряд 1, место 27", text)
        clean_doc.close()

    def test_replace_fio_operann(self):
        pdf_bytes = self._make_operann_pdf(spectator_name="Аристова Анна")
        ok, cleaned_bytes, info = bot._clean_ticket_with_report_impl(
            pdf_bytes, spectator_fio="Смирнов Алексей"
        )
        self.assertTrue(ok)
        self.assertTrue(info["changed"])
        self.assertEqual("Смирнов Алексей", info.get("spectator_fio"))

        clean_doc = fitz.open(stream=cleaned_bytes, filetype="pdf")
        text = clean_doc[0].get_text().replace("\xa0", " ")
        self.assertNotIn("Аристова", text)
        self.assertIn("Смирнов Алексей", text)
        clean_doc.close()

    def test_price_and_seat_editing_operann(self):
        pdf_bytes = self._make_operann_pdf(price=3000, row="1", seat="27")
        seat_info = {"row": "7", "seat": "14", "sector": "Балкон"}
        ok, cleaned_bytes, info = bot._clean_ticket_with_report_impl(
            pdf_bytes, custom_price=5500, seat_info=seat_info
        )
        self.assertTrue(ok)
        clean_doc = fitz.open(stream=cleaned_bytes, filetype="pdf")
        text = clean_doc[0].get_text().replace("\xa0", " ")
        self.assertIn("5 500", text)
        self.assertNotIn("3000", text)
        self.assertIn("Балкон, ряд 7, место 14", text)
        self.assertNotIn("Партер, ряд 1, место 27", text)
        clean_doc.close()
