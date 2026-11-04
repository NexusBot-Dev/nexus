"""Honeypot: gemeinsame Logik für /honeypot, das Dashboard und den Listener (cogs/honeypot.py).

Alles, was in Discord landet (Warn-Nachricht, Standardtitel), nutzt die Bot-Sprache des Servers —
Aufrufer aus dem Dashboard übergeben dafür get_lang(guild_id), nicht die Dashboard-Sprache.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta

import discord

from database import db_honeypot, db_settings
from systems.hierarchy import PermCheck, can_act_on_member, can_apply_overwrites

log = logging.getLogger(__name__)

BAN_DELETE_SECONDS = 24 * 60 * 60   # Ban löscht die Nachrichten der letzten 24 h (Spam steht meist in mehreren Channels)


# ─── Kleinkram ───────────────────────────────────────────────────────────────

def slugify_channel_name(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    slug = slug.strip("-") or "honeypot"
    return slug[:90]

def default_title(lang: dict) -> str:
    return lang.get("honeypot_channel_title", "⚠️ Warning")

def counter_footer(lang: dict, catches: int) -> str:
    return lang.get("honeypot_counter", "Nexus • Honeypot • 🍯 {count} caught").format(count=f"{catches:,}")

def punishment_label(lang: dict, action: str, days: int) -> str:
    """Kurzer Text für eine Bestrafung, z. B. "Timeout (7 Tage)", "Kick", "Ban"."""
    if action == "timeout":
        return lang.get("honeypot_punish_timeout", "Timeout ({days} days)").format(days=days)
    if action == "ban":
        return lang.get("honeypot_punish_ban", "Ban")
    return lang.get("honeypot_punish_kick", "Kick")


# ─── Zustand ─────────────────────────────────────────────────────────────────

async def get_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Der Honeypot-Channel — oder None. Wurde der Channel von Hand gelöscht, gilt das wie
    "kein Honeypot": Die gespeicherte ID wird entfernt, damit /honeypot wieder "Erstellen" zeigt."""
    settings = await db_settings.get_guild(guild.id)
    channel_id = settings.get("honeypot_channel_id") if settings else None
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        log.info("[HONEYPOT] Channel %s auf %s existiert nicht mehr — Honeypot zurückgesetzt.", channel_id, guild.name)
        await db_settings.update_guild(guild.id, honeypot_channel_id=None)
        await db_honeypot.update_honeypot_settings(guild.id, message_id=None)
    return channel

async def find_message(guild: discord.Guild, channel: discord.TextChannel, hp: dict) -> discord.Message | None:
    """Die Warn-Nachricht im Honeypot-Channel — oder None, wenn es keine gibt.

    Ältere Honeypots haben keine message_id gespeichert: Dann wird die erste Embed-Nachricht des Bots
    im Channel gesucht und ihre ID gespeichert. discord.HTTPException (z. B. Forbidden) wird
    weitergereicht — dann ist die Nachricht gerade nicht lesbar, nicht "nicht vorhanden"."""
    if hp.get("message_id"):
        try:
            message = await channel.fetch_message(hp["message_id"])
            if message.embeds:
                return message
        except discord.NotFound:
            pass

    async for candidate in channel.history(limit=10, oldest_first=True):
        if candidate.author.id == guild.me.id and candidate.embeds:
            await db_honeypot.update_honeypot_settings(guild.id, message_id=candidate.id)
            return candidate
    return None

async def load_texts(guild: discord.Guild, channel: discord.TextChannel | None, hp: dict, lang: dict) -> tuple[str, str]:
    """Titel und Text der Warn-Nachricht.

    Migration ohne Skript: Honeypots von vor diesem Update haben warn_title/warn_text = NULL. Beim
    ersten Aufruf liest Nexus sie einmalig aus der bestehenden Nachricht und speichert sie. Ist die
    Nachricht weg, wird der Standard gespeichert; ist sie gerade nicht lesbar, wird es beim nächsten
    Mal erneut versucht."""
    if hp.get("warn_title") is not None or hp.get("warn_text") is not None:
        return hp.get("warn_title") or default_title(lang), hp.get("warn_text") or ""

    title, text = default_title(lang), ""
    if channel is None:
        return title, text

    try:
        message = await find_message(guild, channel, hp)
    except discord.HTTPException:   # u. a. Forbidden: später erneut versuchen
        return title, text
    if message is not None:
        embed = message.embeds[0]
        title = (embed.title or title)[:db_honeypot.WARN_TITLE_MAX]
        text = (embed.description or "")[:db_honeypot.WARN_TEXT_MAX]

    await db_honeypot.update_honeypot_settings(guild.id, warn_title=title, warn_text=text)
    if message is not None:
        log.info("[HONEYPOT] Warn-Texte auf %s aus der bestehenden Nachricht übernommen.", guild.name)
    else:
        log.info("[HONEYPOT] Keine Warn-Nachricht auf %s gefunden — Standardtitel gespeichert.", guild.name)
    return title, text


# ─── Anlegen / Bearbeiten / Entfernen ────────────────────────────────────────

class HoneypotError(Exception):
    """code: exists | perms (Details in check) | forbidden"""

    def __init__(self, code: str, check: PermCheck | None = None):
        super().__init__(code)
        self.code = code
        self.check = check

async def create(guild: discord.Guild, name: str, title: str, text: str, lang: dict) -> discord.TextChannel:
    """Legt Channel und Warn-Nachricht an und speichert alles. lang: Bot-Sprache des Servers."""
    if await get_channel(guild) is not None:
        raise HoneypotError("exists")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True,
            embed_links=True, read_message_history=True,
        ),
    }
    check = can_apply_overwrites(guild, overwrites)
    if not check:
        raise HoneypotError("perms", check)

    try:
        channel = await guild.create_text_channel(
            name=slugify_channel_name(name), overwrites=overwrites, reason="[Nexus Honeypot] Erstellt",
        )
    except discord.Forbidden:
        raise HoneypotError("forbidden") from None
    try:
        await channel.edit(position=0)
    except discord.HTTPException:
        pass

    title = title.strip() or default_title(lang)
    hp = await db_honeypot.get_honeypot_settings(guild.id)
    embed = discord.Embed(title=title, description=text, color=await db_settings.get_color(guild.id))
    embed.set_footer(text=counter_footer(lang, hp["catches"]))
    message_id = None
    try:
        message_id = (await channel.send(embed=embed)).id
    except discord.HTTPException:
        log.warning("[HONEYPOT] Konnte Warn-Embed nicht in %s posten.", channel)

    await db_settings.update_guild(guild.id, honeypot_channel_id=channel.id)
    await db_honeypot.update_honeypot_settings(guild.id, message_id=message_id, warn_title=title, warn_text=text)
    return channel

async def update_texts(guild: discord.Guild, title: str, text: str, lang: dict) -> str:
    """Speichert Titel/Text und passt die Warn-Nachricht an (Footer mit Zähler und Farbe bleiben).

    Rückgabe: "updated" | "reposted" (Nachricht fehlte, neu gepostet) | "saved_only" (Channel/Rechte fehlen)."""
    title = title.strip() or default_title(lang)
    await db_honeypot.update_honeypot_settings(guild.id, warn_title=title, warn_text=text)

    channel = await get_channel(guild)
    if channel is None:
        return "saved_only"
    hp = await db_honeypot.get_honeypot_settings(guild.id)

    try:
        message = await find_message(guild, channel, hp)
    except discord.HTTPException:
        return "saved_only"

    try:
        if message is not None:
            embed = message.embeds[0].copy()
            embed.title, embed.description = title, text
            await message.edit(embed=embed)
            return "updated"
        embed = discord.Embed(title=title, description=text, color=await db_settings.get_color(guild.id))
        embed.set_footer(text=counter_footer(lang, hp["catches"]))
        sent = await channel.send(embed=embed)
    except discord.HTTPException:
        log.warning("[HONEYPOT] Warn-Nachricht auf %s konnte nicht angepasst werden.", guild.name)
        return "saved_only"
    await db_honeypot.update_honeypot_settings(guild.id, message_id=sent.id)
    return "reposted"

async def cleanup(guild: discord.Guild, honeypot_channel_id: int | None) -> None:
    """Löscht den Channel und setzt den Honeypot zurück. Fänge, Texte und Bestrafung bleiben gespeichert."""
    if honeypot_channel_id is None:
        return
    channel = guild.get_channel(honeypot_channel_id)
    if channel is not None:
        try:
            await channel.delete(reason="[Nexus Honeypot] Deaktiviert")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.warning("[HONEYPOT] Konnte Honeypot-Channel %s auf %s nicht löschen.", honeypot_channel_id, guild.name)
    await db_settings.update_guild(guild.id, honeypot_channel_id=None)
    await db_honeypot.update_honeypot_settings(guild.id, message_id=None)


# ─── Bestrafen ───────────────────────────────────────────────────────────────

@dataclass
class PunishResult:
    applied: bool
    check: PermCheck | None = None   # gesetzt, wenn Hierarchie/Rechte es verhindert haben

_ACTION_PERMS = {"timeout": "moderate_members", "kick": "kick_members", "ban": "ban_members"}

async def punish(guild: discord.Guild, member: discord.Member, action: str, days: int) -> PunishResult:
    """Wendet die eingestellte Bestrafung an (timeout | kick | ban)."""
    check = can_act_on_member(guild, member, perms=(_ACTION_PERMS[action],), is_timeout=action == "timeout")
    if not check:
        return PunishResult(False, check)
    try:
        if action == "timeout":
            until = discord.utils.utcnow() + timedelta(days=days)
            await member.timeout(until, reason="[Nexus Honeypot] Nachricht im Honeypot-Channel gepostet")
        elif action == "kick":
            await guild.kick(member, reason="[Nexus Honeypot] Nachricht im Honeypot-Channel gepostet")
        else:
            await guild.ban(
                member, reason="[Nexus Honeypot] Nachricht im Honeypot-Channel gepostet",
                delete_message_seconds=BAN_DELETE_SECONDS,
            )
    except discord.NotFound:
        log.info("[HONEYPOT] %s war auf %s schon nicht mehr auf dem Server.", member, guild.name)
        return PunishResult(False)
    except discord.HTTPException:
        log.warning("[HONEYPOT] %s für %s auf %s fehlgeschlagen.", action, member, guild.name, exc_info=True)
        return PunishResult(False)
    return PunishResult(True)
