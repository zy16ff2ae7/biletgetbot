"""Event titles must survive processing, even when they look like a name."""
from pathlib import Path
import unittest

import fitz

from test_processing import bot
import validation


def event_ticket(width=600, height=852, top=75, multiline=False, size=11):
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    font = str(Path(bot.BILETNAYA) / "vendor/Arial.ttf")

    def text(x, y, value, fontsize=9):
        page.insert_text((x, y), value, fontname="F0", fontfile=font,
                         fontsize=fontsize)

    text(156, top, "Наименование", 7)
    text(380, top, "Адрес", 7)
    titles = ["Поколение Маугли", "Александр Пушкин"] if multiline else ["Поколение Маугли"]
    for index, title in enumerate(titles):
        text(156, top + 18 + index * 16, title, size)
    text(380, top + 18, "Москва, Камергерский переулок, 3")
    text(156, top + 65, "Сектор", 7)
    text(156, top + 80, "Партер")
    text(156, top + 120, "Зритель", 7)
    text(300, top + 120, "Иванов Иван")
    text(35, top + 160, 'ООО "Компания Афиша"', 7)
    return doc, titles


class AfishaTitlePreservationTests(unittest.TestCase):
    def test_title_survives_different_page_layouts(self):
        for dimensions, top in [((600, 852), 75), ((600, 852), 300),
                                ((842, 595), 75), ((842, 595), 300)]:
            with self.subTest(dimensions=dimensions, top=top):
                source, titles = event_ticket(*dimensions, top=top)
                with source:
                    output, _report = bot.srv.autoclean_doc(source)
                    with output:
                        for title in titles:
                            self.assertEqual(source[0].search_for(title), output[0].search_for(title))

    def test_multiline_title_is_never_a_personal_field(self):
        source, titles = event_ticket(top=300, multiline=True, size=9)
        with source:
            slots = bot._find_name_slots(source)
            self.assertFalse(any(slot.get("text") in titles for slot in slots))
            targets = bot._cleanup_targets(source)
            self.assertFalse(set(titles) & targets[0])
            output, _report = bot.srv.autoclean_doc(source)
            with output:
                for title in titles:
                    self.assertEqual(source[0].search_for(title), output[0].search_for(title))

    def test_validator_rejects_title_loss_even_if_marked_for_removal(self):
        source, titles = event_ticket(multiline=True)
        with source, fitz.open(stream=source.tobytes(), filetype="pdf") as output:
            for rect in output[0].search_for(titles[1]):
                output[0].add_redact_annot(rect, fill=False)
            output[0].apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                        graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            with self.assertRaisesRegex(validation.ValidationError, "название"):
                validation.validate_ticket(source, output, bot.srv, [titles])

    def test_validator_accepts_unchanged_title(self):
        source, _titles = event_ticket(multiline=True)
        with source, fitz.open(stream=source.tobytes(), filetype="pdf") as output:
            validation.validate_ticket(source, output, bot.srv, [])
