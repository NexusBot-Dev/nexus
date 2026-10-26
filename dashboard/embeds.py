import asyncio
import logging
import discord
from zoneinfo import ZoneInfo
from datetime import datetime, timezone as tz
from .registry import register_action, get_bot, parse_id, DashCtx
from database import db_embeds, db_settings, db_limits
from systems.premium import is_premium
from systems.embed_posts import build_embed, track_post, refresh_posts

log = logging.getLogger(__name__)

async def _ensure_active(guild_id: int, embed_id: int) -> None:
    premium = await is_premium(guild_id)
    limit = await db_limits.get_limit(guild_id, "saved_embeds", premium)
    if not await db_embeds.is_embed_active(embed_id, guild_id, limit):
        raise ValueError(
            "Embed ist inaktiv: Der Server liegt über dem Embed-Limit. "
            "Lösche ältere Embeds oder hol dir Premium."
        )

@register_action("embeds", "list")
async def list_embeds(ctx: DashCtx) -> dict:
    guild_id = ctx.guild_id
    embeds, premium, limit_free, limit_premium = await asyncio.gather(
        db_embeds.get_embeds(guild_id),
        is_premium(guild_id),
        db_limits.get_limit(guild_id, "saved_embeds", False),
        db_limits.get_limit(guild_id, "saved_embeds", True),
    )
    return {
        "embeds": [
            {
                "id": e["id"],
                "name": e["name"],
                "title": e["title"],
                "description": e["description"],
                "channel_id": str(e["channel_id"]) if e["channel_id"] else None,
                "scheduled_at": e["scheduled_at"].isoformat() + "Z" if e["scheduled_at"] else None,
            }
            for e in embeds
        ],
        "limit_free": limit_free,
        "limit_premium": limit_premium,
        "premium_active": premium,
    }

@register_action("embeds", "add")
async def add_embed(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    name = (payload.get("name") or "").strip()
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    channel_id = payload.get("channel_id")

    if not name or not description:
        raise ValueError("Name und Inhalt werden benötigt")
    if len(title) > 256:
        raise ValueError("Titel zu lang (max. 256 Zeichen)")
    if len(description) > 4000:
        raise ValueError("Inhalt zu lang (max. 4000 Zeichen)")
    channel_id = parse_id(channel_id, "Gültiger Channel wird benötigt")

    if guild.get_channel(channel_id) is None:
        raise ValueError("Channel existiert nicht (mehr) auf diesem Server")

    if not await db_embeds.can_save_embed(guild_id):
        premium = await is_premium(guild_id)
        limit = await db_limits.get_limit(guild_id, "saved_embeds", premium)
        raise ValueError(f"Limit erreicht ({limit} Embeds)")

    color = await db_settings.get_color(guild_id)
    embed_id = await db_embeds.save_embed(
        guild_id=guild_id, name=name, title=title, description=description,
        color=color, channel_id=channel_id,
    )
    return {"id": embed_id, "name": name, "title": title, "description": description, "channel_id": str(channel_id)}

@register_action("embeds", "edit")
async def edit_embed(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    embed_id = parse_id(payload.get("embed_id"), "embed_id und Inhalt werden benötigt")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    channel_id = payload.get("channel_id")

    if not description:
        raise ValueError("embed_id und Inhalt werden benötigt")
    if len(title) > 256:
        raise ValueError("Titel zu lang (max. 256 Zeichen)")
    if len(description) > 4000:
        raise ValueError("Inhalt zu lang (max. 4000 Zeichen)")

    existing = await db_embeds.get_embed(embed_id, guild_id)
    if existing is None:
        raise ValueError("Embed wurde nicht gefunden")

    bot = get_bot()
    # Wird kein Channel mitgeschickt, bleibt der gespeicherte erhalten (statt auf NULL zu fallen)
    channel_id_int = existing["channel_id"]
    if channel_id not in (None, ""):
        channel_id_int = parse_id(channel_id, "Ungültiger Channel")
        if guild.get_channel(channel_id_int) is None:
            raise ValueError("Channel existiert nicht (mehr) auf diesem Server")

    updated = await db_embeds.update_embed(embed_id, guild_id, title, description, channel_id_int)
    if not updated:
        raise ValueError("Embed wurde nicht gefunden")

    # Bereits gepostete Nachrichten mitziehen
    refreshed = await refresh_posts(bot, guild_id, {"id": embed_id, "title": title, "description": description})
    return {
        "id": embed_id,
        "posts_updated": refreshed.updated,
        "posts_removed": refreshed.removed,
        "posts_failed": refreshed.failed,
    }

@register_action("embeds", "delete")
async def delete_embed(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    embed_id = parse_id(payload.get("embed_id"), "embed_id wird benötigt")
    deleted = await db_embeds.delete_embed(embed_id, guild_id)
    if not deleted:
        raise ValueError("Embed wurde nicht gefunden")
    return {"id": embed_id}

@register_action("embeds", "post-now")
async def post_now(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    embed_id = parse_id(payload.get("embed_id"), "embed_id wird benötigt")

    embed_data = await db_embeds.get_embed(embed_id, guild_id)
    if embed_data is None:
        raise ValueError("Embed wurde nicht gefunden")
    await _ensure_active(guild_id, embed_id)
    if not embed_data.get("channel_id"):
        raise ValueError("Kein Ziel-Channel hinterlegt")

    channel = guild.get_channel(embed_data["channel_id"])
    if channel is None:
        raise ValueError("Channel existiert nicht (mehr)")

    color = await db_settings.get_color(guild_id)
    discord_embed = build_embed(embed_data["title"], embed_data["description"], color)

    try:
        message = await channel.send(embed=discord_embed)
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung zum Posten in diesem Channel")
    except discord.HTTPException:
        log.exception("Dashboard: Embed %s auf %s konnte nicht gepostet werden", embed_id, guild_id)
        raise ValueError("Discord hat das Posten abgelehnt — bitte später erneut versuchen")

    # Wie beim Posten über /embed merken, damit spätere Edits die Nachricht mitziehen
    await track_post(embed_data["id"], message)
    return {"id": embed_id, "message_id": str(message.id)}

@register_action("embeds", "schedule")
async def schedule_embed(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    if not await is_premium(guild_id):
        raise ValueError("Zeitgesteuertes Posten ist ein Premium-Feature")

    embed_id = parse_id(payload.get("embed_id"), "embed_id und scheduled_at werden benötigt")
    scheduled_at_iso = payload.get("scheduled_at")
    if not scheduled_at_iso:
        raise ValueError("embed_id und scheduled_at werden benötigt")
    await _ensure_active(guild_id, embed_id)

    try:
        tz_name = await db_settings.get_timezone(guild_id)
        local_tz = ZoneInfo(tz_name)
        scheduled_local = datetime.fromisoformat(scheduled_at_iso).replace(tzinfo=local_tz)
    except (ValueError, KeyError, TypeError):
        raise ValueError("Ungültiges Datumsformat")

    if scheduled_local < datetime.now(local_tz):
        raise ValueError("Der Zeitpunkt liegt in der Vergangenheit")

    scheduled_utc = scheduled_local.astimezone(tz.utc).replace(tzinfo=None)
    updated = await db_embeds.set_schedule(embed_id, guild_id, scheduled_utc)
    if not updated:
        raise ValueError("Embed wurde nicht gefunden")

    return {"id": embed_id, "scheduled_at": scheduled_utc.isoformat() + "Z"}  # wie in "list": naive UTC + "Z"

@register_action("embeds", "unschedule")
async def unschedule_embed(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    embed_id = parse_id(payload.get("embed_id"), "embed_id wird benötigt")
    updated = await db_embeds.set_schedule(embed_id, guild_id, None)
    if not updated:
        raise ValueError("Embed wurde nicht gefunden")
    return {"id": embed_id}
