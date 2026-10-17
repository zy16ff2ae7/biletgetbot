"""Build ДЕД РЕДАЧИТ brand assets from brand/welcome_base.jpg.

Outputs (next to the base image):
  welcome.jpg  — static 16:9 cover with the typed title (sendPhoto fallback)
  welcome.mp4  — dynamic greeting: camera push-in, lamp pulse, dust in the
                 beam, typewriter shake, title typed letter by letter
  ded.jpg      — square avatar crop of the grandfather

Typefaces (SIL OFL, bundled in brand/): Prata for the title,
Cormorant Italic for the subtitle.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
BRAND = HERE / "brand"
BASE = BRAND / "welcome_base.jpg"
FONT_TITLE = BRAND / "Prata-Regular.ttf"
FONT_SUB = BRAND / "Cormorant-Italic.ttf"

W, H = 960, 540
FPS = 15
FRAMES = 72
TITLE = "ДЕД РЕДАЧИТ"
SUBTITLE = "театральные билеты — с чистого листа"
HANDLE = "@biletgetbot"
CREAM = (246, 235, 212)
ROSE = (247, 183, 210)
MUTED = (206, 186, 200)


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


def base_frame(zoom, cx, cy, shake=(0, 0), src=None):
    img = cover_crop(src or SRC, W, H, cx, cy, zoom)
    if shake != (0, 0):
        img = img.transform((W, H), Image.AFFINE, (1, 0, shake[0], 0, 1, shake[1]))
    return img


def glow_layer(strength):
    """Warm additive light centred on the desk lamp (fractional position)."""
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    lamp = (int(0.20 * W), int(0.36 * H))
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
        self.x = rng.uniform(0.10, 0.40, count)
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


def _font(path, size, weight=None):
    font = ImageFont.truetype(str(path), size)
    if weight is not None:
        try:
            font.set_variation_by_axes([weight])
        except Exception:
            pass
    return font


def draw_title(img, visible_chars, caret_on, with_subtitle):
    d = ImageDraw.Draw(img)
    title_font = _font(FONT_TITLE, 76)
    sub_font = _font(FONT_SUB, 35, weight=540)
    small_font = _font(FONT_SUB, 22, weight=460)
    tracking = 12

    def width_of(text, font, gap):
        return sum(d.textlength(ch, font=font) + gap for ch in text) - gap

    tw = width_of(TITLE, title_font, tracking)
    x0 = (W - tw) / 2
    y0 = H - 158

    # Тёмная сливовая подложка-лента под заголовком.
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rectangle([0, y0 - 30, W, H], fill=(24, 12, 28, 172))
    band = band.filter(ImageFilter.GaussianBlur(10))
    img.paste(band, (0, 0), band)

    # Тень, затем кремовые буквы с межбуквенным просветом.
    d = ImageDraw.Draw(img)
    x = x0
    for ch in TITLE[:visible_chars]:
        d.text((x + 2, y0 + 3), ch, font=title_font, fill=(10, 5, 12))
        d.text((x, y0), ch, font=title_font, fill=CREAM)
        x += d.textlength(ch, font=title_font) + tracking

    if caret_on and visible_chars < len(TITLE):
        cx = x0 + sum(d.textlength(c, font=title_font) + tracking for c in TITLE[:visible_chars])
        d.rectangle([cx + 5, y0 + 8, cx + 9, y0 + 68], fill=CREAM)

    if with_subtitle:
        sw = width_of(SUBTITLE, sub_font, 2)
        x = (W - sw) / 2
        for ch in SUBTITLE:
            d.text((x + 1, y0 + 96 + 1), ch, font=sub_font, fill=(10, 5, 12))
            d.text((x, y0 + 96), ch, font=sub_font, fill=ROSE)
            x += d.textlength(ch, font=sub_font) + 2
        hw = width_of(HANDLE, small_font, 1)
        d.text(((W - hw) / 2, y0 + 132), HANDLE, font=small_font, fill=MUTED)
    return img


SRC = Image.open(BASE).convert("RGB")
SCENE_LAUGH_PATH = BRAND / "key_laugh.jpg"   # дед хохочет
SCENE_WIN_PATH = BRAND / "key_win.jpg"       # дед вскидывает билет
SOURCES = {"type": SRC,
           "laugh": Image.open(SCENE_LAUGH_PATH).convert("RGB"),
           "win": Image.open(SCENE_WIN_PATH).convert("RGB")}

# --- статичная обложка -------------------------------------------------------
poster = base_frame(1.04, 0.54, 0.52)
poster = screen(poster, glow_layer(0.35))
poster = Dust(seed=11).draw(poster, 8)
poster = draw_title(poster, len(TITLE), False, True)
poster = ImageEnhance.Contrast(poster).enhance(1.03)
poster.save(BRAND / "welcome.jpg", quality=90)

# --- аватар: триумфальный кадр с поднятым билетом ----------------------------
portrait = cover_crop(SOURCES["win"], 800, 800, cx=0.63, cy=0.52, zoom=1.45)
portrait = ImageEnhance.Color(portrait).enhance(1.06)
portrait = ImageEnhance.Sharpness(portrait).enhance(1.15)
portrait.save(BRAND / "ded.jpg", quality=90)

# --- кадры видео: три сцены с эмоциями деда ---------------------------------
def scene_of(f):
    """(ключ сцены, локальный номер кадра) по глобальному кадру."""
    if f <= 27:
        return "type", f
    if f <= 30:
        return "flash1", f - 28          # вспышка-переход
    if f <= 45:
        return "laugh", f - 31
    if f <= 47:
        return "flash2", f - 46          # вторая вспышка
    return "win", f - 48


class Sparks:
    """Розовые искры вокруг поднятого билета в финальной сцене."""

    def __init__(self, count=20, seed=3):
        rng = np.random.default_rng(seed)
        self.angle = rng.uniform(-2.4, -0.7, count)      # веер вверх
        self.speed = rng.uniform(0.010, 0.030, count)
        self.size = rng.uniform(1.5, 3.6, count)
        self.phase = rng.uniform(0, 2 * np.pi, count)
        self.count = count

    def draw(self, img, local):
        overlay = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(overlay)
        ox, oy = 0.72 * W, 0.16 * H                      # точка поднятого билета
        for i in range(self.count):
            t = (local / 10 + self.phase[i]) % 1.6       # цикл разлёта
            if t > 1.0:
                continue
            x = ox + np.cos(self.angle[i]) * self.speed[i] * t * W
            y = oy + np.sin(self.angle[i]) * self.speed[i] * t * W + 0.012 * t * t * W
            fade = max(0.0, 1.0 - t)
            r = self.size[i] * (1 - 0.4 * t)
            c = (int(255 * fade), int(170 * fade), int(205 * fade))
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        return screen(img, overlay.filter(ImageFilter.GaussianBlur(0.7)))


def flash_overlay(img, local):
    """Короткая белая вспышка между сценами (3 кадра)."""
    alpha = (230, 110, 35)[min(local, 2)]
    return Image.blend(img, Image.new("RGB", (W, H), (255, 246, 230)), alpha / 255)


tmp = HERE / "work" / "frames"
tmp.mkdir(parents=True, exist_ok=True)
dust = Dust()
sparks = Sparks()
TYPE_START = 6
CHARS = len(TITLE)
TYPE_FRAMES = 3
for f in range(FRAMES):
    scene, local = scene_of(f)
    if scene == "flash1" or scene == "flash2":
        frame = base_frame(1.05, 0.55, 0.52)
        frame = draw_title(frame, CHARS, False, True)
        frame = flash_overlay(frame, local)
        frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)
        continue

    if scene == "type":
        zoom = 1.0 + 0.045 * (local / 28)
        cx = 0.54 + 0.02 * (local / 28)
        shake = (1, -1) if local % 3 == 0 else ((-1, 1) if local % 3 == 1 else (0, 0))
    elif scene == "laugh":
        zoom = 1.035 + 0.009 * np.sin(local * 0.5)       # «дыхание» камеры
        cx = 0.54
        shake = (0, -1) if local % 6 == 0 else (0, 0)    # плечи трясутся от смеха
    else:  # win: панч-наезд и лёгкая вибрация триумфа
        ease = min(1.0, local / 6)
        zoom = 1.05 + 0.05 * (1 - (1 - ease) ** 3)
        cx = 0.56
        shake = (1, 0) if local % 4 == 0 else (0, 0)

    frame = base_frame(zoom, cx, 0.52, shake, src=SOURCES[scene])
    pulse = 0.30 + 0.10 * np.sin(f * 0.45) + (0.06 if f % 12 < 2 else 0)
    frame = screen(frame, glow_layer(pulse))
    frame = dust.draw(frame, f)
    if scene == "win":
        frame = sparks.draw(frame, local)
    typed = max(0, min(CHARS, (f - TYPE_START) // TYPE_FRAMES + 1)) if f >= TYPE_START else 0
    caret = (f // 2) % 2 == 0
    frame = draw_title(frame, typed, caret, typed >= CHARS)
    frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)

print("frames:", FRAMES, "size:", W, "x", H)
