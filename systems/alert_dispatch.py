"""Gemeinsame Alert-Pipeline für Twitch, Kick und YouTube.

Pro Plattform bleibt nur das Plattform-Spezifische (Event-Parsing, API-Calls, Live-Embed).
Alles andere läuft hier an einer Stelle: Limit-Prüfung, Duplikat-Unterdrückung,
Kanal-Auflösung, Ping-Rolle, Senden (inkl. AllowedMentions) und Parallelisierung.
"""
import asyncio
import logging
from typing import Callable

import discord

from cogs.utils import get_lang
from config import NEXUS_COLOR, NEXUS_FOOTER
from database import db_limits, db_stream_alerts
from systems.premium import is_premium

log = logging.getLogger(__name__)

# Max. parallel verarbeitete Subscriptions pro Event (DB-Pool hat maxsize=25)
ALERT_CONCURRENCY = 10

PLATFORM_LABELS = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}

# (lang) -> fertiges Live-Embed der jeweiligen Plattform
LiveEmbedBuilder = Callable[[dict], discord.Embed]

def build_offline_embed(lang: dict, streamer_name: str) -> discord.Embed:
    """Offline-Embed ist für alle Plattformen identisch."""
    return discord.Embed(
        title=lang.get("stream_offline_title", "⚫ Stream ended"),
        description=lang.get("stream_offline_desc", "**{streamer}** has gone offline.").format(
            streamer=streamer_name
        ),
        color=NEXUS_COLOR,
    )

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
        return await channel.send(content=content, embed=embed, allowed_mentions=allowed)
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
            )

            if msg and is_live and dedupe:
                await db_stream_alerts.mark_alert_posted(sub["id"])
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
    dedupe: bool = True,
) -> list[discord.Message]:
    """Schickt den Alert an alle Subscriptions (parallel, begrenzt durch ALERT_CONCURRENCY).

    platform:         "twitch" | "kick" | "youtube" (bestimmt Limit-Key, Ping-Rolle, Logs)
    build_live_embed: lang -> Embed, nur nötig bei is_live=True
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
            dedupe=dedupe,
            sem=sem,
        )
        for sub in subs
    ))
    return [m for m in results if m is not None]