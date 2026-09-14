"""
Dies ist eine funktionslose Platzhalter-Version des Premium-Systems.
Die echte Implementierung (inkl. Zahlungsabwicklung, Discord-Entitlements)
ist Teil der proprietären Nexus-Infrastruktur und nicht Teil dieses Repos.
Self-Hoster haben standardmäßig keinen Zugriff auf Premium-Features.
"""

async def is_premium(guild_id: int) -> bool:
    return False

async def set_premium_from_discord(bot, guild_id, entitlement, reason, expires_at=None):
    pass

async def remove_premium(bot, guild_id, reason):
    pass