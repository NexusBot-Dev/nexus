from .registry import register_action, get_bot
from systems.hierarchy import check_bot_hierarchy

@register_action("guild", "channels")
async def get_channels(guild_id: int, payload: dict) -> dict:
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    return {
        "channels": [
            {"id": str(ch.id), "name": ch.name}
            for ch in guild.text_channels
        ]
    }

@register_action("guild", "info")
async def get_guild_info(guild_id: int, payload: dict) -> dict:
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    hierarchy_ok = hierarchy_ok = check_bot_hierarchy(guild)

    return {
        "name": guild.name,
        "icon_url": guild.icon.url if guild.icon else None,
        "member_count": guild.member_count,
        "bot_nickname": guild.me.display_name,
        "online": bot.is_ready(),
        "hierarchy_ok": hierarchy_ok,
        "boost_tier": guild.premium_tier,
        "boost_count": guild.premium_subscription_count,
        "uptime_since": bot.started_at.isoformat(),
    }

@register_action("guild", "roles")
async def get_roles(guild_id: int, payload: dict) -> dict:
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    return {
        "roles": [
            {"id": str(r.id), "name": r.name, "color": r.color.value}
            for r in guild.roles
            if not r.managed and r.name != "@everyone"
        ]
    }

@register_action("guild", "emojis")
async def get_emojis(guild_id: int, payload: dict) -> dict:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    return {
        "emojis": [
            {"name": e.name, "id": str(e.id), "animated": e.animated, "url": str(e.url)}
            for e in guild.emojis
        ]
    }
