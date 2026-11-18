import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

import fitz

from test_processing import bot
import seat_tools
import ticket_meta


class KeepFioModeTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(bot, "progress_request", return_value={})
        progress.start()
        self.addCleanup(progress.stop)

    def _ticketland_pushkin_pdf(
        self,
        title="Снежная королева",
        row="4",
        seat="1",
        fio="Моисеева Полина Владимировна",
        email="e.tarabaeva@list.ru",
    ):
        doc = fitz.open()
        page = doc.new_page(width=842, height=595)
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        def t(x, y, val, size=9):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

        t(10, 580, "TICKETLAND.RU", 8)
        t(231, 80, "ГБУК РБ УГТК", 9)
        t(231, 160, title, 11)
        t(231, 270, "21 ноября 2026, 12:00", 9)
        t(231, 310, "Партер", 9)
        t(231, 350, f"Ряд {row} Место {seat}", 9)
        t(231, 430, "700 рублей", 9)
        t(425, 525, 'В рамках программы "Пушкинская карта".', 8)
        t(425, 535, "Допускаются зрители строго от 14 до 22 лет.", 8)
        t(425, 558, fio, 10)
        t(425, 573, email, 10)
        data = doc.tobytes()
        doc.close()
        return data

    def _kassir_pushkin_pdf(self, fio="Иванова Анна Александровна"):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

        def t(x, y, val, size=8.5):
            page.insert_text((x, y), val, fontname="F0", fontfile=font, fontsize=size)

        t(50, 50, "МХТ им. Чехова", 10)
        t(50, 70, "Спектакль: Чайка", 10)
        t(50, 90, "04.11.2026 19:00", 9)
        t(50, 110, "Партер Ряд 3 Место 12", 9)
        t(50, 130, "3 000 руб.", 9)
        t(211, 307, "Имя, фамилия")
        t(301, 307, "Name")
        t(345, 307, fio)
        t(42, 325, 'Билет в рамках программы "Пушкинская Карта".', 7.1)
        data = doc.tobytes()
        doc.close()
        return data

    def test_ticketland_avito_vs_agent_mode(self):
        raw = self._ticketland_pushkin_pdf()

        # 1. Default / Avito mode: strip Pushkin + erase FIO, keep email
        ok_avito, res_avito, info_avito = bot.clean_ticket_with_report(raw, keep_fio=False)
        self.assertTrue(ok_avito)
        self.assertFalse(info_avito.get("keep_fio", False))
        with fitz.open(stream=res_avito, filetype="pdf") as doc:
            txt = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Пушкинская", txt)
            self.assertNotIn("14 до 22", txt)
            self.assertNotIn("Моисеева", txt)
            self.assertIn("e.tarabaeva@list.ru", txt)
            self.assertIn("Снежная королева", txt)

        # 2. Agent mode (keep_fio=True): strip Pushkin ONLY, keep original FIO + email
        ok_agent, res_agent, info_agent = bot.clean_ticket_with_report(raw, keep_fio=True)
        self.assertTrue(ok_agent)
        self.assertTrue(info_agent.get("keep_fio"))
        with fitz.open(stream=res_agent, filetype="pdf") as doc:
            txt = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Пушкинская", txt)
            self.assertNotIn("14 до 22", txt)
            self.assertIn("Моисеева Полина Владимировна", txt)
            self.assertIn("e.tarabaeva@list.ru", txt)
            self.assertIn("Снежная королева", txt)

        meta = ticket_meta.extract_ticket_metadata(res_agent, srv=bot.srv)
        caption = ticket_meta.format_single_ticket_caption(meta, info_agent)
        self.assertIn("сохранено из билета", caption)

        # 3. Custom spectator_fio overrides keep_fio=True
        ok_custom, res_custom, info_custom = bot.clean_ticket_with_report(
            raw, spectator_fio="Петров Илья Сергеевич", keep_fio=True
        )
        self.assertTrue(ok_custom)
        self.assertEqual("Петров Илья Сергеевич", info_custom.get("spectator_fio"))
        with fitz.open(stream=res_custom, filetype="pdf") as doc:
            txt = " ".join(doc[0].get_text().split())
            self.assertNotIn("Пушкинская", txt)
            self.assertNotIn("Моисеева", txt)
            self.assertIn("Петров Илья Сергеевич", txt)
            self.assertIn("e.tarabaeva@list.ru", txt)

    def test_kassir_keep_fio_vs_erase_fio(self):
        raw = self._kassir_pushkin_pdf("Иванова Анна Александровна")

        ok1, res_erase, _ = bot.clean_ticket_with_report(raw, keep_fio=False)
        self.assertTrue(ok1)
        with fitz.open(stream=res_erase, filetype="pdf") as doc:
            txt = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Пушкинская", txt)
            self.assertNotIn("Иванова", txt)

        ok2, res_keep, info2 = bot.clean_ticket_with_report(raw, keep_fio=True)
        self.assertTrue(ok2)
        self.assertTrue(info2.get("keep_fio"))
        with fitz.open(stream=res_keep, filetype="pdf") as doc:
            txt = doc[0].get_text().replace("\xa0", " ")
            self.assertNotIn("Пушкинская", txt)
            self.assertIn("Имя, фамилия", txt)
            self.assertIn("Иванова Анна Александровна", txt)

    def test_settings_toggle_and_fio_commands(self):
        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            uid = 7001
            store.grant(uid, 30, uid)

            sent = []
            edited = []

            def fake_send(chat, txt, markup=None, **kwargs):
                sent.append((txt, markup))
                return {"message_id": 10}

            def fake_edit(chat, mid, txt, markup=None):
                edited.append((txt, markup))
                return True

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "send_text", side_effect=fake_send), \
                 patch.object(bot, "edit_or_send", side_effect=fake_edit), \
                 patch.object(bot, "tg", return_value=True):

                # Default card: FIO erased
                card_txt, card_kb = bot.build_settings_card(uid)
                self.assertIn("не установлено (очищается)", card_txt)
                btn_texts = [b["text"] for row in card_kb["inline_keyboard"] for b in row]
                self.assertTrue(any("Очищается ➔ 📌 Оставлять родное" in t for t in btn_texts))

                # Click 1-click toggle set:keep_fio -> ON
                bot.handle_callback_query({
                    "id": "cb1",
                    "data": "set:keep_fio",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 10},
                })
                self.assertTrue(store.get_user_keep_fio(uid))
                self.assertIn("исходное из билета (не очищается)", edited[-1][0])
                btn_texts_on = [b["text"] for row in edited[-1][1]["inline_keyboard"] for b in row]
                self.assertTrue(any("Родное остаётся ➔ 🧹 Очищать" in t for t in btn_texts_on))

                # Click set:fio menu -> sends prompt with both keep & clear buttons
                bot.handle_callback_query({
                    "id": "cb2",
                    "data": "set:fio",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 10},
                })
                fio_menu_kb = sent[-1][1] or edited[-1][1]
                fio_menu_datas = [b.get("callback_data") for row in fio_menu_kb["inline_keyboard"] for b in row]
                self.assertIn("set:fio_keep", fio_menu_datas)
                self.assertIn("set:fio_clear", fio_menu_datas)

                # Click set:fio_clear -> OFF
                bot.handle_callback_query({
                    "id": "cb3",
                    "data": "set:fio_clear",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 10},
                })
                self.assertFalse(store.get_user_keep_fio(uid))

                # /fio родное -> ON
                bot.handle({"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": "/fio родное"})
                self.assertTrue(store.get_user_keep_fio(uid))
                self.assertIn("сохраняются", sent[-1][0])

                # /fio status when keep_fio=True
                bot.handle({"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": "/fio"})
                self.assertIn("сохраняется (не очищается)", sent[-1][0])

                # Setting custom FIO disables keep_fio automatically
                bot.handle({"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": "/fio Орлов Максим"})
                self.assertEqual("Орлов Максим", store.get_user_fio(uid))
                self.assertFalse(store.get_user_keep_fio(uid))

                # /fio сброс clears both
                bot.handle({"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": "/fio сброс"})
                self.assertIsNone(store.get_user_fio(uid))
                self.assertFalse(store.get_user_keep_fio(uid))

    def test_presets_builtin_and_custom_keep_fio(self):
        with tempfile.TemporaryDirectory() as td:
            store = bot.SubscriptionStore(Path(td) / "subscriptions.sqlite3")
            uid = 7002
            store.grant(uid, 30, uid)

            with patch.object(bot, "_store", store), \
                 patch.object(bot, "edit_or_send", return_value=True), \
                 patch.object(bot, "send_text", return_value=True), \
                 patch.object(bot, "tg", return_value=True):

                # Builtin preset tpl:apply:builtin_keep_fio
                bot.handle_callback_query({
                    "id": "cb_p1",
                    "data": "tpl:apply:builtin_keep_fio",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 20},
                })
                self.assertTrue(store.get_user_keep_fio(uid))
                self.assertIsNone(store.get_user_fio(uid))

                # Save custom preset with keep_fio=1
                store.set_user_state(uid, "waiting_preset_name")
                bot.handle({"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": "Для агентки"})
                presets = store.get_user_presets(uid)
                self.assertEqual(1, len(presets))
                self.assertEqual(1, presets[0]["keep_fio"])

                # Switch to builtin_std (Avito clean mode)
                bot.handle_callback_query({
                    "id": "cb_p2",
                    "data": "tpl:apply:builtin_std",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 20},
                })
                self.assertFalse(store.get_user_keep_fio(uid))

                # Re-apply saved custom preset "Для агентки"
                bot.handle_callback_query({
                    "id": "cb_p3",
                    "data": f"tpl:apply:custom_{presets[0]['id']}",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 20},
                })
                self.assertTrue(store.get_user_keep_fio(uid))

    def test_single_ticket_togglefio_qfio_and_delivery_cache_isolation(self):
        raw = self._ticketland_pushkin_pdf()

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            uid = 7003
            store.grant(uid, 30, uid)

            delivered = []

            def fake_send_cleaned(chat, data, name, caption, cache_keys=(), reuse_id=None, reply_markup=None, **kwargs):
                fid = reuse_id or ("cached_tg_file_id_" + str(len(delivered) + 1))
                bot._remember_delivery(cache_keys, fid, caption)
                delivered.append({
                    "data": data,
                    "name": name,
                    "caption": caption,
                    "markup": reply_markup,
                    "reuse_id": reuse_id,
                })
                return fid

            with patch.object(bot, "STATE_DIR", td_path), \
                 patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=raw), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_text", return_value=True), \
                 patch.object(bot, "edit_or_send", return_value=True), \
                 patch.object(bot, "tg", return_value=True):

                doc_msg = {
                    "file_id": "orig_tl_1",
                    "file_unique_id": "uniq_tl_same",
                    "file_name": "ticket.pdf",
                    "file_size": len(raw),
                }

                tg_msg = {
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": uid},
                    "document": doc_msg,
                }

                # 1. Process in default Avito mode (keep_fio=False)
                bot.process_document(tg_msg)
                self.assertEqual(1, len(delivered))
                self.assertIsNone(delivered[-1]["reuse_id"])
                with fitz.open(stream=delivered[-1]["data"], filetype="pdf") as d:
                    self.assertNotIn("Моисеева", d[0].get_text())

                token = store.get_user_last_doc_token(uid)
                self.assertIsNotNone(token)
                cb_datas = [b.get("callback_data") for r in delivered[-1]["markup"]["inline_keyboard"] for b in r]
                self.assertIn(f"togglefio:{token}:1", cb_datas)

                # Resend in SAME mode -> hits DeliveryCache (reuse_id is populated)
                bot.process_document(tg_msg)
                self.assertEqual(2, len(delivered))
                self.assertEqual("cached_tg_file_id_1", delivered[-1]["reuse_id"])

                # 2. Switch user setting to keep_fio=True and resend SAME document (same file_unique_id)
                # DeliveryCache must NOT serve stale erased PDF!
                store.set_user_keep_fio(uid, True)
                bot.process_document(tg_msg)
                self.assertEqual(3, len(delivered))
                self.assertIsNone(delivered[-1]["reuse_id"])
                with fitz.open(stream=delivered[-1]["data"], filetype="pdf") as d:
                    txt = d[0].get_text().replace("\xa0", " ")
                    self.assertIn("Моисеева Полина Владимировна", txt)
                    self.assertIn("e.tarabaeva@list.ru", txt)
                    self.assertNotIn("Пушкинская", txt)

                token2 = store.get_user_last_doc_token(uid)
                cb_datas2 = [b.get("callback_data") for r in delivered[-1]["markup"]["inline_keyboard"] for b in r]
                self.assertIn(f"togglefio:{token2}:0", cb_datas2)

                # 3. Click togglefio:{token2}:0 -> erases FIO in 1 click
                bot.handle_callback_query({
                    "id": "cb_tf0",
                    "data": f"togglefio:{token2}:0",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 50},
                })
                with fitz.open(stream=delivered[-1]["data"], filetype="pdf") as d:
                    self.assertNotIn("Моисеева", d[0].get_text())

                token3 = store.get_user_last_doc_token(uid)

                # 4. Click qfio:{token3}:keep -> restores original FIO
                bot.handle_callback_query({
                    "id": "cb_qkeep",
                    "data": f"qfio:{token3}:keep",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 51},
                })
                with fitz.open(stream=delivered[-1]["data"], filetype="pdf") as d:
                    self.assertIn("Моисеева Полина Владимировна", d[0].get_text().replace("\xa0", " "))

                token4 = store.get_user_last_doc_token(uid)

                # 5. Click qfio:{token4}:empty -> erases FIO even though user setting keep_fio=True
                bot.handle_callback_query({
                    "id": "cb_qempty",
                    "data": f"qfio:{token4}:empty",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 52},
                })
                with fitz.open(stream=delivered[-1]["data"], filetype="pdf") as d:
                    self.assertNotIn("Моисеева", d[0].get_text().replace("\xa0", " "))

    def test_batch_keep_fio_and_mass_toggle(self):
        t1 = self._ticketland_pushkin_pdf(seat="1", fio="Моисеева Полина Владимировна", email="e.tarabaeva@list.ru")
        t2 = self._ticketland_pushkin_pdf(seat="2", fio="Соколова Дарья Игоревна", email="e.tarabaeva@list.ru")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("t1.pdf", t1)
            z.writestr("t2.pdf", t2)
        zip_bytes = buf.getvalue()

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = bot.SubscriptionStore(td_path / "subscriptions.sqlite3")
            uid = 7004
            store.grant(uid, 30, uid)
            store.set_user_keep_fio(uid, True)

            delivered_zips = []

            def fake_send_cleaned(chat, data, name, caption, reply_markup=None, **kwargs):
                delivered_zips.append({"data": data, "name": name, "caption": caption, "markup": reply_markup})
                return "zip_fid"

            with patch.object(bot, "STATE_DIR", td_path), \
                 patch.object(bot, "_store", store), \
                 patch.object(bot, "download_file", return_value=zip_bytes), \
                 patch.object(bot, "send_cleaned", side_effect=fake_send_cleaned), \
                 patch.object(bot, "send_photo", return_value=True), \
                 patch.object(bot, "send_text", return_value=True), \
                 patch.object(bot, "edit_or_send", return_value=True), \
                 patch.object(bot, "tg", return_value=True):

                # 1. Process ZIP while user is in Agent mode (keep_fio=True)
                bot.process_document({
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": uid},
                    "document": {
                        "file_id": "zip_in_1",
                        "file_unique_id": "uniq_zip_1",
                        "file_name": "orders.zip",
                        "file_size": len(zip_bytes),
                    },
                })
                self.assertEqual(1, len(delivered_zips))
                with zipfile.ZipFile(io.BytesIO(delivered_zips[-1]["data"])) as zf:
                    pdf_texts = []
                    for name in sorted(zf.namelist()):
                        if name.endswith(".pdf"):
                            with fitz.open(stream=zf.read(name), filetype="pdf") as d:
                                pdf_texts.append(d[0].get_text().replace("\xa0", " "))
                joined = "\n".join(pdf_texts)
                self.assertNotIn("Пушкинская", joined)
                self.assertIn("Моисеева Полина Владимировна", joined)
                self.assertIn("Соколова Дарья Игоревна", joined)
                self.assertIn("e.tarabaeva@list.ru", joined)

                # Extract batch_token from inline keyboard
                kb = delivered_zips[-1]["markup"]["inline_keyboard"]
                cb_datas = [b.get("callback_data", "") for r in kb for b in r]
                clear_cbs = [c for c in cb_datas if c.startswith("bmass:clearfio:")]
                self.assertEqual(1, len(clear_cbs))
                batch_token = clear_cbs[0].split(":", 2)[2]

                def _extract_batch_text(payload_item):
                    raw_bytes = payload_item["data"]
                    if payload_item["name"].endswith(".zip"):
                        parts = []
                        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                            for n in sorted(zf.namelist()):
                                if n.endswith(".pdf"):
                                    with fitz.open(stream=zf.read(n), filetype="pdf") as d:
                                        parts.append(d[0].get_text().replace("\xa0", " "))
                        return "\n".join(parts)
                    with fitz.open(stream=raw_bytes, filetype="pdf") as d:
                        return "\n".join(p.get_text().replace("\xa0", " ") for p in d)

                # 2. Click bmass:clearfio:{batch_token} -> strips FIO on all tickets in batch
                bot.handle_callback_query({
                    "id": "cb_bm_clear",
                    "data": f"bmass:clearfio:{batch_token}",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 60},
                })
                joined_clear = _extract_batch_text(delivered_zips[-1])
                self.assertNotIn("Моисеева", joined_clear)
                self.assertNotIn("Соколова", joined_clear)
                self.assertIn("e.tarabaeva@list.ru", joined_clear)

                # 3. Click bmass:keepfio:{batch_token} -> restores original FIO on all tickets in batch
                bot.handle_callback_query({
                    "id": "cb_bm_keep",
                    "data": f"bmass:keepfio:{batch_token}",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 61},
                })
                joined_keep = _extract_batch_text(delivered_zips[-1])
                self.assertIn("Моисеева Полина Владимировна", joined_keep)
                self.assertIn("Соколова Дарья Игоревна", joined_keep)

                # 3b. Also verify ZIP download (bzip:{batch_token}) contains updated PDFs with original FIOs
                bot.handle_callback_query({
                    "id": "cb_bzip",
                    "data": f"bzip:{batch_token}",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 61},
                })
                self.assertTrue(delivered_zips[-1]["name"].endswith(".zip"))
                joined_zip = _extract_batch_text(delivered_zips[-1])
                self.assertIn("Моисеева Полина Владимировна", joined_zip)
                self.assertIn("Соколова Дарья Игоревна", joined_zip)

                # 4. Individual item toggle btog:fio:{batch_token}:0
                bot.handle_callback_query({
                    "id": "cb_btog",
                    "data": f"btog:fio:{batch_token}:0",
                    "from": {"id": uid},
                    "message": {"chat": {"id": uid}, "message_id": 62},
                })
                sess = store.get_batch_session(batch_token)
                self.assertFalse(sess["items"][0].get("keep_fio"))
                self.assertTrue(sess["items"][1].get("keep_fio"))

    def test_quick_edit_keep_fio_parsing(self):
        q1 = seat_tools.parse_quick_edit("оставить фио 2500")
        self.assertIs(q1["keep_fio"], True)
        self.assertEqual(2500, q1["custom_price"])
        self.assertIsNone(q1["fio"])

        q2 = seat_tools.parse_quick_edit("стереть фио")
        self.assertIs(q2["keep_fio"], False)
        self.assertTrue(q2["reset_fio"])


if __name__ == "__main__":
    unittest.main()
