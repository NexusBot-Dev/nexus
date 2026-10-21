"""Gemeinsame Logik für gepostete Embeds (Bot + Dashboard):
Embed bauen, Posts merken und bei Bearbeitung alle noch existierenden Nachrichten aktualisieren."""
import logging
from dataclasses import dataclass
import discord
from config import NEXUS_FOOTER
from database import db_embeds, db_settings

log = logging.getLogger(__name__)


def build_embed(title: str | None, description: str, color: int | None) -> discord.Embed:
    embed = discord.Embed(title=title or None, description=description, color=color)
    embed.set_footer(text=NEXUS_FOOTER)
    return embed


async def track_post(embed_id: int | None, message: discord.Message | None) -> None:
    """Merkt sich eine gepostete Nachricht. Best effort: ein Fehler hier darf das Posten nie scheitern lassen."""
    if embed_id is None or message is None or message.guild is None:
        return
    try:
        await db_embeds.add_post(embed_id, message.guild.id, message.channel.id, message.id)
    except Exception:
        log.exception("Konnte gepostete Nachricht von Embed %s nicht speichern.", embed_id)


@dataclass
class RefreshResult:
    updated: int = 0
    removed: int = 0
    failed: int = 0


async def refresh_posts(bot: discord.Client, guild_id: int, embed_data: dict) -> RefreshResult:
    """Schreibt den aktuellen Stand von `embed_data` in alle gemerkten Nachrichten."""
    result = RefreshResult()
    posts = await db_embeds.get_posts(embed_data["id"], guild_id)
    if not posts:
        return result

    color = await db_settings.get_color(guild_id)
    embed = build_embed(embed_data["title"], embed_data["description"], color)
    guild = bot.get_guild(guild_id)
    dead: list[int] = []

    for post in posts:
        channel = guild.get_channel_or_thread(post["channel_id"]) if guild else None
        if channel is None:
            try:
                channel = await bot.fetch_channel(post["channel_id"])
            except discord.NotFound:
                dead.append(post["id"])
                result.removed += 1
                continue
            except discord.HTTPException:
                result.failed += 1
                continue
        try:
            await channel.get_partial_message(post["message_id"]).edit(embed=embed)
            result.updated += 1
        except discord.NotFound:
            dead.append(post["id"])
            result.removed += 1
        except discord.HTTPException as e:
            log.warning("Konnte Embed-Nachricht %s nicht aktualisieren: %s", post["message_id"], e)
            result.failed += 1

    await db_embeds.delete_posts(dead)
    return result