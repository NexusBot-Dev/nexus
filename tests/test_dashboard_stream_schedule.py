"""Streamplan im Dashboard (dashboard/stream_alerts.py: schedule, schedule-toggle) — dieselbe Logik wie /stream_schedule."""
import base64
import io
from datetime import datetime, timedelta, timezone

import discord
import pytest
from PIL import Image

from database import db_stream_alerts, db_stream_schedule
from systems import stream_schedule
from tests.conftest import set_premium, t
from tests.stream_schedule_fakes import cover, follow, segment

SA = "stream_alerts"
SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)   # der Bot gleicht sofort ab, mit echter Uhrzeit


@pytest.fixture(autouse=True)
def released(monkeypatch):
    """Diese Tests prüfen die Funktion selbst — die Sperre (RELEASED = False) steht in test_stream_schedule_testphase.py."""
    monkeypatch.setattr(stream_schedule, "RELEASED", True)


async def test_uebersicht(dash, guild, twitch):
    first = await follow(guild, "tw-1", "Rusirda", sync=False)
    channel = guild.add_text_channel("yt")
    youtube = await db_stream_alerts.get_or_create_monitored_channel("youtube", "yt-1", "TubeStar")
    await db_stream_alerts.add_subscription(guild.id, channel.id, youtube["id"])

    data = await dash.ok(SA, "schedule")
    assert data == {
        "entries": [{"monitored_channel_id": first, "streamer_name": "Rusirda", "enabled": False, "active": False,
                     "error": None, "events": 0, "image_source": "banner",
                     "custom_text": None, "custom_image_hash": None}],   # nur Twitch
        "limit": 1, "premium_limit": 5, "days": 7, "premium_days": 7,
        "premium_active": False, "can_create_events": True, "available": True,
    }


async def test_einschalten_legt_sofort_events_an(dash, guild, twitch):
    twitch.plan("tw-1", segment("a", SOON), segment("b", SOON + timedelta(days=1)))
    mc_id = await follow(guild, sync=False)
    data = await dash.ok(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": True})
    [entry] = data["entries"]
    assert (entry["enabled"], entry["active"], entry["events"], entry["error"]) == (True, True, 2, None)
    assert len(guild.events_by_id) == 2

    data = await dash.ok(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": False})
    assert (data["entries"][0]["enabled"], data["entries"][0]["events"]) == (False, 0)
    assert guild.events_by_id == {}


async def test_fehler_aus_dem_abgleich(dash, guild, twitch):
    twitch.plans["tw-1"] = None   # kein Streamplan auf Twitch
    mc_id = await follow(guild, sync=False)
    data = await dash.ok(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": True})
    assert data["entries"][0]["error"] == "no_schedule"


async def test_limit(dash, guild, twitch):
    await follow(guild, "tw-1", "Rusirda")
    second = await follow(guild, "tw-2", "Max", sync=False)
    error = await dash.error(SA, "schedule-toggle", {"monitored_channel_id": str(second), "enabled": True})
    assert error == t("dash_err_sched_limit", limit=1)

    await set_premium(guild)
    data = await dash.ok(SA, "schedule-toggle", {"monitored_channel_id": str(second), "enabled": True})
    assert [e["active"] for e in data["entries"]] == [True, True]
    assert (data["limit"], data["days"], data["premium_active"]) == (5, 7, True)


async def test_premium_abgelaufen_inaktiv(dash, guild, twitch):
    await set_premium(guild)
    await follow(guild, "tw-1", "Rusirda")
    await follow(guild, "tw-2", "Max")
    await set_premium(guild, active=False)
    data = await dash.ok(SA, "schedule")
    assert [(e["enabled"], e["active"]) for e in data["entries"]] == [(True, True), (True, False)]


async def test_ohne_recht_events_erstellen(dash, guild, twitch):
    guild.bot_role.permissions = discord.Permissions(view_channel=True, send_messages=True, manage_guild=True)
    await follow(guild, sync=False)
    assert (await dash.ok(SA, "schedule"))["can_create_events"] is False


async def test_entfernter_kanal(dash, guild, twitch):
    mc_id = await follow(guild, sync=False)
    await db_stream_alerts.remove_subscription(guild.id, mc_id)
    error = await dash.error(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": True})
    assert error == t("dash_err_sa_not_found")
    assert await db_stream_schedule.get_syncs_for_guild(guild.id) == []


async def test_ungueltige_eingaben(dash, guild, twitch):
    mc_id = await follow(guild, sync=False)
    await dash.error(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": "ja"})
    await dash.error(SA, "schedule-toggle", {"enabled": True})
    assert await db_stream_schedule.get_syncs_for_guild(guild.id) == []


def test_meldung_in_allen_sprachen():
    from translations import TRANSLATIONS
    for code, lang in TRANSLATIONS.items():
        assert "{" not in lang["dash_err_sched_limit"].format(limit=1), code


async def test_bildquelle_waehlen(dash, guild, twitch):
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", SOON))
    mc_id = await follow(guild)
    data = await dash.ok(SA, "schedule-image", {"monitored_channel_id": str(mc_id), "source": "none"})
    assert data["entries"][0]["image_source"] == "none"
    assert [e.image for e in guild.events_by_id.values()] == [None]


async def test_bildquelle_ungueltig_oder_ausgeschaltet(dash, guild, twitch):
    mc_id = await follow(guild, sync=False)
    await dash.error(SA, "schedule-image", {"monitored_channel_id": str(mc_id), "source": "gif"})
    error = await dash.error(SA, "schedule-image", {"monitored_channel_id": str(mc_id), "source": "offline"})
    assert error == t("dash_err_sa_not_found")


# ── Eigener Text und eigenes Bild (Premium) ──────────────────────────────────────────────────

TEMPLATE = "Heute {category}: {title}"


def picture_b64(fmt="JPEG", size=(800, 320)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 160, 90)).save(buffer, fmt)
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture
def planned(twitch):
    twitch.plan("tw-1", segment("a", SOON, title="Blind-Run"))
    twitch.covers["1"] = cover()
    return twitch


async def test_text_und_bild_zusammen_einmal_abgeglichen(dash, guild, planned, monkeypatch):
    await set_premium(guild)
    mc_id = await follow(guild)
    calls = []
    real = stream_schedule.sync_guild

    async def counting(bot, guild_id, **kwargs):
        calls.append(guild_id)
        return await real(bot, guild_id, **kwargs)
    monkeypatch.setattr(stream_schedule, "sync_guild", counting)

    data = await dash.ok(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": f" {TEMPLATE} ",
                                                 "image": picture_b64("PNG", (1600, 900))})
    assert calls == [guild.id]
    [entry] = data["entries"]
    assert (entry["custom_text"], entry["image_source"], len(entry["custom_image_hash"])) == (TEMPLATE, "custom", 64)
    [event] = guild.events_by_id.values()
    assert event.description == "Heute Elden Ring: Blind-Run"
    stored = await db_stream_schedule.get_custom_image(guild.id, mc_id)
    assert event.image == stored and Image.open(io.BytesIO(stored)).size == (1600, 640)

    preview = await dash.ok(SA, "schedule-custom-image", {"monitored_channel_id": str(mc_id)})
    assert base64.b64decode(preview["image"]) == stored


async def test_nur_text_oder_nur_bild_aendern(dash, guild, planned):
    await set_premium(guild)
    mc_id = await follow(guild)
    await dash.ok(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": TEMPLATE, "image": picture_b64()})
    data = await dash.ok(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": ""})   # Bild bleibt
    assert (data["entries"][0]["custom_text"], data["entries"][0]["image_source"]) == (None, "custom")
    data = await dash.ok(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "image": None})   # entfernen
    assert (data["entries"][0]["custom_image_hash"], data["entries"][0]["image_source"]) == (None, "banner")
    assert (await dash.ok(SA, "schedule-custom-image", {"monitored_channel_id": str(mc_id)}))["image"] is None


async def test_ohne_premium(dash, guild, planned):
    mc_id = await follow(guild)
    for payload in ({"text": TEMPLATE}, {"image": picture_b64()}):
        error = await dash.error(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), **payload})
        assert error == t("dash_err_sched_premium")
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)
    # Entfernen geht immer
    await dash.ok(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": None, "image": None})


async def test_premium_abgelaufen_uebersicht_zeigt_was_gilt(dash, guild, planned):
    await set_premium(guild)
    mc_id = await follow(guild)
    await dash.ok(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": TEMPLATE, "image": picture_b64()})
    await set_premium(guild, active=False)
    [entry] = (await dash.ok(SA, "schedule"))["entries"]
    assert entry["image_source"] == "banner"   # gespeichert ist „custom“, es gilt aber das Banner
    assert entry["custom_text"] == TEMPLATE and entry["custom_image_hash"]   # gespeichert, für den Hinweis


async def test_abgelehntes_bild_speichert_auch_den_text_nicht(dash, guild, planned):
    await set_premium(guild)
    mc_id = await follow(guild)
    junk = base64.b64encode(b"%PDF-1.7 kein Bild").decode()
    error = await dash.error(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": TEMPLATE, "image": junk})
    assert error == t("dash_err_sched_image_not_an_image")
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)


async def test_anfragen_ueber_1_mib_lehnt_der_server_ab(dash, guild, planned):
    """Das Dashboard schickt das Bild schon auf 800×320 zugeschnitten — mehr als 1 MiB nimmt der Bot nicht an."""
    await set_premium(guild)
    mc_id = await follow(guild)
    huge = "A" * (1024 * 1024)
    assert await dash.error(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "image": huge}) == "invalid_request"
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)


async def test_text_zu_lang(dash, guild, planned):
    await set_premium(guild)
    mc_id = await follow(guild)
    error = await dash.error(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), "text": "x" * 801,
                                                     "image": picture_b64()})
    assert error == t("dash_err_sched_text_too_long", max=800)
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)   # auch das Bild nicht


async def test_eigenes_ungueltige_eingaben(dash, guild, planned):
    await set_premium(guild)
    mc_id = await follow(guild)
    off = await follow(guild, "tw-2", "Max", sync=False)
    for payload in ({}, {"text": 5}, {"image": ["x"]}, {"image": "kein base64!"}, {"image": "aGFsbG8=!"}):
        assert await dash.error(SA, "schedule-custom", {"monitored_channel_id": str(mc_id), **payload}) == t("dash_err_invalid_request")
    error = await dash.error(SA, "schedule-custom", {"monitored_channel_id": str(off), "text": TEMPLATE})
    assert error == t("dash_err_sa_not_found")   # ausgeschaltet
    assert await db_stream_schedule.get_custom_meta(guild.id) == {}


def test_eigenes_meldungen_in_allen_sprachen():
    from translations import TRANSLATIONS
    for code, lang in TRANSLATIONS.items():
        texts = [lang["dash_err_sched_premium"], lang["dash_err_sched_text_too_long"].format(max=800),
                 lang["dash_err_sched_image_too_large"].format(mb=2), lang["dash_err_sched_image_too_many_pixels"],
                 lang["dash_err_sched_image_not_an_image"]]
        assert all(texts) and "{" not in "".join(texts), code
