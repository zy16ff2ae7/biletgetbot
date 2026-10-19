"""Tests for new templates (Afisha, Intickets, Kassa24) and multi-page batch FIO replacement."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import fitz
import ticket_meta

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

    def test_make_clean_ticket_filename_with_fio(self):
        meta = {
            "title": "Чайка",
            "theater": "МХТ Чехова",
            "short_date": "04.11",
            "sector": "Партер",
            "row": "6",
            "seats": ["5"],
            "price": 2500,
        }
        fn = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", fio="Иванов И.И.")
        self.assertIn("(Иванов И.И.)", fn)
        self.assertIn("Чайка", fn)
        self.assertIn("р6 м5", fn)
        self.assertTrue(fn.endswith(".pdf"))

    def test_admin_stats_and_text(self):
        import tempfile
        from subscriptions import SubscriptionStore
        with tempfile.TemporaryDirectory() as td:
            store = SubscriptionStore(Path(td) / "test_sub.sqlite3")
            store.touch_user(100, "user100")
            store.bump_processed(100, 3)
            store.add_ticket_history(100, "tok1", title="Чайка", price=2500)
            store.add_ticket_history(100, "tok2", title="Чайка", price=2500)
            store.add_ticket_history(100, "tok3", title="Серёжа", price=3000)

            stats = store.admin_stats()
            self.assertEqual(stats["users_total"], 1)
            self.assertEqual(stats["processed_total"], 3)
            self.assertIn("top_shows", stats)
            self.assertEqual(stats["top_shows"][0][0], "Чайка")
            self.assertEqual(stats["top_shows"][0][1], 2)

            with patch.object(bot, "get_store", return_value=store):
                txt = bot.admin_stats_text()
                self.assertIn("СТАТИСТИКА БОТА", txt)
                self.assertIn("Чайка — 2 шт.", txt)
                self.assertIn("Сервер VDS:", txt)

    def test_notify_admins_ticket_error_and_antiflood(self):
        sent_messages = []
        def fake_send(chat_id, text, **kwargs):
            sent_messages.append((chat_id, text))
            return True

        with patch.object(bot, "send_admin_alert", side_effect=fake_send), \
             patch.dict(bot.CONFIG, {"admin_ids": [999111]}):
            bot._last_admin_alert.clear()
            bot.notify_admins_ticket_error(12345, "baduser", "Файл поврежден", "bad.pdf")
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(sent_messages[0][0], 999111)
            self.assertIn("@baduser", sent_messages[0][1])
            self.assertIn("bad.pdf", sent_messages[0][1])
            self.assertIn("Файл поврежден", sent_messages[0][1])

            # Anti-flood check: duplicate call immediately should be suppressed
            bot.notify_admins_ticket_error(12345, "baduser", "Файл поврежден", "bad.pdf")
            self.assertEqual(len(sent_messages), 1)

    def test_quick_price_and_fio_pickers(self):
        import tempfile
        import time
        from subscriptions import SubscriptionStore
        with tempfile.TemporaryDirectory() as td:
            store = SubscriptionStore(Path(td) / "test_sub.sqlite3")
            store.record_recent_fio(101, "Иванов И.И.")
            time.sleep(0.01)
            store.record_recent_fio(101, "Петров П.П.")
            recent = store.get_user_recent_fios(101)
            self.assertEqual(recent, ["Петров П.П.", "Иванов И.И."])

            with patch.object(bot, "get_store", return_value=store):
                p_kb = bot._price_picker_markup("testtok", cur_price=2000)
                flat_p = [btn["text"] for row in p_kb["inline_keyboard"] for btn in row]
                self.assertIn("2 500 ₽", flat_p)
                self.assertIn("🎁 Скрыть цену", flat_p)
                self.assertIn("🔄 Исходная цена", flat_p)

                f_kb = bot._fio_picker_markup("testtok", 101, cur_fio="Петров П.П.")
                flat_f = [btn["text"] for row in f_kb["inline_keyboard"] for btn in row]
                self.assertIn("✅ Петров П.П.", flat_f)
                self.assertIn("👤 Иванов И.И.", flat_f)
                self.assertIn("❌ Оставить пустым (стереть)", flat_f)

    def test_quick_seat_picker_markup(self):
        s_kb = bot._seat_picker_markup("tok123", cur_seat="Ряд 5, Место 12")
        flat = [btn["text"] for row in s_kb["inline_keyboard"] for btn in row]
        self.assertIn("⬅️ Место 11", flat)
        self.assertIn("Место 13 ➡️", flat)
        self.assertIn("🎭 Партер", flat)
        self.assertIn("🏛 Бельэтаж", flat)
        self.assertIn("🔄 Исходное из билета", flat)
        self.assertIn("❌ Оставить пустым (стереть)", flat)

    def test_batch_fio_wizard_and_multi_fio(self):
        import tempfile
        import time
        from subscriptions import SubscriptionStore
        with tempfile.TemporaryDirectory() as td:
            store = SubscriptionStore(Path(td) / "test_sub.sqlite3")
            store.record_recent_fio(202, "Сидоров С.С.")
            time.sleep(0.01)
            store.record_recent_fio(202, "Козлов К.К.")

            items = [
                {"orig_name": "t1.pdf", "clean_path": "/tmp/c1.pdf", "orig_path": "/tmp/o1.pdf", "meta": {"title": "Чайка", "row": 1, "seats": [5]}, "fio": None},
                {"orig_name": "t2.pdf", "clean_path": "/tmp/c2.pdf", "orig_path": "/tmp/o2.pdf", "meta": {"title": "Чайка", "row": 1, "seats": [6]}, "fio": None},
            ]
            store.save_batch_session("btest1", 202, 1000, items)

            with patch.object(bot, "get_store", return_value=store):
                text, kb = bot._build_batch_fio_wizard("btest1", 0, 202)
                self.assertIn("Билет №1 из 2", text)
                self.assertIn("Чайка", text)
                flat_btn = [b["text"] for row in kb["inline_keyboard"] for b in row]
                self.assertIn("👤 Козлов К.К.", flat_btn)
                self.assertIn("👤 Сидоров С.С.", flat_btn)
                self.assertIn("🏁 Готово (завершить)", flat_btn)

                # Test split_fio_list with user_id recording
                lines = bot.split_fio_list("1. Анна Ахматова\n2. Марина Цветаева")
                self.assertEqual(lines, ["Анна Ахматова", "Марина Цветаева"])
                with patch.object(bot, "_reprocess_batch_item", return_value=(True, {})):
                    count = bot._apply_mass_fio_to_batch("btest1", lines, user_id=202)
                    self.assertEqual(count, 2)
                    recent_after = store.get_user_recent_fios(202, limit=5)
                    self.assertIn("Марина Цветаева", recent_after)
                    self.assertIn("Анна Ахматова", recent_after)


if __name__ == "__main__":
    unittest.main()


