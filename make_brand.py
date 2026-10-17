"""Build ДЕД РЕДАЧИТ brand assets from brand/welcome_base.jpg.

Outputs (next to the base image):
  welcome.jpg  — static 16:9 cover with the typed title (sendPhoto fallback)
  welcome.mp4  — dynamic greeting: camera push-in, lamp pulse, dust in the
                 beam, typewriter shake, title typed letter by letter
  ded.jpg      — square avatar crop of the grandfather
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
BRAND = HERE / "brand"
BASE = BRAND / "welcome_base.jpg"
# Шрифт Oranienbaum (SIL OFL) лежит рядом со скриптом, в brand/.
FONT_PATH = Path(__file__).resolve().parent / "brand" / "Oranienbaum.ttf"

W, H = 960, 540
FPS = 15
FRAMES = 72
TITLE = "ДЕД РЕДАЧИТ"
SUBTITLE = "театральные билеты — начисто"
HANDLE = "@biletgetbot"
CREAM = (245, 233, 208)
PINK = (255, 168, 200)


def cover_crop(img, w, h, cx=0.5, cy=0.5, zoom=1.0):
    """Crop a w:h box at fractional center and zoom (>1 = closer)."""
    src_w, src_h = img.size
    box_h = src_h / zoom
    box_w = box_h * (w / h)
    if box_w > src_w:
        box_w = src_w
        box_h = box_w * (h / w)
    left = cx * src_w - box_w / 2
    top = cy * src_h - box_h / 2
    left = max(0, min(src_w - box_w, left))
    top = max(0, min(src_h - box_h, top))
    return img.crop((int(left), int(top), int(left + box_w), int(top + box_h))).resize((w, h), Image.LANCZOS)


def base_frame(zoom, cx, cy, shake=(0, 0)):
    img = cover_crop(SRC, W, H, cx, cy, zoom)
    if shake != (0, 0):
        img = img.transform((W, H), Image.AFFINE, (1, 0, shake[0], 0, 1, shake[1]))
    return img


def glow_layer(strength):
    """Warm additive light centred on the desk lamp (fractional position)."""
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    lamp = (int(0.135 * W), int(0.40 * H))
    for radius, alpha in ((300, 70), (190, 110), (110, 150)):
        d.ellipse([lamp[0] - radius, lamp[1] - radius * 0.8,
                   lamp[0] + radius, lamp[1] + radius * 0.8], fill=alpha)
    layer = layer.filter(ImageFilter.GaussianBlur(60))
    warm = Image.new("RGB", (W, H), (255, 196, 120))
    a = layer.point(lambda v: int(v * strength))
    return Image.composite(warm, Image.new("RGB", (W, H), (0, 0, 0)), a)


def screen(base, overlay):
    return ImageChops.screen(base, overlay)


class Dust:
    """Soft particles drifting upward inside the lamp beam."""

    def __init__(self, count=26, seed=7):
        rng = np.random.default_rng(seed)
        self.x = rng.uniform(0.02, 0.34, count)
        self.y = rng.uniform(0.05, 0.85, count)
        self.speed = rng.uniform(0.004, 0.012, count)
        self.size = rng.uniform(1.2, 3.2, count)
        self.phase = rng.uniform(0, 2 * np.pi, count)
        self.count = count

    def draw(self, img, frame):
        overlay = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for i in range(self.count):
            y = (self.y[i] - self.speed[i] * frame) % 0.95
            x = self.x[i] + 0.004 * np.sin(self.phase[i] + frame * 0.25)
            twinkle = 0.55 + 0.45 * np.sin(self.phase[i] * 3 + frame * 0.6)
            r = self.size[i]
            c = (int(255 * twinkle), int(230 * twinkle), int(210 * twinkle))
            px, py = int(x * W), int(y * H)
            d.ellipse([px - r, py - r, px + r, py + r], fill=c)
        return screen(img, overlay.filter(ImageFilter.GaussianBlur(0.8)))


def draw_title(img, visible_chars, caret_on, with_subtitle):
    d = ImageDraw.Draw(img)
    title_font = ImageFont.truetype(str(FONT_PATH), 86)
    sub_font = ImageFont.truetype(str(FONT_PATH), 30)
    small_font = ImageFont.truetype(str(FONT_PATH), 24)
    tracking = 10

    def width_of(text, font):
        return sum(d.textlength(ch, font=font) + tracking for ch in text) - tracking

    tw = width_of(TITLE, title_font)
    x0 = (W - tw) / 2
    y0 = H - 148

    # Тёмная сливовая подложка-лента под заголовком.
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rectangle([0, y0 - 26, W, H], fill=(24, 12, 28, 168))
    band = band.filter(ImageFilter.GaussianBlur(10))
    img.paste(band, (0, 0), band)

    # Мягкая тень под буквами, затем сам текст.
    d = ImageDraw.Draw(img)
    x = x0
    for ch in TITLE[:visible_chars]:
        d.text((x + 2, y0 + 3), ch, font=title_font, fill=(10, 5, 12))
        d.text((x, y0), ch, font=title_font, fill=CREAM)
        x += d.textlength(ch, font=title_font) + tracking

    if caret_on and visible_chars < len(TITLE):
        cx = x0 + (sum(d.textlength(c, font=title_font) + tracking for c in TITLE[:visible_chars])
                   if visible_chars else 0)
        d.rectangle([cx + 4, y0 + 6, cx + 4 + 5, y0 + 78], fill=CREAM)

    if with_subtitle:
        sw = width_of(SUBTITLE, sub_font)
        x = (W - sw) / 2
        for ch in SUBTITLE:
            d.text((x + 1, y0 + 108 + 1), ch, font=sub_font, fill=(10, 5, 12))
            d.text((x, y0 + 108), ch, font=sub_font, fill=PINK)
            x += d.textlength(ch, font=sub_font) + 3
        hw = d.textlength(HANDLE, font=small_font)
        d.text(((W - hw) / 2, y0 + 148), HANDLE, font=small_font, fill=(210, 190, 200))
    return img


SRC = Image.open(BASE).convert("RGB")

# --- статичная обложка -------------------------------------------------------
poster = base_frame(1.04, 0.54, 0.52)
poster = screen(poster, glow_layer(0.35))
poster = Dust(seed=11).draw(poster, 8)
poster = draw_title(poster, len(TITLE), False, True)
poster = ImageEnhance.Contrast(poster).enhance(1.03)
poster.save(BRAND / "welcome.jpg", quality=90)

# --- аватар ------------------------------------------------------------------
portrait = cover_crop(SRC, 800, 800, cx=0.66, cy=0.52, zoom=1.55)
portrait = ImageEnhance.Color(portrait).enhance(1.05)
portrait.save(BRAND / "ded.jpg", quality=90)

# --- кадры видео -------------------------------------------------------------
tmp = HERE / "work" / "frames"
tmp.mkdir(parents=True, exist_ok=True)
dust = Dust()
TYPE_START = 6
CHARS = len(TITLE)
TYPE_FRAMES = 3
for f in range(FRAMES):
    zoom = 1.0 + 0.045 * (f / FRAMES)          # медленный наезд камеры
    cx = 0.52 + 0.02 * (f / FRAMES)            # лёгкий дрейф к деду
    shake = (1, -1) if f % 3 == 0 else ((-1, 1) if f % 3 == 1 else (0, 0))  # тряска машинки
    frame = base_frame(zoom, cx, 0.52, shake)
    pulse = 0.30 + 0.10 * np.sin(f * 0.45) + (0.06 if f % 12 < 2 else 0)
    frame = screen(frame, glow_layer(pulse))
    frame = dust.draw(frame, f)
    typed = max(0, min(CHARS, (f - TYPE_START) // TYPE_FRAMES + 1)) if f >= TYPE_START else 0
    caret = (f // 2) % 2 == 0
    frame = draw_title(frame, typed, caret, typed >= CHARS)
    frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)

print("frames:", FRAMES, "size:", W, "x", H)
