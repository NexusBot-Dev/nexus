import database.database as db
from cachetools import TTLCache

# ─── Cache (analog zu _settings_cache in db_settings.py) ──────────────────
_antinuke_cache: TTLCache[int, int] = TTLCache(maxsize=2_000, ttl=900)

async def get_antinuke(guild_id: int) -> int:
    """Liest den Status von Anti-Nuke aus (0 oder 1) — gecacht."""
    if guild_id in _antinuke_cache:
        return _antinuke_cache[guild_id]

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