from dataclasses import dataclass, field
from enum import Enum

import discord

# ─── Ergebnis-Typ ──────────────────────────────────────────────────────────────

class Issue(str, Enum):
    OK                 = "ok"
    MISSING_PERMS      = "missing_perms"       # Nexus fehlt eine Permission
    BOT_HIERARCHY      = "bot_hierarchy"       # Ziel steht gleich/über Nexus
    ACTOR_HIERARCHY    = "actor_hierarchy"     # Ziel steht gleich/über dem ausführenden User
    TARGET_OWNER       = "target_owner"        # Server-Owner ist unantastbar
    TARGET_SELF        = "target_self"         # Nexus oder der User selbst
    TARGET_ADMIN       = "target_admin"        # z.B. Timeout gegen Admins (Discord blockt das)
    ROLE_MANAGED       = "role_managed"        # Bot-/Integrations-/Booster-Rolle
    ROLE_DEFAULT       = "role_default"        # @everyone
    NO_CHANNEL_ACCESS  = "no_channel_access"   # Channel nicht sichtbar

@dataclass(frozen=True)
class PermCheck:
    issue: Issue = Issue.OK
    missing: tuple[str, ...] = field(default_factory=tuple)
    subject: str | None = None  # Mention/Name für die Fehlermeldung

    @property
    def ok(self) -> bool:
        return self.issue is Issue.OK

    def __bool__(self) -> bool:
        return self.ok

OK = PermCheck()

# Rechte, die in der Diagnose als "gefährlich" gelten, wenn eine Rolle damit über Nexus steht.
DANGEROUS_PERMS: frozenset[str] = frozenset({
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "ban_members", "kick_members",
})

# ─── Basis-Bausteine ───────────────────────────────────────────────────────────

def missing_perms(
    where: discord.Guild | discord.abc.GuildChannel,
    *perms: str,
) -> tuple[str, ...]:
    """Welche der angefragten Permissions fehlen Nexus (guild-weit oder im Channel)?"""
    if isinstance(where, discord.Guild):
        have = where.me.guild_permissions
    else:
        have = where.permissions_for(where.guild.me)
    return tuple(p for p in perms if not getattr(have, p, False))


def _outranks(a: discord.Member, b: discord.Member) -> bool:
    """True wenn a strikt über b steht. Owner steht über allen."""
    if a.guild.owner_id == a.id:
        return b.id != a.id
    if b.guild.owner_id == b.id:
        return False
    return a.top_role > b.top_role

# ─── Aktionen gegen Member ─────────────────────────────────────────────────────

def can_act_on_member(
    guild: discord.Guild,
    target: discord.Member,
    *,
    actor: discord.Member | None = None,
    perms: tuple[str, ...] = (),
    is_timeout: bool = False,
) -> PermCheck:
    """
    Darf Nexus (und optional der ausführende User) gegen `target` vorgehen?
    Für kick/ban/timeout/Rollen am Member/Nickname.
    """
    me = guild.me

    if target.id == me.id or (actor is not None and target.id == actor.id):
        return PermCheck(Issue.TARGET_SELF, subject=target.mention)

    if target.id == guild.owner_id:
        return PermCheck(Issue.TARGET_OWNER, subject=target.mention)

    if miss := missing_perms(guild, *perms):
        return PermCheck(Issue.MISSING_PERMS, missing=miss)

    if not _outranks(me, target):
        return PermCheck(Issue.BOT_HIERARCHY, subject=target.mention)

    if actor is not None and not _outranks(actor, target):
        return PermCheck(Issue.ACTOR_HIERARCHY, subject=target.mention)

    if is_timeout and target.guild_permissions.administrator:
        return PermCheck(Issue.TARGET_ADMIN, subject=target.mention)

    return OK

# ─── Rollen verwalten (vergeben/entziehen/bearbeiten) ─────────────────────────

def can_manage_role(
    guild: discord.Guild,
    role: discord.Role,
    *,
    actor: discord.Member | None = None,
) -> PermCheck:
    """Darf Nexus diese Rolle vergeben/entziehen? (Level-Rewards, Autorollen, Reaction Roles)"""
    if role.is_default():
        return PermCheck(Issue.ROLE_DEFAULT, subject=role.name)

    if role.managed:
        return PermCheck(Issue.ROLE_MANAGED, subject=role.mention)

    if miss := missing_perms(guild, "manage_roles"):
        return PermCheck(Issue.MISSING_PERMS, missing=miss)

    if role >= guild.me.top_role:
        return PermCheck(Issue.BOT_HIERARCHY, subject=role.mention)

    if actor is not None and actor.id != guild.owner_id and role >= actor.top_role:
        return PermCheck(Issue.ACTOR_HIERARCHY, subject=role.mention)

    return OK

# ─── Channels ──────────────────────────────────────────────────────────────────

def can_use_channel(channel: discord.abc.GuildChannel, *perms: str) -> PermCheck:
    """Kann Nexus in diesem Channel X tun? view_channel wird immer mitgeprüft."""
    have = channel.permissions_for(channel.guild.me)
    if not have.view_channel:
        return PermCheck(Issue.NO_CHANNEL_ACCESS, subject=channel.mention)
    if miss := tuple(p for p in perms if not getattr(have, p, False)):
        return PermCheck(Issue.MISSING_PERMS, missing=miss, subject=channel.mention)
    return OK

def can_apply_overwrites(
    where: discord.Guild | discord.CategoryChannel | discord.abc.GuildChannel,
    overwrites: dict,
) -> PermCheck:
    """
    Ohne Admin darf Nexus in Overwrites nur Rechte ERLAUBEN, die es dort selbst hat.
    Vor dem Erstellen von Ticket-Channels o.ä. aufrufen.
    """
    base = missing_perms(where, "manage_channels", "manage_roles")
    if base:
        return PermCheck(Issue.MISSING_PERMS, missing=base)

    if isinstance(where, discord.Guild):
        have = where.me.guild_permissions
    else:
        have = where.permissions_for(where.guild.me)

    lacking: set[str] = set()
    for ow in overwrites.values():
        allow, _deny = ow.pair()
        for name, value in allow:
            if value and not getattr(have, name, False):
                lacking.add(name)

    if lacking:
        return PermCheck(Issue.MISSING_PERMS, missing=tuple(sorted(lacking)))
    return OK

# ─── Diagnose (/setup check, Dashboard) ───────────────────────────────────────

def dangerous_roles_above_bot(guild: discord.Guild) -> list[discord.Role]:
    """Rollen mit gefährlichen Rechten, die gleich/über Nexus stehen — gegen deren Träger ist Nexus machtlos."""
    my_top = guild.me.top_role
    my_ids = {r.id for r in guild.me.roles}
    result = []
    for role in guild.roles:
        if role.id in my_ids or role.is_default() or role.managed:
            continue
        if role >= my_top and any(getattr(role.permissions, p) for p in DANGEROUS_PERMS):
            result.append(role)
    return sorted(result, key=lambda r: r.position, reverse=True)


def everyone_is_dangerous(guild: discord.Guild) -> tuple[str, ...]:
    """Gefährliche Rechte auf @everyone — sollte die Diagnose laut anzeigen."""
    perms = guild.default_role.permissions
    return tuple(p for p in sorted(DANGEROUS_PERMS) if getattr(perms, p))

# ─── Ausgabe ───────────────────────────────────────────────────────────────────

def perm_label(name: str) -> str:
    return name.replace("_", " ").title()


_FALLBACKS: dict[Issue, str] = {
    Issue.MISSING_PERMS:     "❌ I'm missing the following permissions: {perms}",
    Issue.BOT_HIERARCHY:     "❌ {subject} is ranked equal to or higher than my role. Please move the Nexus role above it.",
    Issue.ACTOR_HIERARCHY:   "❌ {subject} is ranked equal to or higher than you.",
    Issue.TARGET_OWNER:      "❌ The server owner cannot be targeted.",
    Issue.TARGET_SELF:       "❌ This action can't target that user.",
    Issue.TARGET_ADMIN:      "❌ Discord doesn't allow timing out members with Administrator ({subject}).",
    Issue.ROLE_MANAGED:      "❌ {subject} is managed by an integration and can't be assigned.",
    Issue.ROLE_DEFAULT:      "❌ @everyone can't be assigned.",
    Issue.NO_CHANNEL_ACCESS: "❌ I can't see {subject}. Please give Nexus access to that channel.",
}

def format_issue(check: PermCheck, lang: dict[str, str] | None = None) -> str:
    """Lokalisierte Meldung. Keys: perm_issue_<issue>, z.B. perm_issue_bot_hierarchy."""
    if check.ok:
        return ""
    lang = lang or {}
    template = lang.get(f"perm_issue_{check.issue.value}", _FALLBACKS[check.issue])
    return template.format(
        subject=check.subject or "",
        perms=", ".join(f"`{perm_label(p)}`" for p in check.missing),
    )

# ─── Bestehende API (setup.py) ──────────────────────────────────

def check_bot_hierarchy(guild: discord.Guild) -> bool:
    """Prüft, ob der Bot über mindestens einer fremden, nicht-administrativen Rolle steht."""
    bot_role_ids = {r.id for r in guild.me.roles}
    bot_role = guild.me.top_role

    for role in guild.roles:
        if role.id in bot_role_ids:
            continue
        if role.name == "@everyone":
            continue
        if not role.permissions.administrator and bot_role > role:
            return True
    return False

def get_bot_position_info(guild: discord.Guild) -> tuple[int, int]:
    roles = [r for r in guild.roles if r.name != "@everyone"]
    total = len(roles)
    bot_pos = guild.me.top_role.position
    return bot_pos, total