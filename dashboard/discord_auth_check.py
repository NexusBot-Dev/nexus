import discord
from .registry import get_bot


async def resolve_actor(user_id: int, guild_id: int) -> tuple[discord.Guild, discord.Member] | None:
    """Löst Guild und den eingeloggten Dashboard-User als Member auf.

    Gibt None zurück, wenn der Bot nicht auf der Guild ist, der User dort kein
    Mitglied (mehr) ist oder weder Administrator noch 'Server verwalten' hat.
    Discord-Ausfälle (HTTPException außer NotFound/Forbidden) werden bewusst
    durchgereicht — die Middleware macht daraus ein 503.
    """
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        return None

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    # guild_permissions liefert für den Server-Owner automatisch alle Rechte
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return guild, member
    return None