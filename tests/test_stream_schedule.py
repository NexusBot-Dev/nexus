"""Twitch-Streamplan als Discord-Events (systems/stream_schedule.py, systems/twitch_api.get_schedule)."""
import io

import aiohttp
import discord
import pytest
from PIL import Image

from database import db_limits, db_settings, db_stream_alerts, db_stream_schedule
from emojis import NexusEmojis
from systems import stream_schedule, twitch_api
from tests.conftest import add_guild, query, set_premium, t
from tests.stream_schedule_fakes import IMAGE, NOW, at, cover, follow, iso, segment
from translations import TRANSLATIONS

@pytest.fixture(autouse=True)
def released(monkeypatch):
    """Diese Tests prüfen die Funktion selbst — die Sperre (RELEASED = False) steht in test_stream_schedule_testphase.py."""
    monkeypatch.setattr(stream_schedule, "RELEASED", True)


async def sync(bot, now=NOW):
    await stream_schedule.sync_all(bot, now=now)


def events(guild) -> list:
    return sorted(guild.events_by_id.values(), key=lambda e: e.start_time)


def calls(guild, kind: str) -> list:
    return [c for c in guild.calls if c[0] == kind]


async def status(guild, mc_id) -> str | None:
    [row] = [r for r in await db_stream_schedule.get_syncs_for_guild(guild.id) if r["monitored_channel_id"] == mc_id]
    return row["last_error"]


# ── Aus dem Plan werden Events ─────────────────────────────────────────────────────────────────

async def test_legt_events_aus_dem_plan_an(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)), segment("b", at(3), hours=2, title="", category="Just Chatting"))
    twitch.images["tw-1"] = "https://img/offline.png"
    await follow(guild)
    await sync(bot)

    first, second = events(guild)
    assert (first.name, first.start_time, first.end_time) == ("Elden Ring Blind-Run", at(1), at(1, 3))
    assert first.location == "https://twitch.tv/rusirda"
    assert first.description == (f"🎮 Elden Ring\n{NexusEmojis.Integrations.TWITCH} twitch.tv/rusirda\n\n"
                                 + t("schedule_event_source", streamer="Rusirda"))
    assert first.privacy_level is discord.PrivacyLevel.guild_only
    assert first.image == IMAGE
    assert second.name == t("schedule_event_title", streamer="Rusirda", category="Just Chatting")   # ohne Titel
    assert twitch.image_calls == ["tw-1"]   # Bild einmal geladen, für beide Events


async def test_ersatztitel_ohne_titel_und_kategorie_und_ohne_bild(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1), title="  ", category=None, end=False))
    await follow(guild)
    await sync(bot)
    [event] = events(guild)
    assert event.name == t("schedule_event_title_plain", streamer="Rusirda")
    assert event.description == f"{NexusEmojis.Integrations.TWITCH} twitch.tv/rusirda\n\n" + t("schedule_event_source", streamer="Rusirda")
    assert event.end_time == at(1) + stream_schedule.DEFAULT_DURATION   # Discord verlangt eine Endzeit
    assert event.image is None


async def test_texte_in_der_sprache_des_servers(bot, guild, twitch):
    await query("UPDATE guild_settings SET locale = 'en' WHERE guild_id = %s", guild.id)
    twitch.plan("tw-1", segment("a", at(1), title=""))
    await follow(guild)
    await sync(bot)
    [event] = events(guild)
    assert event.name == TRANSLATIONS["en"]["schedule_event_title"].format(streamer="Rusirda", category="Elden Ring")


async def test_lange_titel_werden_gekuerzt(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1), title="x" * 140))
    await follow(guild)
    await sync(bot)
    assert len(events(guild)[0].name) == 100


async def test_abgesagt_urlaub_vergangen_und_ausserhalb_des_fensters_kommen_nicht(bot, guild, twitch):
    twitch.plan(
        "tw-1",
        segment("vorbei", at(hours=-1)),
        segment("abgesagt", at(1), canceled=True),
        segment("urlaub", at(4)),
        segment("ok", at(6)),
        segment("zu-spaet", at(8)),   # Free: 7 Tage
        vacation={"start_time": iso(at(3)), "end_time": iso(at(5))},
    )
    await follow(guild)
    await sync(bot)
    assert [e.start_time for e in events(guild)] == [at(6)]


async def test_premium_hat_dasselbe_zeitfenster(bot, guild, twitch):
    """Der Twitch-Plan ist meist wöchentlich — mehr als 7 Tage zeigten nur denselben Termin doppelt."""
    await set_premium(guild)
    twitch.plan("tw-1", segment("a", at(6)), segment("b", at(8)))
    await follow(guild)
    await sync(bot)
    assert [e.start_time for e in events(guild)] == [at(6)]


async def test_zeitfenster_kommt_aus_feature_limits(bot, guild, twitch):
    """Stellschraube für später, z. B. wenn Twitch einmalige Termine für alle freigibt."""
    await query("UPDATE feature_limits SET premium_limit = 14 WHERE feature_key = %s", stream_schedule.LIMIT_DAYS)
    db_limits._limits_cache.clear()
    await set_premium(guild)
    twitch.plan("tw-1", segment("a", at(8)), segment("b", at(13)), segment("c", at(15)))
    await follow(guild)
    await sync(bot)
    assert [e.start_time for e in events(guild)] == [at(8), at(13)]

    await set_premium(guild, active=False)   # Fenster schrumpft auf 7 Tage: was darüber hinausgeht, verschwindet
    await sync(bot)
    assert events(guild) == []


# ── Bild: Banner aus dem Kategorie-Cover, sonst Offline-Bild ──────────────────────────────────

def banner_of(event) -> Image.Image:
    return Image.open(io.BytesIO(event.image))


async def test_mit_kategorie_cover_bekommt_das_event_ein_banner(bot, guild, twitch):
    twitch.covers["32982"] = cover()
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", at(1), category="Grand Theft Auto V", category_id="32982"),
                segment("b", at(2), category=None))
    await follow(guild)
    await sync(bot)
    with_category, without = events(guild)
    assert (banner_of(with_category).format, banner_of(with_category).size) == ("JPEG", (800, 320))
    assert without.image == IMAGE   # ohne Kategorie: Offline-Bild


async def test_ohne_cover_das_offline_bild(bot, guild, twitch):
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", at(1), category_id="unbekannt"))
    await follow(guild)
    await sync(bot)
    assert events(guild)[0].image == IMAGE


async def test_kaputtes_cover_das_offline_bild(bot, guild, twitch):
    twitch.covers["32982"] = b"kein Bild"
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", at(1), category_id="32982"))
    await follow(guild)
    await sync(bot)
    assert events(guild)[0].image == IMAGE


async def test_banner_je_kategorie_nur_einmal_gerechnet(bot, guild, twitch, monkeypatch):
    rendered = []
    real = stream_schedule.schedule_banner.render_banner
    monkeypatch.setattr(stream_schedule.schedule_banner, "render_banner",
                        lambda *args: rendered.append(args[1]) or real(*args))
    twitch.covers["32982"] = cover()
    twitch.plan("tw-1", *(segment(str(i), at(i + 1), category="GTA V", category_id="32982") for i in range(3)))
    await follow(guild)
    await sync(bot)
    assert len(events(guild)) == 3 and rendered == ["GTA V"]


async def test_kategorie_geaendert_neues_banner(bot, guild, twitch):
    twitch.covers["32982"] = cover((200, 40, 40))
    twitch.covers["21779"] = cover((40, 40, 200))
    twitch.plan("tw-1", segment("a", at(1), category="GTA V", category_id="32982"))
    await follow(guild)
    await sync(bot)
    [event] = events(guild)
    before = event.image

    twitch.plan("tw-1", segment("a", at(1), category="League of Legends", category_id="21779"))
    await sync(bot)
    assert events(guild) == [event] and event.image != before
    assert any(c[0] == "event.edit" and "image" in c[2] for c in guild.calls)


async def test_neue_banner_version_aktualisiert_bestehende_events(bot, guild, twitch, monkeypatch):
    """Auch wenn sich am Termin nichts ändert: Neues Banner-Aussehen (oder das erste Banner überhaupt) kommt an."""
    twitch.covers["32982"] = cover()
    twitch.plan("tw-1", segment("a", at(1), category="GTA V", category_id="32982"))
    await follow(guild)
    await sync(bot)
    guild.calls.clear()
    monkeypatch.setattr(stream_schedule, "BANNER_VERSION", stream_schedule.BANNER_VERSION + 1)
    await sync(bot)
    assert [c[0] for c in guild.calls] == ["event.edit"]


async def test_bildquelle_offline_und_keins(bot, guild, twitch):
    twitch.covers["32982"] = cover()
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", at(1), category="GTA V", category_id="32982"))
    mc_id = await follow(guild)
    await sync(bot)
    [event] = events(guild)
    assert banner_of(event).size == (800, 320)   # Standard: Banner

    await db_stream_schedule.set_image_source(guild.id, mc_id, stream_schedule.IMAGE_OFFLINE)
    await sync(bot)
    assert events(guild) == [event] and event.image == IMAGE

    await db_stream_schedule.set_image_source(guild.id, mc_id, stream_schedule.IMAGE_NONE)
    await sync(bot)
    assert event.image is None   # Bild entfernt, Event bleibt


async def test_bildquelle_nur_fuer_eingeschaltete_kanaele(bot, guild, twitch):
    mc_id = await follow(guild, sync=False)
    with pytest.raises(stream_schedule.ScheduleError):
        await stream_schedule.set_image_source(bot, guild.id, mc_id, stream_schedule.IMAGE_NONE)
    with pytest.raises(ValueError):
        await stream_schedule.set_image_source(bot, guild.id, mc_id, "gif")


# ── Änderungen im Plan ────────────────────────────────────────────────────────────────────────

async def test_unveraenderter_plan_aendert_nichts(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await sync(bot)
    guild.calls.clear()
    await sync(bot, now=at(hours=1))
    assert guild.calls == []


async def test_verschoben_und_umbenannt_wird_angepasst(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await sync(bot)
    [event] = events(guild)

    twitch.plan("tw-1", segment("a", at(2), hours=4, title="Neuer Titel"))
    await sync(bot)
    assert events(guild) == [event]   # dasselbe Event, nicht neu angelegt
    assert (event.name, event.start_time, event.end_time) == ("Neuer Titel", at(2), at(2, 4))
    assert len(calls(guild, "guild.create_scheduled_event")) == 1


async def test_abgesagt_oder_entfernt_wird_geloescht(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)), segment("b", at(2)))
    await follow(guild)
    await sync(bot)

    twitch.plan("tw-1", segment("a", at(1), canceled=True))   # a abgesagt, b ganz aus dem Plan
    await sync(bot)
    assert events(guild) == []
    assert len(calls(guild, "event.delete")) == 2
    assert await query("SELECT * FROM stream_schedule_events") == []


async def test_laufende_und_vergangene_events_bleiben(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await sync(bot)
    [event] = events(guild)
    event.status = discord.EventStatus.active   # Discord hat es zur Startzeit gestartet

    twitch.plan("tw-1")   # Twitch liefert nur kommende Termine — der laufende fehlt
    await sync(bot, now=at(1, 1))
    assert events(guild) == [event]
    assert calls(guild, "event.delete") == []
    assert await query("SELECT * FROM stream_schedule_events") == []   # vergessen, nicht gelöscht


async def test_im_server_geloeschtes_event_kommt_nicht_wieder(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await sync(bot)
    guild.events_by_id.clear()   # ein Admin löscht das Event

    twitch.plan("tw-1", segment("a", at(1), title="Anderer Titel"))
    await sync(bot)
    await sync(bot)
    assert events(guild) == []
    assert len(calls(guild, "guild.create_scheduled_event")) == 1
    [row] = await query("SELECT event_id FROM stream_schedule_events")
    assert row["event_id"] is None   # als „im Server gelöscht“ gemerkt


async def test_vorzeitig_gestartetes_event_wird_nicht_geloescht(bot, guild, twitch):
    """Admins können Events auch vor der Zeit starten — ein laufendes Event löscht der Bot nie."""
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await sync(bot)
    [event] = events(guild)
    event.status = discord.EventStatus.active

    twitch.plan("tw-1", segment("a", at(1), canceled=True))
    await sync(bot)
    assert events(guild) == [event]
    assert calls(guild, "event.delete") == []


async def test_fremde_events_bleiben_unberuehrt(bot, guild, twitch):
    own = guild.add_scheduled_event("Community-Abend", at(2), at(2, 2))
    twitch.plan("tw-1", segment("a", at(1)))
    mc_id = await follow(guild)
    await sync(bot)
    await db_stream_schedule.disable(guild.id, mc_id)
    await sync(bot)
    assert events(guild) == [own]


# ── Ausschalten, Limits, Modul ────────────────────────────────────────────────────────────────

async def test_ausschalten_loescht_die_kommenden_events(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)), segment("b", at(2)))
    mc_id = await follow(guild)
    await sync(bot)
    await db_stream_schedule.disable(guild.id, mc_id)
    await sync(bot)
    assert events(guild) == []
    assert await query("SELECT * FROM stream_schedule_events") == []
    assert twitch.schedule_calls == ["tw-1"]   # zum Aufräumen fragt der Bot Twitch nicht mehr


async def test_stream_alert_entfernt_loescht_die_events(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    mc_id = await follow(guild)
    await sync(bot)
    await db_stream_alerts.remove_subscription(guild.id, mc_id)
    await sync(bot)
    assert events(guild) == []
    assert await query("SELECT * FROM stream_schedule_events") == []


async def test_free_nur_ein_streamer(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    twitch.plan("tw-2", segment("b", at(2)), login="max")
    await follow(guild, "tw-1")
    await follow(guild, "tw-2", "Max")
    await sync(bot)
    assert [e.location for e in events(guild)] == ["https://twitch.tv/rusirda"]   # der zuerst eingeschaltete

    await set_premium(guild)
    await sync(bot)
    assert len(events(guild)) == 2

    await set_premium(guild, active=False)   # wieder Free: der zweite verliert seine Events
    await sync(bot)
    assert [e.location for e in events(guild)] == ["https://twitch.tv/rusirda"]


async def test_modul_aus_loescht_die_events(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await sync(bot)
    await db_settings.set_module(guild.id, "stream_alerts", False)
    await sync(bot)
    assert events(guild) == []


async def test_ein_abruf_pro_streamer_fuer_mehrere_server(bot, guild, twitch):
    second = await add_guild(bot)
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await follow(second)
    await sync(bot)
    assert twitch.schedule_calls == ["tw-1"]
    assert len(events(guild)) == len(events(second)) == 1


# ── Fehlerfälle ───────────────────────────────────────────────────────────────────────────────

async def test_ohne_recht_events_erstellen(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    guild.bot_role.permissions = discord.Permissions(view_channel=True, send_messages=True)
    mc_id = await follow(guild)
    await sync(bot)
    assert events(guild) == [] and guild.calls == []
    assert await status(guild, mc_id) == stream_schedule.ERR_PERMISSION
    assert twitch.schedule_calls == []


async def test_recht_entzogen_events_bleiben_gespeichert(bot, guild, twitch):
    """Ohne Recht kann der Bot nichts löschen — die Zuordnung bleibt, bis er es wieder darf."""
    twitch.plan("tw-1", segment("a", at(1)))
    mc_id = await follow(guild)
    await sync(bot)
    guild.bot_role.permissions = discord.Permissions(view_channel=True)
    await db_stream_schedule.disable(guild.id, mc_id)
    await sync(bot)
    assert len(await query("SELECT * FROM stream_schedule_events")) == 1

    guild.bot_role.permissions = discord.Permissions(create_events=True)
    await sync(bot)
    assert events(guild) == []


async def test_kein_plan_bei_twitch(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    mc_id = await follow(guild)
    await sync(bot)
    twitch.plans["tw-1"] = None   # Plan gelöscht (Twitch antwortet mit 404)
    await sync(bot)
    assert events(guild) == []
    assert await status(guild, mc_id) == stream_schedule.ERR_NO_SCHEDULE


async def test_twitch_nicht_erreichbar_events_bleiben(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    mc_id = await follow(guild)
    await sync(bot)
    assert await status(guild, mc_id) is None
    twitch.fail.add("tw-1")
    await sync(bot)
    assert len(events(guild)) == 1
    assert await status(guild, mc_id) == stream_schedule.ERR_TWITCH


async def test_100_events_erreicht_rest_folgt_spaeter(bot, guild, twitch):
    guild.max_events = 2
    guild.add_scheduled_event("Community-Abend", at(5), at(5, 1))
    twitch.plan("tw-1", segment("a", at(1)), segment("b", at(2)))
    mc_id = await follow(guild)
    await sync(bot)
    assert [e.name for e in events(guild)] == ["Elden Ring Blind-Run", "Community-Abend"]
    assert await status(guild, mc_id) == stream_schedule.ERR_DISCORD

    guild.max_events = 100
    await sync(bot)
    assert len(events(guild)) == 3
    assert await status(guild, mc_id) is None


async def test_fehler_in_einem_server_stoppt_die_anderen_nicht(bot, guild, twitch, monkeypatch):
    second = await add_guild(bot)
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    await follow(second)

    async def broken(*args, **kwargs):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(guild, "create_scheduled_event", broken)
    await sync(bot)
    assert len(events(second)) == 1


async def test_server_ohne_bot_wird_uebersprungen(bot, guild, twitch):
    twitch.plan("tw-1", segment("a", at(1)))
    await follow(guild)
    bot.guilds_by_id.clear()
    await sync(bot)
    assert twitch.schedule_calls == []


# ── Twitch-Schnittstelle ──────────────────────────────────────────────────────────────────────

async def test_get_schedule_blaettert_bis_zum_fensterende(monkeypatch):
    pages = {
        None: {"data": {"segments": [segment("a", at(1)), segment("b", at(5))], "broadcaster_login": "rusirda",
                        "vacation": None}, "pagination": {"cursor": "c1"}},
        "c1": {"data": {"segments": [segment("c", at(9)), segment("d", at(20))], "broadcaster_login": "rusirda",
                        "vacation": None}, "pagination": {"cursor": "c2"}},
        "c2": {"data": {"segments": [segment("e", at(30))]}, "pagination": {}},
    }
    requested = []

    async def helix_get(endpoint, params):
        requested.append((endpoint, dict(params)))
        return pages[params.get("after")]
    monkeypatch.setattr(twitch_api, "helix_get", helix_get)

    schedule = await twitch_api.get_schedule("tw-1", NOW, at(14))
    assert [s["id"] for s in schedule["segments"]] == ["a", "b", "c", "d"]   # Seite 3 nicht mehr nötig
    assert schedule["broadcaster_login"] == "rusirda"
    assert requested[0] == ("schedule", {"broadcaster_id": "tw-1", "start_time": "2026-11-09T12:00:00Z", "first": 25})
    assert len(requested) == 2


async def test_get_schedule_ohne_plan_gibt_none(monkeypatch):
    async def helix_get(endpoint, params):
        raise aiohttp.ClientResponseError(None, (), status=404)
    monkeypatch.setattr(twitch_api, "helix_get", helix_get)
    assert await twitch_api.get_schedule("tw-1", NOW, at(7)) is None


async def test_get_schedule_andere_fehler_kommen_durch(monkeypatch):
    async def helix_get(endpoint, params):
        raise aiohttp.ClientResponseError(None, (), status=500)
    monkeypatch.setattr(twitch_api, "helix_get", helix_get)
    with pytest.raises(aiohttp.ClientResponseError):
        await twitch_api.get_schedule("tw-1", NOW, at(7))


def test_texte_in_allen_sprachen():
    for code, lang in TRANSLATIONS.items():
        for key in ("schedule_event_title", "schedule_event_title_plain", "schedule_event_source"):
            text = lang[key].format(streamer="Rusirda", category="Elden Ring")
            assert "Rusirda" in text and "{" not in text, (code, key)


# ── Bild laden (echter HTTP-Abruf gegen einen lokalen Server) ─────────────────────────────────

async def _serve(body: bytes, status: int = 200):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    async def image(request):
        return web.Response(body=body, status=status, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/img", image)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_bild_wird_vollstaendig_geladen():
    """Twitch-Offline-Bilder sind oft mehrere hundert KB — read(n) lieferte nur den ersten Teil."""
    body = bytes(range(256)) * 2400   # ~600 KB
    server = await _serve(body)
    try:
        assert await stream_schedule.download_image(str(server.make_url("/img"))) == body
    finally:
        await server.close()


async def test_zu_grosses_bild_und_fehlerstatus(monkeypatch):
    monkeypatch.setattr(stream_schedule, "MAX_IMAGE_BYTES", 1000)
    server = await _serve(b"x" * 1001)
    missing = await _serve(b"", status=404)
    try:
        assert await stream_schedule.download_image(str(server.make_url("/img"))) is None
        assert await stream_schedule.download_image(str(missing.make_url("/img"))) is None
    finally:
        await server.close()
        await missing.close()
