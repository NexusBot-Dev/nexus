"""Event-Banner (800×320) aus dem Twitch-Cover einer Kategorie.

Twitch-Cover sind hochkant (3:4), Discord-Event-Banner quer (2,5:1). Statt zu strecken: das Cover weichgezeichnet
und abgedunkelt als Hintergrund, darauf das Cover unverzerrt links, rechts daneben Spielname und Kanal.

Rechnet nur mit Pillow und ohne geteilten Zustand — läuft in asyncio.to_thread.
"""
import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

WIDTH, HEIGHT = 800, 320
FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "BebasNeue-Regular.ttf"
ACCENT = (45, 226, 200)            # Türkis wie auf der Levelcard
COVER_SIZE = (600, 800)            # so groß wird das Cover bei Twitch angefordert
MAX_COVER_PIXELS = 4_000_000       # Schutz gegen übergroße Bilder (Twitch liefert 600×800)

_MARGIN = 16                       # Abstand des Covers zum oberen und unteren Rand
_COVER_X = 40
_TEXT_GAP = 36
_RIGHT_MARGIN = 36
_TITLE_BASELINE = 150
_SUBTITLE_BASELINE = 196

def _cover(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"])
    if image.width * image.height > MAX_COVER_PIXELS:
        raise ValueError(f"Cover zu groß: {image.size}")
    return image.convert("RGB")

def fit_text(text: str, max_width: int, max_size: int, min_size: int) -> tuple[ImageFont.FreeTypeFont, str]:
    """Größte Schrift, in die der Text passt; reicht auch min_size nicht, wird mit „…“ gekürzt."""
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(FONT_PATH), size)   # je Aufruf neu: FreeType-Schriften nicht zwischen Threads teilen
        if draw.textlength(text, font=font) <= max_width:
            return font, text
    font = ImageFont.truetype(str(FONT_PATH), min_size)
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return font, trimmed.rstrip() + "…"

def render_banner(cover_bytes: bytes, title: str, subtitle: str) -> bytes:
    """Das fertige Banner als JPEG. title: Spielname, subtitle: z. B. „RUSIRDA · TWITCH.TV/RUSIRDA“."""
    cover = _cover(cover_bytes)

    background = ImageOps.fit(cover, (WIDTH, HEIGHT), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    background = ImageEnhance.Brightness(background).enhance(0.55)

    cover_h = HEIGHT - 2 * _MARGIN
    cover_w = round(cover.width * cover_h / cover.height)
    front = cover.resize((cover_w, cover_h), Image.LANCZOS)

    shadow = Image.new("L", (cover_w + 40, cover_h + 40), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((20, 20, cover_w + 20, cover_h + 20), radius=14, fill=170)
    background.paste((0, 0, 0), (_COVER_X - 20, _MARGIN - 14), shadow.filter(ImageFilter.GaussianBlur(10)))
    corners = Image.new("L", (cover_w, cover_h), 0)
    ImageDraw.Draw(corners).rounded_rectangle((0, 0, cover_w - 1, cover_h - 1), radius=12, fill=255)
    background.paste(front, (_COVER_X, _MARGIN), corners)

    text_x = _COVER_X + cover_w + _TEXT_GAP
    text_width = WIDTH - text_x - _RIGHT_MARGIN
    draw = ImageDraw.Draw(background)
    title_font, title = fit_text(title.upper(), text_width, 58, 30)
    sub_font, subtitle = fit_text(subtitle.upper(), text_width, 30, 20)
    # An der Grundlinie ausgerichtet: kleiner gesetzte lange Namen stehen so genauso nah über der zweiten Zeile
    draw.text((text_x, _TITLE_BASELINE), title, font=title_font, fill=(255, 255, 255), anchor="ls")
    draw.text((text_x, _SUBTITLE_BASELINE), subtitle, font=sub_font, fill=ACCENT, anchor="ls")

    out = io.BytesIO()
    background.save(out, "JPEG", quality=90)
    return out.getvalue()
