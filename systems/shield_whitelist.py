"""Whitelist fürs Anti-Nuke-Shield (cogs/shield.py): Bots und Rollen, deren Aktionen das Shield gar nicht
bewertet — keine Punkte, keine Vorwarnung, keine Strafe (wie beim Serverbesitzer).

Anlass: Moderations-Bots kicken oder bannen im Alltag manchmal mehrere Accounts in Sekundenbruchteilen (etwa bei
Spam oder einem Raid) und lösten so die Vorwarnung aus. Eine zweite Vorwarnung innerhalb von 2 Minuten hätte
den Bot gebannt und den Einladenden in Quarantäne gesteckt.

Wer darf was (gestaffelt, weil die Whitelist das Shield gezielt aushebelt):
  - Bots:  Serverbesitzer und Administratoren — Bots werden per ID eingetragen, nicht per Rolle: sonst wäre
           jeder ausgenommen, der diese Rolle bekommt.
  - Rollen: nur der Serverbesitzer — ein gekaperter Admin könnte sonst seine eigene Rolle eintragen und
           ungestört nuken. Aus demselben Grund darf nur der Besitzer das Shield ausschalten.
Verlässt ein Bot den Server oder wird eine Rolle gelöscht, fällt der Eintrag weg (cogs/shield.py).
"""

import discord

from database import db_automod

MAX_BOTS = 25
MAX_ROLES = 10

# Rechte, mit denen ein Bot dem Server ernsthaft schaden kann — für die Markierung „Mod-Rechte“
MOD_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "ban_members", "kick_members",
)


class WhitelistError(Exception):
    """Änderung nicht übernommen. key: Übersetzungs-Key (dash_err_shield_*), params für den Text."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key, self.params = key, params


def has_mod_permissions(perms: discord.Permissions) -> bool:
    return any(getattr(perms, name) for name in MOD_PERMISSIONS)


def is_owner(guild: discord.Guild, member: discord.abc.User) -> bool:
    return member.id == guild.owner_id


def can_manage_bots(guild: discord.Guild, member: discord.Member) -> bool:
    return is_owner(guild, member) or member.guild_permissions.administrator


async def is_exempt(guild: discord.Guild, user: discord.abc.User) -> bool:
    """Nimmt das Shield diesen Akteur aus? Bots über ihre ID, Menschen über ihre Rollen."""
    whitelist = await db_automod.get_shield_whitelist(guild.id)
    if user.bot and user.id in whitelist.bots:
        return True
    if not whitelist.roles:
        return False
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    # Mitglied nicht im Cache → nicht ausnehmen. Lieber eine Vorwarnung zu viel als einen Angreifer durchlassen.
    return member is not None and any(role.id in whitelist.roles for role in member.roles)


# ─── Ändern (Dashboard, später auch Rückfragen in Discord) ───────────────────

async def set_bot(guild: discord.Guild, actor: discord.Member, bot_id: int, whitelisted: bool) -> None:
    if not can_manage_bots(guild, actor):
        raise WhitelistError("dash_err_shield_admin_only")
    whitelist = await db_automod.get_shield_whitelist(guild.id)
    if not whitelisted:
        await db_automod.remove_shield_whitelist(guild.id, [bot_id])   # auch, wenn der Bot nicht mehr da ist
        return
    if bot_id in whitelist.bots:
        return
    member = guild.get_member(bot_id)
    if member is None:
        try:
            member = await guild.fetch_member(bot_id)
        except (discord.NotFound, discord.Forbidden):
            member = None
    if member is None or not member.bot or member.id == guild.me.id:
        raise WhitelistError("dash_err_shield_not_a_bot")
    if len(whitelist.bots) >= MAX_BOTS:
        raise WhitelistError("dash_err_shield_too_many_bots", max=MAX_BOTS)
    await db_automod.add_shield_whitelist(guild.id, "bot", [bot_id], actor.id)


async def set_roles(guild: discord.Guild, actor: discord.Member, role_ids: list[int]) -> None:
    """Rollen-Whitelist ersetzen (nur der Serverbesitzer)."""
    if not is_owner(guild, actor):
        raise WhitelistError("dash_err_shield_owner_only")
    wanted = list(dict.fromkeys(role_ids))
    for role_id in wanted:
        role = guild.get_role(role_id)
        # @everyone nähme alle aus; Bot-Rollen (managed) gehören über die Bot-Whitelist
        if role is None or role.is_default() or role.managed:
            raise WhitelistError("dash_err_shield_role_invalid")
    if len(wanted) > MAX_ROLES:
        raise WhitelistError("dash_err_shield_too_many_roles", max=MAX_ROLES)
    current = (await db_automod.get_shield_whitelist(guild.id)).roles
    await db_automod.remove_shield_whitelist(guild.id, [r for r in current if r not in wanted])
    await db_automod.add_shield_whitelist(guild.id, "role", [r for r in wanted if r not in current], actor.id)


async def forget(guild_id: int, target_id: int) -> None:
    """Bot hat den Server verlassen oder Rolle wurde gelöscht. Kommt der Bot zurück, wird neu entschieden."""
    whitelist = await db_automod.get_shield_whitelist(guild_id)
    if target_id in whitelist.bots or target_id in whitelist.roles:
        await db_automod.remove_shield_whitelist(guild_id, [target_id])


# ─── Lesen (Dashboard) ───────────────────────────────────────────────────────

async def bot_members(guild: discord.Guild) -> list[discord.Member]:
    """Alle Bots auf dem Server außer Nexus. Nexus lädt die Mitgliederliste nicht beim Start (bot.py:
    chunk_guilds_at_startup=False) — ohne einmaliges Nachladen fehlten Bots in der Liste."""
    if not guild.chunked:
        await guild.chunk(cache=True)
    return [m for m in guild.members if m.bot and m.id != guild.me.id]


async def overview(guild: discord.Guild, actor: discord.Member) -> dict:
    whitelist = await db_automod.get_shield_whitelist(guild.id)
    bots = await bot_members(guild)
    present = {m.id for m in bots}
    entries = [
        {
            "id": str(m.id),
            "name": m.display_name,
            "avatar": str(m.display_avatar.url),
            "mod_permissions": has_mod_permissions(m.guild_permissions),
            "whitelisted": m.id in whitelist.bots,
        }
        for m in bots
    ]
    # Eingetragen, aber gerade nicht im Cache (z. B. Discord-Störung) — bleibt sichtbar, damit man ihn austragen kann
    entries += [
        {"id": str(bot_id), "name": None, "avatar": None, "mod_permissions": False, "whitelisted": True}
        for bot_id in whitelist.bots if bot_id not in present
    ]
    # Mod-Rechte zuerst, dann nach Name
    entries.sort(key=lambda e: (not e["mod_permissions"], (e["name"] or "").lower()))
    return {
        "bots": entries,
        "roles": [str(r) for r in sorted(whitelist.roles)],
        "can_manage_bots": can_manage_bots(guild, actor),
        "is_owner": is_owner(guild, actor),
        "limits": {"bots": MAX_BOTS, "roles": MAX_ROLES},
    }
