"""Tests for new templates (Afisha, Intickets, Kassa24) and multi-page batch FIO replacement."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import fitz
import ticket_meta

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
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


def create_mos_bilet_pdf():
    """МОС:БИЛЕТ / bilet.mos.ru: метка «Посетитель:» отдельно от ФИО."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

    def text(x, y, val, size=9):
        page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

    text(40, 50, "МОС:БИЛЕТ", 12)
    text(40, 90, "21.10.2026 19:00", 10)
    text(40, 130, "Посетитель:", 8)
    text(40, 148, "Незговоров Станислав Сергеевич", 11)
    text(40, 180, "В рамках программы:", 8)
    text(40, 196, "«Пушкинская карта»", 8)
    text(40, 220, "Категория посетителей:", 8)
    text(40, 236, "Взрослый", 8)
    text(40, 280, "Цена 4500 руб.", 10)
    text(40, 310, "Ряд 5 Место 9", 10)
    text(40, 340, "Номер заказа 11100-532143", 10)
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


def create_muza_bashkir_pdf():
    """Горизонтальный eTicket «Муза»: таблица с пустой ЛОЖА и ФИО в 3 строки."""
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

    def text(x, y, val, size=11):
        page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

    text(35, 40, "Электронный билет № 10186447", 18)
    text(35, 80, "Фауст [16+]", 22)
    text(35, 105, "22 октября 2026, 19:00", 13)
    text(35, 140, "ГБУКИ РБ БГАТОиБ", 14)
    text(35, 160, "Уфа, ул. Ленина, 5/1", 11)
    for x, label in ((35, "ЗАЛ"), (109, "СЕКТОР"), (179, "ЛОЖА"),
                     (240, "РЯД"), (296, "МЕСТО"), (338, "ПОКУПАТЕЛЬ")):
        text(x, 218, label, 8.5)
    text(35, 240, "Большой", 11.6)
    text(35, 254, "зал", 11.6)
    text(109, 240, "Партер", 11.6)
    text(240, 240, "7", 11.6)
    text(296, 240, "22", 11.6)
    text(338, 240, "Эргашев", 11.6)
    text(338, 254, "Самандар", 11.6)
    text(338, 268, "назирович", 11.6)
    text(255, 320, "ЦЕНА", 8.5)
    text(255, 340, "1300.00", 11.6)
    text(35, 405, "В рамках программы «ПУШКИНСКАЯ КАРТА»", 9)
    text(444, 200, "Пушкинской карте.", 9)
    text(444, 570, "Продажи производятся при помощи билетной платформы Муза", 9)
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

    def test_kassa24_buyer_name_removed_header_kept_line_of_fold_preserved(self):
        pdf_data = create_sample_kassa24_pdf()
        ok, res, info = bot.clean_ticket_with_report(pdf_data)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Султанова", text)
            self.assertIn("ПОКУПАТЕЛЬ", text)
            self.assertNotIn("В РАМКАХ ПРОГРАММЫ", text)
            self.assertIn("ЛИНИЯ СГИБА", text)
            self.assertIn("РЯД 2", text)
            self.assertIn("МЕСТО 4", text)

    def test_kassa24_fio_replacement_keeps_buyer_header(self):
        pdf_data = create_sample_kassa24_pdf()
        ok, res, info = bot.clean_ticket_with_report(
            pdf_data, spectator_fio="Иванов Иван Иванович")
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
        self.assertIn("ПОКУПАТЕЛЬ", text)
        self.assertIn("Иванов Иван Иванович", text)
        self.assertNotIn("Султанова", text)

    def test_mos_bilet_visitor_label_kept_name_removed(self):
        pdf_data = create_mos_bilet_pdf()
        with fitz.open(stream=pdf_data, filetype="pdf") as doc:
            srv.strip_pushkin_doc(doc, clear_name=True)
            text = doc[0].get_text().replace("\xa0", " ")
        self.assertIn("Посетитель:", text)
        self.assertNotIn("Незговоров", text)
        self.assertNotIn("Пушкинская карта", text)

    def test_real_mos_bilet_visitor_label_kept(self):
        sample_path = Path("/Users/mac/Downloads/Билет_57447048.pdf")
        if not sample_path.exists():
            return
        with fitz.open(sample_path) as doc:
            srv.strip_pushkin_doc(doc, clear_name=True)
            text = doc[0].get_text().replace("\xa0", " ")
        self.assertIn("Посетитель:", text)
        self.assertNotIn("Незговоров", text)

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


    def test_concurrent_user_jobs_claim(self):
        import tempfile
        from subscriptions import SubscriptionStore
        with tempfile.TemporaryDirectory() as td:
            store = SubscriptionStore(Path(td) / "test_jobs.sqlite3")
            # User 1 sends two jobs
            store.enqueue(101, {"kind": "message", "message": {"chat": {"id": 1001}, "from": {"id": 555}, "document": {"file_name": "t1.pdf"}}})
            store.enqueue(102, {"kind": "message", "message": {"chat": {"id": 1001}, "from": {"id": 555}, "document": {"file_name": "t2.pdf"}}})
            # User 2 sends one job
            store.enqueue(103, {"kind": "message", "message": {"chat": {"id": 1002}, "from": {"id": 777}, "document": {"file_name": "t3.pdf"}}})

            # Worker 1 claims job 101 for user 555
            j1 = store.claim()
            self.assertIsNotNone(j1)
            self.assertEqual(j1[0], 101)

            # Worker 2 attempts claim; job 102 (user 555) must be skipped because user 555 is active!
            # It should claim job 103 (user 777) instead!
            j2 = store.claim()
            self.assertIsNotNone(j2)
            self.assertEqual(j2[0], 103)

            # Once worker 1 finishes job 101, job 102 can be claimed
            store.finish(101)
            j3 = store.claim()
            self.assertIsNotNone(j3)
            self.assertEqual(j3[0], 102)

    def test_download_file_retry_and_timeout(self):
        import requests
        from unittest.mock import MagicMock
        with patch.object(bot, "file_metadata", return_value={"file_path": "documents/ticket.pdf"}), \
             patch.object(bot.time, "sleep"):
            mock_resp_ok = MagicMock()
            mock_resp_ok.status_code = 200
            mock_resp_ok.iter_content.return_value = [b"%PDF-1.4 mock"]
            mock_resp_ok.__enter__.return_value = mock_resp_ok
            mock_resp_ok.__exit__.return_value = False

            mock_session = MagicMock()
            mock_session.get.side_effect = [
                requests.exceptions.ReadTimeout("Read timed out"),
                mock_resp_ok
            ]

            with patch.object(bot, "http_session", return_value=mock_session):
                data = bot.download_file("mock_file_id")
                self.assertEqual(data, b"%PDF-1.4 mock")
                self.assertEqual(mock_session.get.call_count, 2)

    def test_button_cooldown_debounces_silently(self):
        with patch.object(bot, "cooldown_ok", return_value=False), \
             patch.object(bot, "send_text") as mock_send:
            # When prompt_msg_id is provided (interactive button click), cooldown debounces silently
            res = bot.apply_ticket_fio(1001, 555, "dummy_tok", "Иванов И.И.", prompt_msg_id=12345)
            mock_send.assert_not_called()

            # When prompt_msg_id is NOT provided (direct text input), cooldown warns user
            res2 = bot.apply_ticket_fio(1001, 555, "dummy_tok", "Иванов И.И.", prompt_msg_id=None)
            mock_send.assert_called_once()
            self.assertIn("Слишком часто", mock_send.call_args[0][1])


class MuzaBashkirOperaTests(unittest.TestCase):
    """eTicket «Муза» Башкирской оперы: ФИО в 3 строки, пустая ЛОЖА, Пушкин."""

    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def load_sample(self):
        path = Path("/Users/mac/Downloads/eTicket-1931328-100461391025.pdf")
        return path.read_bytes() if path.exists() else create_muza_bashkir_pdf()

    def test_cleaning_removes_name_and_buyer_header_keeps_legal_text(self):
        ok, result, info = bot.clean_ticket_with_report(self.load_sample())
        self.assertTrue(ok, result)
        with fitz.open(stream=result, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
        for unwanted in ("Эргашев", "Самандар", "назирович", "ПОКУПАТЕЛЬ",
                         "В рамках программы", "ПУШКИНСКАЯ"):
            self.assertNotIn(unwanted, text)
        for kept in ("Фауст", "22 октября 2026", "ГБУКИ РБ БГАТОиБ",
                     "Партер", "1300.00", "Пушкинской карте."):
            self.assertIn(kept, text)

    def test_metadata_and_filename(self):
        ok, result, info = bot.clean_ticket_with_report(self.load_sample())
        self.assertTrue(ok, result)
        meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
        self.assertEqual("Фауст", meta["title"])
        self.assertEqual("Башкирский театр оперы и балета", meta["theater"])
        self.assertEqual("22.10.2026", meta["date"])
        self.assertEqual("19:00", meta["time"])
        self.assertEqual("Партер", meta["sector"])
        self.assertEqual("7", meta["row"])
        self.assertEqual(["22"], meta["seats"])
        self.assertEqual(1300, meta["price"])
        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf")
        self.assertEqual(
            "Фауст (Башкирский театр оперы и балета, 22.10) — Партер р7 м22 — 1300р.pdf",
            filename,
        )

    def test_fio_replacement_removes_old_multiline_name(self):
        ok, result, info = bot.clean_ticket_with_report(
            self.load_sample(), spectator_fio="Иванов Иван Иванович")
        self.assertTrue(ok, result)
        with fitz.open(stream=result, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
        self.assertIn("Иванов", text)
        for unwanted in ("Эргашев", "Самандар", "назирович"):
            self.assertNotIn(unwanted, text)


class KazanTuzTemplateTests(unittest.TestCase):
    """Казанский ТЮЗ: оплата картой, снятие «Пушкинской карты» и ФИО покупателя."""

    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def load_fixture(self):
        path = FIXTURES / "kazan_tuz_pushkin.pdf"
        if not path.exists():
            self.skipTest(f"missing fixture: {path}")
        return path.read_bytes()

    def test_metadata(self):
        meta = ticket_meta.extract_ticket_metadata(self.load_fixture(), srv=srv)
        self.assertEqual("Счастливая Шарлотта", meta["title"])
        self.assertEqual("Казанский ТЮЗ", meta["theater"])
        self.assertEqual("01.11.2026", meta["date"])
        self.assertEqual("17:30", meta["time"])
        self.assertEqual("01.11", meta["short_date"])
        self.assertEqual("Стол", meta["sector"])
        self.assertEqual("5", meta["row"])
        self.assertEqual(["17"], meta["seats"])
        self.assertEqual(2600, meta["price"])

    def test_cleaning_replaces_payment_and_removes_buyer(self):
        ok, result, info = bot.clean_ticket_with_report(self.load_fixture())
        self.assertTrue(ok, result)
        with fitz.open(stream=result, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
        self.assertIn("Тип оплаты", text)
        self.assertIn("Банковская карта", text)
        for unwanted in ("Пушкин", "ПУШКИН", "Смирнова", "Валерия", "Ивановна",
                         "ОБЯЗАТЕЛЬНО НУЖЕН ДОКУМЕНТ"):
            self.assertNotIn(unwanted, text)
        for kept in ("Счастливая Шарлотта", "01.11.2026", "17:30",
                     "Стол", "18+", "362819"):
            self.assertIn(kept, text)

    def test_filename(self):
        meta = ticket_meta.extract_ticket_metadata(self.load_fixture(), srv=srv)
        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf")
        self.assertEqual(
            "Счастливая Шарлотта (Казанский ТЮЗ, 01.11) — Стол р5 м17 — 2600р.pdf",
            filename,
        )


class YandexAfishaTemplateTests(unittest.TestCase):
    """Яндекс Афиша: многострочное ФИО посетителя, паспортный блок и значки Пушкина."""

    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def load_fixture(self):
        path = FIXTURES / "yandex_afisha_pushkin.pdf"
        if not path.exists():
            self.skipTest(f"missing fixture: {path}")
        return path.read_bytes()

    def test_metadata(self):
        meta = ticket_meta.extract_ticket_metadata(self.load_fixture(), srv=srv)
        self.assertIn("Стоп,кода", meta["title"])
        self.assertEqual("Театр им. Тинчурина", meta["theater"])
        self.assertEqual("19.11.2026", meta["date"])
        self.assertEqual("18:30", meta["time"])
        self.assertEqual("19.11", meta["short_date"])
        self.assertEqual("Партер", meta["sector"])
        self.assertEqual("2", meta["row"])
        self.assertEqual(["12"], meta["seats"])
        self.assertEqual(1200, meta["price"])

    def test_cleaning_removes_pushkin_and_visitor(self):
        ok, result, info = bot.clean_ticket_with_report(self.load_fixture())
        self.assertTrue(ok, result)
        with fitz.open(stream=result, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
        for unwanted in ("Бегиашвили", "Джумбер", "Юрьевич", "ПОСЕТИТЕЛЬ",
                         "Не забудьте паспорт", "Пушкинской карты"):
            self.assertNotIn(unwanted, text)
        for kept in ("Стоп,кода", "19 ноября 2026", "18:30",
                     "Театр им. Тинчурина", "Партер", "1200.00"):
            self.assertIn(kept, text)

    def test_fio_replacement_writes_new_fio_and_keeps_buyer_header(self):
        ok, result, info = bot.clean_ticket_with_report(
            self.load_fixture(), spectator_fio="Иванов Иван Иванович")
        self.assertTrue(ok, result)
        with fitz.open(stream=result, filetype="pdf") as doc:
            text = doc[0].get_text().replace("\xa0", " ")
        self.assertIn("ПОСЕТИТЕЛЬ", text)
        self.assertIn("Иванов", text)
        self.assertIn("Иван", text)
        self.assertIn("Иванович", text)
        for unwanted in ("Бегиашвили", "Джумбер", "Юрьевич", "Пушкинской карты"):
            self.assertNotIn(unwanted, text)


if __name__ == "__main__":
    unittest.main()



