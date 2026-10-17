import io
from pathlib import Path
import unittest
import zipfile

from test_processing import bot, ticket
import batches

FONT = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")


def archive(items):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            z.writestr(name, data)
    return buf.getvalue()


class BatchTests(unittest.TestCase):
    def test_pdf_zip_returns_cleaned_files_and_report(self):
        source = archive([("a.pdf", ticket("kassir")), ("b.pdf", ticket("intickets"))])
        result = batches.process_zip(source, bot.clean_ticket_with_report)
        with zipfile.ZipFile(io.BytesIO(result["data"])) as z:
            self.assertEqual(2, len([n for n in z.namelist() if n.endswith(".pdf")]))
            self.assertIn("report.txt", z.namelist())
            for name in z.namelist():
                if name.endswith(".pdf"):
                    with bot.srv.fitz.open(stream=z.read(name), filetype="pdf") as doc:
                        self.assertNotIn("ФИО:", doc[0].get_text())

    def test_bad_pdf_does_not_discard_successful_items(self):
        source = archive([("good.pdf", ticket("kassir")), ("bad.pdf", b"bad")])
        result = batches.process_zip(source, bot.clean_ticket_with_report)
        self.assertEqual((1, 1), (result["ok"], result["failed"]))

    def test_traversal_names_rejected(self):
        for name in ["../a.pdf", "/a.pdf", "dir/../../a.pdf", "C:\\a.pdf"]:
            with self.subTest(name=name), self.assertRaises(batches.BatchError):
                batches.read_zip(archive([(name, ticket("plain"))]))

    def test_no_pdfs_rejected(self):
        with self.assertRaises(batches.BatchError):
            batches.read_zip(archive([("note.txt", b"no tickets")]))

    def test_duplicate_basenames_get_unique_output_names(self):
        result = batches.process_zip(archive([("one/a.pdf", ticket("plain")),
                                              ("two/a.pdf", ticket("plain"))]),
                                     bot.clean_ticket_with_report)
        with zipfile.ZipFile(io.BytesIO(result["data"])) as z:
            names = [n for n in z.namelist() if n.endswith(".pdf")]
            self.assertEqual(2, len(set(names)))

    def test_output_names_are_unique_on_case_insensitive_filesystems(self):
        result = batches.process_zip(archive([("A.pdf", ticket("plain")), ("a.pdf", ticket("plain"))]),
                                     bot.clean_ticket_with_report)
        with zipfile.ZipFile(io.BytesIO(result["data"])) as z:
            names = [n.casefold() for n in z.namelist() if n.endswith(".pdf")]
            self.assertEqual(2, len(set(names)))

    def test_large_number_of_archive_records_is_rejected(self):
        with self.assertRaises(batches.BatchError):
            batches.read_zip(archive([(f"note-{n}.txt", b"") for n in range(1001)]))

    def test_burst_more_than_two_tickets_returns_zip(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(111, 30, 111)
            for i in range(1, 4):
                store.enqueue(i, {
                    "kind": "message",
                    "message": {
                        "chat": {"id": 111},
                        "from": {"id": 111},
                        "document": {"file_id": f"fid_{i}", "file_name": f"ticket_{i}.pdf"}
                    }
                })

            ident, payload = store.claim()
            delivered_files = []

            def fake_send(chat, data, name, caption, **kwargs):
                delivered_files.append((name, data))
                return True

            with patch.object(bot, "_store", store), \
                 patch.dict("os.environ", {"BILET_AUTO_DEBOUNCE": "0.01"}), \
                 patch.object(bot, "download_file", return_value=ticket("plain")), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send):
                bot.process_incoming_document(payload["message"], job_id=ident)

            self.assertEqual(1, len(delivered_files))
            name, data = delivered_files[0]
            self.assertTrue(name.endswith(".zip"))
            self.assertIn("р6 м23", name)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                pdf_names = [n for n in z.namelist() if n.endswith(".pdf")]
                self.assertEqual(3, len(pdf_names))
                self.assertIn("report.txt", z.namelist())

    def test_burst_two_tickets_returns_zip(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(111, 30, 111)
            for i in range(1, 3):
                store.enqueue(i, {
                    "kind": "message",
                    "message": {
                        "chat": {"id": 111},
                        "from": {"id": 111},
                        "document": {"file_id": f"fid_{i}", "file_name": f"ticket_{i}.pdf"}
                    }
                })

            ident, payload = store.claim()
            delivered_files = []

            def fake_send(chat, data, name, caption, **kwargs):
                delivered_files.append((name, data))
                return True

            with patch.object(bot, "_store", store), \
                 patch.dict("os.environ", {"BILET_AUTO_DEBOUNCE": "0.01"}), \
                 patch.object(bot, "download_file", return_value=ticket("plain")), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send):
                bot.process_incoming_document(payload["message"], job_id=ident)

            self.assertEqual(1, len(delivered_files))
            name, data = delivered_files[0]
            self.assertTrue(name.endswith(".zip"))
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                pdf_names = [n for n in z.namelist() if n.endswith(".pdf")]
                self.assertEqual(2, len(pdf_names))
                self.assertIn("report.txt", z.namelist())

    def test_burst_single_ticket_returns_individual_pdf(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(111, 30, 111)
            store.enqueue(1, {
                "kind": "message",
                "message": {
                    "chat": {"id": 111},
                    "from": {"id": 111},
                    "document": {"file_id": "fid_1", "file_name": "ticket_1.pdf"}
                }
            })

            ident, payload = store.claim()
            delivered_files = []

            def fake_send(chat, data, name, caption, **kwargs):
                delivered_files.append((name, data))
                return True

            with patch.object(bot, "_store", store), \
                 patch.dict("os.environ", {"BILET_AUTO_DEBOUNCE": "0.01"}), \
                 patch.object(bot, "download_file", return_value=ticket("plain")), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send):
                bot.process_incoming_document(payload["message"], job_id=ident)

            self.assertEqual(1, len(delivered_files))
            for name, _ in delivered_files:
                self.assertTrue(name.endswith(".pdf"))

    def test_zip_items_numbered_and_sorted_by_seat(self):
        def make_t(seat):
            import fitz
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 100), f"Концерт\nРяд 3 Место {seat}\n1500", fontname="F0", fontfile=FONT, fontsize=12)
            return doc.tobytes()

        items = [
            ("ticket_c.pdf", make_t("15")),
            ("ticket_a.pdf", make_t("11")),
            ("ticket_b.pdf", make_t("12")),
        ]
        res = batches.process_items(items, bot.clean_ticket_with_report)
        with zipfile.ZipFile(io.BytesIO(res["data"])) as z:
            names = sorted([n for n in z.namelist() if n.endswith(".pdf")])
            self.assertEqual(3, len(names))
            self.assertEqual("01. Концерт — р3 м11.pdf", names[0])
            self.assertEqual("02. Концерт — р3 м12.pdf", names[1])
            self.assertEqual("03. Концерт — р3 м15.pdf", names[2])

    def test_multi_event_batch_creates_subfolders(self):
        def make_event_ticket(show, date, seat):
            import fitz
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 100), f"МХТ Чехова\n{show}\nДата: {date}\nПартер ряд 3 место {seat}\n3000", fontname="F0", fontfile=FONT, fontsize=12)
            return doc.tobytes()

        items = [
            ("t1.pdf", make_event_ticket("Чайка", "04.11.2026", "11")),
            ("t2.pdf", make_event_ticket("Чайка", "04.11.2026", "12")),
            ("t3.pdf", make_event_ticket("Серёжа", "28.12.2026", "5")),
        ]
        res = batches.process_items(items, bot.clean_ticket_with_report)
        self.assertTrue(res["zip_name"].endswith(".zip"))
        self.assertIn("Билеты МХТ Чехова", res["zip_name"])

        with zipfile.ZipFile(io.BytesIO(res["data"])) as z:
            names = z.namelist()
            self.assertIn("report.txt", names)
            pdf_names = [n for n in names if n.endswith(".pdf")]
            self.assertEqual(3, len(pdf_names))

            # Multi-event batch must organize tickets into subfolders
            chaika_folder = "Чайка (МХТ Чехова, 04.11)"
            serezha_folder = "Серёжа (МХТ Чехова, 28.12)"
            chaika_files = [n for n in pdf_names if n.startswith(f"{chaika_folder}/")]
            serezha_files = [n for n in pdf_names if n.startswith(f"{serezha_folder}/")]

            self.assertEqual(2, len(chaika_files))
            self.assertEqual(1, len(serezha_files))
            self.assertTrue(any("01." in f for f in chaika_files))
            self.assertTrue(any("02." in f for f in chaika_files))

    def test_single_event_batch_has_no_subfolders(self):
        def make_event_ticket(show, date, seat):
            import fitz
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 100), f"МХТ Чехова\n{show}\nДата: {date}\nПартер ряд 3 место {seat}\n3000", fontname="F0", fontfile=FONT, fontsize=12)
            return doc.tobytes()

        items = [
            ("t1.pdf", make_event_ticket("Чайка", "04.11.2026", "11")),
            ("t2.pdf", make_event_ticket("Чайка", "04.11.2026", "12")),
        ]
        res = batches.process_items(items, bot.clean_ticket_with_report)
        self.assertTrue(res["zip_name"].startswith("Чайка (МХТ Чехова, 04.11)"))
        self.assertIn("р3 м11, 12", res["zip_name"])

        with zipfile.ZipFile(io.BytesIO(res["data"])) as z:
            names = z.namelist()
            self.assertIn("report.txt", names)
            pdf_names = [n for n in names if n.endswith(".pdf")]
            self.assertEqual(2, len(pdf_names))
            # Single event must NOT have subfolders
            for name in pdf_names:
                self.assertNotIn("/", name)

    def test_batch_result_sends_unpack_button(self):
        from unittest.mock import MagicMock, patch
        res = {
            "data": b"dummy_zip",
            "ok": 2,
            "failed": 0,
            "items_meta": [],
            "zip_name": "Чайка.zip",
        }
        progress = MagicMock()
        delivered = []

        def fake_send(chat, data, name, caption, reply_markup=None, **kwargs):
            delivered.append((name, reply_markup))
            return True

        with patch.object(bot, "send_cleaned", side_effect=fake_send):
            bot._send_batch_result(111, res, progress, "retry")

        self.assertEqual(1, len(delivered))
        name, reply_markup = delivered[0]
        self.assertEqual("Чайка.zip", name)
        self.assertIsNotNone(reply_markup)
        button = reply_markup["inline_keyboard"][0][0]
        self.assertEqual("📄 Прислать билеты по отдельности", button["text"])
        self.assertEqual("unpack", button["callback_data"])

    def test_handle_callback_query_unpacks_tickets_in_memory(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        def make_t(seat):
            import fitz
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 100), f"МХТ Чехова\nЧайка\nДата: 04.11.2026\nПартер ряд 3 место {seat}\n3000", fontname="F0", fontfile=FONT, fontsize=12)
            return doc.tobytes()

        items = [
            ("t1.pdf", make_t("11")),
            ("t2.pdf", make_t("12")),
        ]
        res = batches.process_items(items, bot.clean_ticket_with_report)
        zip_bytes = res["data"]

        delivered_tickets = []
        edited_markup = []
        sent_messages = []

        def fake_send_cleaned(chat, data, name, caption, **kwargs):
            delivered_tickets.append((name, caption))
            return True

        def fake_tg(method, **kwargs):
            if method == "editMessageReplyMarkup":
                edited_markup.append(kwargs)
            return {"ok": True}

        def fake_send_text(chat, text):
            sent_messages.append(text)
            return True

        cb = {
            "id": "cb_123",
            "data": "unpack",
            "from": {"id": 777},
            "message": {
                "message_id": 456,
                "chat": {"id": 777},
                "document": {"file_id": "fid_zip_1", "file_name": "tickets.zip", "file_size": len(zip_bytes)},
            },
        }

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(777, 30, 777)

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=zip_bytes), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_text", side_effect=fake_send_text), \
                 patch.object(bot, "tg", side_effect=fake_tg):
                bot.handle_callback_query(cb)

        self.assertEqual(2, len(delivered_tickets))
        for name, caption in delivered_tickets:
            self.assertTrue(name.endswith(".pdf"))
            self.assertIn("Чайка", caption)
            self.assertIn("МХТ Чехова", caption)
            self.assertNotIn("ФИО", caption)

        self.assertEqual(1, len(edited_markup))
        self.assertEqual(456, edited_markup[0]["message_id"])
        self.assertEqual({"inline_keyboard": []}, edited_markup[0]["reply_markup"])
        self.assertTrue(any("Распаковано билетов: 2" in msg for msg in sent_messages))


if __name__ == "__main__":
    unittest.main()
