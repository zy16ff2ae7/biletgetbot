"""A readable code must not exempt an unrelated unreadable code-like area."""
import io
import unittest

import fitz
import numpy as np
from PIL import Image
import zxingcpp

import validation
from test_processing import bot, ticket


class MixedCodeValidationTests(unittest.TestCase):
    def test_erasing_unknown_patch_in_image_with_readable_qr_is_rejected(self):
        barcode = zxingcpp.create_barcode('entry-known', zxingcpp.BarcodeFormat.QRCode)
        qr = np.asarray(zxingcpp.write_barcode_to_image(barcode, scale=4))
        pixels = np.full((512, 512), 255, dtype=np.uint8)
        pixels[20:20 + qr.shape[0], 20:20 + qr.shape[1]] = qr
        noise = np.random.default_rng(12).integers(0, 2, size=(90, 90), dtype=np.uint8) * 255
        pixels[250:510, 250:510] = np.asarray(
            Image.fromarray(noise).resize((260, 260), Image.Resampling.NEAREST))
        image = io.BytesIO()
        Image.fromarray(pixels).save(image, format='PNG')
        region = fitz.Rect(60, 400, 280, 620)

        with fitz.open(stream=ticket('plain'), filetype='pdf') as before:
            before[0].insert_image(region, stream=image.getvalue())
            with fitz.open(stream=before.tobytes(), filetype='pdf') as after:
                # Erase only the undecodable patch, leaving the QR untouched.
                patch = fitz.Rect(60 + 250 * 220 / 512, 400 + 250 * 220 / 512,
                                  60 + 510 * 220 / 512, 400 + 510 * 220 / 512)
                after[0].draw_rect(patch, color=None, fill=(1, 1, 1))
                source_codes = [value for value, _box in validation._codes(before[0])]
                self.assertEqual([('QR Code', b'entry-known')], source_codes)
                self.assertEqual(source_codes,
                                 [value for value, _box in validation._codes(after[0])])
                self.assertIn(region, validation._raster_code_regions(before[0]))
                with self.assertRaises(validation.ValidationError):
                    validation.validate_ticket(before, after, bot.srv, [])


if __name__ == '__main__':
    unittest.main()
