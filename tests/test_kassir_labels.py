import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

sys.modules.setdefault("zxingcpp", MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class KassirLabelsTests(unittest.TestCase):

    def _get_biletnaya_dir(self):
        repo_root = Path(__file__).resolve().parent.parent
        default_biletnaya = repo_root / "biletnaya"
        if not default_biletnaya.exists():
            default_biletnaya = Path("/Users/mac/Билетная")
        return os.environ.get("BILETNAYA", str(default_biletnaya))

    def test_kassir_fio_replacement_preserves_and_restores_labels(self):
        biletnaya_dir = self._get_biletnaya_dir()
        font_path = str(Path(biletnaya_dir) / "vendor" / "Arial.ttf")
        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BILETNAYA": biletnaya_dir
        }):
            import fitz
            import bot
            import server

            def make_kassir_doc(name="Донских Савелий Юрьевич", order_no="61713594"):
                doc = fitz.open()
                page = doc.new_page(width=595, height=842)
                def text(x, y, value, size=8.5):
                    page.insert_text((x, y), value, fontname="F0", fontfile=font_path, fontsize=size)
                text(211, 292, "Номер заказа")
                text(301, 292, "Order #")
                text(345, 292, order_no)
                text(211, 307, "Имя, фамилия")
                text(301, 307, "Name")
                text(345, 307, name)
                text(42, 325, 'Билет в рамках программы "Пушкинская Карта".', 7.1)
                text(100, 50, "KASSIR.RU")
                return doc.tobytes()

            pdf_orig = make_kassir_doc()

            # 1. Clean with new spectator FIO -> labels and new FIO must be present, old FIO absent
            ok1, res1, _ = bot.clean_ticket_with_report(pdf_orig, spectator_fio="Губанов Тимур")
            self.assertTrue(ok1, f"Clean 1 failed: {res1}")
            d1 = fitz.open(stream=res1, filetype="pdf")
            t1 = d1[0].get_text().replace('\xa0', ' ')
            self.assertIn("Name", t1)
            self.assertIn("Имя, фамилия", t1)
            self.assertIn("Губанов Тимур", t1)
            self.assertNotIn("Донских", t1)

            # 2. Re-edit ticket: change FIO on already cleaned ticket
            ok2, res2, _ = bot.clean_ticket_with_report(res1, spectator_fio="Сидоров Петр")
            self.assertTrue(ok2, f"Clean 2 failed: {res2}")
            d2 = fitz.open(stream=res2, filetype="pdf")
            t2 = d2[0].get_text().replace('\xa0', ' ')
            self.assertIn("Name", t2)
            self.assertIn("Имя, фамилия", t2)
            self.assertIn("Сидоров Петр", t2)
            self.assertNotIn("Губанов", t2)

            # 3. Clean anonymously (no spectator_fio) -> entire row must be removed
            ok3, res3, _ = bot.clean_ticket_with_report(pdf_orig, spectator_fio=None)
            self.assertTrue(ok3, f"Clean 3 failed: {res3}")
            d3 = fitz.open(stream=res3, filetype="pdf")
            t3 = d3[0].get_text().replace('\xa0', ' ')
            self.assertNotIn("Name", t3)
            self.assertNotIn("Имя, фамилия", t3)
            self.assertNotIn("Донских", t3)

            # 4. Set FIO on an anonymously cleaned Kassir ticket -> labels and new FIO are restored
            ok4, res4, _ = bot.clean_ticket_with_report(res3, spectator_fio="Губанов Тимур")
            self.assertTrue(ok4, f"Clean 4 failed: {res4}")
            d4 = fitz.open(stream=res4, filetype="pdf")
            t4 = d4[0].get_text().replace('\xa0', ' ')
            self.assertIn("Name", t4)
            self.assertIn("Имя, фамилия", t4)
            self.assertIn("Губанов Тимур", t4)

if __name__ == "__main__":
    unittest.main()
