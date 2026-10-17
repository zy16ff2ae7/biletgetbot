import io
from pathlib import Path
import unittest
from unittest.mock import patch

import fitz
import numpy as np
from PIL import Image
import zxingcpp

from test_processing import bot, ticket
import validation


def coded_ticket(background=None, ink=0):
    doc = fitz.open(stream=ticket("kassir"), filetype="pdf")
    barcode = zxingcpp.create_barcode("entry-12345", zxingcpp.BarcodeFormat.QRCode)
    pixels = np.asarray(zxingcpp.write_barcode_to_image(barcode, scale=4))
    buf = io.BytesIO()
    if background is None:
        Image.fromarray(pixels).save(buf, format="PNG")
    else:
        doc[0].draw_rect(fitz.Rect(58, 448, 152, 542), color=None,
                         fill=(background / 255,) * 3)
        rgba = np.empty((*pixels.shape, 4), dtype=np.uint8)
        rgba[:, :, :3] = ink
        rgba[:, :, 3] = np.where(pixels < 128, 255, 0)
        Image.fromarray(rgba).save(buf, format="PNG")
    doc[0].insert_image(fitz.Rect(60, 450, 150, 540), stream=buf.getvalue())
    return doc


def caption_over_barcode():
    """A program caption drawn over an otherwise unchanged barcode image."""
    doc = fitz.open(stream=ticket("plain"), filetype="pdf")
    pixels = np.full((60, 400), 255, dtype=np.uint8)
    widths = np.random.default_rng(21).integers(2, 6, 65)
    x = 10
    for index, width in enumerate(widths):
        pixels[:, x:x + int(width)] = 0 if index % 2 == 0 else 255
        x += int(width)
    buf = io.BytesIO()
    Image.fromarray(pixels).save(buf, format="PNG")
    doc[0].insert_image(fitz.Rect(60, 450, 260, 480), stream=buf.getvalue())
    doc[0].insert_text((74, 459), 'В рамках программы «ПУШКИНСКАЯ КАРТА»',
                       fontname="F0", fontfile=str(Path(bot.BILETNAYA) / "vendor/Arial.ttf"), fontsize=6)
    caption = bot.srv.extract_lines(doc[0])[-1]
    out = fitz.open(stream=doc.tobytes(), filetype="pdf")
    out[0].add_redact_annot(fitz.Rect(caption["bbox"]), fill=False)
    out[0].apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                             graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    return doc, out, caption


class ValidationTests(unittest.TestCase):
    def test_table_grid_is_not_a_vector_barcode(self):
        with fitz.open(stream=ticket("plain"), filetype="pdf") as doc:
            page = doc[0]
            # Each row / column has only a few matching spans, although the
            # connected table contains more than twenty dark thin rectangles.
            for row in range(7):
                for column in range(8):
                    x, y = 35 + column * 62, 160 + row * 20
                    page.draw_rect(fitz.Rect(x, y, x + 62, y + .6), color=None, fill=(0, 0, 0))
                    page.draw_rect(fitz.Rect(x, y, x + .6, y + 20), color=None, fill=(0, 0, 0))
            page.insert_text((46, 192), "Viewer Name", fontsize=8)
            page.draw_rect(fitz.Rect(120, 175, 150, 182), color=None, fill=(.8, .8, .8))
            with fitz.open(stream=doc.tobytes(), filetype="pdf") as out:
                for rect in out[0].search_for("Viewer Name"):
                    out[0].add_redact_annot(rect, fill=False)
                out[0].apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                         graphics=fitz.PDF_REDACT_LINE_ART_NONE)
                out[0].draw_rect(fitz.Rect(120, 175, 150, 182), color=None, fill=(1, 1, 1))
                validation.validate_ticket(doc, out, bot.srv, [])

    def test_caption_removal_reveals_unchanged_barcode_image(self):
        doc, out, caption = caption_over_barcode()
        with doc, out:
            self.assertFalse(validation._codes(doc[0]))
            report = validation.validate_ticket(doc, out, bot.srv, [[caption["text"]]])
            self.assertGreater(report["code_regions"], 0)

    def test_caption_exception_requires_actual_removed_program_text(self):
        doc, out, _caption = caption_over_barcode()
        with doc, out, self.assertRaises(validation.ValidationError):
            validation.validate_ticket(doc, out, bot.srv, [[]])

    def test_changed_bar_inside_removed_caption_is_rejected(self):
        doc, out, caption = caption_over_barcode()
        with doc, out:
            out[0].draw_rect(fitz.Rect(80, 455, 84, 459), color=None, fill=(1, 1, 1))
            with self.assertRaises(validation.ValidationError):
                validation.validate_ticket(doc, out, bot.srv, [[caption["text"]]])

    def test_changed_bar_outside_removed_caption_is_rejected(self):
        doc, out, caption = caption_over_barcode()
        with doc, out:
            out[0].draw_rect(fitz.Rect(80, 470, 84, 474), color=None, fill=(1, 1, 1))
            with self.assertRaises(validation.ValidationError):
                validation.validate_ticket(doc, out, bot.srv, [[caption["text"]]])

    def test_annotation_cannot_change_bar_inside_removed_caption(self):
        doc, out, caption = caption_over_barcode()
        with doc, out:
            annotation = out[0].add_rect_annot(fitz.Rect(80, 455, 84, 459))
            annotation.set_colors(stroke=(1, 1, 1), fill=(1, 1, 1))
            annotation.update()
            with self.assertRaises(validation.ValidationError):
                validation.validate_ticket(doc, out, bot.srv, [[caption["text"]]])

    def test_caption_change_cannot_overlap_decoded_region(self):
        doc, out, caption = caption_over_barcode()
        with doc, out:
            region = validation._raster_code_regions(doc[0])[0]
            self.assertFalse(validation._caption_reveals_original_image(
                doc[0], out[0], region, [fitz.Rect(caption['bbox'])],
                [fitz.Rect(73, 450, 245, 479)]))

    def test_undecodable_vector_bar_removal_is_rejected(self):
        with fitz.open(stream=ticket("plain"), filetype="pdf") as doc:
            for index in range(30):
                x = 65 + index * 3
                doc[0].draw_rect(fitz.Rect(x, 450, x + 1, 480), color=None, fill=(0, 0, 0))
            self.assertFalse(validation._codes(doc[0]))
            with fitz.open(stream=doc.tobytes(), filetype="pdf") as out:
                out[0].draw_rect(fitz.Rect(80, 450, 81, 480), color=None, fill=(1, 1, 1))
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(doc, out, bot.srv, [])

    def test_undecodable_vector_square_removal_is_rejected(self):
        with fitz.open(stream=ticket("plain"), filetype="pdf") as doc:
            for row in range(10):
                for column in range(10):
                    x, y = 60 + column * 4, 450 + row * 4
                    doc[0].draw_rect(fitz.Rect(x, y, x + 2, y + 2), color=None, fill=(0, 0, 0))
            self.assertFalse(validation._codes(doc[0]))
            with fitz.open(stream=doc.tobytes(), filetype="pdf") as out:
                out[0].draw_rect(fitz.Rect(60, 450, 62, 452), color=None, fill=(1, 1, 1))
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(doc, out, bot.srv, [])

    def test_lightened_qr_background_preserves_code(self):
        with coded_ticket(background=229) as before, coded_ticket(background=255) as after:
            report = validation.validate_ticket(before, after, bot.srv, [])
            self.assertGreater(report["readable_codes"], 0)

    def test_readable_code_with_changed_pixel_and_same_payload_is_accepted(self):
        with coded_ticket(background=229) as before, coded_ticket(background=255) as after:
            barcode = zxingcpp.create_barcode("entry-12345", zxingcpp.BarcodeFormat.QRCode)
            pixels = np.asarray(zxingcpp.write_barcode_to_image(barcode, scale=4))
            ys, xs = np.where(pixels[pixels.shape[0] // 2:, pixels.shape[1] // 2:] < 128)
            y = int(ys[0]) + pixels.shape[0] // 2
            x = int(xs[0]) + pixels.shape[1] // 2
            px, py = 60 + (x + 0.5) * 90 / pixels.shape[1], 450 + (y + 0.5) * 90 / pixels.shape[0]
            after[0].draw_rect(fitz.Rect(px - .5, py - .5, px + .5, py + .5), color=None, fill=(1, 1, 1))
            self.assertEqual([value for value, _ in validation._codes(before[0])],
                             [value for value, _ in validation._codes(after[0])])
            report = validation.validate_ticket(before, after, bot.srv, [])
            self.assertGreater(report['readable_codes'], 0)

    def test_readable_code_with_lighter_ink_and_same_payload_is_accepted(self):
        with coded_ticket(background=229) as before, coded_ticket(background=255, ink=100) as after:
            self.assertEqual([value for value, _ in validation._codes(before[0])],
                             [value for value, _ in validation._codes(after[0])])
            validation.validate_ticket(before, after, bot.srv, [])

    def test_readable_code_with_changed_background_is_accepted(self):
        with coded_ticket(background=255) as before, coded_ticket(background=229) as after:
            self.assertEqual([value for value, _ in validation._codes(before[0])],
                             [value for value, _ in validation._codes(after[0])])
            validation.validate_ticket(before, after, bot.srv, [])

    def test_changed_readable_code_payload_is_rejected(self):
        with coded_ticket() as before, coded_ticket() as after:
            barcode = zxingcpp.create_barcode('different-entry', zxingcpp.BarcodeFormat.QRCode)
            pixels = np.asarray(zxingcpp.write_barcode_to_image(barcode, scale=4))
            buf = io.BytesIO()
            Image.fromarray(pixels).save(buf, format='PNG')
            after[0].replace_image(after[0].get_images()[0][0], stream=buf.getvalue())
            with self.assertRaises(validation.ValidationError):
                validation.validate_ticket(before, after, bot.srv, [])

    def test_undecodable_raster_code_removal_is_rejected(self):
        with fitz.open(stream=ticket("kassir"), filetype="pdf") as doc:
            pixels = np.random.default_rng(12).integers(0, 2, size=(45, 45), dtype=np.uint8) * 255
            buf = io.BytesIO()
            Image.fromarray(pixels).resize((180, 180), Image.Resampling.NEAREST).save(buf, format="PNG")
            doc[0].insert_image(fitz.Rect(60, 400, 240, 580), stream=buf.getvalue())
            self.assertFalse(validation._codes(doc[0]))
            out = fitz.open(stream=doc.tobytes(), filetype="pdf")
            try:
                out[0].delete_image(out[0].get_images()[0][0])
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(doc, out, bot.srv, [])
            finally:
                out.close()

    def test_entry_code_and_fields_survive_normal_cleanup(self):
        with coded_ticket() as doc:
            out, _ = bot.srv.autoclean_doc(doc)
            try:
                report = validation.validate_ticket(doc, out, bot.srv, bot._cleanup_targets(doc))
                self.assertGreater(report["readable_codes"], 0)
                self.assertGreater(report["fields"], 0)
            finally:
                out.close()

    def test_removed_entry_qr_rejected(self):
        with coded_ticket() as doc:
            out = fitz.open(stream=doc.tobytes(), filetype="pdf")
            try:
                out[0].delete_image(out[0].get_images()[0][0])
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(doc, out, bot.srv, [])
            finally:
                out.close()

    def test_changed_row_rejected(self):
        with coded_ticket() as doc:
            out = fitz.open(stream=doc.tobytes(), filetype="pdf")
            try:
                for rect in out[0].search_for("Ряд 6 Место 23"):
                    out[0].add_redact_annot(rect)
                out[0].apply_redactions()
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(doc, out, bot.srv, [])
            finally:
                out.close()

    def test_page_loss_rejected(self):
        with coded_ticket() as doc:
            out = fitz.open()
            try:
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(doc, out, bot.srv, [])
            finally:
                out.close()

    def test_intentionally_removed_promo_qr_allowed(self):
        with coded_ticket() as doc:
            region = fitz.Rect(60, 450, 150, 540)
            out = fitz.open(stream=doc.tobytes(), filetype="pdf")
            try:
                out[0].delete_image(out[0].get_images()[0][0])
                with patch.object(bot.srv, "_promo_qr_geom", return_value=[(1, region)]):
                    validation.validate_ticket(doc, out, bot.srv, [])
            finally:
                out.close()

    def test_pushkin_banner_removal_allowed(self):
        doc = fitz.open(stream=ticket("plain"), filetype="pdf")
        pixels = np.full((50, 175), 255, dtype=np.uint8)
        pixels[:, ::4] = 0
        buf = io.BytesIO()
        Image.fromarray(pixels).save(buf, format="PNG")
        doc[0].insert_image(fitz.Rect(430, 20, 560, 50), stream=buf.getvalue())
        doc[0].insert_text((430, 15), "В РАМКАХ ПРОГРАММЫ",
                           fontname="F0", fontfile=str(Path(bot.BILETNAYA) / "vendor/Arial.ttf"), fontsize=6)
        out = fitz.open(stream=doc.tobytes(), filetype="pdf")
        try:
            images = out[0].get_images()
            out[0].delete_image(images[-1][0])
            for r in out[0].search_for("В РАМКАХ ПРОГРАММЫ"):
                out[0].add_redact_annot(r, fill=False)
            out[0].apply_redactions()
            validation.validate_ticket(doc, out, bot.srv, [["В РАМКАХ ПРОГРАММЫ"]])
        finally:
            doc.close()
            out.close()


if __name__ == "__main__":
    unittest.main()
