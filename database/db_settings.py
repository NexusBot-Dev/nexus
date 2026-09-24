import logging
import json
import copy
import aiomysql
import database.database as db
from cachetools import TTLCache
from config import NEXUS_COLOR

log = logging.getLogger(__name__)

DEFAULT_MODULES = {
    "moderation":     True,
    "levels":         False,
    "welcome":        False,
    "reaction_roles": False,
    "boosts":         False,
    "embeds":         False,
    "automod":        False,
    "stream_alerts":  True,
    "tickets":        True,
}
_settings_cache: TTLCache[int, dict] = TTLCache(maxsize=2_000, ttl=900)

async def get_guild(guild_id: int) -> dict | None:
    if guild_id in _settings_cache:
        return copy.deepcopy(_settings_cache[guild_id])

    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM guild_settings WHERE guild_id = %s",
                (guild_id,)
            )
            row = await cur.fetchone()

            if not row:
                return None

            if isinstance(row.get("modules"), str):
                try:
                    row["modules"] = json.loads(row["modules"])
                except json.JSONDecodeError:
                    row["modules"] = {}

            _settings_cache[guild_id] = copy.deepcopy(row)
            return row

async def get_or_create_guild(guild_id: int) -> dict:
    row = await get_guild(guild_id)
    if row is not None:
        return row

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO guild_settings (guild_id, modules) VALUES (%s, %s) ON DUPLICATE KEY UPDATE guild_id=guild_id",
                (guild_id, json.dumps(DEFAULT_MODULES))
            )
    row = await get_guild(guild_id)
    if row is None:
        raise RuntimeError(f"Konnte Guild {guild_id} nach Erstellung nicht aus der DB laden.")
    return row

async def update_guild(guild_id: int, **kwargs):
    if not kwargs:
        return

    if "modules" in kwargs and not isinstance(kwargs["modules"], str):
        kwargs["modules"] = json.dumps(kwargs["modules"])

    sets   = ", ".join(f"{k} = %s" for k in kwargs)
    values = list(kwargs.values()) + [guild_id]

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE guild_settings SET {sets} WHERE guild_id = %s",
                values,
            )
    # Cache für diese Guild sofort invalidieren
    _settings_cache.pop(guild_id, None)

async def update_timezone(guild_id: int, timezone_str: str):
    """Speichert oder aktualisiert die Zeitzone in den Guild-Settings."""
    await get_or_create_guild(guild_id)
    await update_guild(guild_id, timezone=timezone_str)

async def get_timezone(guild_id: int) -> str:
    """Holt die Zeitzone des Servers aus den Guild-Settings. Fallback: UTC."""
    row = await get_guild(guild_id)
    if row and row.get("timezone"):
        return row["timezone"]
    return "UTC"

async def get_color(guild_id: int) -> int:
    """Gibt die Guild-Farbe zurück — Premium kann eigene Farbe setzen."""
    row = await get_guild(guild_id)
    if row and row.get("color"):
        return row["color"]
    return NEXUS_COLOR

async def set_color(guild_id: int, color: int) -> bool:
    """Setzt eine eigene Farbe — nur für Premium Guilds."""
    from systems.premium import is_premium
    if not await is_premium(guild_id):
        return False
    await update_guild(guild_id, color=color)
    return True

async def is_module_enabled(guild_id: int, module: str) -> bool:
    row = await get_guild(guild_id)
    if row is None:
        return DEFAULT_MODULES.get(module, False)
    modules = row.get("modules") or {}
    return modules.get(module, DEFAULT_MODULES.get(module, False))

async def set_module(guild_id: int, module: str, enabled: bool):
    row = await get_or_create_guild(guild_id)
    modules = row["modules"]
    modules[module] = enabled
    await update_guild(guild_id, modules=modules)

async def set_locale(guild_id: int, locale: str):
    await update_guild(guild_id, locale=locale)

async def reset_guild(guild_id: int):
    """Setzt alle Guild-Settings zurück — behält den Eintrag aber löscht alle Werte."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE guild_settings SET
                    log_channel_id      = NULL,
                    rules_channel_id    = NULL,
                    member_role_id      = NULL,
                    welcome_channel_id  = NULL,
                    boost_channel_id    = NULL,
                    color               = NULL,
                    locale              = 'de',
                    modules             = %s,
                    setup_complete      = FALSE,
                    left_at             = NULL
                WHERE guild_id = %s""",
                (json.dumps(DEFAULT_MODULES), guild_id),
            )
    _settings_cache.pop(guild_id, None)

async def delete_old_guilds(days: int = 7):
    """Löscht alle Daten von Guilds, die länger als X Tage entfernt sind (DSGVO)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT guild_id FROM guild_settings
                   WHERE left_at IS NOT NULL
                   AND left_at < NOW() - INTERVAL %s DAY""",
                (days,)
            )
            rows = await cur.fetchall()
            if not rows:
                return

            guild_ids = [row[0] for row in rows]
            format_strings = ','.join(['%s'] * len(guild_ids))

            EXCLUDED_TABLES = {"premium"}
            await cur.execute(
                """SELECT table_name FROM information_schema.columns
                   WHERE table_schema = DATABASE() AND column_name = 'guild_id'"""
            )
            tables = [row[0] for row in await cur.fetchall() if row[0] not in EXCLUDED_TABLES]
            tables.sort(key=lambda t: t == "guild_settings")

            await conn.begin()
            try:
                for table in tables:
                    await cur.execute(
                        f"DELETE FROM {table} WHERE guild_id IN ({format_strings})",
                        tuple(guild_ids)
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                log.error(
                    "DSGVO-Löschung fehlgeschlagen, Rollback ausgeführt für Guilds: %s",
                    guild_ids, exc_info=True
                )
                return

            # Cache erst NACH erfolgreichem Commit säubern
            for g_id in guild_ids:
                _settings_cache.pop(g_id, None)

            log.info(
                "DSGVO: %s Guilds nach %s Tagen Inaktivität vollständig gelöscht (%s Tabellen bereinigt).",
                len(guild_ids), days, len(tables)
            )

async def get_enabled_modules(guild_id: int) -> list[str]:
    """Liste aller aktiven Module — berücksichtigt DEFAULT_MODULES für Keys, die (noch) nicht in der DB stehen."""
    row = await get_guild(guild_id)
    modules = row["modules"] if row else {}
    merged = {**DEFAULT_MODULES, **modules}
    return [module for module, enabled in merged.items() if enabled]

async def is_setup_complete(guild_id: int) -> bool:
    row = await get_guild(guild_id)
    if row is None:
        return False
    return bool(row["setup_complete"])