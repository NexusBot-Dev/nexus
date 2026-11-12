"""/rank: Level-Karte (systems/card_generator.py) und der Befehl (cogs/user_commands.py)."""
import io
import logging
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from cogs.user_commands import UserCommands
from database import db_levels
from systems import card_generator
from systems.card_generator import CIRCLE_CENTER, CIRCLE_RADIUS, generate_rank_card
from tests.conftest import query
from tests.fakes import FakeAsset, FakeInteraction

RED = (220, 30, 30)


def image_bytes(size: tuple[int, int], fmt: str = "PNG", color=RED, frames: int = 1) -> bytes:
    buffer = io.BytesIO()
    first = Image.new("RGB", size, color)
    if frames > 1:
        rest = [Image.new("RGB", size, (30, 30, 220)) for _ in range(frames - 1)]
        first.save(buffer, fmt, save_all=True, append_images=rest)
    else:
        first.save(buffer, fmt)
    return buffer.getvalue()


def card(avatar: bytes, name: str = "Rusirda") -> Image.Image:
    return Image.open(generate_rank_card(name, avatar, 1234, 5000, 3, 12))


def close(pixel, color, tolerance=30) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, color))


# ── Karte ────────────────────────────────────────────────────────────────────────────────────

def test_karte_ist_ein_kleines_webp():
    image = card(image_bytes((512, 512)))
    assert (image.format, image.size) == ("WEBP", (1200, 400))
    buffer = generate_rank_card("Rusirda", image_bytes((512, 512)), 1, 2, 1, 1)
    assert buffer.getbuffer().nbytes < 150 * 1024   # als PNG waren es ~360 KB


def test_avatar_rund_in_der_mitte_auch_wenn_nicht_quadratisch():
    """Breites Bild: blau | rot | blau. Mittig zugeschnitten bleibt nur Rot — verzerrt wäre links Blau im Kreis."""
    wide = Image.new("RGB", (900, 300), (30, 30, 220))
    wide.paste(Image.new("RGB", (300, 300), RED), (300, 0))
    buffer = io.BytesIO()
    wide.save(buffer, "PNG")
    image = card(buffer.getvalue()).convert("RGB")
    cx, cy = CIRCLE_CENTER
    assert close(image.getpixel((cx, cy)), RED)                                  # Mitte: Avatar
    assert close(image.getpixel((cx - CIRCLE_RADIUS + 20, cy)), RED)             # links im Kreis: nicht verzerrt
    corner = (cx - CIRCLE_RADIUS + 5, cy - CIRCLE_RADIUS + 5)                     # Ecke außerhalb des Kreises
    assert not close(image.getpixel(corner), RED)


def test_animierter_avatar_nutzt_das_erste_bild():
    image = card(image_bytes((256, 256), "GIF", frames=3)).convert("RGB")
    assert close(image.getpixel(CIRCLE_CENTER), RED, tolerance=40)


def test_gleichzeitig_in_mehreren_threads():
    """/rank rechnet in asyncio.to_thread — zwei Aufrufe gleichzeitig dürfen sich nicht stören (Schriften je Thread)."""
    avatar = image_bytes((512, 512))
    names = ["A" * n for n in range(5, 45)]   # lange Namen: Schriftgröße wird angepasst
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda name: generate_rank_card(name, avatar, 10, 100, 1, 2).getvalue(), names))
    alone = [generate_rank_card(name, avatar, 10, 100, 1, 2).getvalue() for name in names]
    assert results == alone


def test_template_wird_nur_einmal_gelesen(monkeypatch):
    card_generator._template.cache_clear()
    opened = []
    real_open = Image.open

    def counting_open(fp, *args, **kwargs):
        if fp == card_generator.TEMPLATE_PATH:
            opened.append(fp)
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(card_generator.Image, "open", counting_open)
    avatar = image_bytes((128, 128))
    for _ in range(3):
        generate_rank_card("X", avatar, 1, 2, 1, 1)
    assert len(opened) == 1


# ── Befehl ───────────────────────────────────────────────────────────────────────────────────

async def run_rank(bot, guild, member=None):
    interaction = FakeInteraction(guild)
    await UserCommands.rank.callback(UserCommands(bot), interaction, member=member)
    [message] = interaction.sent
    return interaction, message


async def test_rank_laedt_kleinen_statischen_avatar_und_sendet_webp(bot, guild, caplog):
    member = guild.admin
    member.display_avatar = FakeAsset("https://cdn.discordapp.com/avatars/1/a_animiert.gif?size=1024")
    await db_levels.set_level(guild.id, member.id, 5)
    with caplog.at_level(logging.INFO, logger="cogs.user_commands"):
        _, message = await run_rank(bot, guild)
    assert member.display_avatar.reads == ["https://cdn.discordapp.com/avatars/1/a_animiert.png?size=512"]
    assert message["file"].filename == "rank.webp"
    assert Image.open(message["file"].fp).format == "WEBP"
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("/rank")]
    for part in ("DB", "Avatar", "Karte", "Senden", "gesamt"):
        assert part in line


async def test_rank_mit_ersatz_avatar_wenn_der_download_scheitert(bot, guild):
    member = guild.admin
    member.display_avatar = FakeAsset("https://cdn.discordapp.com/avatars/1/abc.png", fail=True)
    _, message = await run_rank(bot, guild)
    assert member.default_avatar.reads   # Standard-Avatar statt Abbruch
    assert message["file"].filename == "rank.webp"


async def test_index_fuer_die_rang_abfrage():
    rows = await query("""SELECT column_name FROM information_schema.statistics
                          WHERE table_schema = DATABASE() AND table_name = 'levels' AND index_name = 'idx_guild_xp'
                          ORDER BY seq_in_index""")
    assert [r["column_name"] for r in rows] == ["guild_id", "xp"]
