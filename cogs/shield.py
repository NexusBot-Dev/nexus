import logging
import time
import discord
import asyncio
from discord.ext import commands, tasks
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal
from database import db_settings
from database import db_automod
from cogs.utils import get_lang, get_member_safe

log = logging.getLogger(__name__)

# ─── Anti-Nuke Scoring & Tracking (In-Memory) ──────────────────────────────
_shield_action_tracker = defaultdict(lambda: defaultdict(list))
_shield_initiated_bans: set[tuple[int, int]] = set()
_quarantined_users = set()
_restore_cache = defaultdict(list)
_restore_flagged: dict[tuple[int, int], float] = {}   # (guild, user) -> monotonic-Zeitpunkt der letzten Vorwarnung

RESTORE_WINDOW = 15
RESTORE_STAGGER_SECONDS = 0.35

SHIELD_WINDOW_SECONDS = 5          # Zeitfenster für den Score
RESTORE_THRESHOLD = 3.0            # Stufe 1: Vorwarnung — wiederherstellen, niemanden bestrafen
PUNISH_THRESHOLD = 6.0             # Stufe 2: volle Reaktion
ESCALATION_WINDOW_SECONDS = 120    # erneute Stufe 1 innerhalb dieser Zeit wird zu Stufe 2

# Gewichtung der einzelnen Aktionen
SHIELD_ACTION_WEIGHTS = {
    discord.AuditLogAction.channel_delete:     1.0,
    discord.AuditLogAction.role_delete:        1.0,
    discord.AuditLogAction.bot_add:            1.0,
    discord.AuditLogAction.kick:               0.75,
    discord.AuditLogAction.ban:                0.75,
    discord.AuditLogAction.role_update:        0.3,
    discord.AuditLogAction.member_role_update: 0.2,
}

DANGEROUS_PERMS = {
    "administrator", "manage_roles", "manage_channels", "manage_guild",
    "manage_webhooks", "kick_members", "ban_members", "mention_everyone",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _cache_restore_entry(guild_id: int, entry_type: str, data: dict) -> None:
    now = datetime.now(timezone.utc)
    cached = _restore_cache[guild_id]
    cached = [e for e in cached if now - e["timestamp"] < timedelta(seconds=RESTORE_WINDOW)]
    cached.append({"timestamp": now, "type": entry_type, "data": data})
    _restore_cache[guild_id] = cached

def _serialize_overwrites(overwrites: dict) -> list[dict]:
    result = []
    for target, overwrite in overwrites.items():
        result.append({
            "id": target.id,
            "is_role": isinstance(target, discord.Role),
            "allow": overwrite.pair()[0].value,
            "deny": overwrite.pair()[1].value,
        })
    return result

# ─── Scoring ─────────────────────────────────────────────────────────────────

def _is_dangerous_permission_change(before, after) -> bool:
    try:
        before_perms = {p for p, v in iter(before.permissions) if v} if before.permissions else set()
        after_perms  = {p for p, v in iter(after.permissions) if v}  if after.permissions  else set()
    except (AttributeError, TypeError):
        return False
    gained = after_perms - before_perms
    return bool(gained & DANGEROUS_PERMS)

def _calc_tempo(user_actions: list[dict]) -> tuple[float | None, float]:
    if len(user_actions) < 3:
        return None, 1.0

    timestamps = sorted(e["timestamp"] for e in user_actions)
    intervals = [(t2 - t1).total_seconds() for t1, t2 in zip(timestamps, timestamps[1:])]
    avg_interval = sum(intervals) / len(intervals)
    variance = sum((i - avg_interval) ** 2 for i in intervals) / len(intervals)

    if avg_interval < 0.5 and variance < 0.05:
        multiplier = 2.5
    elif avg_interval < 1.5:
        multiplier = 1.3
    elif avg_interval < 4.0:
        multiplier = 1.0
    else:
        multiplier = 0.7

    return round(avg_interval * 1000), multiplier

def _event_weight(guild: discord.Guild, user_actions: list[dict], e: dict) -> float:
    action = e["action"]
    weight = SHIELD_ACTION_WEIGHTS.get(action, 0.0) * e.get("count", 1)

    if action in (discord.AuditLogAction.role_update, discord.AuditLogAction.member_role_update):
        dangerous = _is_dangerous_permission_change(e.get("before"), e.get("after"))
        weight *= 1.5 if dangerous else 0.1

    if action == discord.AuditLogAction.channel_delete:
        total_channels = max(len(guild.channels), 1)
        deletes_in_window = sum(x.get("count", 1) for x in user_actions if x["action"] == discord.AuditLogAction.channel_delete)
        channel_ratio = deletes_in_window / total_channels
        weight *= (1 + min(channel_ratio * 4, 3))

    return weight

def calculate_score(guild: discord.Guild, user_actions: list[dict]) -> dict:
    tempo_avg_ms, tempo_multiplier = _calc_tempo(user_actions)
    total_weight = sum(_event_weight(guild, user_actions, e) for e in user_actions)
    total_score = total_weight * tempo_multiplier

    return {
        "score": round(total_score, 3),
        "would_restore": total_score >= RESTORE_THRESHOLD,
        "would_punish": total_score >= PUNISH_THRESHOLD,
        "tempo_avg_ms": tempo_avg_ms,
        "tempo_multiplier": round(tempo_multiplier, 3),
    }

_background_tasks: set[asyncio.Task] = set()

def _fire_and_forget(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)
    return task

def _on_background_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("[SHIELD] Fehler in einem Hintergrund-Task: %s", exc, exc_info=exc)

# ─── Shield Response Helpers ─────────────────────────────────────────────────

async def _pause_all_invites(guild: discord.Guild) -> bool:
    try:
        pause_until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        route = discord.http.Route("PUT", f"/guilds/{guild.id}/incident-actions")
        await guild._state.http.request(
            route,
            json={"invites_disabled_until": pause_until},
            reason="[Nexus Shield] Anti-Nuke getriggert — Invites temporär pausiert"
        )
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("[SHIELD] Konnte Invites via Incident-Action auf %s nicht pausieren: %s", guild.name, e)
        return False

async def _strip_roles(member: discord.Member, reason: str) -> list[discord.Role] | None:
    """Entfernt alle Rollen. Gibt die entfernten Rollen zurück (für das Log) oder None bei Fehlschlag."""
    removed = [r for r in member.roles if not r.is_default() and not r.managed]
    try:
        await member.edit(roles=[], reason=reason)
        return removed
    except (discord.Forbidden, discord.HTTPException):
        return None

async def _quarantine_member(member: discord.Member, roles_reason: str, timeout_reason: str) -> list[discord.Role] | None:
    """Rollen entfernen + 28 Tage Timeout. Der Timeout wird auch versucht, wenn das Entfernen scheitert."""
    removed = await _strip_roles(member, roles_reason)
    try:
        until = discord.utils.utcnow() + timedelta(days=28) - timedelta(minutes=5)
        await member.timeout(until, reason=timeout_reason)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("[SHIELD] Konnte %s nicht timeouten: %s", member, e)
    return removed

async def _punish_member(
    guild: discord.Guild,
    member: discord.Member,
    action: Literal["ban", "kick"],
    reason: str
) -> bool:
    try:
        if action == "ban":
            await guild.ban(member, reason=reason)
        elif action == "kick":
            await guild.kick(member, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

async def _find_bot_authorizer(guild: discord.Guild, bot_id: int) -> discord.Member | discord.User | None:
    try:
        integrations = await guild.integrations()
        for integration in integrations:
            app = getattr(integration, "application", None)
            if app:
                bot_obj = getattr(app, "bot", None)
                target_bot_id = getattr(bot_obj, "id", None) if bot_obj else getattr(app, "id", None)
                if target_bot_id == bot_id and integration.user:
                    log.info("[SHIELD] Autorisierer %s via Integration für Bot %s ermittelt.", integration.user, bot_id)
                    return integration.user
    except discord.Forbidden:
        log.warning("[SHIELD] Keine Berechtigung 'Manage Server' für guild.integrations auf %s.", guild.name)
    except discord.HTTPException as e:
        log.warning("[SHIELD] Fehler beim Abrufen der Integrationen auf %s: %s", guild.name, e)

    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=50):
            if entry.target and entry.target.id == bot_id:
                log.info("[SHIELD] Autorisierer %s via Audit-Log für Bot %s ermittelt.", entry.user, bot_id)
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        log.exception("[SHIELD] Fehler beim Durchsuchen des Audit-Logs nach Bot-Autorisierer auf %s.", guild.name)

    return None

def _format_removed_roles(lang: dict, roles: list[discord.Role] | None) -> str:
    if not roles:
        return ""
    text = ", ".join(r.mention for r in roles)
    if len(text) > 900:
        text = text[:900].rsplit(",", 1)[0] + " …"
    return "\n" + lang.get("shield_log_removed_roles", "Removed roles: {roles}").format(roles=text)

async def _send_shield_log(
    bot,
    guild: discord.Guild,
    lang: dict,
    attacker: discord.Member | discord.User,
    attacker_is_bot: bool,
    total_score: float,
    *,
    warning: bool = False,
    escalated: bool = False,
    invites_paused: bool | None = None,
    attacker_action_taken: bool = False,
    attacker_roles: list[discord.Role] | None = None,
    authorizer: discord.Member | discord.User | None = None,
    authorizer_demoted: bool | None = None,
    authorizer_roles: list[discord.Role] | None = None,
    rogue_bot: discord.Member | None = None,
    rogue_bot_demoted: bool | None = None,
    restore_results: dict | None = None,
):
    settings = await db_settings.get_guild(guild.id)
    if settings is None or not settings.get("log_channel_id"):
        return
    channel = bot.get_channel(settings["log_channel_id"])
    if channel is None:
        return

    if warning:
        embed = discord.Embed(
            title=lang.get("shield_warn_title", "⚠️ Nexus Shield — Suspicious Activity"),
            description=lang.get(
                "shield_warn_desc",
                "{user} triggered the early warning (Score: {score}). Deleted roles and channels were restored, nobody was punished. If this repeats within {minutes} minutes, the full reaction follows.",
            ).format(user=attacker.mention, score=f"{total_score:.2f}", minutes=ESCALATION_WINDOW_SECONDS // 60),
            color=discord.Color.orange(),
        )
    else:
        description = lang.get(
            "shield_triggered_desc",
            "Suspicious activity detected from {user} (Score: {score}).",
        ).format(user=attacker.mention, score=f"{total_score:.2f}")
        if escalated:
            description += "\n" + lang.get(
                "shield_log_escalated",
                "Escalated: the activity continued after the early warning.",
            )
        embed = discord.Embed(
            title=lang.get("shield_triggered_title", "🚨 Nexus Shield — Anti-Nuke Triggered!"),
            description=description,
            color=discord.Color.red(),
        )

    attacker_label = lang.get("shield_log_attacker_bot", "🤖 Attacking Bot") if attacker_is_bot else lang.get("shield_log_attacker", "👤 Attacker")
    if warning:
        attacker_status = lang.get("shield_log_warn_no_action", "No action taken (warning only)")
    elif attacker_is_bot:
        attacker_status = lang.get("shield_log_banned", "Banned from server") if attacker_action_taken else lang.get("shield_log_ban_failed", "Ban failed (Missing permissions/hierarchy)")
    else:
        attacker_status = lang.get("shield_log_roles_removed", "Roles removed") if attacker_action_taken else lang.get("shield_log_roles_failed", "Role removal failed (Missing permissions/hierarchy)")
        attacker_status += _format_removed_roles(lang, attacker_roles)
    embed.add_field(
        name=attacker_label,
        value=f"{attacker.mention} (`{attacker.id}`)\n{attacker_status}",
        inline=False,
    )

    if authorizer is not None:
        auth_status = lang.get("shield_log_roles_removed", "Roles removed") if authorizer_demoted else lang.get("shield_log_roles_failed", "Role removal failed (Missing permissions/hierarchy)")
        auth_status += _format_removed_roles(lang, authorizer_roles)
        embed.add_field(
            name=lang.get("shield_log_authorizer", "👤 Authorized by"),
            value=f"{authorizer.mention} (`{authorizer.id}`)\n{auth_status}",
            inline=False,
        )

    if rogue_bot is not None:
        bot_status = lang.get("shield_log_roles_removed", "Roles removed") if rogue_bot_demoted else lang.get("shield_log_roles_failed", "Role removal failed (Missing permissions/hierarchy)")
        embed.add_field(
            name=lang.get("shield_log_bot", "🤖 Recently invited bot"),
            value=f"{rogue_bot.mention} (`{rogue_bot.id}`)\n{bot_status}",
            inline=False,
        )

    if restore_results is not None:
        restore_lines = (
            f"{restore_results['roles']} " + lang.get("shield_log_restore_roles", "Roles") + "\n"
            f"{restore_results['channels']} " + lang.get("shield_log_restore_channels", "Channels")
        )
        if not warning:
            restore_lines += "\n" + f"{restore_results['bans']} " + lang.get("shield_log_restore_bans", "Bans revoked")
        if not guild.me.guild_permissions.administrator:
            restore_lines += "\n\n" + lang.get(
                "shield_log_restore_no_admin",
                "⚠️ Nexus has no Administrator. Roles and channels with permissions Nexus doesn't have may not have been restored.",
            )
        embed.add_field(
            name=lang.get("shield_log_restore_title", "♻️ Restored"),
            value=restore_lines,
            inline=False,
        )

    if invites_paused is not None:
        invite_status = (
            lang.get("shield_log_invites_paused", "🔒 Invites temporarily paused")
            if invites_paused else
            lang.get("shield_log_invites_pause_failed", "⚠️ Failed to pause invites (Missing permissions)")
        )
        embed.add_field(
            name=lang.get("shield_log_invites", "🔗 Invite-Links"),
            value=invite_status,
            inline=False,
        )
    embed.set_footer(text="Nexus • Anti-Nuke Shield")

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("[SHIELD] Keine Berechtigung, Log-Channel %s zu schreiben.", channel)
    except discord.HTTPException:
        log.exception("[SHIELD] Fehler beim Schreiben in Log-Channel %s.", channel)

async def _release_quarantine(guild_id: int, user_id: int):
    await asyncio.sleep(10)
    _quarantined_users.discard((guild_id, user_id))

async def _restore_from_cache(guild: discord.Guild, entries: list[dict]) -> dict:
    results = {"roles": 0, "channels": 0, "bans": 0}
    role_id_map: dict[int, discord.Role] = {}

    roles    = [e["data"] for e in entries if e["type"] == "role"]
    channels = [e["data"] for e in entries if e["type"] == "channel"]
    bans     = [e["data"] for e in entries if e["type"] == "ban"]

    for data in roles:
        try:
            new_role = await guild.create_role(
                name=data["name"],
                color=discord.Color(data["color"]),
                permissions=discord.Permissions(data["permissions"]),
                hoist=data["hoist"],
                mentionable=data["mentionable"],
                reason="[Nexus Shield] Anti-Nuke Restore",
            )
            role_id_map[data["old_id"]] = new_role
            results["roles"] += 1
            await asyncio.sleep(RESTORE_STAGGER_SECONDS)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[SHIELD] Konnte Rolle %s nicht wiederherstellen: %s", data["name"], e)

    for data in channels:
        rebuilt_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        for ow in data["overwrites"]:
            target = None
            if ow["is_role"]:
                target = role_id_map.get(ow["id"]) or guild.get_role(ow["id"])
            else:
                target = guild.get_member(ow["id"])
            if target is None:
                continue
            rebuilt_overwrites[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
            )
        data["overwrites"] = rebuilt_overwrites

    if channels:
        restored_channels = await _restore_deleted_channels(guild, channels)
        results["channels"] = restored_channels

    for data in bans:
        try:
            await guild.unban(discord.Object(id=data["user_id"]), reason="[Nexus Shield] Anti-Nuke Restore")
            results["bans"] += 1
            await asyncio.sleep(RESTORE_STAGGER_SECONDS)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

    return results

async def _restore_deleted_channels(guild: discord.Guild, deleted_data: list[dict]) -> int:
    restored_count = 0
    category_map: dict[int, discord.CategoryChannel] = {}

    categories = [d for d in deleted_data if d["type"] == discord.ChannelType.category]
    for data in categories:
        try:
            new_cat = await guild.create_category(
                name=data["name"],
                position=data["position"],
                overwrites=data["overwrites"],
                reason="[Nexus Shield] Auto-Recreate: Kategorie wiederhergestellt"
            )
            category_map[data["id"]] = new_cat
            restored_count += 1
            await asyncio.sleep(0.35)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[SHIELD] Konnte Kategorie %s nicht wiederherstellen: %s", data["name"], e)

    channels = [d for d in deleted_data if d["type"] != discord.ChannelType.category]
    for data in channels:
        try:
            target_category = None
            if data["category_id"]:
                target_category = category_map.get(data["category_id"])
                if not target_category:
                    target_category = guild.get_channel(data["category_id"])

            if data["type"] == discord.ChannelType.text:
                await guild.create_text_channel(
                    name=data["name"],
                    category=target_category,
                    position=data["position"],
                    topic=data["topic"],
                    slowmode_delay=data["slowmode_delay"],
                    nsfw=data["nsfw"],
                    overwrites=data["overwrites"],
                    reason="[Nexus Shield] Auto-Recreate nach Nuke-Angriff"
                )
                restored_count += 1

            elif data["type"] == discord.ChannelType.voice:
                await guild.create_voice_channel(
                    name=data["name"],
                    category=target_category,
                    position=data["position"],
                    bitrate=data["bitrate"],
                    user_limit=data["user_limit"],
                    overwrites=data["overwrites"],
                    reason="[Nexus Shield] Auto-Recreate nach Nuke-Angriff"
                )
                restored_count += 1

            await asyncio.sleep(0.35)

        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[SHIELD] Konnte Kanal %s nicht wiederherstellen: %s", data["name"], e)

    return restored_count

def _take_restore_entries(guild_id: int, include_bans: bool) -> list[dict]:
    """Entnimmt Einträge aus dem Restore-Cache. Ohne include_bans bleiben Ban-Einträge im Cache
    (Vorwarnstufe: Bans werden nie zurückgenommen, das könnte legitime Moderation sein)."""
    entries = _restore_cache.get(guild_id, [])
    if include_bans:
        _restore_cache.pop(guild_id, None)
        return entries
    taken = [e for e in entries if e["type"] != "ban"]
    if taken:
        _restore_cache[guild_id] = [e for e in entries if e["type"] == "ban"]
    return taken

# ─── Cog ─────────────────────────────────────────────────────────────────────

class Shield(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cleanup_memory_trackers.start()

    def cog_unload(self):
        self._cleanup_memory_trackers.cancel()
        for task in list(_background_tasks):
            task.cancel()

    @tasks.loop(minutes=10)
    async def _cleanup_memory_trackers(self):
        try:
            now = datetime.now(timezone.utc)
            removed_tracker_entries = 0
            for guild_id in list(_shield_action_tracker.keys()):
                for user_id in list(_shield_action_tracker[guild_id].keys()):
                    fresh = [e for e in _shield_action_tracker[guild_id][user_id]
                            if now - e["timestamp"] < timedelta(seconds=SHIELD_WINDOW_SECONDS)]
                    if fresh:
                        _shield_action_tracker[guild_id][user_id] = fresh
                    else:
                        del _shield_action_tracker[guild_id][user_id]
                        removed_tracker_entries += 1
                if not _shield_action_tracker[guild_id]:
                    del _shield_action_tracker[guild_id]

            removed_restore_entries = 0
            for guild_id in list(_restore_cache.keys()):
                fresh = [e for e in _restore_cache[guild_id]
                        if now - e["timestamp"] < timedelta(seconds=RESTORE_WINDOW)]
                if fresh:
                    _restore_cache[guild_id] = fresh
                else:
                    del _restore_cache[guild_id]
                    removed_restore_entries += 1

            mono = time.monotonic()
            removed_flags = 0
            for key in [k for k, t in _restore_flagged.items() if mono - t >= ESCALATION_WINDOW_SECONDS]:
                del _restore_flagged[key]
                removed_flags += 1

            if removed_tracker_entries or removed_restore_entries or removed_flags:
                log.info(
                    "[SHIELD-CLEANUP] %s Tracker-Einträge, %s Restore-Caches, %s Vorwarnungen bereinigt.",
                    removed_tracker_entries, removed_restore_entries, removed_flags,
                )
        except Exception:
            log.exception("[SHIELD-CLEANUP] Fehler beim Bereinigen der In-Memory-Tracker.")

    @_cleanup_memory_trackers.before_loop
    async def _before_cleanup_memory(self):
        await self.bot.wait_until_ready()

    # ─── Cache-Listener für Restore ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        _cache_restore_entry(channel.guild.id, "channel", {
            "id": channel.id,
            "name": channel.name,
            "type": channel.type,
            "category_id": channel.category_id,
            "position": channel.position,
            "overwrites": _serialize_overwrites(channel.overwrites),
            "topic": getattr(channel, "topic", None),
            "slowmode_delay": getattr(channel, "slowmode_delay", 0),
            "nsfw": getattr(channel, "nsfw", False),
            "bitrate": getattr(channel, "bitrate", 64000),
            "user_limit": getattr(channel, "user_limit", 0),
        })

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if role.managed:
            return
        _cache_restore_entry(role.guild.id, "role", {
            "old_id": role.id,
            "name": role.name,
            "color": role.color.value,
            "permissions": role.permissions.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
        })

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if (guild.id, user.id) in _shield_initiated_bans:
            _shield_initiated_bans.discard((guild.id, user.id))
            return
        _cache_restore_entry(guild.id, "ban", {"user_id": user.id})

    # ─── Anti-Nuke Audit Log Listener (Risk-Score Logic) ──────────────────────

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        user  = entry.user

        if not user or user.id == self.bot.user.id:
            return

        # Der Serverbesitzer ist nicht bestrafbar → gar nicht erst bewerten
        if user.id == guild.owner_id:
            return

        # Schutz vor doppelter Bearbeitung während Quarantäne
        key = (guild.id, user.id)
        if key in _quarantined_users:
            return

        if entry.action not in SHIELD_ACTION_WEIGHTS:
            return

        if not bool(await db_automod.get_antinuke(guild.id)):
            return

        # extra.count-Absicherung gegen gebündelte Events
        action_count = getattr(entry.extra, "count", 1) or 1

        now = datetime.now(timezone.utc)
        user_actions = [
            e for e in _shield_action_tracker[guild.id][user.id]
            if now - e["timestamp"] < timedelta(seconds=SHIELD_WINDOW_SECONDS)
        ]
        user_actions.append({
            "timestamp": now,
            "action":    entry.action,
            "count":     action_count,
            "target":    entry.target,
            "before":    entry.before,
            "after":     entry.after,
        })
        _shield_action_tracker[guild.id][user.id] = user_actions

        result = calculate_score(guild, user_actions)
        if not result["would_restore"]:
            return

        total_score = result["score"]

        # Stufe 1 → Stufe 2, wenn derselbe Akteur kurz nach einer Vorwarnung erneut auffällt
        punish = result["would_punish"]
        escalated = False
        if not punish:
            flagged_at = _restore_flagged.get(key)
            if flagged_at is not None and time.monotonic() - flagged_at < ESCALATION_WINDOW_SECONDS:
                punish = True
                escalated = True

        _quarantined_users.add(key)
        try:
            log.warning(
                "[SHIELD] %s auf Guild %s (%s) durch %s %s (%s). Score: %.2f (Restore ab %.1f, Strafe ab %.1f, Tempo x%.2f)%s",
                "Anti-Nuke getriggert" if punish else "Vorwarnung",
                guild.name, guild.id, "Bot" if user.bot else "User", user, user.id,
                total_score, RESTORE_THRESHOLD, PUNISH_THRESHOLD, result["tempo_multiplier"],
                " [eskaliert]" if escalated else "",
            )
            if punish:
                _restore_flagged.pop(key, None)
                await self._react_punish(guild, user, user_actions, total_score, escalated)
            else:
                _restore_flagged[key] = time.monotonic()
                await self._react_warn(guild, user, total_score)
        finally:
            # Tracker erst nach Abschluss der Maßnahmen zurücksetzen
            _shield_action_tracker[guild.id][user.id] = []
            _fire_and_forget(_release_quarantine(guild.id, user.id))

    async def _react_warn(self, guild: discord.Guild, user: discord.Member | discord.User, total_score: float) -> None:
        """Stufe 1: Rollen/Channels wiederherstellen, Bans nicht zurücknehmen, niemanden bestrafen."""
        entries = _take_restore_entries(guild.id, include_bans=False)
        restore_results = None
        if entries:
            restore_results = await _restore_from_cache(guild, entries)
            log.info(
                "[SHIELD] Restore (Vorwarnung) auf %s: %s Rollen, %s Channels wiederhergestellt.",
                guild.name, restore_results["roles"], restore_results["channels"],
            )

        lang = await get_lang(guild.id)
        await _send_shield_log(
            self.bot, guild, lang, user, user.bot, total_score,
            warning=True, restore_results=restore_results,
        )

    async def _react_punish(
        self,
        guild: discord.Guild,
        user: discord.Member | discord.User,
        user_actions: list[dict],
        total_score: float,
        escalated: bool,
    ) -> None:
        """Stufe 2: Angreifer bestrafen, alles wiederherstellen, Invites pausieren."""
        attacker_member = await get_member_safe(guild, user.id)
        attacker_action_taken = False
        attacker_roles = None
        authorizer = None
        authorizer_demoted = None
        authorizer_roles = None
        rogue_bot_member = None
        rogue_bot_demoted = None

        if user.bot:
            if attacker_member:
                _shield_initiated_bans.add((guild.id, attacker_member.id))
                attacker_action_taken = await _punish_member(
                    guild,
                    attacker_member,
                    action="ban",
                    reason=f"[Nexus Shield] Anti-Nuke Triggered (Score: {total_score:.2f})"
                )
                log.info(
                    "[SHIELD] Ban für nukenden Bot %s auf %s: %s",
                    user, guild.name, "erfolgreich" if attacker_action_taken else "fehlgeschlagen",
                )

            authorizer = await _find_bot_authorizer(guild, user.id)
            if authorizer is not None:
                authorizer_member = await get_member_safe(guild, authorizer.id)
                if authorizer_member:
                    authorizer_roles = await _quarantine_member(
                        authorizer_member,
                        f"[Nexus Shield] Anti-Nuke Triggered — hat nukenden Bot {user} eingeladen (Score: {total_score:.2f})",
                        f"[Nexus Shield] Quarantäne: Nukenden Bot {user} autorisiert",
                    )
                    authorizer_demoted = authorizer_roles is not None
        else:
            if attacker_member:
                attacker_roles = await _quarantine_member(
                    attacker_member,
                    f"[Nexus Shield] Anti-Nuke Triggered (Score: {total_score:.2f})",
                    f"[Nexus Shield] Anti-Nuke Quarantäne (Score: {total_score:.2f})",
                )
                attacker_action_taken = attacker_roles is not None
                log.info(
                    "[SHIELD] Demote für %s auf %s: %s (entfernte Rollen: %s)",
                    user.name, guild.name,
                    "erfolgreich" if attacker_action_taken else "fehlgeschlagen (Hierarchie?)",
                    [r.id for r in attacker_roles] if attacker_roles else [],
                )

            rogue_bot_target = next(
                (e["target"] for e in user_actions if e["action"] == discord.AuditLogAction.bot_add and e["target"]),
                None,
            )
            if rogue_bot_target is not None:
                rogue_bot_member = await get_member_safe(guild, rogue_bot_target.id)
                if rogue_bot_member:
                    rogue_bot_demoted = (await _strip_roles(
                        rogue_bot_member, f"[Nexus Shield] Anti-Nuke Triggered — Bot von {user} eingeladen (Score: {total_score:.2f})"
                    )) is not None

        # Cache sofort leeren, damit keine doppelten Restores bei schnellen Folgeevents laufen
        cached_entries = _take_restore_entries(guild.id, include_bans=True)
        restore_results = None
        if cached_entries:
            restore_results = await _restore_from_cache(guild, cached_entries)
            log.info(
                "[SHIELD] Restore auf %s: %s Rollen, %s Channels, %s Bans wiederhergestellt.",
                guild.name, restore_results["roles"], restore_results["channels"], restore_results["bans"],
            )

        invites_paused = await _pause_all_invites(guild)

        lang = await get_lang(guild.id)
        await _send_shield_log(
            self.bot, guild, lang, user, user.bot, total_score,
            escalated=escalated,
            invites_paused=invites_paused,
            attacker_action_taken=attacker_action_taken,
            attacker_roles=attacker_roles,
            authorizer=authorizer,
            authorizer_demoted=authorizer_demoted,
            authorizer_roles=authorizer_roles,
            rogue_bot=rogue_bot_member,
            rogue_bot_demoted=rogue_bot_demoted,
            restore_results=restore_results,
        )

async def setup(bot):
    await bot.add_cog(Shield(bot))