from dataclasses import dataclass
import discord
from database import db_levels, db_reaction_roles, db_stream_alerts
from systems.hierarchy import (
    can_manage_role, can_use_channel, missing_perms,
    dangerous_roles_above_bot, everyone_is_dangerous, perm_label,
)

# Rechte, die Nexus für den vollen Funktionsumfang ohne Administrator braucht
RECOMMENDED_PERMS = (
    "view_channel", "send_messages", "embed_links", "attach_files",
    "read_message_history", "add_reactions", "manage_messages",
    "manage_roles", "manage_channels", "moderate_members",
    "kick_members", "ban_members", "view_audit_log",
)

@dataclass(frozen=True)
class GuildIssue:
    """Ein einzelnes, bereits lokalisiertes Problem — von /diagnose (Embed)
    und dem Dashboard (Notifications) gemeinsam konsumierbar."""
    section: str   # "perms" | "hierarchy" | "channels" | "roles" | "tickets" | "alerts"
    severity: str  # "info" | "warn" | "error" — "info" zählt NICHT als Problem
    text: str

def _perm_list(perms) -> str:
    return ", ".join(f"`{perm_label(p)}`" for p in perms)

def _name_list(names: list[str], limit: int = 3) -> str:
    """Streamer-Namen für eine Zeile: höchstens `limit` Stück, Rest als '+N'."""
    unique = list(dict.fromkeys(names))
    shown = ", ".join(unique[:limit])
    return shown + (f" +{len(unique) - limit}" if len(unique) > limit else "")

async def get_guild_issues(guild: discord.Guild, settings: dict, lang: dict) -> list[GuildIssue]:
    """Zentrale Diagnose-Logik. Gibt NUR tatsächliche Probleme zurück (kein
    'alles ok'-Rauschen) — der Dashboard-Aufrufer kann die Liste direkt als
    Notifications rendern, /diagnose baut sich daraus zusätzlich die
    Sektionen mit 'alles ok'-Zeilen für Bereiche ohne Probleme.
    """
    modules = settings.get("modules") or {}
    issues: list[GuildIssue] = []

    # ─── 1. Grundrechte ────────────────────────────────────────────────
    if not guild.me.guild_permissions.administrator:
        if miss := missing_perms(guild, *RECOMMENDED_PERMS):
            issues.append(GuildIssue("perms", "warn", lang.get(
                "diag_perms_missing", "Missing permissions: {perms}"
            ).format(perms=_perm_list(miss))))

    # ─── 2. Hierarchie ─────────────────────────────────────────────────
    everyone_perms = everyone_is_dangerous(guild)
    if everyone_perms:
        issues.append(GuildIssue("hierarchy", "error", lang.get(
            "diag_everyone_dangerous", "@everyone has dangerous permissions: {perms}"
        ).format(perms=_perm_list(everyone_perms))))

    above = dangerous_roles_above_bot(guild)
    if above:
        names = ", ".join(r.mention for r in above)
        # Bewusst nur informativ (wie im ursprünglichen /diagnose): kein Fehler, zählt nicht als Problem
        issues.append(GuildIssue("hierarchy", "info", lang.get(
            "diag_roles_above_short", "Roles with moderation rights are above Nexus: {roles}"
        ).format(roles=names)))

    # ─── 3. Konfigurierte Channels ─────────────────────────────────────
    channel_checks = [
        ("log_channel_id",        lang.get("settings_log", "📋 Log-Channel"),        ("send_messages", "embed_links", "attach_files")),
        ("welcome_channel_id",    lang.get("settings_welcome", "👋 Welcome Channel"), ("send_messages", "embed_links")),
        ("rules_channel_id",      lang.get("settings_rules", "📜 Rules Channel"),     ("send_messages", "embed_links")),
        ("ticket_log_channel_id", lang.get("diag_ticket_log", "🎫 Ticket log"),       ("send_messages", "embed_links", "attach_files")),
    ]
    if settings.get("goodbye_enabled", False):
        channel_checks.append((
            "goodbye_channel_id", lang.get("diag_goodbye_channel", "👋 Goodbye channel"),
            ("send_messages", "embed_links"),
        ))
    if modules.get("boosts"):
        channel_checks.append((
            "boost_channel_id", lang.get("diag_boost_channel", "🚀 Boost channel"),
            ("send_messages", "embed_links"),
        ))
    check_settings = dict(settings)
    if modules.get("levels"):
        levels_cfg = await db_levels.get_levels_settings(guild.id)
        check_settings["levelup_channel_id"] = levels_cfg.get("levelup_channel_id")
        channel_checks.append((
            "levelup_channel_id", lang.get("diag_levelup_channel", "📈 Level-up channel"),
            ("send_messages", "embed_links"),
        ))
    for key, label, perms in channel_checks:
        cid = check_settings.get(key)
        if not cid:
            continue
        channel = guild.get_channel(cid)
        if channel is None:
            issues.append(GuildIssue("channels", "error", f"{label}: " + lang.get(
                "diag_channel_deleted", "channel no longer exists"
            )))
            continue
        check = can_use_channel(channel, *perms)
        if not check:
            reason = (
                lang.get("diag_channel_no_access", "no access")
                if not check.missing else _perm_list(check.missing)
            )
            issues.append(GuildIssue("channels", "error", f"{label}: {channel.mention} — {reason}"))

    # ─── 4. Rollen aus Modulen (Levels, Reaction Roles) ────────────────
    role_sources: list[tuple[str, int]] = []
    if modules.get("levels"):
        for row in await db_levels.get_all_level_roles(guild.id):
            role_sources.append((lang.get("diag_src_level", "Level {level}").format(level=row["level"]), row["role_id"]))
    if modules.get("reaction_roles"):
        for msg in await db_reaction_roles.get_all_reaction_role_messages(guild.id):
            for rr in await db_reaction_roles.get_reaction_roles(guild.id, msg["message_id"]):
                role_sources.append((lang.get("diag_src_rr", "Reaction Role"), rr["role_id"]))

    seen: set[int] = set()
    for source, role_id in role_sources:
        if role_id in seen:
            continue
        seen.add(role_id)
        role = guild.get_role(role_id)
        if role is None:
            issues.append(GuildIssue("roles", "error", f"{source}: " + lang.get(
                "diag_role_deleted", "role no longer exists"
            )))
            continue
        check = can_manage_role(guild, role)
        if not check:
            issues.append(GuildIssue("roles", "error", f"{source}: {role.mention} — " + lang.get(
                "diag_role_not_manageable", "Nexus can't assign this role"
            )))

    # ─── 5. Tickets ────────────────────────────────────────────────────
    if modules.get("tickets"):
        if miss := missing_perms(guild, "manage_channels", "manage_roles"):
            issues.append(GuildIssue("tickets", "error", lang.get(
                "diag_tickets_missing", "Tickets can't be created. Missing: {perms}"
            ).format(perms=_perm_list(miss))))

    # ─── 6. Stream-Alerts (Ziel-Channels + Ping-Rollen) ────────────────
    if modules.get("stream_alerts"):
        subs = await db_stream_alerts.get_subscriptions_for_guild(guild.id)

        # Pro Ziel-Channel EINE Zeile, auch wenn dort mehrere Streamer landen
        by_channel: dict[int, list[str]] = {}
        for sub in subs:
            by_channel.setdefault(sub["channel_id"], []).append(sub["streamer_name"])
        for cid, streamers in by_channel.items():
            label = lang.get("diag_alert_channel", "📡 Alerts ({streamers})").format(streamers=_name_list(streamers))
            channel = guild.get_channel(cid)
            if channel is None:
                issues.append(GuildIssue("alerts", "error", f"{label}: " + lang.get(
                    "diag_channel_deleted", "channel no longer exists"
                )))
                continue
            check = can_use_channel(channel, "send_messages", "embed_links")
            if not check:
                reason = (
                    lang.get("diag_channel_no_access", "no access")
                    if not check.missing else _perm_list(check.missing)
                )
                issues.append(GuildIssue("alerts", "error", f"{label}: {channel.mention} — {reason}"))

        roles: dict[int, list[str]] = {}
        for platform in sorted({sub["platform"] for sub in subs}):
            role_id = await db_stream_alerts.get_ping_role(guild.id, platform)
            if role_id:
                roles.setdefault(role_id, []).append(platform.capitalize())

        can_ping_any = guild.me.guild_permissions.mention_everyone
        for role_id, platforms in roles.items():
            role = guild.get_role(role_id)
            if role is None:
                issues.append(GuildIssue("alerts", "warn", lang.get(
                    "diag_alert_role_deleted", "🔔 Ping role for {platform} no longer exists"
                ).format(platform=", ".join(platforms))))
            elif not role.mentionable and not can_ping_any:
                issues.append(GuildIssue("alerts", "warn", lang.get(
                    "diag_alert_role_unmentionable",
                    "🔔 {role} can't be pinged: it isn't mentionable and I lack “Mention Everyone”",
                ).format(role=role.mention)))

    return issues