import unittest
import ticket_meta


class TicketMetaTests(unittest.TestCase):
    def test_corrupt_or_empty_stream_returns_empty_dict(self):
        self.assertEqual({}, ticket_meta.extract_ticket_metadata(b""))
        self.assertEqual({}, ticket_meta.extract_ticket_metadata(b"invalid pdf data"))
        self.assertEqual({}, ticket_meta.extract_ticket_metadata(b"%PDF-1.4 header only"))

    def test_make_clean_ticket_filename_full_meta(self):
        meta = {
            "title": "Чайка",
            "theater": "МХТ Чехова",
            "short_date": "04.11",
            "sector": "Балкон",
            "row": "4",
            "seats": ["14"],
            "price": 3000,
        }
        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="fallback.pdf")
        self.assertEqual("Чайка (МХТ Чехова, 04.11) — Балкон р4 м14 — 3000р.pdf", filename)

    def test_make_clean_ticket_filename_partial_meta(self):
        meta = {
            "title": "Спартак",
            "theater": "",
            "short_date": "",
            "sector": "Партер",
            "row": "12",
            "seats": ["5", "6"],
            "price": 5000,
        }
        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="fallback.pdf")
        self.assertEqual("Спартак — Партер р12 м5, 6 — 5000р.pdf", filename)

    def test_make_clean_ticket_filename_row_seat_price_only(self):
        meta = {
            "title": "",
            "theater": "",
            "short_date": "",
            "sector": "",
            "row": "6",
            "seats": ["23"],
            "price": 1100,
        }
        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="fallback.pdf")
        self.assertEqual("р6 м23 — 1100р.pdf", filename)

    def test_make_clean_ticket_filename_fallback_when_empty(self):
        filename = ticket_meta.make_clean_ticket_filename({}, fallback="original_file.pdf")
        self.assertEqual("original_file_cleaned.pdf", filename)

    def test_make_clean_ticket_filename_sanitizes_unsafe_chars(self):
        meta = {
            "title": 'Спектакль: "1+1" / Тест? *',
            "theater": "Театр <Мастер>",
            "short_date": "10.11",
            "sector": "Ложа|1",
            "row": "2",
            "seats": ["10"],
            "price": 1500,
        }
        filename = ticket_meta.make_clean_ticket_filename(meta, fallback="fallback.pdf")
        for bad_char in [":", '"', "/", "\\", "?", "*", "<", ">", "|"]:
            self.assertNotIn(bad_char, filename)
        self.assertTrue(filename.endswith(".pdf"))

    def test_format_seats_string(self):
        self.assertEqual("", ticket_meta.format_seats_string([]))
        self.assertEqual("14", ticket_meta.format_seats_string(["14"]))
        self.assertEqual("10, 12", ticket_meta.format_seats_string(["10", "12"]))
        self.assertEqual("14–16", ticket_meta.format_seats_string(["14", "15", "16"]))
        self.assertEqual("1, 2, 4–6", ticket_meta.format_seats_string(["1", "2", "4", "5", "6"]))

    def test_format_single_ticket_caption(self):
        meta = {
            "title": "Чайка",
            "theater": "МХТ Чехова",
            "date": "04.11.2026",
            "time": "19:00",
            "sector": "Балкон середина",
            "row": "4",
            "seats": ["14"],
            "price_str": "3 000 ₽",
            "total_price_str": "3 000 ₽",
        }
        caption = ticket_meta.format_single_ticket_caption(meta, {"changed": True})
        self.assertIn("Чайка", caption)
        self.assertIn("МХТ Чехова", caption)
        self.assertIn("04.11.2026 19:00", caption)
        self.assertIn("Балкон середина", caption)
        self.assertIn("ряд 4", caption)
        self.assertIn("место 14", caption)
        self.assertIn("Мама очистила билет", caption)
        self.assertNotIn("ФИО", caption)

    def test_format_batch_report_table(self):
        items = [
            {
                "input_name": "t1.pdf",
                "output_name": "Чайка (МХТ Чехова, 04.11) — р4 м14 — 3000р.pdf",
                "meta": {
                    "title": "Чайка",
                    "theater": "МХТ Чехова",
                    "date": "04.11.2026",
                    "time": "19:00",
                    "sector": "Балкон",
                    "row": "4",
                    "seats": ["14"],
                    "price_str": "3 000 ₽",
                    "price": 3000,
                },
                "status": "OK",
            }
        ]
        table = ticket_meta.format_batch_report_table(items)
        self.assertIn("РЕЕСТР ОБРАБОТАННЫХ БИЛЕТОВ", table)
        self.assertIn("Чайка", table)
        self.assertIn("МХТ Чехова", table)
        self.assertIn("р4 м14", table)
        self.assertIn("3 000 ₽", table)

    def test_format_batch_tg_caption(self):
        items = [
            {
                "output_name": "Чайка — р4 м14 — 3000р.pdf",
                "meta": {
                    "title": "Чайка",
                    "row": "4",
                    "seats": ["14"],
                    "price_str": "3 000 ₽",
                },
                "status": "OK",
            }
        ]
        caption = ticket_meta.format_batch_tg_caption(items, good=1, failed=0)
        self.assertIn("Мама упаковала пачку: 1 готово", caption)
        self.assertIn("Чайка", caption)
        self.assertIn("р4 м14", caption)
        self.assertIn("3 000 ₽", caption)

    def test_make_batch_zip_filename_single_show_same_row(self):
        items = [
            {"meta": {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "sector": "Партер", "row": 3, "seats": [11]}},
            {"meta": {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "sector": "Партер", "row": 3, "seats": [12]}},
            {"meta": {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "sector": "Партер", "row": 3, "seats": [13]}},
        ]
        name = ticket_meta.make_batch_zip_filename(items, good=3)
        self.assertEqual("Чайка (МХТ Чехова, 04.11) — Партер р3 м11–13.zip", name)

    def test_make_batch_zip_filename_single_show_diff_rows(self):
        items = [
            {"meta": {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "sector": "Партер", "row": 3, "seats": [11]}},
            {"meta": {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "sector": "Партер", "row": 5, "seats": [12]}},
        ]
        name = ticket_meta.make_batch_zip_filename(items, good=2)
        self.assertEqual("Чайка (МХТ Чехова, 04.11) — 2 билета.zip", name)

    def test_make_batch_zip_filename_multiple_shows(self):
        items = [
            {"meta": {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "sector": "Партер", "row": 3, "seats": [11]}},
            {"meta": {"title": "Серёжа", "theater": "МХТ Чехова", "short_date": "28.12", "sector": "Партер", "row": 1, "seats": [5]}},
        ]
        name = ticket_meta.make_batch_zip_filename(items, good=2)
        self.assertEqual("Билеты МХТ Чехова (04.11–28.12) — 2 билета.zip", name)

    def test_get_event_key_and_make_event_folder_name(self):
        meta1 = {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "04.11", "date": "04.11.2026"}
        meta2 = {"title": "Чайка", "theater": "МХТ Чехова", "short_date": "05.11", "date": "05.11.2026"}
        meta3 = {"title": "Серёжа", "theater": "МХТ Чехова", "short_date": "28.12", "date": "28.12.2026"}

        k1 = ticket_meta.get_event_key(meta1)
        k2 = ticket_meta.get_event_key(meta2)
        k3 = ticket_meta.get_event_key(meta3)

        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

        folder1 = ticket_meta.make_event_folder_name(meta1)
        folder3 = ticket_meta.make_event_folder_name(meta3)
        self.assertEqual("Чайка (МХТ Чехова, 04.11)", folder1)
        self.assertEqual("Серёжа (МХТ Чехова, 28.12)", folder3)


if __name__ == "__main__":
    unittest.main()
