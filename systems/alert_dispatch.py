"""Gemeinsame Alert-Pipeline für Twitch, Kick und YouTube.

Pro Plattform bleibt nur das Plattform-Spezifische (Event-Parsing, API-Calls, Live-Embed).
Alles andere läuft hier an einer Stelle: Limit-Prüfung, Duplikat-Unterdrückung,
Kanal-Auflösung, Ping-Rolle, Senden (inkl. AllowedMentions) und Parallelisierung.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

import discord

from cogs.utils import get_lang
from config import NEXUS_COLOR, NEXUS_FOOTER
from database import db_limits, db_stream_alerts
from systems.premium import is_premium

log = logging.getLogger(__name__)

# Max. parallel verarbeitete Subscriptions pro Event (DB-Pool hat maxsize=25)
ALERT_CONCURRENCY = 10

# So lange darf ein Stream offline sein, bevor er als beendet gilt. Kommt er vorher zurück
# (Verbindungsabbruch), bleibt es derselbe Stream: keine neue Nachricht, kein neuer Ping.
OFFLINE_GRACE_SECONDS = 180

PLATFORM_LABELS = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}

# Ohne diese Rechte im Ziel-Kanal kommt kein Alert an (Nachricht mit Embed und Button)
ALERT_CHANNEL_PERMS = ("view_channel", "send_messages", "embed_links")

# (lang) -> fertiges Live-Embed der jeweiligen Plattform
LiveEmbedBuilder = Callable[[dict], discord.Embed]

def build_offline_embed(lang: dict, streamer_name: str, duration: timedelta | None = None) -> discord.Embed:
    """Offline-Embed ist für alle Plattformen identisch."""
    embed = discord.Embed(
        title=lang.get("stream_offline_title", "⚫ Stream ended"),
        description=lang.get("stream_offline_desc", "**{streamer}** has gone offline.").format(
            streamer=streamer_name
        ),
        color=NEXUS_COLOR,
    )
    if duration is not None and duration.total_seconds() > 0:
        minutes = int(duration.total_seconds()) // 60
        embed.add_field(name=lang.get("stream_duration", "⏱️ Duration"), value=f"{minutes // 60}:{minutes % 60:02d} h")
    return embed

def build_watch_view(lang: dict, url: str) -> discord.ui.View:
    """Link-Button zum Stream. Link-Buttons lösen keine Interaktion aus — kein Handler, übersteht Neustarts."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.link, url=url, label=lang.get("stream_watch_button", "Watch stream"),
    ))
    return view

async def _resolve_channel(bot, sub: dict):
    # 1. Globaler Cache (extrem schnell, 0 REST-Calls)
    target = bot.get_channel(int(sub["channel_id"]))
    if target is not None:
        return target

    # 2. Nur als Fallback per REST holen
    try:
        return await bot.fetch_channel(int(sub["channel_id"]))
    except discord.NotFound:
        log.warning("Kanal %s existiert nicht mehr.", sub["channel_id"])
    except discord.Forbidden:
        log.warning("Kein Zugriff auf Kanal %s (Guild %s).", sub["channel_id"], sub["guild_id"])
    except discord.HTTPException:
        log.exception("Fehler beim Abrufen des Kanals %s via API.", sub["channel_id"])
    return None

async def send_alert_message(
    channel,
    embed: discord.Embed,
    *,
    is_live: bool,
    ping_role_id: int | None,
    custom_message: str | None,
    view: discord.ui.View | None = None,
) -> discord.Message | None:
    content_parts = []
    if is_live and ping_role_id:
        content_parts.append(f"<@&{ping_role_id}>")
    if is_live and custom_message:
        content_parts.append(custom_message)
    content = " ".join(content_parts) if content_parts else None

    # Nur die konfigurierte Ping-Rolle darf pingen, kein @everyone/@here, keine User-Mentions
    allowed = discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=ping_role_id)] if (is_live and ping_role_id) else False,
    )

    try:
        return await channel.send(content=content, embed=embed, allowed_mentions=allowed, view=view)
    except discord.Forbidden:
        log.warning("Fehlende Berechtigungen zum Senden von Alerts in Kanal %s", channel.id)
    except Exception:
        log.exception("Konnte Stream-Alert nicht in %s posten.", channel)
    return None

async def _process_subscription(
    bot,
    sub: dict,
    *,
    platform: str,
    is_live: bool,
    streamer_name: str,
    build_live_embed: LiveEmbedBuilder | None,
    stream_url: str | None,
    dedupe: bool,
    sem: asyncio.Semaphore,
) -> discord.Message | None:
    """Verarbeitet eine einzelne Subscription. Fehler bleiben auf diese Subscription begrenzt."""
    label = PLATFORM_LABELS.get(platform, platform)
    async with sem:
        try:
            guild_id = sub["guild_id"]

            if bot.get_guild(guild_id) is None:
                log.warning("Bot ist nicht mehr auf Guild %s. Überspringe.", guild_id)
                return None

            premium = await is_premium(guild_id)
            limit = await db_limits.get_limit(guild_id, f"stream_alerts_{platform}", premium)
            active = await db_stream_alerts.is_subscription_active(
                guild_id, platform, sub["monitored_channel_id"], limit
            )
            if not active:
                log.info(
                    "%s-Alert für Guild %s übersprungen (Limit überschritten, Premium-Status: %s).",
                    label, guild_id, premium,
                )
                return None

            if dedupe and is_live and await db_stream_alerts.should_suppress_duplicate(sub["id"]):
                log.info(
                    "Live-Alert für Subscription %s (Guild %s) unterdrückt — verlinkte Plattform hat gerade erst gepostet.",
                    sub["id"], guild_id,
                )
                return None

            target = await _resolve_channel(bot, sub)
            if target is None:
                return None

            lang = await get_lang(guild_id)
            ping_role_id = await db_stream_alerts.get_ping_role(guild_id, platform) if is_live else None

            embed = build_live_embed(lang) if is_live else build_offline_embed(lang, streamer_name)
            embed.set_footer(text=NEXUS_FOOTER)

            msg = await send_alert_message(
                target,
                embed,
                is_live=is_live,
                ping_role_id=ping_role_id,
                custom_message=sub.get("message"),
                view=build_watch_view(lang, stream_url) if (is_live and stream_url) else None,
            )

            if msg and is_live:
                # Eigener try: Die Nachricht ist schon gesendet — ein Fehler hier darf sie nicht "verlieren"
                # (sonst bekommt z. B. Kick sie nicht zurück und lädt kein Vorschaubild nach)
                try:
                    if dedupe:
                        await db_stream_alerts.mark_alert_posted(sub["id"])
                    await db_stream_alerts.set_live_message(sub["id"], msg.channel.id, msg.id)
                    # Alte Live-Nachricht, deren Ende nie ankam (z. B. Bot war beim Offline-Event down)
                    if sub.get("live_message_id") and sub.get("live_message_channel_id"):
                        await _end_message(bot, sub["live_message_channel_id"], sub["live_message_id"], lang, streamer_name)
                except Exception:
                    log.exception("%s-Alert für Subscription %s gesendet, aber Speichern der Nachricht fehlgeschlagen.",
                                  label, sub.get("id"))
            return msg
        except Exception:
            log.exception("%s-Alert für Subscription %s fehlgeschlagen.", label, sub.get("id"))
            return None

async def dispatch_alerts(
    bot,
    *,
    platform: str,
    subs: list[dict],
    is_live: bool,
    streamer_name: str,
    build_live_embed: LiveEmbedBuilder | None = None,
    stream_url: str | None = None,
    dedupe: bool = True,
) -> list[discord.Message]:
    """Schickt den Alert an alle Subscriptions (parallel, begrenzt durch ALERT_CONCURRENCY).

    platform:         "twitch" | "kick" | "youtube" (bestimmt Limit-Key, Ping-Rolle, Logs)
    build_live_embed: lang -> Embed, nur nötig bei is_live=True
    stream_url:       Ziel des "Zum Stream"-Buttons unter dem Live-Alert
    dedupe:           plattformübergreifende Duplikat-Unterdrückung nutzen (should_suppress_duplicate
                      + mark_alert_posted)

    Gibt die erfolgreich gesendeten Nachrichten zurück.
    """
    await bot.wait_until_ready()
    sem = asyncio.Semaphore(ALERT_CONCURRENCY)
    results = await asyncio.gather(*(
        _process_subscription(
            bot,
            sub,
            platform=platform,
            is_live=is_live,
            streamer_name=streamer_name,
            build_live_embed=build_live_embed,
            stream_url=stream_url,
            dedupe=dedupe,
            sem=sem,
        )
        for sub in subs
    ))
    sent = [m for m in results if m is not None]
    log.info("%s-%s-Alert für %s: %d von %d Abo(s) gesendet.", PLATFORM_LABELS.get(platform, platform),
             "Live" if is_live else "Offline", streamer_name, len(sent), len(subs))
    return sent

# ─── Stream-Ende: Live-Nachricht bearbeiten statt neu posten ──────────────────

async def _end_message(bot, channel_id: int, message_id: int, lang: dict, streamer_name: str,
                       ended_at: datetime | None = None):
    """Macht aus der Live-Nachricht die Offline-Nachricht. Gelöschte Nachricht / fehlende Rechte: still überspringen."""
    duration = ended_at - discord.utils.snowflake_time(message_id) if ended_at else None
    embed = build_offline_embed(lang, streamer_name, duration)
    embed.set_footer(text=NEXUS_FOOTER)
    try:
        # attachments=[] entfernt ein hochgeladenes Vorschaubild (Kick); Inhalt (Ping, eigene Nachricht)
        # und Button bleiben — ein Edit pingt nicht erneut.
        await bot.get_partial_messageable(int(channel_id)).get_partial_message(int(message_id)).edit(
            embed=embed, attachments=[],
        )
    except (discord.NotFound, discord.Forbidden):
        pass
    except discord.HTTPException:
        log.exception("Konnte Live-Nachricht %s nicht als beendet markieren.", message_id)

async def _finish_stream(bot, monitored: dict, offline_for: int):
    """Stream gilt als beendet: alle Live-Nachrichten bearbeiten. Dauer bis zum Offline-Event, nicht bis jetzt."""
    live_messages = await db_stream_alerts.get_live_messages(monitored["id"])
    # Erst nach dem Lesen beanspruchen: Ein neuer Stream danach setzt eigene Nachrichten, die wir nicht anfassen
    if not await db_stream_alerts.claim_stream_end(monitored["id"], monitored["offline_since"]):
        return
    ended_at = discord.utils.utcnow() - timedelta(seconds=offline_for or 0)
    for sub in live_messages:
        try:
            lang = await get_lang(sub["guild_id"])
            await _end_message(bot, sub["live_message_channel_id"], sub["live_message_id"], lang,
                               monitored["streamer_name"], ended_at)
            await db_stream_alerts.clear_live_message(sub["id"], sub["live_message_id"])
        except Exception:
            log.exception("Stream-Ende für Subscription %s fehlgeschlagen.", sub["id"])

async def stream_went_offline(monitored_channel_id: int):
    """Offline-Event: nur vormerken — beendet wird erst nach OFFLINE_GRACE_SECONDS (finish_ended_streams)."""
    await db_stream_alerts.mark_offline_pending(monitored_channel_id)

async def stream_came_back(monitored_channel_id: int) -> bool:
    """Online-Event: True, wenn der Stream innerhalb der Schonfrist zurück ist — dann keinen neuen Alert senden."""
    return await db_stream_alerts.resume_if_offline_pending(monitored_channel_id)

async def finish_ended_streams(bot):
    """Regelmäßig aufrufen: beendet alle Streams, deren Schonfrist abgelaufen ist (auch nach einem Neustart)."""
    for monitored in await db_stream_alerts.get_streams_past_grace(OFFLINE_GRACE_SECONDS):
        try:
            await _finish_stream(bot, monitored, monitored["offline_for"])
        except Exception:
            log.exception("Stream-Ende für Kanal %s fehlgeschlagen.", monitored["id"])

async def end_stream_now(bot, monitored_channel_id: int):
    """Ohne Schonfrist beenden (YouTube: wird nur alle 15 Minuten abgefragt, ein Abbruch fällt dort kaum auf)."""
    await db_stream_alerts.mark_offline_pending(monitored_channel_id)
    monitored = await db_stream_alerts.get_monitored_channel_by_id(monitored_channel_id)
    if monitored and monitored.get("offline_since"):
        await _finish_stream(bot, monitored, 0)
