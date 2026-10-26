import discord
from .registry import get_bot

async def verify_guild_access(user_id: int, guild_id: int) -> bool:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        return False
    if guild.owner_id == user_id:
        return True
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden):
            return False
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild

async def get_acting_member(guild: discord.Guild, payload: dict) -> discord.Member:
    """Holt den im Dashboard eingeloggten User als Member dieser Guild.
    Die Auth-Middleware hat acting_user_id bereits auf Guild-Zugriff geprüft,
    aber für Rechte-/Hierarchie-Checks brauchen wir das volle Member-Objekt."""
    raw_user_id = payload.get("acting_user_id")
    if not raw_user_id or not str(raw_user_id).isdigit():
        raise ValueError("acting_user_id wird benötigt")

    acting_user_id = int(raw_user_id)
    member = guild.get_member(acting_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(acting_user_id)
        except discord.NotFound:
            raise ValueError("Du bist kein Mitglied dieses Servers (mehr)")
    return member
