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
FONT_ITALIC = BRAND / "PlayfairDisplay-Italic.ttf"

W, H = 960, 540
FPS = 24
FRAMES = 204
TITLE = "ДЕД РЕДАЧИТ"
SUBTITLE = "перепечатаем начисто · сохраним главное"
HANDLE = "@ded_bilet_bot"

GOLD_TOP = (255, 248, 230)
GOLD_MID = (245, 215, 150)
GOLD_DEEP = (195, 145, 80)
GOLD_EDGE = (242, 208, 140)
CREAM = (252, 246, 235)
PLATINUM = (245, 240, 248)
BAR = (6, 3, 8)

BAR_B = 132          # нижний литер-бар
BAR_T = 42           # верхний бар
TITLE_IN = (20, 40)
SUB_IN = (46, 62)
HANDLE_IN = (58, 72)
FREE_LINE = "2 БИЛЕТА В ДЕНЬ БЕСПЛАТНО · КАЖДОМУ"
PUSHKIN_LINE = "АЛЕКСАНДР СЕРГЕЕВИЧ БЫЛ БЫ ДОВОЛЕН"
CTA_LINE = "ПАЧКИ И БЕЗЛИМИТ · 750 РУБЛЕЙ ВМЕСТО 1500 · /subscribe"
BARS_IN = 18          # бары задвигаются за 18 кадров


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
        self.speed = rng.uniform(0.0025, 0.0075, count)
        self.size = rng.uniform(1.2, 3.2, count)
        self.phase = rng.uniform(0, 2 * np.pi, count)
        self.count = count

    def draw(self, img, frame):
        overlay = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for i in range(self.count):
            y = (self.y[i] - self.speed[i] * frame) % 0.95
            x = self.x[i] + 0.004 * np.sin(self.phase[i] + frame * 0.16)
            twinkle = 0.55 + 0.45 * np.sin(self.phase[i] * 3 + frame * 0.38)
            r = self.size[i]
            c = (int(255 * twinkle), int(230 * twinkle), int(210 * twinkle))
            px, py = int(x * W), int(y * H)
            d.ellipse([px - r, py - r, px + r, py + r], fill=c)
        return screen(img, overlay.filter(ImageFilter.GaussianBlur(0.8)))


class Sparks:
    """Золотые благородные искры вокруг поднятого билета в финальной сцене."""

    def __init__(self, count=50, seed=42):
        rng = np.random.default_rng(seed)
        self.angle = rng.uniform(0, 2 * np.pi, count)
        self.speed = rng.uniform(0.012, 0.045, count)
        self.size = rng.uniform(2.0, 4.2, count)
        self.phase = rng.uniform(0, 2 * np.pi, count)
        self.count = count

    def draw(self, img, local):
        overlay = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(overlay)
        ox, oy = int(0.44 * W), int(0.165 * H)
        for i in range(self.count):
            t = (local / 20.0 + self.phase[i]) % 1.5
            if t > 1.0:
                continue
            dist = self.speed[i] * t * W
            x = ox + np.cos(self.angle[i]) * dist
            y = oy + np.sin(self.angle[i]) * dist + 0.006 * t * t * W
            fade = max(0.0, 1.0 - t) * (0.6 + 0.4 * np.sin(self.phase[i] * 5 + t * 9))
            r = self.size[i] * (1.0 - 0.3 * t)
            c = (int(255 * fade), int(225 * fade), int(130 * fade))
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        glow = overlay.filter(ImageFilter.GaussianBlur(1.0))
        return ImageChops.screen(img, glow)


def flash_overlay(img, local):
    """Мягкая тёплая янтарная вспышка между сценами (до 6 кадров при 24 fps)."""
    alphas = (145, 110, 70, 35, 14, 4)
    alpha = alphas[min(local, len(alphas) - 1)]
    return Image.blend(img, Image.new("RGB", (W, H), (255, 238, 210)), alpha / 255)



def _font(path, size):
    return ImageFont.truetype(str(path), size)


def _gold_gradient(y0, y1):
    """Вертикальный золотой градиент высотой y1-y0 (RGB, ширина W)."""
    stops = [(0.0, GOLD_TOP), (0.40, GOLD_MID), (0.75, GOLD_DEEP), (1.0, GOLD_EDGE)]
    ys = np.linspace(0.0, 1.0, max(1, y1 - y0))
    channels = []
    for i in range(3):
        xs = [s[0] for s in stops]
        vals = [s[1][i] for s in stops]
        channels.append(np.interp(ys, xs, vals))
    row = np.stack(channels, axis=1).astype(np.uint8)          # (h, 3)
    return Image.fromarray(np.repeat(row[:, None, :], W, axis=1), "RGB")


def overlay(title_a, sub_a, handle_a, bars_e, cta_a=0.0, free_a=0.0, pushkin_a=0.0, f=None):
    """Кинематографичный letterbox: глубокие бархатные бары с двойной золотой
    кромкой и центральным ромбом; титры — внутри нижнего бара с динамическим
    световым бликом (shimmer) и каллиграфическим подзаголовком; верхний бар
    раскрывает сюжет из трёх актов (бесплатные билеты -> улыбка Пушкина -> абонемент)."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if bars_e <= 0:
        return ov
    d = ImageDraw.Draw(ov)
    bar_b = int(round(BAR_B * bars_e))
    bar_t = int(round(BAR_T * bars_e))
    top = H - bar_b

    # Базовая заливка бар
    d.rectangle([0, top, W, H], fill=(*BAR, 255))
    d.rectangle([0, 0, W, bar_t], fill=(*BAR, 255))

    line_a = int(220 * bars_e)
    mid_x = W // 2
    r_bar = 2.8

    # 1. Верхний бар: двойная золотая кромка снизу с центральным ромбом
    d.line([0, bar_t - 1, W, bar_t - 1], fill=(212, 170, 96, line_a), width=1)
    d.line([48, bar_t - 3, W - 48, bar_t - 3], fill=(180, 140, 75, int(line_a * 0.7)), width=1)
    d.polygon([(mid_x, bar_t - 3 - r_bar), (mid_x + r_bar, bar_t - 3),
               (mid_x, bar_t - 3 + r_bar), (mid_x - r_bar, bar_t - 3)],
              fill=(*GOLD_EDGE, line_a))

    # 2. Нижний бар: двойная золотая кромка сверху с центральным ромбом
    d.line([0, top, W, top], fill=(212, 170, 96, line_a), width=1)
    d.line([48, top + 3, W - 48, top + 3], fill=(180, 140, 75, int(line_a * 0.7)), width=1)
    d.polygon([(mid_x, top + 3 - r_bar), (mid_x + r_bar, top + 3),
               (mid_x, top + 3 + r_bar), (mid_x - r_bar, top + 3)],
              fill=(*GOLD_EDGE, line_a))

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    # Текст верхнего бара: 3 драматургических акта
    top_font = _font(FONT_MAIN, 15)
    fy = (bar_t - 4 - 18) // 2
    cy = fy + 10

    # Акт 1: "2 БИЛЕТА В ДЕНЬ · БЕСПЛАТНО"
    if free_a > 0:
        fa = int(240 * free_a)
        gap = 2
        fw = sum(probe.textlength(ch, font=top_font) + gap for ch in FREE_LINE) - gap
        fx = (W - fw) / 2
        diam_r = 2.5
        d.polygon([(fx - 16, cy - diam_r), (fx - 16 + diam_r, cy),
                   (fx - 16, cy + diam_r), (fx - 16 - diam_r, cy)], fill=(*GOLD_EDGE, fa))
        d.polygon([(fx + fw + 16, cy - diam_r), (fx + fw + 16 + diam_r, cy),
                   (fx + fw + 16, cy + diam_r), (fx + fw + 16 - diam_r, cy)], fill=(*GOLD_EDGE, fa))
        for ch in FREE_LINE:
            d.text((fx, fy), ch, font=top_font, fill=(*GOLD_MID, fa))
            fx += probe.textlength(ch, font=top_font) + gap

    # Акт 2: "АЛЕКСАНДР СЕРГЕЕВИЧ БЫЛ БЫ ДОВОЛЕН" с пульсирующими ромбами и золотым лучом
    if pushkin_a > 0:
        pa = int(250 * pushkin_a)
        gap = 2
        pw = sum(probe.textlength(ch, font=top_font) + gap for ch in PUSHKIN_LINE) - gap
        px = (W - pw) / 2
        pulse = np.sin(np.pi * min(1.0, max(0.0, (f - 72) / 20.0))) if (f is not None and 72 <= f <= 92) else 0.0
        diam_r = 2.5 + 0.8 * pulse
        col_diam = (int(GOLD_EDGE[0] * (1 - pulse) + GOLD_TOP[0] * pulse),
                    int(GOLD_EDGE[1] * (1 - pulse) + GOLD_TOP[1] * pulse),
                    int(GOLD_EDGE[2] * (1 - pulse) + GOLD_TOP[2] * pulse), pa)
        d.polygon([(px - 18, cy - diam_r), (px - 18 + diam_r, cy),
                   (px - 18, cy + diam_r), (px - 18 - diam_r, cy)], fill=col_diam)
        d.polygon([(px + pw + 18, cy - diam_r), (px + pw + 18 + diam_r, cy),
                   (px + pw + 18, cy + diam_r), (px + pw + 18 - diam_r, cy)], fill=col_diam)

        # Слой текста с бегущим лучом
        p_mask = Image.new("L", (W, H), 0)
        pmd = ImageDraw.Draw(p_mask)
        cur_x = px
        for ch in PUSHKIN_LINE:
            pmd.text((cur_x, fy), ch, font=top_font, fill=255)
            cur_x += probe.textlength(ch, font=top_font) + gap

        p_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pld = ImageDraw.Draw(p_layer)
        cur_x = px
        for ch in PUSHKIN_LINE:
            pld.text((cur_x, fy), ch, font=top_font, fill=(*GOLD_MID, pa))
            cur_x += probe.textlength(ch, font=top_font) + gap

        if f is not None and 74 <= f <= 96:
            g_pos = (f - 74) / 22.0
            gx = px + g_pos * pw
            gm = Image.new("L", (W, H), 0)
            gd = ImageDraw.Draw(gm)
            gd.polygon([(gx - 20, 0), (gx + 20, 0), (gx + 35, bar_t), (gx - 5, bar_t)], fill=255)
            gm = gm.filter(ImageFilter.GaussianBlur(8))
            gleam_text = ImageChops.multiply(p_mask, gm)
            gleam_fill = Image.new("RGBA", (W, H), (*GOLD_TOP, pa))
            p_layer.paste(gleam_fill, (0, 0), gleam_text)

        ov = Image.alpha_composite(ov, p_layer)

    # Акт 3: "ПАЧКИ И БЕЗЛИМИТ · 750 ЗВЁЗД ВМЕСТО 1500 · /subscribe"
    if cta_a > 0:
        ca = int(255 * cta_a)
        gap = 2
        cw = sum(probe.textlength(ch, font=top_font) + gap for ch in CTA_LINE) - gap
        cx = (W - cw) / 2
        diam_r = 2.5
        d.polygon([(cx - 16, cy - diam_r), (cx - 16 + diam_r, cy),
                   (cx - 16, cy + diam_r), (cx - 16 - diam_r, cy)], fill=(*GOLD_EDGE, ca))
        d.polygon([(cx + cw + 16, cy - diam_r), (cx + cw + 16 + diam_r, cy),
                   (cx + cw + 16, cy + diam_r), (cx + cw + 16 - diam_r, cy)], fill=(*GOLD_EDGE, ca))

        c_mask = Image.new("L", (W, H), 0)
        c_md = ImageDraw.Draw(c_mask)
        cur_x = cx
        for ch in CTA_LINE:
            c_md.text((cur_x, fy), ch, font=top_font, fill=255)
            cur_x += probe.textlength(ch, font=top_font) + gap
        c_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        c_fill = Image.new("RGBA", (W, H), (*GOLD_MID, ca))
        c_layer.paste(c_fill, (0, 0), c_mask)

        if f is not None and 130 <= f <= 165:
            g_pos = (f - 130) / 35.0
            gx = cx + g_pos * cw
            gm = Image.new("L", (W, H), 0)
            gd = ImageDraw.Draw(gm)
            gd.polygon([(gx - 25, 0), (gx + 25, 0), (gx + 40, bar_t), (gx - 10, bar_t)], fill=255)
            gm = gm.filter(ImageFilter.GaussianBlur(8))
            gleam_text = ImageChops.multiply(c_mask, gm)
            gleam_fill = Image.new("RGBA", (W, H), (*GOLD_TOP, ca))
            c_layer.paste(gleam_fill, (0, 0), gleam_text)

        ov = Image.alpha_composite(ov, c_layer)

    # Определение фазы светового блика (shimmer) по номеру кадра:
    gleam_pos = None
    if f is not None:
        if 24 <= f <= 52:
            gleam_pos = (f - 24) / 28.0
        elif 124 <= f <= 152:
            gleam_pos = (f - 124) / 28.0

    # Заголовок: ДЕД РЕДАЧИТ (золотой градиент, фаска, глубокая тень, бегущий блик)
    if title_a > 0:
        title_font = _font(FONT_MAIN, 48)
        tracking = 12
        tw = sum(probe.textlength(ch, font=title_font) + tracking for ch in TITLE) - tracking
        tx0 = (W - tw) / 2

        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        x = tx0
        for ch in TITLE:
            md.text((x, top + 14), ch, font=title_font, fill=255)
            x += probe.textlength(ch, font=title_font) + tracking

        # Мягкая глубокая тень
        shadow = ImageChops.offset(mask.filter(ImageFilter.GaussianBlur(5)), 2, 4)
        shadow = shadow.point(lambda v: int(v * 0.55 * title_a))
        sh_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sh_layer.paste(Image.new("RGBA", (W, H), (0, 0, 0, 255)), (0, 0), shadow)
        ov = Image.alpha_composite(ov, sh_layer)

        # Золотой градиент букв
        ink = mask.getbbox()
        if ink:
            grad = _gold_gradient(ink[1], ink[3]).crop((ink[0], 0, ink[2], ink[3] - ink[1])).convert("RGBA")
            gold = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            gold.paste(grad, (ink[0], ink[1]), mask.crop(ink))

            # Световой скос (bevel) по верхней грани букв
            bevel = ImageChops.subtract(mask, ImageChops.offset(mask, 0, 1)).point(lambda v: int(v * 0.65))
            gold.paste(Image.new("RGBA", (W, H), (255, 252, 240, 255)), (0, 0), bevel)

            # Изящный золотой контур (hairline gold rim) для эффекта металлического оттиска
            rim = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask).point(lambda v: int(v * 0.50))
            gold.paste(Image.new("RGBA", (W, H), (*GOLD_EDGE, 255)), (0, 0), rim)

            # Кинематографичный бегущий световой блик (shimmer)
            if gleam_pos is not None and 0.0 <= gleam_pos <= 1.0:
                gx = tx0 + gleam_pos * tw
                gleam_mask = Image.new("L", (W, H), 0)
                gd = ImageDraw.Draw(gleam_mask)
                beam_w = 42
                gd.polygon([
                    (gx - beam_w - 20, top), (gx + beam_w - 20, top),
                    (gx + beam_w + 20, top + BAR_B), (gx - beam_w + 20, top + BAR_B)
                ], fill=255)
                gleam_mask = gleam_mask.filter(ImageFilter.GaussianBlur(16))
                gleam_on_text = ImageChops.multiply(mask, gleam_mask)
                gold.paste(Image.new("RGBA", (W, H), (255, 252, 238, 255)), (0, 0), gleam_on_text)

            gold.putalpha(mask.point(lambda v: int(v * title_a)))
            ov = Image.alpha_composite(ov, gold)

    # Подзаголовок: перепечатаем начисто · сохраним главное в PlayfairDisplay-Italic
    if sub_a > 0:
        sa = int(255 * sub_a)
        d = ImageDraw.Draw(ov)
        sub_font = _font(FONT_ITALIC, 23)
        gap = 2
        sw = sum(probe.textlength(ch, font=sub_font) + gap for ch in SUBTITLE) - gap
        sx = (W - sw) / 2
        sy = top + 74
        for ch in SUBTITLE:
            d.text((sx + 1, sy + 1.5), ch, font=sub_font, fill=(0, 0, 0, int(sa * 0.90)))
            fill_c = GOLD_MID if ch == "·" else CREAM
            d.text((sx, sy), ch, font=sub_font, fill=(*fill_c, sa))
            sx += probe.textlength(ch, font=sub_font) + gap

    # Канал: @biletgetbot с тонкими гравюрными крыльями
    if handle_a > 0:
        ha = int(255 * handle_a)
        d = ImageDraw.Draw(ov)
        small_font = _font(FONT_MAIN, 19)
        hw = probe.textlength(HANDLE, font=small_font)
        hx = (W - hw) / 2
        hy = top + 103
        for gdx, gdy in ((1, 1), (-1, 1)):
            d.text((hx + gdx, hy + gdy), HANDLE, font=small_font, fill=(0, 0, 0, int(ha * 0.85)))
        d.text((hx, hy), HANDLE, font=small_font, fill=(*PLATINUM, ha))

        cy = hy + 10
        r_wing = 2.5
        for side in (-1, 1):
            inner = hx - 16 if side < 0 else hx + hw + 16
            outer = inner + side * 85
            d.line([outer, cy, inner, cy], fill=(198, 172, 182, int(ha * 0.85)), width=1)
            mid = (outer + inner) / 2
            d.polygon([(mid, cy - r_wing), (mid + r_wing, cy), (mid, cy + r_wing), (mid - r_wing, cy)],
                      fill=(*GOLD_EDGE, ha))
            d.ellipse([outer - 1.5, cy - 1.5, outer + 1.5, cy + 1.5], fill=(*GOLD_MID, int(ha * 0.9)))

    return ov


def compose(img, title_a, sub_a, handle_a, bars_e, cta_a=0.0, free_a=0.0, pushkin_a=0.0, f=None):
    o = overlay(title_a, sub_a, handle_a, bars_e, cta_a, free_a, pushkin_a, f=f)
    return Image.alpha_composite(img.convert("RGBA"), o).convert("RGB")


SRC = Image.open(BASE).convert("RGB")
SOURCES = {"type": SRC,
           "laugh": Image.open(BRAND / "key_laugh.jpg").convert("RGB"),
           "win": Image.open(BRAND / "key_win.jpg").convert("RGB")}

# --- статичная обложка -------------------------------------------------------
poster = base_frame(1.04, 0.54, 0.52, src=SRC)
poster = screen(poster, glow_layer(0.35))
poster = Dust(seed=11).draw(poster, 8)
poster = vignette(poster)
poster = grain(poster, 3)
poster = compose(poster, 1, 1, 1, 1, cta_a=0.0, free_a=1.0, pushkin_a=0.0, f=50)
poster.save(BRAND / "welcome.jpg", quality=90)

# --- аватар: триумфальный кадр с поднятым билетом (идеально для кружка Telegram) -
portrait = cover_crop(SOURCES["win"], 800, 800, cx=0.56, cy=0.36, zoom=1.20)
portrait = ImageEnhance.Color(portrait).enhance(1.06)
portrait = ImageEnhance.Sharpness(portrait).enhance(1.15)
portrait.save(BRAND / "ded.jpg", quality=92)

# --- миниатюра видео: точные 16:9 (320x180) для Telegram без скачков ----------
thumb = poster.resize((320, 180), Image.LANCZOS)
thumb.save(BRAND / "thumb.jpg", quality=88)

# --- кадры видео -------------------------------------------------------------
def scene_of(f):
    """(ключ сцены, локальный номер кадра) по глобальному кадру (24 fps, 204 кадра / 8.5 сек)."""
    if f <= 64:
        return "type", f
    if f <= 69:
        return "flash1", f - 65          # мягкий переход в сцену 2
    if f <= 106:
        return "laugh", f - 70
    if f <= 111:
        return "flash2", f - 107         # мягкий переход в сцену 3
    if f <= 197:
        return "win", f - 112            # триумф с билетом и спарками
    return "flash3", f - 198             # бесшовный возврат в сцену 1 (6 кадров)


def get_top_bar_alphas(f):
    """Чистые бесконфликтные прозрачности для 3 актов в верхнем баре (без наложения текста)."""
    if f <= 64:
        return 1.0, 0.0, 0.0
    if f <= 68:
        return smooth(1.0 - (f - 64) / 4.0), 0.0, 0.0
    if f <= 106:
        tp = smooth((f - 69) / 6.0) if f <= 75 else 1.0
        return 0.0, tp, 0.0
    if f <= 112:
        tp = smooth(1.0 - (f - 107) / 5.0)
        return 0.0, tp, 0.0
    if f <= 195:
        tc = smooth((f - 114) / 8.0) if f <= 122 else 1.0
        return 0.0, 0.0, tc
    if f <= 198:
        tc = smooth(1.0 - (f - 195) / 3.0)
        return 0.0, 0.0, tc
    # f in 199..203: возврат в free_line к первому кадру
    tf = smooth((f - 198) / 5.0)
    return tf, 0.0, 0.0


tmp = HERE / "work" / "frames"
tmp.mkdir(parents=True, exist_ok=True)
dust = Dust()
sparks = Sparks()

print(f"Rendering {FRAMES} frames at {FPS} FPS...")
for f in range(FRAMES):
    scene, local = scene_of(f)

    # Постоянный стабильный letterbox и золотые титры:
    t_bars = 1.0
    t_title = 1.0
    t_sub = 1.0
    t_handle = 1.0

    t_free, t_pushkin, t_cta = get_top_bar_alphas(f)

    if scene == "flash1":
        t = (local + 1) / 5.0
        f_from = base_frame(1.045, 0.56, 0.52, src=SOURCES["type"])
        f_to = base_frame(1.047, 0.54, 0.52, src=SOURCES["laugh"])
        frame = Image.blend(f_from, f_to, t)
        frame = flash_overlay(frame, local)
        frame = vignette(frame)
        frame = grain(frame, f)
        frame = compose(frame, t_title, t_sub, t_handle, t_bars, t_cta, t_free, t_pushkin, f=f)
        frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)
        continue

    if scene == "flash2":
        t = (local + 1) / 5.0
        f_from = base_frame(1.047, 0.54, 0.52, src=SOURCES["laugh"])
        f_to = base_frame(1.05, 0.56, 0.48, src=SOURCES["win"])
        frame = Image.blend(f_from, f_to, t)
        frame = flash_overlay(frame, local)
        frame = vignette(frame)
        frame = grain(frame, f)
        frame = compose(frame, t_title, t_sub, t_handle, t_bars, t_cta, t_free, t_pushkin, f=f)
        frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)
        continue

    if scene == "flash3":
        # 6 кадров тёплой янтарной вспышки без двоения: переключение на сцену 1 на пике света
        is_type = local >= 3
        flash_alphas = (60, 160, 240, 240, 140, 45)
        src_img = SOURCES["type"] if is_type else SOURCES["win"]
        zoom = 1.0 if is_type else 1.10
        cx = 0.54 if is_type else 0.56
        cy = 0.52 if is_type else 0.48
        frame = base_frame(zoom, cx, cy, src=src_img)
        alpha = flash_alphas[min(local, len(flash_alphas) - 1)]
        frame = Image.blend(frame, Image.new("RGB", (W, H), (255, 238, 210)), alpha / 255.0)
        frame = vignette(frame)
        frame = grain(frame, f)
        frame = compose(frame, t_title, t_sub, t_handle, t_bars, t_cta, t_free, t_pushkin, f=f)
        frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)
        continue

    if scene == "type":
        zoom = 1.0 + 0.045 * (local / 65)
        cx, cy = 0.54 + 0.02 * (local / 65), 0.52
        shake = (1, -1) if local % 5 == 0 else ((-1, 1) if local % 5 == 2 else (0, 0))
    elif scene == "laugh":
        zoom = 1.047 + 0.008 * np.sin(local * 0.35)
        cx, cy = 0.54, 0.52
        shake = (0, -1) if local % 8 == 0 else (0, 0)
    else:  # win
        ease = min(1.0, local / 14)
        zoom = 1.05 + 0.05 * (1 - (1 - ease) ** 3)
        # Органичное живое покачивание билета в руках под светом лампы
        wobble = 0.0025 * np.sin(local * 0.22) if local > 14 else 0.0
        cx, cy = 0.56, 0.48 + wobble
        shake = (1 if local % 14 < 3 else (0 if local % 14 < 10 else -1), 0)

    frame = base_frame(zoom, cx, cy, shake, src=SOURCES[scene])
    pulse = 0.34 + 0.10 * np.sin(f * 0.30) + (0.06 if f % 18 < 3 else 0)
    frame = screen(frame, glow_layer(pulse))
    frame = dust.draw(frame, f)
    if scene == "win":
        frame = sparks.draw(frame, local)
    frame = vignette(frame)
    frame = grain(frame, f)
    frame = compose(frame, t_title, t_sub, t_handle, t_bars, t_cta, t_free, t_pushkin, f=f)
    frame.save(tmp / f"frame_{f:03d}.jpg", quality=92)

print(f"Rendered {FRAMES} frames ({W}x{H}) at {FPS} FPS.")

# --- Генерация атмосферного звукового оформления ----------------------------
import wave

sr = 44100
duration = FRAMES / FPS
total_samples = int(sr * duration)
audio = np.zeros(total_samples, dtype=float)

# 1. Стук клавиш пишущей машинки (сцена 1: 0.3..2.6 сек)
np.random.seed(42)
click_times = [0.35, 0.52, 0.68, 0.85, 0.98, 1.15, 1.32, 1.50, 1.66, 1.84, 2.02, 2.18, 2.35, 2.50]
for t in click_times:
    idx = int(t * sr)
    n_len = int(0.02 * sr)
    noise = np.random.normal(0, 0.35, n_len) * np.exp(-np.linspace(0, 14, n_len))
    t_arr = np.linspace(0, 0.04, int(0.04 * sr))
    ring = 0.16 * np.sin(2 * np.pi * 1250 * t_arr) * np.exp(-t_arr * 80)
    ring += 0.10 * np.sin(2 * np.pi * 820 * t_arr) * np.exp(-t_arr * 60)
    audio[idx:idx + n_len] += noise
    audio[idx:idx + len(ring)] += ring

# 2. Колокольчик возврата каретки (дзинь!) на переходе (t = 2.7 сек)
bell_idx = int(2.70 * sr)
t_bell = np.linspace(0, 1.4, int(1.4 * sr))
bell = (0.35 * np.sin(2 * np.pi * 2489 * t_bell) * np.exp(-t_bell * 4.5) +
        0.18 * np.sin(2 * np.pi * 4978 * t_bell) * np.exp(-t_bell * 8.0) +
        0.10 * np.sin(2 * np.pi * 1244 * t_bell) * np.exp(-t_bell * 3.0))
audio[bell_idx:bell_idx + len(bell)] += bell

# Скольжение каретки (t = 2.75..3.05 сек)
t_slide = np.linspace(0, 0.3, int(0.3 * sr))
slide = np.random.normal(0, 0.08, len(t_slide)) * np.sin(np.pi * t_slide / 0.3)
audio[bell_idx + int(0.05 * sr):bell_idx + int(0.05 * sr) + len(slide)] += slide

# 3. Тёплый торжественный аккорд победы (сцена 3: t = 4.6 сек)
chord_idx = int(4.65 * sr)
t_chord = np.linspace(0, 2.8, int(2.8 * sr))
notes = [174.61, 220.0, 261.63, 329.63, 440.0, 523.25]
weights = [0.25, 0.22, 0.20, 0.18, 0.14, 0.10]
chord = np.zeros_like(t_chord)
for freq, w in zip(notes, weights):
    note = (np.sin(2 * np.pi * freq * t_chord) +
            0.4 * np.sin(2 * np.pi * freq * 2 * t_chord) +
            0.15 * np.sin(2 * np.pi * freq * 3 * t_chord))
    env = np.minimum(t_chord / 0.06, 1.0) * np.exp(-t_chord * 0.9)
    chord += w * note * env

# Волшебный перезвон золотых искр над билетом
sparkle = np.zeros_like(t_chord)
for f_sp, t_offset in [(1568, 0.0), (1760, 0.12), (2093, 0.24), (2637, 0.36)]:
    sp_idx = int(t_offset * sr)
    if sp_idx < len(t_chord):
        sp_t = t_chord[sp_idx:]
        sparkle[sp_idx:] += 0.09 * np.sin(2 * np.pi * f_sp * sp_t) * np.exp(-(sp_t - t_offset) * 4.0)

audio[chord_idx:chord_idx + len(chord)] += chord * 0.52
audio[chord_idx:chord_idx + len(sparkle)] += sparkle * 0.40

# Нормализация громкости
audio = audio / (np.max(np.abs(audio)) + 1e-6) * 0.88

sfx_wav = HERE / "work" / "sfx.wav"
with wave.open(str(sfx_wav), "w") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sr)
    raw = (audio * 32767).astype(np.int16)
    wf.writeframes(raw.tobytes())

# --- Кодирование видео через ffmpeg ------------------------------------------
import shutil
import subprocess

ffmpeg_bin = "/opt/homebrew/bin/ffmpeg"
mp4_out = BRAND / "welcome.mp4"
mp4_sound_out = BRAND / "welcome_with_sound.mp4"
gif_out = BRAND / "welcome.gif"

print("Encoding MP4 video (clean muted for Telegram sendAnimation)...")
cmd_mp4 = [
    ffmpeg_bin, "-y",
    "-framerate", str(FPS),
    "-i", str(tmp / "frame_%03d.jpg"),
    "-c:v", "libx264",
    "-crf", "18",
    "-preset", "slow",
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
    str(mp4_out)
]
subprocess.run(cmd_mp4, check=True)
print(f"Generated: {mp4_out} ({mp4_out.stat().st_size} bytes)")

print("Encoding MP4 video WITH SOUND (for channels, presentations, and stories)...")
cmd_mp4_sound = [
    ffmpeg_bin, "-y",
    "-i", str(mp4_out),
    "-i", str(sfx_wav),
    "-c:v", "copy",
    "-c:a", "aac",
    "-b:a", "192k",
    "-shortest",
    str(mp4_sound_out)
]
subprocess.run(cmd_mp4_sound, check=True)
print(f"Generated: {mp4_sound_out} ({mp4_sound_out.stat().st_size} bytes)")

print("Generating optimized Master GIF (960x540, diff_mode=rectangle)...")
cmd_gif = [
    ffmpeg_bin, "-y",
    "-i", str(mp4_out),
    "-vf", "fps=16,split[s0][s1];[s0]palettegen=max_colors=192:stats_mode=diff[p];[s1][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
    str(gif_out)
]
subprocess.run(cmd_gif, check=True)
print(f"Generated: {gif_out} ({gif_out.stat().st_size} bytes)")

# Compact GIF for mobile auto-play (< 8-10 MB, exact 12 fps divisor)
print("Generating ultra-light Mobile GIF (640x360, 12 fps, auto-play ready)...")
gif_compact_out = BRAND / "welcome_compact.gif"
cmd_gif_compact = [
    ffmpeg_bin, "-y",
    "-i", str(mp4_out),
    "-vf", "scale=640:-1:flags=lanczos,fps=12,split[s0][s1];[s0]palettegen=max_colors=160:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
    str(gif_compact_out)
]
subprocess.run(cmd_gif_compact, check=True)
print(f"Generated: {gif_compact_out} ({gif_compact_out.stat().st_size} bytes)")

# Copy to Downloads for user convenience
downloads_dir = Path.home() / "Downloads"
if downloads_dir.exists():
    shutil.copy2(mp4_out, downloads_dir / "welcome_improved.mp4")
    shutil.copy2(mp4_out, downloads_dir / "welcome.mp4")
    shutil.copy2(mp4_sound_out, downloads_dir / "welcome_with_sound.mp4")
    shutil.copy2(gif_out, downloads_dir / "welcome_improved.gif")
    shutil.copy2(gif_compact_out, downloads_dir / "welcome_compact.gif")
    shutil.copy2(BRAND / "welcome.jpg", downloads_dir / "welcome.jpg")
    shutil.copy2(BRAND / "ded.jpg", downloads_dir / "ded.jpg")
    shutil.copy2(BRAND / "thumb.jpg", downloads_dir / "thumb.jpg")
    print("All files successfully copied to ~/Downloads!")
