import asyncio
import discord
from .registry import register_action, DashCtx
from systems.hierarchy import can_act_on_member, Issue

_chunk_locks: dict[int, asyncio.Lock] = {}

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
async def list_bans(ctx: DashCtx) -> dict:
    guild = ctx.guild
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
async def list_timeouts(ctx: DashCtx) -> dict:
    guild = ctx.guild
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
async def unban(ctx: DashCtx) -> dict:
    guild, actor, payload = ctx.guild, ctx.actor, ctx.payload
    user_id = payload.get("user_id")
    if not user_id or not str(user_id).isdigit():
        raise ValueError("Gültige user_id wird benötigt")

    # Unban kennt keine Rollen-Hierarchie (der User ist nicht mehr Member) —
    # hier reicht die reine Discord-Berechtigung des Dashboard-Nutzers.
    if not actor.guild_permissions.ban_members:
        raise ValueError("Dir fehlt die Berechtigung 'Mitglieder bannen' (Ban Members)")

    user = discord.Object(id=int(user_id))
    try:
        await guild.unban(user, reason=f"Nexus Dashboard — entbannt von {actor}")
    except discord.NotFound:
        raise ValueError("User ist nicht gebannt")
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung zum Entbannen")

    return {"user_id": user_id}

@register_action("moderation", "remove-timeout")
async def remove_timeout(ctx: DashCtx) -> dict:
    guild, actor, payload = ctx.guild, ctx.actor, ctx.payload
    user_id = payload.get("user_id")
    if not user_id or not str(user_id).isdigit():
        raise ValueError("Gültige user_id wird benötigt")

    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            raise ValueError("Mitglied nicht gefunden")

    check = can_act_on_member(
        guild, member,
        actor=actor, perms=("moderate_members",), is_timeout=True,
    )
    if not check:
        if check.issue is Issue.ACTOR_HIERARCHY:
            raise ValueError("Du kannst keine Timeouts bei Mitgliedern aufheben, die dir in der Rolle gleich- oder übergeordnet sind")
        raise ValueError("Dir fehlt die Berechtigung 'Timeout aufheben' (Moderate Members) oder die Hierarchie passt nicht")

    try:
        await member.timeout(None, reason=f"Nexus Dashboard — Timeout aufgehoben von {actor}")
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung (Hierarchie?)")

    return {"user_id": user_id}
