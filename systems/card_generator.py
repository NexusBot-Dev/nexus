"""
Level-Card-Generator für /rank.
Zeichnet Avatar, Username, XP-Balken, Rang und Level auf ein statisches
Circuit-Template (levelcard.webp, 1200x400) und liefert die Karte als WebP.

Gemessene Koordinaten (aus dem Template extrahiert):
- Avatar-Kreis:  Zentrum (214, 199), Radius 176
- Box 1 (Name):  X 423-1064, Y 38-146
- Box 2 (Balken): X 424-1174, Y 200-254
- Box 3 (Info):  X 424-1174, Y 308-362
"""
import re
import io
import threading
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pathlib import Path

# Pfade relativ zu DIESER Datei, nicht zum Arbeitsverzeichnis des Bot-Prozesses --
# funktioniert unabhängig davon, von wo aus der Bot gestartet wird oder welches
# Cog gerade importiert. Annahme: assets/ liegt im Projekt-Root, dieses Modul
# liegt eine Ebene tiefer (z.B. systems/card_generator.py) -> .parent.parent
BASE_DIR      = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = BASE_DIR / "assets" / "levelcard.webp"
FONT_PATH     = BASE_DIR / "assets" / "BebasNeue-Regular.ttf"

CIRCLE_CENTER = (214, 199)
CIRCLE_RADIUS = 176

BOX_NAME   = (423, 38, 1064, 146)
BOX_BAR    = (424, 200, 1174, 254)
BOX_INFO   = (424, 308, 1174, 362)

ACCENT_COLOR = (45, 226, 200)       # Cyan/Türkis, aus dem Ring/Template abgeleitet
TRACK_COLOR  = (20, 45, 45)         # gedimmte Balken-Basis
BAR_PADDING  = 12                   # Innenabstand des Balkens in Box 2
CARD_QUALITY = 90                   # WebP: ~50 KB statt ~360 KB als PNG — schneller hochgeladen

AVATAR_SIZE = 512   # so groß lädt /rank den Avatar (auf der Karte: 352 px Durchmesser)


@lru_cache(maxsize=1)
def _template() -> Image.Image:
    """Template einmal von der Platte lesen — jede Karte arbeitet auf einer Kopie (nur gelesen, darum teilbar)."""
    return Image.open(TEMPLATE_PATH).convert("RGBA")


@lru_cache(maxsize=1)
def _circle_mask(size: int) -> Image.Image:
    """Kreismaske mit glatter Kante: vierfach groß gezeichnet, dann verkleinert."""
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


_fonts = threading.local()   # FreeType-Schriften nicht zwischen Threads teilen (/rank läuft in to_thread)


def _font(size: int) -> ImageFont.FreeTypeFont:
    cache = _fonts.__dict__.setdefault("by_size", {})
    if size not in cache:
        cache[size] = ImageFont.truetype(str(FONT_PATH), size)
    return cache[size]


def _circular_avatar(avatar_bytes: bytes, size: int) -> Image.Image:
    """Schneidet ein Avatar-Bild kreisrund zu. Nur die Maske wird hochauflösend gerechnet, nicht der Avatar.
    Nicht quadratische Bilder werden mittig zugeschnitten; bei animierten zählt das erste Bild."""
    avatar = Image.open(io.BytesIO(avatar_bytes))
    avatar = ImageOps.fit(avatar.convert("RGBA"), (size, size), Image.LANCZOS)
    avatar.putalpha(_circle_mask(size))
    return avatar

def _draw_progress_bar(draw: ImageDraw.ImageDraw, box: tuple, progress: float):
    """Zeichnet Track + gefüllten Balken mit abgerundeten Enden. progress: 0.0-1.0"""
    x1, y1, x2, y2 = box
    x1, y1, x2, y2 = x1 + BAR_PADDING, y1 + BAR_PADDING, x2 - BAR_PADDING, y2 - BAR_PADDING
    radius = (y2 - y1) // 2

    # Track (Basis, gedimmt)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=TRACK_COLOR)

    # Fill (tatsächlicher Fortschritt)
    fill_width = int((x2 - x1) * max(0.0, min(1.0, progress)))
    if fill_width > radius:  # zu kurze Balken sonst optisch kaputt (abgerundete Enden > Breite)
        draw.rounded_rectangle((x1, y1, x1 + fill_width, y2), radius=radius, fill=ACCENT_COLOR)

def _sanitize_username(name: str) -> str:
    """Entfernt Zeilenumbrüche, Tabs und unsichtbare Steuerzeichen."""
    # Zeilenumbrüche/Tabs durch Leerzeichen ersetzen
    clean = re.sub(r"[\r\n\t]+", " ", name)
    # Entfernt Zero-Width-Spaces und Richtungs-Steuerzeichen (RTL/LTR-Overrides)
    clean = re.sub(r"[\u200B-\u200D\uFEFF\u202A-\u202E]", "", clean)
    clean = clean.strip()
    return clean or "USER"

def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_size: int, min_size: int = 14) -> tuple[ImageFont.FreeTypeFont, str]:
    """Reduziert Schriftgröße und schneidet bei Bedarf mit Ellipsis ab."""
    size = max_size
    while size >= min_size:
        font = _font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font, text
        size -= 2

    # Wenn selbst min_size zu groß ist: Text kürzen bis er passt
    font = _font(min_size)
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    
    return font, (trimmed + "…" if trimmed else text)


def generate_rank_card(
    username: str,
    avatar_bytes: bytes,
    xp: int,
    xp_for_next: int,
    rank: int,
    level: int,
) -> io.BytesIO:
    """Erzeugt die fertige Rank-Card als WebP-Bytes (für discord.File)."""
    card = _template().copy()
    draw = ImageDraw.Draw(card)

    # Avatar einsetzen
    diameter = CIRCLE_RADIUS * 2
    avatar = _circular_avatar(avatar_bytes, diameter)
    paste_pos = (CIRCLE_CENTER[0] - CIRCLE_RADIUS, CIRCLE_CENTER[1] - CIRCLE_RADIUS)
    card.paste(avatar, paste_pos, avatar)

# Username säubern & vorbereiten
    clean_name = _sanitize_username(username).upper()
    max_w = BOX_NAME[2] - BOX_NAME[0] - 40
    
    name_font, display_text = _fit_text(draw, clean_name, max_w, 64)
    name_bbox = draw.textbbox((0, 0), display_text, font=name_font)
    name_h = name_bbox[3] - name_bbox[1]
    name_y = BOX_NAME[1] + ((BOX_NAME[3] - BOX_NAME[1]) - name_h) // 2 - name_bbox[1]
    draw.text((BOX_NAME[0] + 24, name_y), display_text, font=name_font, fill=(255, 255, 255))

    # Fortschrittsbalken (Box 2)
    progress = xp / xp_for_next if xp_for_next > 0 else 1.0
    _draw_progress_bar(draw, BOX_BAR, progress)

    # Info-Zeile (Box 3): XP / RANK / LEVEL, gleichmäßig verteilt
    info_font = _font(32)
    info_y = BOX_INFO[1] + ((BOX_INFO[3] - BOX_INFO[1]) - 32) // 2 - 4

    xp_text    = f"{xp:,} / {xp_for_next:,} XP".replace(",", ".")
    rank_text  = f"RANK #{rank}"
    level_text = f"LEVEL {level}"

    segment_width = (BOX_INFO[2] - BOX_INFO[0]) // 3
    for i, text in enumerate((xp_text, rank_text, level_text)):
        seg_x = BOX_INFO[0] + i * segment_width
        text_w = draw.textlength(text, font=info_font)
        draw_x = seg_x + (segment_width - text_w) // 2
        draw.text((draw_x, info_y), text, font=info_font, fill=ACCENT_COLOR)

    buffer = io.BytesIO()
    card.convert("RGB").save(buffer, format="WEBP", quality=CARD_QUALITY)
    buffer.seek(0)
    return buffer