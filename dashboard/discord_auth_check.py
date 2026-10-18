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