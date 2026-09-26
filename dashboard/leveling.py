import discord
from .registry import register_action, get_bot
from database import db_levels

@register_action("leveling", "list-roles")
async def list_roles(guild_id: int, payload: dict) -> dict:
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    rows = await db_levels.get_all_level_roles(guild_id)
    roles = []
    for row in rows:
        role = guild.get_role(row["role_id"])
        roles.append({
            "level": row["level"],
            "role_id": str(row["role_id"]),
            "role_name": role.name if role else "(gelöschte Rolle)",
            "role_color": f"#{role.color.value:06x}" if role and role.color.value else "#94a3b8",
        })
    return {"roles": roles}

@register_action("leveling", "set-role")
async def set_role(guild_id: int, payload: dict) -> dict:
    level = payload.get("level")
    role_id = payload.get("role_id")

    if not isinstance(level, int) or not (1 <= level <= 1000):
        raise ValueError("Ungültiges Level (1-1000)")
    if not role_id or not str(role_id).isdigit():
        raise ValueError("Gültige role_id wird benötigt")

    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    role = guild.get_role(int(role_id))
    if role is None:
        raise ValueError("Rolle existiert nicht auf diesem Server")
    if role.managed or role.is_default():
        raise ValueError("Diese Rolle kann nicht zugewiesen werden")

    await db_levels.set_level_role(guild_id, level, int(role_id))
    return {"level": level, "role_id": role_id, "role_name": role.name, "role_color": f"#{role.color.value:06x}" if role.color.value else "#94a3b8"}

@register_action("leveling", "remove-role")
async def remove_role(guild_id: int, payload: dict) -> dict:
    level = payload.get("level")
    if not isinstance(level, int):
        raise ValueError("Ungültiges Level")
    await db_levels.remove_level_role(guild_id, level)
    return {"level": level}

@register_action("leveling", "leaderboard")
async def leaderboard(guild_id: int, payload: dict) -> dict:
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    rows = await db_levels.get_leaderboard(guild_id, limit=10)
    entries = []
    for row in rows:
        member = guild.get_member(row["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(row["user_id"])
            except discord.NotFound:
                member = None
        entries.append({
            "user_id": str(row["user_id"]),
            "username": member.display_name if member else f"Unbekannt ({row['user_id']})",
            "avatar_url": member.display_avatar.url if member and member.display_avatar else None,
            "xp": row["xp"],
            "level": row["level"],
        })
    return {"entries": entries}

@register_action("leveling", "get-xp-settings")
async def get_xp_settings(guild_id: int, payload: dict) -> dict:
    settings = await db_levels.get_levels_settings(guild_id)
    return {
        "xp_min": settings["xp_min"],
        "xp_max": settings["xp_max"],
        "cooldown_seconds": settings["cooldown"],
        "currency_name": settings["currency_name"],
        "points_min": settings["points_min"],
        "points_max": settings["points_max"],
        "shop_enabled": settings["shop_enabled"],
    }

@register_action("leveling", "set-xp-settings")
async def set_xp_settings(guild_id: int, payload: dict) -> dict:
    xp_min = payload.get("xp_min")
    xp_max = payload.get("xp_max")

    if not isinstance(xp_min, int) or not isinstance(xp_max, int):
        raise ValueError("xp_min und xp_max müssen Ganzzahlen sein")
    if not (1 <= xp_min <= 1000) or not (1 <= xp_max <= 1000):
        raise ValueError("Werte müssen zwischen 1 und 1000 liegen")
    if xp_min > xp_max:
        raise ValueError("xp_min darf nicht größer als xp_max sein")

    await db_levels.update_levels_settings(guild_id, xp_min=xp_min, xp_max=xp_max)
    return {"xp_min": xp_min, "xp_max": xp_max}