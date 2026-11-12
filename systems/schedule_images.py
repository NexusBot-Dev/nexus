"""Eigene Event-Bilder (Premium): prüfen, auf 1600×640 zuschneiden und als JPEG neu erzeugen.

Jedes hochgeladene Bild — aus dem Dashboard wie aus Discord — läuft hier durch:
  - nur PNG, JPEG und WebP werden überhaupt geöffnet (exotische Formate waren die häufigsten Pillow-Lücken),
  - Dateigröße und Pixelzahl werden geprüft, bevor das Bild ausgepackt wird (Schutz gegen „Bildbomben“),
  - gespeichert wird nur das frisch erzeugte JPEG, nie die Originaldatei (keine Metadaten, kein versteckter Inhalt).

JPEG, weil Discord für Event-Bilder nur JPG, GIF und PNG annimmt (Doku: Image Data).
Doppelte Größe des Spiel-Banners (800×320 ist nur Discords Mindestgröße): Discord zeigt Event-Bilder auf
hochauflösenden Bildschirmen größer an, 800×320 wirkt dort unscharf.
Rechnet in asyncio.to_thread, höchstens zwei Bilder gleichzeitig.
"""
import asyncio
import hashlib
import io
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 25_000_000
FORMATS = ["PNG", "JPEG", "WEBP"]
WIDTH, HEIGHT = 1600, 640   # Seitenverhältnis 5:2 wie Discords Event-Bild
QUALITY = 90
BACKGROUND = (17, 18, 20)   # für transparente Bilder (JPEG kennt keine Transparenz)

_slots = asyncio.Semaphore(2)


class ImageRejected(Exception):
    """Kein brauchbares Bild. reason: "too_large" | "too_many_pixels" | "not_an_image"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def process(data: bytes) -> bytes:
    """Hochgeladenes Bild → JPEG 1600×640 (mittig zugeschnitten). Wirft ImageRejected."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected("too_large")
    try:
        image = Image.open(io.BytesIO(data), formats=FORMATS)
    except (UnidentifiedImageError, OSError):
        raise ImageRejected("not_an_image") from None
    if image.width * image.height > MAX_PIXELS:   # Kopfdaten gelesen, noch nichts ausgepackt
        raise ImageRejected("too_many_pixels")
    try:
        if image.format == "JPEG":
            image.draft("RGB", (WIDTH * 2, HEIGHT * 2))   # große Fotos gleich verkleinert auspacken
        image.seek(0)                                     # animierte Bilder: das erste
        image = ImageOps.exif_transpose(image)            # Handyfotos richtig herum
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, BACKGROUND)
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
        image = ImageOps.fit(image.convert("RGB"), (WIDTH, HEIGHT), Image.LANCZOS)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ImageRejected("not_an_image") from None
    out = io.BytesIO()
    image.save(out, "JPEG", quality=QUALITY, subsampling=0)   # 4:4:4: Farben in voller Auflösung (scharfe Farbkanten)
    return out.getvalue()


async def process_upload(data: bytes) -> tuple[bytes, str]:
    """Wie process, aber nicht im Thread des Bots. Gibt (JPEG, Prüfsumme) zurück."""
    async with _slots:
        image = await asyncio.to_thread(process, data)
    return image, hashlib.sha256(image).hexdigest()
