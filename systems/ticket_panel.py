"""Ticket-Panel: ein Panel pro Server, gespeichert in ticket_settings.

Gemeinsam genutzt von cogs/tickets.py (Slash-Command, Kontextmenü) und dashboard/tickets.py.
Die Buttons tragen die category_id in der custom_id, das Panel funktioniert also auch nach
einem Neustart, ohne dass eine View im Speicher liegt.
"""
import logging

import discord

from config import NEXUS_FOOTER
from database import db_settings, db_ticket_categories, db_ticket_settings
from systems.hierarchy import can_use_channel

log = logging.getLogger(__name__)

PANEL_BUTTON_PREFIX = "nexus_ticket_open_"


class PanelError(Exception):
    """code: no_categories | no_channel | no_permission | not_a_panel"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_panel_embed(title: str, description: str, color: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=NEXUS_FOOTER)
    return embed


def build_panel_view(categories: list[dict]) -> discord.ui.View | None:
    """Button-Reihe fürs Panel. None, wenn es keine Kategorien gibt (dann ohne Buttons)."""
    if not categories:
        return None
    view = discord.ui.View(timeout=None)
    for cat in categories[:25]:
        view.add_item(discord.ui.Button(
            label=cat["name"][:80],  # Discord-Button-Label-Limit
            style=discord.ButtonStyle.primary,
            custom_id=f"{PANEL_BUTTON_PREFIX}{cat['id']}",
        ))
    return view


def is_ticket_panel(message: discord.Message, bot_user_id: int) -> bool:
    """Nachricht von Nexus mit mindestens einem Ticket-Button."""
    if message.author.id != bot_user_id:
        return False
    for row in message.components:
        for child in getattr(row, "children", []):
            if (getattr(child, "custom_id", None) or "").startswith(PANEL_BUTTON_PREFIX):
                return True
    return False


async def _fetch_stored_panel(guild: discord.Guild, settings: dict) -> tuple[discord.Message | None, bool]:
    """(Nachricht, gone). gone=True heißt: Channel oder Nachricht existiert nicht mehr."""
    channel_id, message_id = settings.get("panel_channel_id"), settings.get("panel_message_id")
    if not channel_id or not message_id:
        return None, False
    channel = guild.get_channel(channel_id)
    if channel is None:
        return None, True
    try:
        return await channel.fetch_message(message_id), False
    except discord.NotFound:
        return None, True
    except discord.HTTPException as e:
        log.warning("Ticket-Panel %s auf %s nicht lesbar: %s", message_id, guild.id, e)
        return None, False


async def publish_panel(
    guild: discord.Guild,
    title: str,
    description: str,
    channel: discord.abc.Messageable | None = None,
) -> tuple[discord.Message, bool]:
    """Panel speichern und anzeigen. Bearbeitet das bestehende Panel, wenn es im selben Channel
    liegt, sonst wird neu gepostet und das alte gelöscht (ein Panel pro Server).
    channel=None: im Channel des bestehenden Panels bleiben.
    Rückgabe: (Nachricht, bearbeitet statt neu gepostet)."""
    settings = await db_ticket_settings.get_ticket_settings(guild.id)
    categories = await db_ticket_categories.get_categories(guild.id)
    if not categories:
        raise PanelError("no_categories")

    old, _ = await _fetch_stored_panel(guild, settings)
    if channel is None:
        if old is None:
            raise PanelError("no_channel")
        channel = old.channel

    embed = build_panel_embed(title, description, await db_settings.get_color(guild.id))
    view = build_panel_view(categories)

    message, edited = None, False
    if old is not None and old.channel.id == channel.id:
        try:
            message = await old.edit(embed=embed, view=view)
            edited = True
        except discord.HTTPException as e:
            log.warning("Ticket-Panel auf %s ließ sich nicht bearbeiten, poste neu: %s", guild.id, e)

    if message is None:
        if not can_use_channel(channel, "send_messages", "embed_links"):
            raise PanelError("no_permission")
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            raise PanelError("no_permission")
        if old is not None and old.id != message.id:
            try:
                await old.delete()
            except discord.HTTPException:
                log.info("Altes Ticket-Panel %s auf %s konnte nicht gelöscht werden.", old.id, guild.id)

    await db_ticket_settings.update_ticket_settings(
        guild.id,
        panel_channel_id=message.channel.id,
        panel_message_id=message.id,
        panel_title=title,
        panel_description=description,
    )
    return message, edited


async def refresh_panel(guild: discord.Guild) -> None:
    """Buttons des gespeicherten Panels an die aktuellen Kategorien anpassen (nach Anlegen,
    Umbenennen, Löschen). Best effort: Fehler werden nur geloggt."""
    try:
        settings = await db_ticket_settings.get_ticket_settings(guild.id)
        if not settings.get("panel_message_id"):
            return
        message, gone = await _fetch_stored_panel(guild, settings)
        if message is None:
            if gone:
                # Panel wurde manuell gelöscht: Verweis entfernen, Text bleibt für das nächste Posten
                await db_ticket_settings.update_ticket_settings(guild.id, panel_message_id=None)
            return
        categories = await db_ticket_categories.get_categories(guild.id)
        await message.edit(view=build_panel_view(categories))
    except Exception:
        log.exception("Ticket-Panel auf %s konnte nicht aktualisiert werden.", guild.id)


async def import_panel(guild: discord.Guild, message: discord.Message, bot_user_id: int) -> discord.Message:
    """Ein altes Panel (vor dieser Funktion gepostet) übernehmen: Text speichern,
    Buttons aktualisieren, ein anderes gespeichertes Panel löschen."""
    if message.guild is None or message.guild.id != guild.id or not is_ticket_panel(message, bot_user_id):
        raise PanelError("not_a_panel")

    embed = message.embeds[0] if message.embeds else None
    title = ((embed.title if embed and embed.title else None) or "🎫 Support")[:256]
    description = ((embed.description if embed and embed.description else None) or "")[:1000]

    settings = await db_ticket_settings.get_ticket_settings(guild.id)
    old, _ = await _fetch_stored_panel(guild, settings)
    if old is not None and old.id != message.id:
        try:
            await old.delete()
        except discord.HTTPException:
            log.info("Bisheriges Ticket-Panel %s auf %s konnte nicht gelöscht werden.", old.id, guild.id)

    await db_ticket_settings.update_ticket_settings(
        guild.id,
        panel_channel_id=message.channel.id,
        panel_message_id=message.id,
        panel_title=title,
        panel_description=description,
    )
    await refresh_panel(guild)
    return message
