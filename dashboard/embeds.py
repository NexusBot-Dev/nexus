from zoneinfo import ZoneInfo
from datetime import datetime, timezone as tz
from .registry import register_action, get_bot
from database import db_embeds, db_settings, db_limits
from systems.premium import is_premium

@register_action("embeds", "list")
async def list_embeds(guild_id: int, payload: dict) -> dict:
    embeds = await db_embeds.get_embeds(guild_id)
    premium = await is_premium(guild_id)
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
        "limit_free": await db_limits.get_limit(guild_id, "saved_embeds", False),
        "limit_premium": await db_limits.get_limit(guild_id, "saved_embeds", True),
        "premium_active": premium,
    }

@register_action("embeds", "add")
async def add_embed(guild_id: int, payload: dict) -> dict:
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
    if not channel_id or not str(channel_id).isdigit():
        raise ValueError("Gültiger Channel wird benötigt")

    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None or guild.get_channel(int(channel_id)) is None:
        raise ValueError("Channel existiert nicht (mehr) auf diesem Server")

    if not await db_embeds.can_save_embed(guild_id):
        premium = await is_premium(guild_id)
        limit = await db_limits.get_limit(guild_id, "saved_embeds", premium)
        raise ValueError(f"Limit erreicht ({limit} Embeds)")

    color = await db_settings.get_color(guild_id)
    embed_id = await db_embeds.save_embed(
        guild_id=guild_id, name=name, title=title, description=description,
        color=color, channel_id=int(channel_id),
    )
    return {"id": embed_id, "name": name, "title": title, "description": description, "channel_id": channel_id}

@register_action("embeds", "edit")
async def edit_embed(guild_id: int, payload: dict) -> dict:
    embed_id = payload.get("embed_id")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    channel_id = payload.get("channel_id")

    if not embed_id or not description:
        raise ValueError("embed_id und Inhalt werden benötigt")
    if len(title) > 256:
        raise ValueError("Titel zu lang (max. 256 Zeichen)")
    if len(description) > 4000:
        raise ValueError("Inhalt zu lang (max. 4000 Zeichen)")

    channel_id_int = None
    if channel_id and str(channel_id).isdigit():
        bot = get_bot()
        guild = bot.get_guild(guild_id)
        if guild is None or guild.get_channel(int(channel_id)) is None:
            raise ValueError("Channel existiert nicht (mehr) auf diesem Server")
        channel_id_int = int(channel_id)

    updated = await db_embeds.update_embed(int(embed_id), guild_id, title, description, channel_id_int)
    if not updated:
        raise ValueError("Embed wurde nicht gefunden")
    return {"id": embed_id}

@register_action("embeds", "delete")
async def delete_embed(guild_id: int, payload: dict) -> dict:
    embed_id = payload.get("embed_id")
    if not embed_id:
        raise ValueError("embed_id wird benötigt")
    deleted = await db_embeds.delete_embed(int(embed_id), guild_id)
    if not deleted:
        raise ValueError("Embed wurde nicht gefunden")
    return {"id": embed_id}

@register_action("embeds", "post-now")
async def post_now(guild_id: int, payload: dict) -> dict:
    import discord

    embed_id = payload.get("embed_id")
    if not embed_id:
        raise ValueError("embed_id wird benötigt")

    embed_data = await db_embeds.get_embed(int(embed_id), guild_id)
    if embed_data is None:
        raise ValueError("Embed wurde nicht gefunden")
    if not embed_data.get("channel_id"):
        raise ValueError("Kein Ziel-Channel hinterlegt")

    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    channel = guild.get_channel(embed_data["channel_id"])
    if channel is None:
        raise ValueError("Channel existiert nicht (mehr)")

    color = embed_data["color"] or await db_settings.get_color(guild_id)
    discord_embed = discord.Embed(title=embed_data["title"], description=embed_data["description"], color=color)

    try:
        await channel.send(embed=discord_embed)
    except discord.HTTPException as e:
        raise ValueError(f"Discord hat das Posten abgelehnt: {e}")

    return {"id": embed_id, "posted_in": str(channel.id)}

@register_action("embeds", "schedule")
async def schedule_embed(guild_id: int, payload: dict) -> dict:
    if not await is_premium(guild_id):
        raise ValueError("Zeitgesteuertes Posten ist ein Premium-Feature")

    embed_id = payload.get("embed_id")
    scheduled_at_iso = payload.get("scheduled_at")
    if not embed_id or not scheduled_at_iso:
        raise ValueError("embed_id und scheduled_at werden benötigt")

    try:
        tz_name = await db_settings.get_timezone(guild_id)
        local_tz = ZoneInfo(tz_name)
        scheduled_local = datetime.fromisoformat(scheduled_at_iso).replace(tzinfo=local_tz)
    except (ValueError, KeyError):
        raise ValueError("Ungültiges Datumsformat")

    if scheduled_local < datetime.now(local_tz):
        raise ValueError("Der Zeitpunkt liegt in der Vergangenheit")

    scheduled_utc = scheduled_local.astimezone(tz.utc).replace(tzinfo=None)
    updated = await db_embeds.set_schedule(int(embed_id), guild_id, scheduled_utc)
    if not updated:
        raise ValueError("Embed wurde nicht gefunden")

    return {"id": embed_id, "scheduled_at": scheduled_utc.isoformat()}

@register_action("embeds", "unschedule")
async def unschedule_embed(guild_id: int, payload: dict) -> dict:
    embed_id = payload.get("embed_id")
    if not embed_id:
        raise ValueError("embed_id wird benötigt")
    updated = await db_embeds.set_schedule(int(embed_id), guild_id, None)
    if not updated:
        raise ValueError("Embed wurde nicht gefunden")
    return {"id": embed_id}