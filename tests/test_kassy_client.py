import io, os, unittest, fitz
import bot
from bot import srv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "biletnaya", "vendor", "Arial.ttf")


def create_kassy_ticket(fio="Дягтев Саид Русланович", cleaned=False):
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)

    def t(x, y, s, sz=10):
        p.insert_text((x, y), s, fontsize=sz, fontname="F0", fontfile=FONT)

    PANEL = fitz.Rect(40, 226, 565, 350)
    p.draw_rect(PANEL, color=None, fill=(0.87, 0.87, 0.87), width=0)
    t(40, 40, "ЭЛЕКТРОННЫЙ БИЛЕТ", 14)
    if not cleaned:
        t(40, 100, "В рамках программы «Пушкинская карта»", 9)
    t(40, 250, "№ заказа: 1782850 — 2213382", 11)
    t(40, 275, "Дата покупки: 06.11.2026 16:21", 11)
    if fio:
        t(40, 300, f"Клиент: {fio}", 11)
    else:
        t(40, 300, "Клиент:", 11)
    t(40, 325, "Стоимость билета: 1 100,00 руб.", 11)
    res = doc.tobytes()
    doc.close()
    return res


class KassyClientTests(unittest.TestCase):
    def test_autoclean_preserves_klient_label(self):
        pdf = create_kassy_ticket(fio="Дягтев Саид Русланович")
        ok, res, info = bot._clean_ticket_with_report_impl(pdf)
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as d:
            lines = [l["text"] for l in srv.extract_lines(d[0])]
            self.assertTrue(any("Клиент" in l for l in lines), f"«Клиент» отсутствует: {lines}")
            self.assertFalse(any("Дягтев" in l for l in lines), f"ФИО не удалено: {lines}")
            self.assertFalse(any("Пушкин" in l for l in lines), f"Пушкин не удален: {lines}")

    def test_spectator_edit_on_uncleaned_ticket(self):
        pdf = create_kassy_ticket(fio="Дягтев Саид Русланович")
        ok, res, info = bot._clean_ticket_with_report_impl(pdf, spectator_fio="Иванов Иван")
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as d:
            lines = [l["text"] for l in srv.extract_lines(d[0])]
            self.assertTrue(any("Клиент: Иванов Иван" in l for l in lines), f"Не найдено 'Клиент: Иванов Иван': {lines}")
            self.assertFalse(any("Дягтев" in l for l in lines), f"Старое ФИО не удалено: {lines}")

    def test_spectator_edit_on_already_cleaned_ticket(self):
        pdf = create_kassy_ticket(fio=None, cleaned=True)
        ok, res, info = bot._clean_ticket_with_report_impl(pdf, spectator_fio="Петров Петр")
        self.assertTrue(ok, res)
        with fitz.open(stream=res, filetype="pdf") as d:
            lines = [l["text"] for l in srv.extract_lines(d[0])]
            self.assertTrue(any("Клиент: Петров Петр" in l for l in lines), f"Не найдено 'Клиент: Петров Петр': {lines}")

    def test_spectator_reedit_previously_edited_ticket(self):
        pdf1 = create_kassy_ticket(fio="Дягтев Саид Русланович")
        ok1, res1, _ = bot._clean_ticket_with_report_impl(pdf1, spectator_fio="Иванов Иван")
        self.assertTrue(ok1)
        ok2, res2, _ = bot._clean_ticket_with_report_impl(res1, spectator_fio="Сидоров Сидор")
        self.assertTrue(ok2)
        with fitz.open(stream=res2, filetype="pdf") as d:
            lines = [l["text"] for l in srv.extract_lines(d[0])]
            self.assertTrue(any("Клиент: Сидоров Сидор" in l for l in lines), f"Не найдено 'Клиент: Сидоров Сидор': {lines}")
            self.assertFalse(any("Иванов" in l for l in lines), f"Старое имя Иванов осталось: {lines}")


if __name__ == "__main__":
    unittest.main()
