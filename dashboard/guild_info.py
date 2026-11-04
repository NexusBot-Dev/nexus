import logging
from datetime import datetime, timezone
from .registry import register_action, get_bot, DashCtx, DashboardError, InvalidRequest
from database import db_settings
from systems.hierarchy import check_bot_hierarchy

log = logging.getLogger(__name__)

@register_action("guild", "channels")
async def get_channels(ctx: DashCtx) -> dict:
    guild = ctx.guild

    return {
        "channels": [
            {"id": str(ch.id), "name": ch.name}
            for ch in guild.text_channels
        ]
    }

@register_action("guild", "info")
async def get_guild_info(ctx: DashCtx) -> dict:
    bot = get_bot()
    guild = ctx.guild

    hierarchy_ok = check_bot_hierarchy(guild)

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
async def get_roles(ctx: DashCtx) -> dict:
    guild = ctx.guild

    return {
        "roles": [
            {"id": str(r.id), "name": r.name, "color": r.color.value}
            for r in guild.roles
            if not r.managed and r.name != "@everyone"
        ]
    }

@register_action("guild", "emojis")
async def get_emojis(ctx: DashCtx) -> dict:
    guild = ctx.guild

    return {
        "emojis": [
            {"name": e.name, "id": str(e.id), "animated": e.animated, "url": str(e.url)}
            for e in guild.emojis
        ]
    }

@register_action("guild", "leave")
async def leave_guild(ctx: DashCtx) -> dict:
    """Danger Zone: Nexus verlässt den Server — wie ein Kick, also 7 Tage Wiederherstellung.
    Mit delete_now werden alle Daten sofort gelöscht (Premium bleibt, siehe delete_guild_data)."""
    delete_now = ctx.payload.get("delete_now", False)
    if not isinstance(delete_now, bool):
        raise InvalidRequest(f"delete_now muss true oder false sein: {delete_now!r}")
    # Unumkehrbar: nur der Owner, nicht jeder mit "Server verwalten"
    if ctx.actor.id != ctx.guild.owner_id:
        raise DashboardError("dash_err_guild_owner_only")

    guild = ctx.guild
    await guild.leave()
    # Selbst setzen statt auf on_guild_remove zu warten: Das Dashboard lädt direkt danach die
    # Serverliste, und die zeigt nur Server ohne left_at als "Nexus ist drauf"
    await db_settings.update_guild(guild.id, left_at=datetime.now(timezone.utc))

    deleted = False
    if delete_now:
        # Schlägt das fehl, bleibt left_at gesetzt — dann löscht der Cleanup nach 7 Tagen
        deleted = await db_settings.delete_guild_data([guild.id])
    log.info(
        "Nexus hat %s (%s) über das Dashboard verlassen (ausgelöst von %s, Daten sofort gelöscht: %s).",
        guild.name, guild.id, ctx.actor.id, deleted,
    )
    return {"left": True, "deleted": deleted}
