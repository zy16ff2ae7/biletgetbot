"""Build ДЕД РЕДАЧИТ brand assets from brand/welcome_base.jpg.

Outputs (next to the base image):
  welcome.jpg  — static 16:9 cover with the gold title (sendPhoto fallback)
  welcome.mp4  — dynamic greeting: camera push-in, lamp pulse, dust in the
                 beam, flash scene transitions, pink sparks; the title appears
                 as a whole with a cinematic fade and a soft upward drift,
                 then the small-caps subtitle and the handle fade in
  ded.jpg      — square avatar crop of the triumphant grandfather

Typeface (SIL OFL, bundled in brand/): Prata — title, subtitle, handle.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
BRAND = HERE / "brand"
BASE = BRAND / "welcome_base.jpg"
FONT_MAIN = BRAND / "Prata-Regular.ttf"

W, H = 960, 540
FPS = 15
FRAMES = 72
TITLE = "ДЕД РЕДАЧИТ"
SUBTITLE = "ПЕРЕПЕЧАТАЕМ БИЛЕТЫ НАЧИСТО"
HANDLE = "@biletgetbot"
GOLD_TOP = (252, 243, 222)
GOLD_MID = (243, 211, 146)
GOLD_DEEP = (198, 150, 86)
GOLD_EDGE = (240, 205, 138)
CREAM = (246, 235, 212)
MUTED = (206, 188, 198)

TITLE_IN = (10, 22)     # окно фейда заголовка, кадры
SUB_IN = (32, 40)       # окно фейда подзаголовка
HANDLE_IN = (40, 46)    # окно фейда канала


def clip01(v):
    return max(0.0, min(1.0, v))


def smooth(t):
    t = clip01(t)
    return t * t * (3 - 2 * t)


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


def vignette(img, strength=0.34):
    """Soft cinematic darkening towards the corners."""
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).ellipse([-W * 0.25, -H * 0.35, W * 1.25, H * 1.35], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(120))
    return Image.composite(img, ImageEnhance.Brightness(img).enhance(1.0 - strength), m)


def grain(img, frame, amount=3.2):
    """Subtle film grain, unique per frame."""
    rng = np.random.default_rng(frame * 7 + 1)
    noise = rng.normal(0.0, amount, (H, W, 1)).astype(np.int16)
    arr = np.asarray(img).astype(np.int16) + noise
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


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


class Sparks:
    """Розовые искры вокруг поднятого билета в финальной сцене."""

    def __init__(self, count=34, seed=3):
        rng = np.random.default_rng(seed)
        self.angle = rng.uniform(-2.5, -0.6, count)
        self.speed = rng.uniform(0.010, 0.034, count)
        self.size = rng.uniform(1.8, 4.2, count)
        self.phase = rng.uniform(0, 2 * np.pi, count)
        self.count = count

    def draw(self, img, local):
        overlay = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(overlay)
        ox, oy = 0.715 * W, 0.115 * H
        for i in range(self.count):
            t = (local / 10 + self.phase[i]) % 1.6
            if t > 1.0:
                continue
            x = ox + np.cos(self.angle[i]) * self.speed[i] * t * W
            y = oy + np.sin(self.angle[i]) * self.speed[i] * t * W + 0.012 * t * t * W
            fade = max(0.0, 1.0 - t) * (0.55 + 0.45 * np.sin(self.phase[i] * 5 + t * 9))
            r = self.size[i] * (1 - 0.4 * t)
            c = (int(255 * fade), int(185 * fade), int(215 * fade))
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        return screen(img, overlay.filter(ImageFilter.GaussianBlur(0.7)))


def flash_overlay(img, local):
    """Короткая тёплая вспышка между сценами (3 кадра), мягкая на пике."""
    alpha = (140, 70, 25)[min(local, 2)]
    return Image.blend(img, Image.new("RGB", (W, H), (255, 244, 228)), alpha / 255)


def _font(path, size):
    return ImageFont.truetype(str(path), size)


def _gold_gradient(y0, y1):
    """Вертикальный золотой градиент высотой y1-y0 (RGB, ширина W)."""
    stops = [(0.0, GOLD_TOP), (0.42, GOLD_MID), (0.72, GOLD_DEEP), (1.0, GOLD_EDGE)]
    ys = np.linspace(0.0, 1.0, y1 - y0)
    channels = []
    for i in range(3):
        xs = [s[0] for s in stops]
        vals = [s[1][i] for s in stops]
        channels.append(np.interp(ys, xs, vals))
    row = np.stack(channels, axis=1).astype(np.uint8)          # (h, 3)
    return Image.fromarray(np.repeat(row[:, None, :], W, axis=1), "RGB")


def text_overlay(title_alpha, sub_alpha, handle_alpha):
    """Весь текстовый блок одним RGBA-слоем: лента, золотой заголовок,
    капители подзаголовка, канал с линейками. Альфа каждого элемента своя."""
    if title_alpha <= 0 and sub_alpha <= 0 and handle_alpha <= 0:
        return Image.new("RGBA", (W, H), (0, 0, 0, 0))
    title_font = _font(FONT_MAIN, 78)
    sub_font = _font(FONT_MAIN, 25)
    small_font = _font(FONT_MAIN, 22)
    tracking = 14
    sub_tracking = 6

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def width_of(text, font, gap):
        return sum(probe.textlength(ch, font=font) + gap for ch in text) - gap

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    drift = int((1 - title_alpha) * 12)          # мягкое всплытие при появлении
    y0 = H - 188 + drift
    glyph_h = int(title_font.size * 1.06)

    # Лента-подложка: появляется вместе с заголовком, верхняя кромка — фейд.
    band_top = y0 - 32
    fade_h = 70
    alpha_col = np.zeros(H)
    band_alpha = 178.0 * title_alpha
    first, last = max(0, band_top), min(H, band_top + fade_h)
    alpha_col[first:last] = (np.linspace(0, 1, last - first) ** 0.8) * band_alpha
    alpha_col[last:] = band_alpha
    band = np.zeros((H, W, 4), dtype=np.uint8)
    band[..., 0], band[..., 1], band[..., 2] = 24, 12, 28
    band[..., 3] = alpha_col.astype(np.uint8)[:, None]
    ov = Image.alpha_composite(ov, Image.fromarray(band, "RGBA").filter(ImageFilter.GaussianBlur(2)))

    # Заголовок: маска букв → тень → золотой градиент → верхний блик.
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    x = (W - width_of(TITLE, title_font, tracking)) / 2
    for ch in TITLE:
        md.text((x, y0), ch, font=title_font, fill=255)
        x += probe.textlength(ch, font=title_font) + tracking

    shadow = ImageChops.offset(mask.filter(ImageFilter.GaussianBlur(6)), 3, 5)
    shadow = shadow.point(lambda v: int(v * 0.55 * title_alpha))
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer.paste(Image.new("RGBA", (W, H), (8, 4, 10, 255)), (0, 0), shadow)
    ov = Image.alpha_composite(ov, shadow_layer)

    if title_alpha > 0:
        gold_mask = mask.point(lambda v: int(v * title_alpha))
        gold_full = Image.new("RGB", (W, H), (0, 0, 0))
        gold_full.paste(_gold_gradient(y0, y0 + glyph_h), (0, y0), mask.crop((0, y0, W, y0 + glyph_h)))
        ov.paste(gold_full, (0, 0), gold_mask)
        bevel = ImageChops.subtract(mask, ImageChops.offset(mask, 0, 1)).point(
            lambda v: int(v * 0.6 * title_alpha))
        ov.paste(Image.new("RGBA", (W, H), (255, 250, 236, 255)), (0, 0), bevel)

    d = ImageDraw.Draw(ov)

    # Подзаголовок: капители Prata, кремовый цвет, золотые линейки по бокам.
    if sub_alpha > 0:
        a = int(255 * sub_alpha)
        sw = width_of(SUBTITLE, sub_font, sub_tracking)
        sx = (W - sw) / 2
        sy = y0 + 96
        for ch in SUBTITLE:
            d.text((sx + 1, sy + 1), ch, font=sub_font, fill=(8, 4, 10, a))
            d.text((sx, sy), ch, font=sub_font, fill=(*CREAM, a))
            sx += probe.textlength(ch, font=sub_font) + sub_tracking
        rule_alpha = int(a * 0.85)
        line_y = sy + 44
        edge = (W - sw) / 2 - 14
        for side in (-1, 1):
            x_from, x_to = (edge, edge - 120) if side < 0 else (W - edge, W - edge + 120)
            d.line([x_from, line_y, x_to, line_y], fill=(168, 138, 148, rule_alpha), width=1)
            mid = (x_from + x_to) / 2
            r = 3.2
            d.polygon([(mid, line_y - r), (mid + r, line_y), (mid, line_y + r), (mid - r, line_y)],
                      fill=(*GOLD_EDGE, rule_alpha))

    # Канал: @biletgetbot между орнаментальными линейками.
    if handle_alpha > 0:
        a = int(255 * handle_alpha)
        hy = y0 + 142
        hw = probe.textlength(HANDLE, font=small_font)
        hx = (W - hw) / 2
        d.text((hx, hy), HANDLE, font=small_font, fill=(*MUTED, a))
        line_y = hy + 15
        for side in (-1, 1):
            line_end = hx + (side * -22)
            line_start = line_end + (side * -104)
            d.line([line_start, line_y, line_end, line_y], fill=(168, 138, 148, int(a * 0.8)), width=1)
            mid = (line_start + line_end) / 2
            r = 3.0
            d.polygon([(mid, line_y - r), (mid + r, line_y), (mid, line_y + r), (mid - r, line_y)],
                      fill=(*GOLD_EDGE, a))
    return ov


def compose_text(img, title_alpha, sub_alpha, handle_alpha):
    overlay = text_overlay(title_alpha, sub_alpha, handle_alpha)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


SRC = Image.open(BASE).convert("RGB")
SOURCES = {"type": SRC,
           "laugh": Image.open(BRAND / "key_laugh.jpg").convert("RGB"),
           "win": Image.open(BRAND / "key_win.jpg").convert("RGB")}

# --- статичная обложка -------------------------------------------------------
poster = base_frame(1.04, 0.54, 0.52)
poster = screen(poster, glow_layer(0.35))
poster = Dust(seed=11).draw(poster, 8)
poster = vignette(poster)
poster = compose_text(poster, 1, 1, 1)
poster = ImageEnhance.Contrast(poster).enhance(1.03)
poster.save(BRAND / "welcome.jpg", quality=90)

# --- аватар: триумфальный кадр с поднятым билетом ----------------------------
portrait = cover_crop(SOURCES["win"], 800, 800, cx=0.63, cy=0.52, zoom=1.45)
portrait = ImageEnhance.Color(portrait).enhance(1.06)
portrait = ImageEnhance.Sharpness(portrait).enhance(1.15)
portrait.save(BRAND / "ded.jpg", quality=90)

# --- кадры видео: три сцены, титры появляются мягким фейдом ------------------
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


tmp = HERE / "work" / "frames"
tmp.mkdir(parents=True, exist_ok=True)
dust = Dust()
sparks = Sparks()
for f in range(FRAMES):
    scene, local = scene_of(f)
    t_title = smooth((f - TITLE_IN[0]) / (TITLE_IN[1] - TITLE_IN[0]))
    t_sub = smooth((f - SUB_IN[0]) / (SUB_IN[1] - SUB_IN[0]))
    t_handle = smooth((f - HANDLE_IN[0]) / (HANDLE_IN[1] - HANDLE_IN[0]))

    if scene == "flash1" or scene == "flash2":
        frame = base_frame(1.05, 0.55, 0.52)
        frame = vignette(frame)
        frame = compose_text(frame, t_title, t_sub, t_handle)
        frame = flash_overlay(frame, local)
        frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)
        continue

    if scene == "type":
        zoom = 1.0 + 0.045 * (local / 28)
        cx = 0.54 + 0.02 * (local / 28)
        shake = (1, -1) if local % 3 == 0 else ((-1, 1) if local % 3 == 1 else (0, 0))
    elif scene == "laugh":
        zoom = 1.047 + 0.008 * np.sin(local * 0.5)       # «дыхание» камеры
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
    frame = vignette(frame)
    frame = compose_text(frame, t_title, t_sub, t_handle)
    frame = grain(frame, f)
    frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)

print("frames:", FRAMES, "size:", W, "x", H)
