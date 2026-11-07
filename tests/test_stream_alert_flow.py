"""Ablauf eines Stream-Alerts: live → (kurzer Abbruch) → offline → Schonfrist → Nachricht wird zu "Stream beendet".

Echte Module (integrations/twitch.py, kick.py, youtube_polling.py, systems/alert_dispatch.py) gegen die
echte Datenbank; nur die Plattform-APIs und Discord sind nachgebaut. Die Schonfrist (3 Minuten) wird nicht
abgewartet, sondern in der Datenbank zurückdatiert.
"""
from datetime import timedelta

import discord
import pytest

import integrations.kick as kick_module
import integrations.twitch as twitch_module
import integrations.youtube_polling as youtube_module
from database import db_stream_alerts
from systems import alert_dispatch
from tests.conftest import query, query_one, t
from tests.fakes import message_id


@pytest.fixture
def live(guild, bot, monkeypatch):
    """Server mit #live und je einem abonnierten Streamer pro Plattform; Plattform-APIs nachgebaut."""
    guild.live = guild.add_text_channel("live")

    async def no_details(*args):
        return None
    monkeypatch.setattr(twitch_module, "get_stream_by_user_id", no_details)

    async def kick_details(broadcaster_id):
        return {"title": "Kick-Stream", "thumbnail": None}
    monkeypatch.setattr(kick_module.systems.kick_api, "get_kick_livestream", kick_details)
    return guild


async def subscribe(guild, platform, external_id, name, message=None):
    monitored = await db_stream_alerts.get_or_create_monitored_channel(platform, external_id, name)
    await db_stream_alerts.add_subscription(guild.id, guild.live.id, monitored["id"], message)
    return monitored


def twitch_event(kind, login="shroud", user_id="tw-1"):
    return {"subscription": {"type": f"stream.{kind}"},
            "event": {"broadcaster_user_id": user_id, "broadcaster_user_login": login, "broadcaster_user_name": login}}


async def end_grace_period():
    """So tun, als wäre die Schonfrist (3 Minuten) seit dem Offline-Event vorbei."""
    await query("UPDATE monitored_channels SET offline_since = NOW() - INTERVAL %s SECOND WHERE offline_since IS NOT NULL",
                alert_dispatch.OFFLINE_GRACE_SECONDS + 20)


async def backdate(guild, message, minutes):
    """Die Live-Nachricht 'vor X Minuten' gepostet — daraus berechnet der Bot die Stream-Dauer."""
    old_id = message.id
    message.id = message_id(discord.utils.utcnow() - timedelta(minutes=minutes))
    message.channel.messages[message.id] = message.channel.messages.pop(old_id)
    await query("UPDATE guild_subscriptions SET live_message_id = %s WHERE live_message_id = %s", message.id, old_id)


def sent(guild):
    return [c[2] for c in guild.calls if c[0] == "channel.send"]


def edits(guild):
    return [c for c in guild.calls if c[0] == "message.edit"]


async def state(monitored):
    return await query_one("SELECT live, offline_since IS NOT NULL AS pending FROM monitored_channels WHERE id = %s",
                           monitored["id"])


# ── Twitch ──────────────────────────────────────────────────────────────────────────────────────

async def test_live_alert(live, bot):
    await subscribe(live, "twitch", "tw-1", "shroud", message="Los geht's!")
    ping = live.add_role("Live-Ping", 2)
    await db_stream_alerts.set_ping_role(live.id, "all", ping.id)

    await twitch_module.Twitch(bot).twitch_process_notification(twitch_event("online"))

    [message] = sent(live)
    assert message.content == f"<@&{ping.id}> Los geht's!"
    assert message.allowed_mentions.roles == [discord.Object(id=ping.id)]   # nur die Ping-Rolle darf pingen
    assert message.allowed_mentions.everyone is False
    assert message.embeds[0].title == t("stream_live_title", streamer="shroud")
    [button] = message.view.children
    assert (button.url, button.label) == ("https://twitch.tv/shroud", t("stream_watch_button"))
    row = await query_one("SELECT live_message_id, live_message_channel_id FROM guild_subscriptions")
    assert row == {"live_message_id": message.id, "live_message_channel_id": live.live.id}


async def test_offline_nach_schonfrist_bearbeitet_nachricht(live, bot):
    monitored = await subscribe(live, "twitch", "tw-1", "shroud")
    twitch = twitch_module.Twitch(bot)
    await twitch.twitch_process_notification(twitch_event("online"))
    [message] = sent(live)
    await backdate(live, message, minutes=134)

    await twitch.twitch_process_notification(twitch_event("offline"))
    assert (await state(monitored)) == {"live": 1, "pending": 1}
    await alert_dispatch.finish_ended_streams(bot)
    assert edits(live) == []   # Schonfrist läuft noch

    await end_grace_period()
    await alert_dispatch.finish_ended_streams(bot)
    embed = message.embeds[0]
    assert (embed.title, embed.description) == (t("stream_offline_title"), t("stream_offline_desc", streamer="shroud"))
    # 2:14 h gepostet, Offline-Event vor gut 3 Minuten → gezählt wird bis zum Offline-Event
    assert [(f.name, f.value) for f in embed.fields] == [(t("stream_duration"), "2:10 h")]
    assert len(sent(live)) == 1   # bearbeitet, nicht neu gepostet
    assert (await state(monitored)) == {"live": 0, "pending": 0}
    assert (await query_one("SELECT live_message_id FROM guild_subscriptions"))["live_message_id"] is None

    await alert_dispatch.finish_ended_streams(bot)
    assert len(edits(live)) == 1   # nichts doppelt


async def test_kurzer_abbruch_kein_neuer_ping(live, bot):
    monitored = await subscribe(live, "twitch", "tw-1", "shroud")
    twitch = twitch_module.Twitch(bot)
    await twitch.twitch_process_notification(twitch_event("online"))
    await twitch.twitch_process_notification(twitch_event("offline"))
    await twitch.twitch_process_notification(twitch_event("online"))   # innerhalb der Schonfrist zurück

    assert len(sent(live)) == 1
    assert (await state(monitored)) == {"live": 1, "pending": 0}
    await end_grace_period()
    await alert_dispatch.finish_ended_streams(bot)
    assert edits(live) == []

    # Späteres echtes Ende bearbeitet dieselbe Nachricht
    await twitch.twitch_process_notification(twitch_event("offline"))
    await end_grace_period()
    await alert_dispatch.finish_ended_streams(bot)
    [edit] = edits(live)
    assert edit[1] == sent(live)[0].id


async def test_verpasstes_offline_event(live, bot):
    """Bot war beim Offline-Event down: Beim nächsten Stream wird die alte Nachricht als beendet markiert."""
    await subscribe(live, "twitch", "tw-1", "shroud")
    twitch = twitch_module.Twitch(bot)
    await twitch.twitch_process_notification(twitch_event("online"))
    await twitch.twitch_process_notification(twitch_event("online"))

    old, new = sent(live)
    assert old.embeds[0].title == t("stream_offline_title")
    assert old.embeds[0].fields == []   # Dauer unbekannt
    assert (await query_one("SELECT live_message_id FROM guild_subscriptions"))["live_message_id"] == new.id


async def test_geloeschte_live_nachricht(live, bot):
    monitored = await subscribe(live, "twitch", "tw-1", "shroud")
    twitch = twitch_module.Twitch(bot)
    await twitch.twitch_process_notification(twitch_event("online"))
    live.live.messages.clear()   # jemand hat den Alert gelöscht

    await twitch.twitch_process_notification(twitch_event("offline"))
    await end_grace_period()
    await alert_dispatch.finish_ended_streams(bot)   # kein Fehler
    assert (await state(monitored)) == {"live": 0, "pending": 0}
    assert (await query_one("SELECT live_message_id FROM guild_subscriptions"))["live_message_id"] is None


async def test_bot_nicht_mehr_auf_dem_server(live, bot):
    await subscribe(live, "twitch", "tw-1", "shroud")
    del bot.guilds_by_id[live.id]
    await twitch_module.Twitch(bot).twitch_process_notification(twitch_event("online"))
    assert sent(live) == []


async def test_limit_ueberschritten_kein_alert(live, bot):
    """Mehr Abos als das Limit (z. B. nach abgelaufenem Premium): nur die ältesten bekommen Alerts."""
    await subscribe(live, "twitch", "tw-1", "shroud")
    await subscribe(live, "twitch", "tw-2", "pokimane")
    twitch = twitch_module.Twitch(bot)
    await twitch.twitch_process_notification(twitch_event("online", "pokimane", "tw-2"))
    assert sent(live) == []
    await twitch.twitch_process_notification(twitch_event("online", "shroud", "tw-1"))
    assert len(sent(live)) == 1


# ── Kick & YouTube ──────────────────────────────────────────────────────────────────────────────

async def test_kick(live, bot, monkeypatch):
    monitored = await subscribe(live, "kick", "k-1", "Trainwreck")
    kick = kick_module.Kick(bot)
    event = {"broadcaster": {"user_id": "k-1"}}
    await kick.kick_process_notification({**event, "is_live": True})
    await kick.kick_process_notification({**event, "is_live": False})
    await kick.kick_process_notification({**event, "is_live": True})

    [message] = sent(live)
    assert message.view.children[0].url == "https://kick.com/trainwreck"
    assert message.embeds[0].description == "Kick-Stream"

    await kick.kick_process_notification({**event, "is_live": False})
    await end_grace_period()
    await alert_dispatch.finish_ended_streams(bot)
    assert message.embeds[0].title == t("stream_offline_title")
    assert (await state(monitored)) == {"live": 0, "pending": 0}


async def test_youtube_ohne_schonfrist(live, bot, monkeypatch):
    monitored = await subscribe(live, "youtube", "UCyt1", "MKBHD")
    await db_stream_alerts.set_youtube_playlist_id(monitored["id"], "UUyt1")
    status = {"video_id": "v1", "live_broadcast_content": "live", "title": "Live!"}

    async def video_status(playlist_id):
        return status
    monkeypatch.setattr(youtube_module, "get_latest_video_status", video_status)
    poller = object.__new__(youtube_module.YoutubePolling)   # ohne den 15-Minuten-Loop zu starten
    poller.bot = bot

    await poller._check_channel(await db_stream_alerts.get_monitored_channel_by_id(monitored["id"]))
    [message] = sent(live)
    assert message.view.children[0].url == "https://youtube.com/watch?v=v1"

    status["live_broadcast_content"] = "none"
    await poller._check_channel(await db_stream_alerts.get_monitored_channel_by_id(monitored["id"]))
    assert message.embeds[0].title == t("stream_offline_title")   # sofort, ohne Schonfrist
    assert len(sent(live)) == 1
    assert (await state(monitored)) == {"live": 0, "pending": 0}


# ── Hinweis bei /alert_add und /alert_edit ──────────────────────────────────────────────────────

def test_hinweis_wenn_bot_im_alert_kanal_nicht_schreiben_darf(guild):
    from cogs.stream_alerts import channel_warning
    from translations import TRANSLATIONS

    lang = TRANSLATIONS["de"]
    assert channel_warning(lang, guild.add_text_channel("offen")) == ""
    assert channel_warning(lang, None) == ""

    readonly = guild.add_text_channel("nur-lesen", bot_denied={"send_messages"})
    assert channel_warning(lang, readonly) == "\n" + t("alert_channel_missing_perms", channel=readonly.mention, perms="`Send Messages`")

    hidden = guild.add_text_channel("versteckt", bot_denied={"view_channel", "send_messages", "embed_links"})
    assert "`View Channel`, `Send Messages`, `Embed Links`" in channel_warning(lang, hidden)
