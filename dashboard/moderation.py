import asyncio
import discord
from .registry import register_action, get_bot

_chunk_locks: dict[int, asyncio.Lock] = {}

def _guild_or_raise(guild_id: int) -> discord.Guild:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")
    return guild

async def ensure_members_cached(guild: discord.Guild) -> None:
    """Der Bot lädt beim Start keine Mitgliederlisten (chunk_guilds_at_startup=False).
    Für alles, was über ALLE Mitglieder laufen muss (z.B. Timeouts), wird die Liste
    hier einmalig pro Server nachgeladen — danach bleibt sie bis zum Neustart im Cache.
    Der Lock verhindert, dass parallele Requests denselben Server mehrfach laden."""
    if guild.chunked:
        return
    lock = _chunk_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        if guild.chunked:
            return
        try:
            await asyncio.wait_for(guild.chunk(), timeout=10)
        except asyncio.TimeoutError:
            pass

@register_action("moderation", "list-bans")
async def list_bans(guild_id: int, payload: dict) -> dict:
    guild = _guild_or_raise(guild_id)
    bans = []
    async for entry in guild.bans(limit=200):
        bans.append({
            "user_id": str(entry.user.id),
            "username": entry.user.name,
            "avatar_url": entry.user.display_avatar.url if entry.user.display_avatar else None,
            "reason": entry.reason,
        })
    return {"bans": bans}

@register_action("moderation", "list-timeouts")
async def list_timeouts(guild_id: int, payload: dict) -> dict:
    guild = _guild_or_raise(guild_id)
    await ensure_members_cached(guild)
    timeouts = []
    for member in guild.members:
        if member.is_timed_out():
            timeouts.append({
                "user_id": str(member.id),
                "username": member.display_name,
                "avatar_url": member.display_avatar.url if member.display_avatar else None,
                "until": member.timed_out_until.isoformat(),
            })
    return {"timeouts": timeouts}

@register_action("moderation", "unban")
async def unban(guild_id: int, payload: dict) -> dict:
    user_id = payload.get("user_id")
    if not user_id or not str(user_id).isdigit():
        raise ValueError("Gültige user_id wird benötigt")

    guild = _guild_or_raise(guild_id)
    user = discord.Object(id=int(user_id))
    try:
        await guild.unban(user, reason="Nexus Dashboard — entbannt")
    except discord.NotFound:
        raise ValueError("User ist nicht gebannt")
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung zum Entbannen")

    return {"user_id": user_id}

@register_action("moderation", "remove-timeout")
async def remove_timeout(guild_id: int, payload: dict) -> dict:
    user_id = payload.get("user_id")
    if not user_id or not str(user_id).isdigit():
        raise ValueError("Gültige user_id wird benötigt")

    guild = _guild_or_raise(guild_id)
    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            raise ValueError("Mitglied nicht gefunden")

    try:
        await member.timeout(None, reason="Nexus Dashboard — Timeout aufgehoben")
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung (Hierarchie?)")

    return {"user_id": user_id}