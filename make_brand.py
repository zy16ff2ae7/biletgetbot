"""Build ДЕД РЕДАЧИТ brand assets from brand/welcome_base.jpg.

Outputs (next to the base image):
  welcome.jpg  — static 16:9 cover (sendPhoto fallback)
  welcome.mp4  — cinematic letterbox greeting: film bars slide in, the gold
                 title fades inside the lower bar, then the small-caps
                 subtitle and the handle; three scenes of the grandfather
                 (typing -> laughing -> triumphant ticket), lamp pulse, dust,
                 pink sparks, film grain on the scene only
  ded.jpg      — square avatar crop of the triumphant grandfather

Typeface (SIL OFL, bundled in brand/): Prata — all text.
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
FRAMES = 110
TITLE = "ДЕД РЕДАЧИТ"
SUBTITLE = "перепечатаем ваши билеты начисто"
HANDLE = "@biletgetbot"

GOLD_TOP = (252, 243, 222)
GOLD_MID = (243, 211, 146)
GOLD_DEEP = (198, 150, 86)
GOLD_EDGE = (240, 205, 138)
CREAM = (250, 242, 226)
PLATINUM = (240, 233, 240)
BAR = (6, 3, 8)

BAR_B = 132          # нижний литер-бар
BAR_T = 42           # верхний бар
TITLE_IN = (14, 26)
SUB_IN = (30, 40)
HANDLE_IN = (38, 46)
CTA_IN = (92, 100)   # подсказка об абонементе в верхнем баре
FREE_LINE = "2 билета в день — бесплатно"
FREE_OUT = (88, 95)  # уходит перед CTA
BARS_IN = 12         # бары задвигаются за 12 кадров


def smooth(v):
    v = max(0.0, min(1.0, v))
    return v * v * (3 - 2 * v)


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


def grain(img, frame, amount=2.6):
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
    """Короткая тёплая вспышка между сценами (3 кадра)."""
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


def _gold_text(ov, mask, alpha):
    """Золотая заливка по фактическому bbox чернил + блик по верхней кромке."""
    if alpha <= 0:
        return ov
    ink = mask.getbbox()
    grad = _gold_gradient(ink[1], ink[3]).crop((ink[0], 0, ink[2], ink[3] - ink[1])).convert("RGBA")
    gold = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gold.paste(grad, (ink[0], ink[1]), mask.crop(ink))
    gold.putalpha(mask.point(lambda v: int(v * alpha)))
    ov = Image.alpha_composite(ov, gold)
    bevel = ImageChops.subtract(mask, ImageChops.offset(mask, 0, 1)).point(
        lambda v: int(v * 0.5 * alpha))
    ov.paste(Image.new("RGBA", (W, H), (255, 250, 236, 255)), (0, 0), bevel)
    return ov


_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def probe_w(font, ch):
    return _probe.textlength(ch, font=font)


def overlay(title_a, sub_a, handle_a, bars_e, cta_a=0.0, free_a=0.0):
    """Кинематографичный letterbox: чистые чёрные бары с тонкой золотой
    кромкой; титры — внутри нижнего бара, на идеально чёрном фоне."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if bars_e <= 0:
        return ov
    d = ImageDraw.Draw(ov)
    bar_b = int(round(BAR_B * bars_e))
    bar_t = int(round(BAR_T * bars_e))
    d.rectangle([0, H - bar_b, W, H], fill=(*BAR, 255))
    d.rectangle([0, 0, W, bar_t], fill=(*BAR, 255))
    # Тонкая золотая кромка по внутренним краям баров.
    line_a = int(150 * bars_e)
    d.line([0, H - bar_b - 1, W, H - bar_b - 1], fill=(212, 170, 96, line_a), width=1)
    d.line([0, bar_t, W, bar_t], fill=(212, 170, 96, int(line_a * 0.55)), width=1)

    # Бесплатный лимит в верхнем баре — до появления CTA.
    if free_a > 0 and cta_a < 1:
        ff = _font(FONT_MAIN, 16)
        fa = int(230 * free_a * (1 - cta_a))
        fw = sum(probe_w(ff, ch) + 2 for ch in FREE_LINE) - 2
        fx = (W - fw) / 2
        fy = max(7, (bar_t - 19) // 2)
        d.text((fx, fy), FREE_LINE, font=ff, fill=(*GOLD_MID, fa))

    # CTA в верхнем баре: появляется в конце цикла.
    if cta_a > 0:
        cta_font = _font(FONT_MAIN, 17)
        cta = "абонемент — 1000 звёзд · /subscribe"
        a = int(255 * cta_a)
        cw = sum(probe_w(cta_font, ch) + 2 for ch in cta) - 2
        cx = (W - cw) / 2
        cy = max(8, (bar_t - 20) // 2)
        for ch in cta:
            d.text((cx, cy), ch, font=cta_font, fill=(*GOLD_MID, a))
            cx += probe_w(cta_font, ch) + 2

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def width_of(text, font, gap):
        return sum(probe.textlength(ch, font=font) + gap for ch in text) - gap

    title_font = _font(FONT_MAIN, 50)
    sub_font = _font(FONT_MAIN, 21)
    small_font = _font(FONT_MAIN, 20)
    tracking = 10
    top = H - bar_b

    # Заголовок золотом по центру бара.
    if title_a > 0:
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        tw = width_of(TITLE, title_font, tracking)
        x = (W - tw) / 2
        for ch in TITLE:
            md.text((x, top + 12), ch, font=title_font, fill=255)
            x += probe.textlength(ch, font=title_font) + tracking
        shadow = ImageChops.offset(mask.filter(ImageFilter.GaussianBlur(4)), 2, 3)
        shadow = shadow.point(lambda v: int(v * 0.35 * title_a))
        shl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shl.paste(Image.new("RGBA", (W, H), (0, 0, 0, 255)), (0, 0), shadow)
        ov = Image.alpha_composite(ov, shl)
        ov = _gold_text(ov, mask, title_a)

    # Подзаголовок: изящные строчные капители + золотой росчерк сверху.
    if sub_a > 0:
        a = int(255 * sub_a)
        d = ImageDraw.Draw(ov)
        sw = width_of(SUBTITLE, sub_font, 4)
        sx = (W - sw) / 2
        sy = top + 72
        for ch in SUBTITLE:
            d.text((sx + 1, sy + 1), ch, font=sub_font, fill=(0, 0, 0, int(a * 0.8)))
            d.text((sx, sy), ch, font=sub_font, fill=(*CREAM, a))
            sx += probe.textlength(ch, font=sub_font) + 4
        # Росчерк: линия с ромбом по центру, над подзаголовком.
        fy = top + 64
        r = 2.6
        d.polygon([(W / 2, fy - r), (W / 2 + r, fy), (W / 2, fy + r), (W / 2 - r, fy)],
                  fill=(*GOLD_EDGE, int(a * 0.9)))
        d.line([W / 2 - 70, fy, W / 2 - 10, fy], fill=(198, 162, 110, int(a * 0.55)), width=1)
        d.line([W / 2 + 10, fy, W / 2 + 70, fy], fill=(198, 162, 110, int(a * 0.55)), width=1)

    # Канал: платиновый, ромбы по бокам на одной строке.
    if handle_a > 0:
        a = int(255 * handle_a)
        d = ImageDraw.Draw(ov)
        hw = probe.textlength(HANDLE, font=small_font)
        hx = (W - hw) / 2
        hy = top + 98
        for gdx, gdy in ((1, 1), (-1, 1)):
            d.text((hx + gdx, hy + gdy), HANDLE, font=small_font, fill=(0, 0, 0, int(a * 0.8)))
        d.text((hx, hy), HANDLE, font=small_font, fill=(*PLATINUM, a))
        cy = hy + 11
        r = 2.8
        for side in (-1, 1):
            # Ромб и линия СНАРУЖИ от текста: слева от левого края, справа от правого.
            inner = hx - 14 if side < 0 else hx + hw + 14
            outer = inner + side * 70
            d.line([outer, cy, inner, cy], fill=(198, 172, 182, int(a * 0.8)), width=1)
            mid = (outer + inner) / 2
            d.polygon([(mid, cy - r), (mid + r, cy), (mid, cy + r), (mid - r, cy)],
                      fill=(*GOLD_EDGE, a))
    return ov


def compose(img, title_a, sub_a, handle_a, bars_e, cta_a=0.0, free_a=0.0):
    o = overlay(title_a, sub_a, handle_a, bars_e, cta_a, free_a)
    return Image.alpha_composite(img.convert("RGBA"), o).convert("RGB")


SRC = Image.open(BASE).convert("RGB")
SOURCES = {"type": SRC,
           "laugh": Image.open(BRAND / "key_laugh.jpg").convert("RGB"),
           "win": Image.open(BRAND / "key_win.jpg").convert("RGB")}

# --- статичная обложка -------------------------------------------------------
poster = base_frame(1.04, 0.54, 0.52)
poster = screen(poster, glow_layer(0.35))
poster = Dust(seed=11).draw(poster, 8)
poster = vignette(poster)
poster = grain(poster, 3)
poster = compose(poster, 1, 1, 1, 1, 0, 0)
poster.save(BRAND / "welcome.jpg", quality=90)

# --- аватар: триумфальный кадр с поднятым билетом ----------------------------
portrait = cover_crop(SOURCES["win"], 800, 800, cx=0.63, cy=0.52, zoom=1.45)
portrait = ImageEnhance.Color(portrait).enhance(1.06)
portrait = ImageEnhance.Sharpness(portrait).enhance(1.15)
portrait.save(BRAND / "ded.jpg", quality=90)

# --- миниатюра видео (Telegram превью) ---------------------------------------
thumb = cover_crop(SOURCES["win"], 320, 320, cx=0.63, cy=0.52, zoom=1.45)
thumb.save(BRAND / "thumb.jpg", quality=85)

# --- кадры видео -------------------------------------------------------------
def scene_of(f):
    """(ключ сцены, локальный номер кадра) по глобальному кадру."""
    if f <= 39:
        return "type", f
    if f <= 43:
        return "flash1", f - 40          # вспышка-переход
    if f <= 64:
        return "laugh", f - 44
    if f <= 67:
        return "flash2", f - 65          # вторая вспышка
    return "win", f - 68


tmp = HERE / "work" / "frames"
tmp.mkdir(parents=True, exist_ok=True)
dust = Dust()
sparks = Sparks()
for f in range(FRAMES):
    scene, local = scene_of(f)
    t_bars = smooth((f + 1) / BARS_IN)
    t_title = smooth((f - TITLE_IN[0]) / (TITLE_IN[1] - TITLE_IN[0]))
    t_sub = smooth((f - SUB_IN[0]) / (SUB_IN[1] - SUB_IN[0]))
    t_handle = smooth((f - HANDLE_IN[0]) / (HANDLE_IN[1] - HANDLE_IN[0]))
    t_cta = smooth((f - CTA_IN[0]) / (CTA_IN[1] - CTA_IN[0]))
    t_free = smooth((f - 10) / 6) * (1 - smooth((f - FREE_OUT[0]) / (FREE_OUT[1] - FREE_OUT[0])))

    if scene in ("flash1", "flash2"):
        frame = base_frame(1.05, 0.55, 0.52)
        frame = vignette(frame)
        frame = grain(frame, f)
        frame = flash_overlay(frame, local)   # бары и титры остаются чёрными
        frame = compose(frame, t_title, t_sub, t_handle, t_bars, t_cta, t_free)
        frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)
        continue

    if scene == "type":
        zoom = 1.0 + 0.045 * (local / 40)
        cx = 0.54 + 0.02 * (local / 40)
        shake = (1, -1) if local % 3 == 0 else ((-1, 1) if local % 3 == 1 else (0, 0))
    elif scene == "laugh":
        zoom = 1.047 + 0.008 * np.sin(local * 0.5)       # «дыхание» камеры
        cx = 0.54
        shake = (0, -1) if local % 6 == 0 else (0, 0)    # плечи трясутся от смеха
    else:  # win: панч-наезд и лёгкая вибрация триумфа
        ease = min(1.0, local / 8)
        zoom = 1.05 + 0.05 * (1 - (1 - ease) ** 3)
        cx = 0.56
        shake = (1, 0) if local % 4 == 0 else (0, 0)

    frame = base_frame(zoom, cx, 0.52, shake, src=SOURCES[scene])
    pulse = 0.34 + 0.10 * np.sin(f * 0.45) + (0.06 if f % 12 < 2 else 0)
    frame = screen(frame, glow_layer(pulse))
    frame = dust.draw(frame, f)
    if scene == "win":
        frame = sparks.draw(frame, local)
    frame = vignette(frame)
    frame = grain(frame, f)
    frame = compose(frame, t_title, t_sub, t_handle, t_bars, t_cta, t_free)
    frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)

print("frames:", FRAMES, "size:", W, "x", H)
