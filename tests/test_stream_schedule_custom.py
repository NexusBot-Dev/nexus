"""Eigener Text und eigenes Bild für Streamplan-Events (Premium): systems/schedule_images.py und
set_custom_text / set_custom_image in systems/stream_schedule.py."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, JpegImagePlugin

from database import db_stream_alerts, db_stream_schedule
from systems import schedule_images, stream_schedule
from systems.stream_schedule import ScheduleError, render_text
from tests.conftest import set_premium
from tests.stream_schedule_fakes import IMAGE, cover, follow, segment

TEMPLATE = "Heute {category}! 🎮\n{title}\nSchaut vorbei: {link}"
# Speichern gleicht sofort ab — mit der echten Uhrzeit, also Termine relativ zu jetzt
SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)


@pytest.fixture(autouse=True)
def released(monkeypatch):
    monkeypatch.setattr(stream_schedule, "RELEASED", True)


def picture(size=(1600, 900), fmt="PNG", color=(30, 160, 90), mode="RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, fmt)
    return buffer.getvalue()


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def events(guild) -> list:
    return sorted(guild.events_by_id.values(), key=lambda e: e.start_time)


async def sync(bot):
    await stream_schedule.sync_all(bot)


# ── Bild verarbeiten ─────────────────────────────────────────────────────────────────────────

def test_bild_wird_jpeg_in_eventgroesse():
    image = opened(schedule_images.process(picture()))
    assert (image.format, image.size) == ("JPEG", (1600, 640))   # doppelte Mindestgröße: scharf auf HiDPI


def test_farben_in_voller_aufloesung():
    # Feines rot-blaues Streifenmuster: mit halbierter Farbauflösung (4:2:0) verschwimmt es zu Lila
    stripes = Image.new("RGB", (1600, 640), (220, 30, 30))
    for x in range(0, 1600, 2):
        stripes.paste((30, 30, 220), (x, 0, x + 1, 640))
    buffer = io.BytesIO()
    stripes.save(buffer, "PNG")
    image = opened(schedule_images.process(buffer.getvalue()))
    assert JpegImagePlugin.get_sampling(image) == 0   # 4:4:4
    red, blue = image.convert("RGB").getpixel((801, 320)), image.convert("RGB").getpixel((800, 320))
    assert red[0] > 150 > red[2] and blue[2] > 150 > blue[0]


def test_hochkant_wird_mittig_zugeschnitten():
    tall = Image.new("RGB", (600, 1800), (30, 30, 220))
    tall.paste(Image.new("RGB", (600, 600), (220, 30, 30)), (0, 600))   # Mitte rot
    buffer = io.BytesIO()
    tall.save(buffer, "PNG")
    image = opened(schedule_images.process(buffer.getvalue())).convert("RGB")
    for x, y in ((10, 10), (1590, 630), (800, 320)):
        r, g, b = image.getpixel((x, y))
        assert r > 180 and b < 80   # überall rot: oben und unten abgeschnitten, nicht gestaucht


def test_transparenz_und_animation():
    image = opened(schedule_images.process(picture(fmt="PNG", mode="RGBA", color=(0, 0, 0, 0)))).convert("RGB")
    assert image.getpixel((10, 10)) == pytest.approx(schedule_images.BACKGROUND, abs=6)
    frames = [Image.new("RGB", (400, 400), c) for c in ((220, 30, 30), (30, 30, 220))]
    buffer = io.BytesIO()
    frames[0].save(buffer, "WEBP", save_all=True, append_images=frames[1:])
    first = opened(schedule_images.process(buffer.getvalue())).convert("RGB").getpixel((400, 160))
    assert first[0] > 180   # erstes Bild (rot)


@pytest.mark.parametrize("data, reason", [
    (b"x" * (schedule_images.MAX_UPLOAD_BYTES + 1), "too_large"),
    (b"kein Bild", "not_an_image"),
    (picture(fmt="GIF"), "not_an_image"),          # nur PNG, JPEG, WebP
    (picture(size=(6000, 6000)), "too_many_pixels"),
])
def test_abgelehnt(data, reason):
    with pytest.raises(schedule_images.ImageRejected) as error:
        schedule_images.process(data)
    assert error.value.reason == reason


async def test_verarbeitung_liefert_pruefsumme():
    image, image_hash = await schedule_images.process_upload(picture())
    assert len(image_hash) == 64 and opened(image).size == (1600, 640)


# ── Text füllen ──────────────────────────────────────────────────────────────────────────────

def test_platzhalter():
    text = render_text(TEMPLATE, title="Blind-Run #12", category="Elden Ring", link="https://twitch.tv/rusirda",
                       streamer="Rusirda")
    assert text == "Heute Elden Ring! 🎮\nBlind-Run #12\nSchaut vorbei: https://twitch.tv/rusirda"


def test_unbekanntes_und_klammern_bleiben_stehen():
    text = render_text("{foo} { {title.__class__} {{title}} {title", title="T", category=None, link="L", streamer="S")
    assert text == "{foo} { {title.__class__} {T} {title"


# ── Eigener Text im Event ────────────────────────────────────────────────────────────────────

async def test_eigener_text_mit_premium(bot, guild, twitch):
    await set_premium(guild)
    twitch.plan("tw-1", segment("a", SOON, title="Blind-Run #12"))
    mc_id = await follow(guild)
    await stream_schedule.set_custom_text(bot, guild.id, mc_id, TEMPLATE)
    await sync(bot)
    [event] = events(guild)
    assert event.description == "Heute Elden Ring! 🎮\nBlind-Run #12\nSchaut vorbei: https://twitch.tv/rusirda"


async def test_eigener_text_ohne_premium_abgelehnt(bot, guild, twitch):
    mc_id = await follow(guild)
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_custom_text(bot, guild.id, mc_id, TEMPLATE)
    assert error.value.reason == "premium"
    await stream_schedule.set_custom_text(bot, guild.id, mc_id, "")   # Zurücksetzen geht immer


async def test_eigener_text_zu_lang_oder_ausgeschaltet(bot, guild, twitch):
    await set_premium(guild)
    mc_id = await follow(guild)
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_custom_text(bot, guild.id, mc_id, "x" * (stream_schedule.MAX_TEXT_LENGTH + 1))
    assert (error.value.reason, error.value.params) == ("text_too_long", {"max": stream_schedule.MAX_TEXT_LENGTH})
    other = await follow(guild, "tw-2", "Max", sync=False)
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_custom_text(bot, guild.id, other, TEMPLATE)
    assert error.value.reason == "not_enabled"


async def test_premium_abgelaufen_standardtext_gespeichertes_bleibt(bot, guild, twitch):
    await set_premium(guild)
    twitch.plan("tw-1", segment("a", SOON))
    mc_id = await follow(guild)
    await stream_schedule.set_custom_text(bot, guild.id, mc_id, TEMPLATE)
    await sync(bot)
    [event] = events(guild)
    assert event.description.startswith("Heute")

    await set_premium(guild, active=False)
    await sync(bot)
    assert not event.description.startswith("Heute")   # Standardtext
    assert (await db_stream_schedule.get_custom_meta(guild.id))[mc_id]["description"] == TEMPLATE

    await set_premium(guild)
    await sync(bot)
    assert event.description.startswith("Heute")


# ── Eigenes Bild im Event ────────────────────────────────────────────────────────────────────

async def test_eigenes_bild_wird_quelle_und_ersetzt_das_banner(bot, guild, twitch):
    await set_premium(guild)
    twitch.covers["32982"] = cover()
    twitch.plan("tw-1", segment("a", SOON, category_id="32982"))
    mc_id = await follow(guild)
    await sync(bot)
    [event] = events(guild)
    banner = event.image

    await stream_schedule.set_custom_image(bot, guild.id, mc_id, picture())
    [sync_row] = await db_stream_schedule.get_syncs_for_guild(guild.id)
    assert sync_row["image_source"] == stream_schedule.IMAGE_CUSTOM
    await sync(bot)
    assert event.image != banner
    assert event.image == await db_stream_schedule.get_custom_image(guild.id, mc_id)
    assert opened(event.image).size == (1600, 640)

    await stream_schedule.set_custom_image(bot, guild.id, mc_id, None)   # entfernen
    [sync_row] = await db_stream_schedule.get_syncs_for_guild(guild.id)
    assert sync_row["image_source"] == stream_schedule.IMAGE_BANNER
    await sync(bot)
    assert event.image == banner


async def test_premium_abgelaufen_banner_statt_eigenem_bild(bot, guild, twitch):
    await set_premium(guild)
    twitch.covers["32982"] = cover()
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", SOON, category_id="32982"))
    mc_id = await follow(guild)
    await stream_schedule.set_custom_image(bot, guild.id, mc_id, picture())
    await sync(bot)
    [event] = events(guild)
    own = event.image

    await set_premium(guild, active=False)
    await sync(bot)
    assert event.image not in (own, IMAGE) and opened(event.image).size == (800, 320)   # Spiel-Banner
    await set_premium(guild)
    await sync(bot)
    assert event.image == own


async def test_eigenes_bild_ohne_premium_oder_kaputt(bot, guild, twitch):
    mc_id = await follow(guild)
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_custom_image(bot, guild.id, mc_id, picture())
    assert error.value.reason == "premium"
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_image_source(bot, guild.id, mc_id, stream_schedule.IMAGE_CUSTOM)
    assert error.value.reason == "premium"

    await set_premium(guild)
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_custom_image(bot, guild.id, mc_id, b"kein Bild")
    assert (error.value.reason, error.value.params) == ("image_rejected", {"problem": "not_an_image"})
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_image_source(bot, guild.id, mc_id, stream_schedule.IMAGE_CUSTOM)
    assert error.value.reason == "no_custom_image"


async def test_ausgeschaltet_wird_nicht_erst_gerechnet(bot, guild, twitch, monkeypatch):
    await set_premium(guild)
    mc_id = await follow(guild, sync=False)
    calls = []
    monkeypatch.setattr(schedule_images, "process", lambda data: calls.append(data) or b"")
    with pytest.raises(ScheduleError) as error:
        await stream_schedule.set_custom_image(bot, guild.id, mc_id, picture())
    assert error.value.reason == "not_enabled" and calls == []


# ── Aufräumen und Übersicht ──────────────────────────────────────────────────────────────────

async def test_alert_entfernt_text_und_bild_weg(bot, guild, twitch):
    await set_premium(guild)
    mc_id = await follow(guild)
    await stream_schedule.set_custom_text(bot, guild.id, mc_id, TEMPLATE)
    await stream_schedule.set_custom_image(bot, guild.id, mc_id, picture())
    await db_stream_alerts.remove_subscription(guild.id, mc_id)
    await sync(bot)
    assert await db_stream_schedule.get_custom_meta(guild.id) == {}


async def test_uebersicht_ohne_bilddaten(bot, guild, twitch):
    await set_premium(guild)
    mc_id = await follow(guild)
    await stream_schedule.set_custom_text(bot, guild.id, mc_id, TEMPLATE)
    await stream_schedule.set_custom_image(bot, guild.id, mc_id, picture())
    meta = (await db_stream_schedule.get_custom_meta(guild.id))[mc_id]
    assert "image" not in meta   # Übersichten laden das Bild nie mit
    [entry] = (await stream_schedule.get_overview(guild)).entries
    assert (entry.custom_text, len(entry.custom_image_hash), entry.image_source) == (TEMPLATE, 64, "custom")
