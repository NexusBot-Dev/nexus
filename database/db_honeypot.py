import copy

import aiomysql
import database.database as db
from cachetools import TTLCache

DEFAULT_TIMEOUT_DAYS = 7
MAX_TIMEOUT_DAYS = 28   # Discord begrenzt Timeouts auf 28 Tage

_cache: TTLCache[int, dict] = TTLCache(maxsize=2000, ttl=900)

UPDATABLE_HONEYPOT_COLUMNS = frozenset({"message_id", "timeout_days"})

_SELECT = "SELECT message_id, catches, timeout_days FROM honeypot_settings WHERE guild_id = %s"

async def get_honeypot_settings(guild_id: int) -> dict:
    """Honeypot-Einstellungen einer Guild (Warn-Embed-ID, Fang-Zähler, Timeout-Dauer).
    Legt bei Bedarf eine Zeile mit den Tabellen-Defaults an."""
    cached = _cache.get(guild_id)
    if cached is not None:
        return copy.deepcopy(cached)

    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(_SELECT, (guild_id,))
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """INSERT INTO honeypot_settings (guild_id) VALUES (%s)
                       ON DUPLICATE KEY UPDATE guild_id = guild_id""",
                    (guild_id,),
                )
                await cur.execute(_SELECT, (guild_id,))
                row = await cur.fetchone()

    row = row or {"message_id": None, "catches": 0, "timeout_days": DEFAULT_TIMEOUT_DAYS}
    _cache[guild_id] = copy.deepcopy(row)
    return row

async def update_honeypot_settings(guild_id: int, **fields) -> dict:
    """Aktualisiert einzelne Spalten (nur Whitelist). Prüft die Timeout-Dauer (1-28 Tage)."""
    if not fields:
        return await get_honeypot_settings(guild_id)

    unknown = fields.keys() - UPDATABLE_HONEYPOT_COLUMNS
    if unknown:
        raise ValueError(f"update_honeypot_settings: unbekannte oder nicht erlaubte Spalte(n): {', '.join(sorted(unknown))}")

    if "timeout_days" in fields:
        days = fields["timeout_days"]
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_TIMEOUT_DAYS:
            raise ValueError(f"timeout_days muss eine Zahl zwischen 1 und {MAX_TIMEOUT_DAYS} sein")

    await get_honeypot_settings(guild_id)   # stellt sicher, dass die Zeile existiert

    columns = ", ".join(f"`{key}` = %s" for key in fields)
    values = list(fields.values()) + [guild_id]

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE honeypot_settings SET {columns} WHERE guild_id = %s",
                values,
            )

    _cache.pop(guild_id, None)
    return await get_honeypot_settings(guild_id)

async def increment_catches(guild_id: int) -> int:
    """Zählt einen Fang atomar hoch und gibt den neuen Stand zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO honeypot_settings (guild_id, catches) VALUES (%s, 1)
                   ON DUPLICATE KEY UPDATE catches = catches + 1""",
                (guild_id,),
            )
            await cur.execute("SELECT catches FROM honeypot_settings WHERE guild_id = %s", (guild_id,))
            (total,) = await cur.fetchone()
    _cache.pop(guild_id, None)
    return int(total)

async def get_total_catches() -> int:
    """Summe aller Fänge über alle Server (z. B. für /info oder die Website)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(SUM(catches), 0) FROM honeypot_settings")
            (total,) = await cur.fetchone()
    return int(total)