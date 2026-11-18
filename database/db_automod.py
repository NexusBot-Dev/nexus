import json
from datetime import datetime
from typing import NamedTuple
import database.database as db
from cachetools import TTLCache

# ─── Cache (analog zu _settings_cache in db_settings.py) ──────────────────
_antinuke_cache: TTLCache[int, int] = TTLCache(maxsize=2_000, ttl=900)
_whitelist_cache: TTLCache[int, "ShieldWhitelist"] = TTLCache(maxsize=2_000, ttl=900)
_escalation_cache: TTLCache[int, dict] = TTLCache(maxsize=2_000, ttl=900)

def forget_guild(guild_id: int) -> None:
    """Cache-Eintrag verwerfen — nach dem Löschen aller Guild-Daten (db_settings.delete_guild_data)."""
    _antinuke_cache.pop(guild_id, None)
    _whitelist_cache.pop(guild_id, None)
    _escalation_cache.pop(guild_id, None)

async def get_antinuke(guild_id: int) -> int:
    """Liest den Status von Anti-Nuke aus (0 oder 1) — gecacht."""
    # Ein Blick in den Cache: Mit „in“ und danach [] kann der Eintrag dazwischen ablaufen (KeyError)
    cached = _antinuke_cache.get(guild_id)
    if cached is not None:
        return cached

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT antinuke FROM guild_settings WHERE guild_id = %s",
                (guild_id,),
            )
            row = await cur.fetchone()
            value = row[0] if row else 0

    _antinuke_cache[guild_id] = value
    return value

async def set_antinuke(guild_id: int, status: bool) -> int:
    """De-/Aktiviert den Stand von Antinuke — aktualisiert den Cache."""
    val = int(status)
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE guild_settings SET antinuke = %s WHERE guild_id = %s",
                (val, guild_id),
            )

    # Cache direkt nach dem erfolgreichen UPDATE mit dem neuen Int-Wert belegen
    _antinuke_cache[guild_id] = val
    return val

# ─── Pausierte Regeln (AutoMod-Modul aus) ─────────────────────────────────

async def add_paused_rule(guild_id: int, rule_id: int) -> None:
    """Merkt sich eine Regel, die beim Ausschalten des Moduls lief (systems/automod_rules.py)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO automod_paused_rules (guild_id, rule_id) VALUES (%s, %s)",
                (guild_id, rule_id),
            )


async def pop_paused_rules(guild_id: int) -> set[int]:
    """Liefert die gemerkten Regeln und vergisst sie (beim Wiedereinschalten des Moduls)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT rule_id FROM automod_paused_rules WHERE guild_id = %s", (guild_id,))
            rule_ids = {row[0] for row in await cur.fetchall()}
            await cur.execute("DELETE FROM automod_paused_rules WHERE guild_id = %s", (guild_id,))
    return rule_ids


# ─── Übernommene Regeln (Regel-ID → Art) ───────────────────────────────────

async def get_rule_kinds(guild_id: int) -> dict[int, str]:
    """Regeln, die Nexus übernommen hat (systems/automod_rules.adopt_rule), mit ihrer Art."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT rule_id, kind FROM automod_rules WHERE guild_id = %s", (guild_id,))
            return {row[0]: row[1] for row in await cur.fetchall()}


async def set_rule_kind(guild_id: int, rule_id: int, kind: str) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO automod_rules (guild_id, rule_id, kind) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE kind = VALUES(kind)",
                (guild_id, rule_id, kind),
            )


async def delete_rule_kinds(guild_id: int, rule_ids: set[int]) -> None:
    """Einträge von Regeln entfernen, die es in Discord nicht mehr gibt."""
    if not rule_ids:
        return
    placeholders = ",".join(["%s"] * len(rule_ids))
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"DELETE FROM automod_rules WHERE guild_id = %s AND rule_id IN ({placeholders})",
                (guild_id, *rule_ids),
            )


# ─── Link-Filter (systems/automod_links.py) ─────────────────────────────────

LINK_DEFAULTS = {"block_invites": True, "allow_support": True, "blocked_domains": [], "partner_codes": []}


async def get_link_settings(guild_id: int) -> dict:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT block_invites, allow_support, blocked_domains, partner_codes "
                "FROM automod_link_settings WHERE guild_id = %s",
                (guild_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return {**LINK_DEFAULTS, "blocked_domains": [], "partner_codes": []}
    return {
        "block_invites": bool(row[0]), "allow_support": bool(row[1]),
        "blocked_domains": json.loads(row[2]), "partner_codes": json.loads(row[3]),
    }


async def save_link_settings(guild_id: int, settings: dict) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO automod_link_settings (guild_id, block_invites, allow_support, blocked_domains, partner_codes) "
                "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE block_invites = VALUES(block_invites), "
                "allow_support = VALUES(allow_support), blocked_domains = VALUES(blocked_domains), "
                "partner_codes = VALUES(partner_codes)",
                (guild_id, settings["block_invites"], settings["allow_support"],
                 json.dumps(settings["blocked_domains"]), json.dumps(settings["partner_codes"])),
            )


async def get_guilds_with_link_rule() -> list[int]:
    """Server mit Nexus-Link-Filter — für den regelmäßigen Abgleich der Einladungen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT guild_id FROM automod_rules WHERE kind = 'links'")
            return [row[0] for row in await cur.fetchall()]


# ─── Shield-Whitelist (systems/shield_whitelist.py) ─────────────────────────

class ShieldWhitelist(NamedTuple):
    bots: frozenset[int]
    roles: frozenset[int]


async def get_shield_whitelist(guild_id: int) -> ShieldWhitelist:
    """Bots und Rollen, die das Shield nicht bewertet — gecacht (läuft bei jedem Audit-Log-Eintrag)."""
    cached = _whitelist_cache.get(guild_id)
    if cached is not None:
        return cached
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT target_id, kind FROM shield_whitelist WHERE guild_id = %s", (guild_id,))
            rows = await cur.fetchall()
    value = ShieldWhitelist(
        bots=frozenset(r[0] for r in rows if r[1] == "bot"),
        roles=frozenset(r[0] for r in rows if r[1] == "role"),
    )
    _whitelist_cache[guild_id] = value
    return value


async def add_shield_whitelist(guild_id: int, kind: str, target_ids: list[int], added_by: int) -> None:
    if target_ids:
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT IGNORE INTO shield_whitelist (guild_id, target_id, kind, added_by) VALUES (%s, %s, %s, %s)",
                    [(guild_id, target_id, kind, added_by) for target_id in target_ids],
                )
    _whitelist_cache.pop(guild_id, None)


async def remove_shield_whitelist(guild_id: int, target_ids: list[int]) -> None:
    if target_ids:
        placeholders = ", ".join(["%s"] * len(target_ids))
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM shield_whitelist WHERE guild_id = %s AND target_id IN ({placeholders})",
                    (guild_id, *target_ids),
                )
    _whitelist_cache.pop(guild_id, None)


# ─── Eskalation (systems/automod_escalation.py) ─────────────────────────────

async def get_escalation(guild_id: int) -> dict:
    """{"enabled": bool, "steps": [...]} — gecacht (läuft bei jedem AutoMod-Eingriff)."""
    cached = _escalation_cache.get(guild_id)
    if cached is not None:
        return cached
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT enabled, steps FROM automod_escalation WHERE guild_id = %s", (guild_id,))
            row = await cur.fetchone()
    value = {"enabled": bool(row[0]), "steps": json.loads(row[1])} if row else {"enabled": False, "steps": []}
    _escalation_cache[guild_id] = value
    return value


async def save_escalation(guild_id: int, enabled: bool, steps: list[dict]) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO automod_escalation (guild_id, enabled, steps) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE enabled = VALUES(enabled), steps = VALUES(steps)",
                (guild_id, enabled, json.dumps(steps)),
            )
    _escalation_cache.pop(guild_id, None)


async def add_strike(guild_id: int, user_id: int, rule_kind: str, at: datetime) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO automod_strikes (guild_id, user_id, rule_kind, created_at) VALUES (%s, %s, %s, %s)",
                (guild_id, user_id, rule_kind, at),
            )


async def get_strike_times(guild_id: int, user_id: int, since: datetime) -> list[datetime]:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT created_at FROM automod_strikes WHERE guild_id = %s AND user_id = %s AND created_at >= %s",
                (guild_id, user_id, since),
            )
            return [row[0] for row in await cur.fetchall()]


async def delete_strikes_before(before: datetime) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM automod_strikes WHERE created_at < %s", (before,))
            return cur.rowcount
