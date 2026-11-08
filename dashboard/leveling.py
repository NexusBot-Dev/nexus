import asyncio
import discord
from .registry import register_action, DashCtx, DashboardError, InvalidRequest
from database import db_levels
from systems.hierarchy import can_use_channel, can_grant_role, issue_text
from .health import plain_mentions
from systems.voice_xp import MAX_XP_PER_MINUTE as VOICE_XP_MAX
from systems.exp_system import CHAT_XP_MAX, xp_range_error

MAX_EXCLUDED = 25
_EXCLUDABLE = (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.CategoryChannel)

def _exclude_target(ch) -> dict:
    return {
        "id": str(ch.id),
        "name": ch.name,
        "type": "category" if isinstance(ch, discord.CategoryChannel) else "channel",
        "category": ch.category.name if getattr(ch, "category", None) else None,
    }

@register_action("leveling", "get-excluded")
async def get_excluded(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id

    excluded = await db_levels.get_excluded_channels(guild_id)
    targets = [_exclude_target(c) for c in (*guild.categories, *guild.text_channels, *guild.forums, *guild.voice_channels)]
    known = {t["id"] for t in targets}
    return {
        # Gelöschte Channels werden ausgeblendet und beim nächsten Speichern entfernt
        "excluded_ids": [str(cid) for cid in sorted(excluded) if str(cid) in known],
        "targets": targets,
        "limit": MAX_EXCLUDED,
    }

@register_action("leveling", "set-excluded")
async def set_excluded(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    raw = payload.get("channel_ids")
    if not isinstance(raw, list) or not all(str(i).isdigit() for i in raw):
        raise InvalidRequest("channel_ids muss eine Liste gültiger IDs sein")

    ids = sorted({int(i) for i in raw})
    if len(ids) > MAX_EXCLUDED:
        raise DashboardError("dash_err_lvl_excluded_limit", max=MAX_EXCLUDED)

    for cid in ids:
        if not isinstance(guild.get_channel(cid), _EXCLUDABLE):
            raise DashboardError("dash_err_lvl_exclude_invalid_channel")

    await db_levels.set_excluded_channels(guild_id, ids)
    return {"excluded_ids": [str(cid) for cid in ids]}

@register_action("leveling", "list-roles")
async def list_roles(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id

    rows = await db_levels.get_all_level_roles(guild_id)
    roles = []
    for row in rows:
        role = guild.get_role(row["role_id"])
        roles.append({
            "level": row["level"],
            "role_id": str(row["role_id"]),
            "role_name": role.name if role else ctx.lang.get("dash_deleted_role", "(deleted role)"),
            "role_color": f"#{role.color.value:06x}" if role and role.color.value else "#94a3b8",
        })
    return {"roles": roles}

@register_action("leveling", "set-role")
async def set_role(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    level = payload.get("level")
    role_id = payload.get("role_id")

    if not isinstance(level, int) or isinstance(level, bool) or not (1 <= level <= 1000):
        raise DashboardError("dash_err_lvl_level_invalid")
    if not role_id or not str(role_id).isdigit():
        raise InvalidRequest(f"Ungültige role_id: {role_id!r}")

    role = guild.get_role(int(role_id))
    if role is None:
        raise DashboardError("dash_err_role_gone")

    if not (check := can_grant_role(guild, role, ctx.actor)):
        raise ValueError(plain_mentions(guild, issue_text(check, ctx.lang)))

    await db_levels.set_level_role(guild_id, level, int(role_id))
    return {"level": level, "role_id": role_id, "role_name": role.name, "role_color": f"#{role.color.value:06x}" if role.color.value else "#94a3b8"}

@register_action("leveling", "remove-role")
async def remove_role(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    level = payload.get("level")
    if not isinstance(level, int):
        raise InvalidRequest(f"Ungültiges Level: {level!r}")
    await db_levels.remove_level_role(guild_id, level)
    return {"level": level}

@register_action("leveling", "leaderboard")
async def leaderboard(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id

    rows = await db_levels.get_leaderboard(guild_id, limit=10)

    async def resolve(user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    # Nicht gecachte Mitglieder parallel nachladen statt bis zu 10 API-Calls nacheinander
    members = await asyncio.gather(*(resolve(row["user_id"]) for row in rows))
    entries = []
    for row, member in zip(rows, members):
        entries.append({
            "user_id": str(row["user_id"]),
            "username": member.display_name if member else ctx.lang.get("dash_unknown_user", "Unknown ({id})").format(id=row["user_id"]),
            "avatar_url": member.display_avatar.url if member and member.display_avatar else None,
            "xp": row["xp"],
            "level": row["level"],
        })
    return {"entries": entries}

@register_action("leveling", "get-xp-settings")
async def get_xp_settings(ctx: DashCtx) -> dict:
    settings = await db_levels.get_levels_settings(ctx.guild_id)
    return {
        "xp_min": settings["xp_min"],
        "xp_max": settings["xp_max"],
        "cooldown_seconds": settings["cooldown"],
        "currency_name": settings["currency_name"],
        "points_min": settings["points_min"],
        "points_max": settings["points_max"],
        "shop_enabled": settings["shop_enabled"],
        "levelup_channel_id": str(settings["levelup_channel_id"]) if settings.get("levelup_channel_id") else None,
        "voice_xp_enabled": bool(settings["voice_xp_enabled"]),
        "voice_xp_min": settings["voice_xp_min"],
        "voice_xp_max": settings["voice_xp_max"],
    }

@register_action("leveling", "set-levelup-channel")
async def set_levelup_channel(ctx: DashCtx) -> dict:
    """Setzt den Level-Up-Channel. Leer/None = Fallback (Nachricht im aktuellen Channel, nach 30s gelöscht)."""
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    channel_id = payload.get("channel_id")

    if channel_id in (None, ""):
        await db_levels.update_levels_settings(guild_id, levelup_channel_id=None)
        return {"levelup_channel_id": None}

    if not str(channel_id).isdigit():
        raise InvalidRequest(f"Ungültiger Channel: {channel_id!r}")

    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        raise DashboardError("dash_err_text_channel_invalid")
    if not can_use_channel(channel, "send_messages", "embed_links"):
        raise DashboardError("dash_err_no_embed_permission")

    await db_levels.update_levels_settings(guild_id, levelup_channel_id=channel.id)
    return {"levelup_channel_id": str(channel.id)}

@register_action("leveling", "set-voice-xp")
async def set_voice_xp(ctx: DashCtx) -> dict:
    payload = ctx.payload
    enabled, xp_min, xp_max = payload.get("enabled"), payload.get("xp_min"), payload.get("xp_max")
    if not isinstance(enabled, bool):
        raise InvalidRequest("enabled muss true/false sein")
    _check_xp_range(xp_min, xp_max, VOICE_XP_MAX, "dash_err_lvl_voice_xp_range")

    await db_levels.update_levels_settings(
        ctx.guild_id, voice_xp_enabled=enabled, voice_xp_min=xp_min, voice_xp_max=xp_max,
    )
    return {"voice_xp_enabled": enabled, "voice_xp_min": xp_min, "voice_xp_max": xp_max}

def _check_xp_range(xp_min, xp_max, max_xp: int = CHAT_XP_MAX, error: str = "dash_err_lvl_xp_range") -> None:
    """Gleiche Regeln wie /xp_settings (systems/exp_system.xp_range_error)."""
    problem = xp_range_error(xp_min, xp_max, max_xp)
    if problem == "range":
        raise DashboardError(error, max=max_xp)
    if problem == "min_gt_max":
        raise DashboardError("dash_err_lvl_xp_min_gt_max")

@register_action("leveling", "set-xp-settings")
async def set_xp_settings(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    xp_min = payload.get("xp_min")
    xp_max = payload.get("xp_max")

    _check_xp_range(xp_min, xp_max)

    await db_levels.update_levels_settings(guild_id, xp_min=xp_min, xp_max=xp_max)
    return {"xp_min": xp_min, "xp_max": xp_max}
