import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import zipfile

import fitz

from test_processing import bot, ticket
import ticket_meta


class FioMergePreviewTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def _sample_ticket_pdf(self, title="Чайка", row="3", seat="12", name=None):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        def text(x, y, val, size=8):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

        text(50, 50, "МХТ им. Чехова")
        text(50, 70, f"Спектакль: {title}")
        text(50, 90, "04.11.2026 19:00")
        text(50, 110, f"Партер Ряд {row} Место {seat}")
        text(50, 130, "3 000 руб.")
        text(211, 307, "Имя, фамилия")
        text(301, 307, "Name")
        text(345, 307, name or "Иванова Анна Александровна")
        text(42, 325, 'Билет в рамках программы "Пушкинская Карта".', 7)
        data = doc.tobytes()
        doc.close()
        return data

    def test_merge_batch_pdfs(self):
        t1 = self._sample_ticket_pdf(title="Чайка", row="3", seat="12")
        t2 = self._sample_ticket_pdf(title="Чайка", row="3", seat="13")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ticket_1.pdf", t1)
            z.writestr("ticket_2.pdf", t2)
        zip_bytes = buf.getvalue()

        merged_bytes, merged_name, count, caption = bot.merge_batch_pdfs(zip_bytes)
        self.assertEqual(2, count)
        self.assertTrue(merged_name.endswith(".pdf"))
        self.assertIn("Чайка", merged_name)
        with fitz.open(stream=merged_bytes, filetype="pdf") as doc:
            self.assertEqual(2, len(doc))

    def test_render_pdf_preview(self):
        t = self._sample_ticket_pdf()
        jpg_bytes = bot.render_pdf_preview(t)
        self.assertIsNotNone(jpg_bytes)
        self.assertTrue(len(jpg_bytes) > 1000)
        self.assertTrue(jpg_bytes.startswith(b"\xff\xd8\xff"))

    def test_clean_ticket_with_spectator_fio(self):
        t = self._sample_ticket_pdf(name="Старое Имя")
        ok, res, info = bot.clean_ticket_with_report(t, spectator_fio="Петров Петр Петрович")
        self.assertTrue(ok)
        self.assertEqual("Петров Петр Петрович", info.get("spectator_fio"))
        with fitz.open(stream=res, filetype="pdf") as doc:
            full_text = doc[0].get_text().replace("\xa0", " ")
            self.assertIn("Петров Петр Петрович", full_text)
            self.assertNotIn("Старое Имя", full_text)

    def test_fio_commands(self):
        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(555, 30, 555)

            sent_texts = []
            def fake_send(chat, txt, markup=None):
                sent_texts.append(txt)
                return True

            with patch.object(bot, "_store", store), patch.object(bot, "send_text", side_effect=fake_send):
                # 1. status when empty
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/fio"})
                self.assertIn("не установлено", sent_texts[-1])

                # 2. set fio
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/fio Сидоров Сидор"})
                self.assertIn("Сидоров Сидор", sent_texts[-1])
                self.assertEqual("Сидоров Сидор", store.get_user_fio(555))

                # 3. status when set
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/fio"})
                self.assertIn("Сидоров Сидор", sent_texts[-1])

                # 4. reset fio
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/fio сброс"})
                self.assertIn("сброшено", sent_texts[-1])
                self.assertIsNone(store.get_user_fio(555))

    def test_callback_merge_and_preview(self):
        t1 = self._sample_ticket_pdf()
        t2 = self._sample_ticket_pdf()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ticket_1.pdf", t1)
            z.writestr("ticket_2.pdf", t2)
        zip_bytes = buf.getvalue()

        delivered_files = []
        def fake_send_cleaned(chat, data, name, caption, **kwargs):
            delivered_files.append((name, data))
            return True

        delivered_photos = []
        def fake_send_photo(chat, data, caption=""):
            delivered_photos.append((data, caption))
            return True

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(888, 30, 888)

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=zip_bytes), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_photo", side_effect=fake_send_photo), \
                 patch.object(bot, "send_text", return_value=True):

                # Callback merge
                bot.handle_callback_query({
                    "data": "merge",
                    "from": {"id": 888},
                    "message": {
                        "chat": {"id": 888},
                        "document": {"file_id": "zip_file_id", "file_name": "tickets.zip"}
                    }
                })
                self.assertEqual(1, len(delivered_files))
                self.assertTrue(delivered_files[0][0].endswith(".pdf"))

                # Callback preview
                bot.handle_callback_query({
                    "data": "preview",
                    "from": {"id": 888},
                    "message": {
                        "chat": {"id": 888},
                        "document": {"file_id": "zip_file_id", "file_name": "tickets.zip"}
                    }
                })
                self.assertEqual(2, len(delivered_photos))
                self.assertTrue(delivered_photos[0][0].startswith(b"\xff\xd8\xff"))
                self.assertIn("ДО", delivered_photos[0][1])
                self.assertIn("ПОСЛЕ", delivered_photos[1][1])

    def test_clean_ticket_hide_price(self):
        t = self._sample_ticket_pdf()
        ok, res, info = bot.clean_ticket_with_report(t, hide_price=True)
        self.assertTrue(ok)
        self.assertTrue(info.get("hide_price"))
        with fitz.open(stream=res, filetype="pdf") as doc:
            full_text = doc[0].get_text()
            self.assertNotIn("3 000", full_text)
            self.assertNotIn("руб.", full_text)
            self.assertIn("Чайка", full_text)

        meta = ticket_meta.extract_ticket_metadata(res, srv=bot.srv)
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        self.assertIn("Номинал: скрыт 🎁", caption)

        clean_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=True)
        self.assertNotIn("3000р", clean_name)
        self.assertIn("Чайка", clean_name)

    def test_split_pdf_tickets(self):
        t1 = self._sample_ticket_pdf(title="Чайка", row="3", seat="12")
        t2 = self._sample_ticket_pdf(title="Чайка", row="3", seat="13")
        doc1 = fitz.open(stream=t1, filetype="pdf")
        doc2 = fitz.open(stream=t2, filetype="pdf")
        doc1.insert_pdf(doc2)
        multi_bytes = doc1.tobytes()
        doc1.close()
        doc2.close()

        entries = bot.split_pdf_tickets(multi_bytes, srv=bot.srv)
        self.assertEqual(2, len(entries))
        self.assertIn("м12", entries[0][0])
        self.assertIn("м13", entries[1][0])
        self.assertEqual(["12"], entries[0][2].get("seats"))
        self.assertEqual(["13"], entries[1][2].get("seats"))

    def test_settings_card_and_callbacks(self):
        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(777, 30, 777)

            sent_texts = []
            sent_markups = []
            def fake_send(chat, txt, markup=None):
                sent_texts.append(txt)
                sent_markups.append(markup)
                return True

            edited_texts = []
            edited_markups = []
            def fake_tg(method, **kwargs):
                if method == "editMessageText":
                    edited_texts.append(kwargs.get("text"))
                    edited_markups.append(kwargs.get("reply_markup"))
                    return True
                return True

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "send_text", side_effect=fake_send), \
                 patch.object(bot, "tg", side_effect=fake_tg):

                # 1. /settings command
                bot.handle({"chat": {"id": 777, "type": "private"}, "from": {"id": 777}, "text": "/settings"})
                self.assertIn("Настройки обработки билетов", sent_texts[-1])
                buttons = [b["callback_data"] for row in sent_markups[-1]["inline_keyboard"] for b in row]
                self.assertIn("set:fio", buttons)
                self.assertIn("set:price", buttons)
                self.assertIn("set:batch", buttons)
                self.assertIn("set:preview", buttons)
                self.assertIn("set:reset", buttons)

                # 2. Toggle price
                bot.handle_callback_query({
                    "data": "set:price",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777}, "message_id": 101}
                })
                self.assertEqual(1, store.get_user_settings(777)["hide_price"])
                self.assertIn("Подарочный режим", edited_texts[-1])

                # 3. Cycle batch mode
                bot.handle_callback_query({
                    "data": "set:batch",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777}, "message_id": 101}
                })
                self.assertEqual("merge", store.get_user_settings(777)["batch_mode"])

                # 4. Toggle preview
                bot.handle_callback_query({
                    "data": "set:preview",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777}, "message_id": 101}
                })
                self.assertEqual(1, store.get_user_settings(777)["auto_preview"])

                # 5. Reset all
                bot.handle_callback_query({
                    "data": "set:reset",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777}, "message_id": 101}
                })
                settings = store.get_user_settings(777)
                self.assertEqual(0, settings["hide_price"])
                self.assertEqual("zip", settings["batch_mode"])
                self.assertEqual(0, settings["auto_preview"])
                self.assertIsNone(settings["fio"])

    def test_toggleprice_and_split_callbacks(self):
        t1 = self._sample_ticket_pdf(title="Чайка", row="3", seat="12")
        t2 = self._sample_ticket_pdf(title="Чайка", row="3", seat="13")
        doc1 = fitz.open(stream=t1, filetype="pdf")
        doc2 = fitz.open(stream=t2, filetype="pdf")
        doc1.insert_pdf(doc2)
        multi_bytes = doc1.tobytes()
        doc1.close()
        doc2.close()

        delivered_files = []
        def fake_send_cleaned(chat, data, name, caption, **kwargs):
            delivered_files.append((name, data, caption))
            return True

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(999, 30, 999)
            store.save_doc_token("tok1", "file_tok1")

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=multi_bytes), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_text", return_value=True), \
                 patch.object(bot, "tg", return_value=True):

                # 1. Toggle price callback
                bot.handle_callback_query({
                    "data": "toggleprice:tok1:1",
                    "from": {"id": 999},
                    "message": {"chat": {"id": 999}, "message_id": 201}
                })
                self.assertEqual(1, len(delivered_files))
                self.assertIn("Номинал: скрыт 🎁", delivered_files[-1][2])

                # 2. Split callback
                bot.handle_callback_query({
                    "data": "split:tok1",
                    "from": {"id": 999},
                    "message": {"chat": {"id": 999}, "message_id": 202}
                })
                # Should deliver 2 split tickets
                self.assertEqual(3, len(delivered_files))
                self.assertIn("м12", delivered_files[1][0])
                self.assertIn("м13", delivered_files[2][0])

    def test_check_batch_duplicates(self):
        items_meta = [
            {"meta": {"title": "Чайка", "date": "04.11.2026", "row": "3", "seats": ["12"]}},
            {"meta": {"title": "Чайка", "date": "04.11.2026", "row": "3", "seats": ["13"]}},
            {"meta": {"title": "Чайка", "date": "04.11.2026", "row": "3", "seats": ["12"]}},
        ]
        dups = ticket_meta.check_batch_duplicates(items_meta)
        self.assertEqual(1, len(dups))
        self.assertEqual(1, dups[0]["first_idx"])
        self.assertEqual(3, dups[0]["duplicate_idx"])
        self.assertEqual("12", dups[0]["seat"])

        caption = ticket_meta.format_batch_tg_caption(items_meta, 3, 0)
        self.assertIn("Дубликаты мест", caption)


    def test_clean_ticket_custom_price(self):
        t = self._sample_ticket_pdf()
        ok, res, info = bot.clean_ticket_with_report(t, custom_price=4500)
        self.assertTrue(ok)
        self.assertEqual(4500, info.get("custom_price"))
        with fitz.open(stream=res, filetype="pdf") as doc:
            full_text = doc[0].get_text().replace("\xa0", " ")
            self.assertIn("4 500", full_text)
            self.assertNotIn("3 000", full_text)

        meta = ticket_meta.extract_ticket_metadata(res, srv=bot.srv)
        self.assertEqual(4500, meta.get("price"))
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        self.assertIn("💰 Номинал: 4 500 ₽ (изменено ✏️)", caption)
        self.assertIn("Мама обновила цену билета", caption)

        clean_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf")
        self.assertIn("4500р.pdf", clean_name)

    def test_price_commands(self):
        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(555, 30, 555)

            sent_texts = []
            def fake_send(chat, txt, markup=None):
                sent_texts.append(txt)
                return True

            with patch.object(bot, "_store", store), patch.object(bot, "send_text", side_effect=fake_send):
                # 1. status when empty
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/price"})
                self.assertIn("не установлена", sent_texts[-1])

                # 2. set price
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/price 3500"})
                self.assertIn("3500 ₽", sent_texts[-1])
                self.assertEqual(3500, store.get_user_custom_price(555))

                # 3. status when set
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/price"})
                self.assertIn("3500 ₽", sent_texts[-1])

                # 4. reset price
                bot.handle({"chat": {"id": 555, "type": "private"}, "from": {"id": 555}, "text": "/price сброс"})
                self.assertIn("сброшена", sent_texts[-1])
                self.assertIsNone(store.get_user_custom_price(555))

    def test_callback_price_and_interactive_edit(self):
        t = self._sample_ticket_pdf()
        delivered_files = []
        def fake_send_cleaned(chat, data, name, caption, reply_markup=None, **kwargs):
            delivered_files.append((name, data, caption))
            return True

        sent_prompts = []
        def fake_send_text(chat, txt, markup=None):
            sent_prompts.append(txt)
            return True

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(777, 30, 777)
            store.save_doc_token("tok_pr", "file_pr")

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=t), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_text", side_effect=fake_send_text):

                # 1. User clicks "Изменить цену" callback
                bot.handle_callback_query({
                    "data": "price:tok_pr",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 777}, "message_id": 301}
                })
                self.assertTrue(any("Введите новую цену" in p for p in sent_prompts))
                self.assertEqual("waiting_price:tok_pr", store.get_user_state(777))

                # 2. User sends "2500"
                bot.handle({
                    "chat": {"id": 777, "type": "private"},
                    "from": {"id": 777},
                    "text": "2500"
                })
                self.assertEqual(1, len(delivered_files))
                self.assertIn("2500р.pdf", delivered_files[0][0])
                self.assertIn("4 500" not in delivered_files[0][2], [True])
                self.assertIn("2 500 ₽ (изменено ✏️)", delivered_files[0][2])

    def test_callback_compare_before_and_after(self):
        t = self._sample_ticket_pdf()
        media_groups = []
        def fake_send_media_group_photos(chat, photos):
            media_groups.append(photos)
            return True

        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            store.grant(666, 30, 666)
            store.save_doc_token("tok_cmp", "file_cmp")

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=t), \
                 patch.object(bot, "send_media_group_photos", side_effect=fake_send_media_group_photos), \
                 patch.object(bot, "send_text", return_value=True):

                bot.handle_callback_query({
                    "data": "compare:tok_cmp",
                    "from": {"id": 666},
                    "message": {"chat": {"id": 666}, "message_id": 401}
                })

                self.assertEqual(1, len(media_groups))
                photos = media_groups[0]
                self.assertEqual(2, len(photos))
                # Photo 1 is BEFORE
                self.assertTrue(photos[0][0].startswith(b"\xff\xd8\xff"))
                self.assertIn("ДО", photos[0][1])
                # Photo 2 is AFTER
                self.assertTrue(photos[1][0].startswith(b"\xff\xd8\xff"))
                self.assertIn("ПОСЛЕ", photos[1][1])

    def test_single_ticket_markup(self):
        markup = bot._single_ticket_markup("test_token", hide_price=False, pages=1)
        buttons = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("👤 Изменить ФИО", buttons)
        self.assertIn("✏️ Изменить цену", buttons)
        self.assertIn("🎁 Скрыть цену", buttons)
        self.assertIn("🖼 Фото: До и После", buttons)


if __name__ == "__main__":
    unittest.main()


