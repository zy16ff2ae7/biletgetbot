import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

sys.modules.setdefault("zxingcpp", MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class NeutralBrandingTests(unittest.TestCase):

    def test_neutral_branding_texts(self):
        with patch.dict(os.environ, {
            "BRAND_MODE": "neutral",
            "BRAND_NAME": "РЕДАКТОР БИЛЕТОВ",
            "BILETNAYA": "/Users/mac/Билетная"
        }):
            import importlib
            import texts
            importlib.reload(texts)

            self.assertTrue(texts.is_neutral())
            self.assertTrue(texts.is_free_all())
            self.assertEqual(texts.BRAND_NAME, "РЕДАКТОР БИЛЕТОВ")
            self.assertIsNone(texts.BRAND_AVATAR)
            self.assertIsNone(texts.WELCOME_ANIMATION)

            for text in (texts.GREETING, texts.HELP_TEXT, texts.FREE_BATCH_TEXT):
                self.assertNotIn("дед", text.lower())
                self.assertNotIn("черномор", text.lower())
                self.assertNotIn("абонемент", text.lower())
                self.assertNotIn("руб", text.lower())
                self.assertNotIn("₽", text.lower())
                self.assertNotIn("бесплат", text.lower())
                self.assertNotIn("чуж", text.lower())

            import ticket_meta
            importlib.reload(ticket_meta)
            single_cap = ticket_meta.format_single_ticket_caption(None, {"changed": True})
            self.assertNotIn("дед", single_cap.lower())
            self.assertNotIn("черномор", single_cap.lower())
            self.assertIn("Билет очищен", single_cap)

            batch_cap = ticket_meta.format_batch_tg_caption([], 5, 0)
            self.assertNotIn("дед", batch_cap.lower())
            self.assertIn("Пакет сформирован", batch_cap)

            import progress
            importlib.reload(progress)
            for stage_lines in progress.NEUTRAL_LINES.values():
                for line in stage_lines:
                    self.assertNotIn("чуж", line.lower())
            pr = progress.Progress("123", "test.pdf", lambda m, **p: {})
            rendered = pr.render()
            self.assertNotIn("дед", rendered.lower())
            self.assertNotIn("черномор", rendered.lower())
            self.assertIn("РЕДАКТОР БИЛЕТОВ", rendered)

            import bot
            importlib.reload(bot)
            self.assertTrue(bot.has_access(999999))
            kb = bot.get_main_markup()
            btn_texts = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
            self.assertNotIn("✍️ Оформить абонемент", btn_texts)
            self.assertNotIn("🎟 Промокод", btn_texts)

    def test_default_ded_branding_preserved(self):
        with patch.dict(os.environ, {"BRAND_MODE": "ded"}):
            import importlib
            import texts
            importlib.reload(texts)

            self.assertFalse(texts.is_neutral())
            self.assertEqual(texts.BRAND_NAME, "ДЕД РЕДАЧИТ")
            self.assertIsNotNone(texts.BRAND_AVATAR)
            self.assertIsNotNone(texts.WELCOME_ANIMATION)
            self.assertIn("дед", texts.GREETING.lower())

            import ticket_meta
            importlib.reload(ticket_meta)
            single_cap = ticket_meta.format_single_ticket_caption(None, {"changed": True})
            self.assertIn("Дед", single_cap)

            import progress
            importlib.reload(progress)
            for stage_lines in progress.NEUTRAL_LINES.values():
                for line in stage_lines:
                    self.assertNotIn("чуж", line.lower())
            pr = progress.Progress("123", "test.pdf", lambda m, **p: {})
            rendered = pr.render()
            self.assertIn("ДЕД РЕДАЧИТ", rendered)
