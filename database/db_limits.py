import time
import aiomysql
import database.database as db

_limits_cache: dict[str, tuple[int, int]] = {}
_cache_loaded_at: float = 0
CACHE_DURATION = 86400

async def _refresh_cache():
    global _cache_loaded_at
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT feature_key, free_limit, premium_limit FROM feature_limits")
            rows = await cur.fetchall()
            _limits_cache.clear()
            for row in rows:
                _limits_cache[row["feature_key"]] = (row["free_limit"], row["premium_limit"])
            _cache_loaded_at = time.time()

async def get_limit(guild_id: int, feature_key: str, is_premium_guild: bool) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT limit_value FROM guild_limit_overrides WHERE guild_id = %s AND feature_key = %s",
                (guild_id, feature_key),
            )
            row = await cur.fetchone()
            if row:
                return row[0]

    if not _limits_cache or time.time() - _cache_loaded_at > CACHE_DURATION:
        await _refresh_cache()

    if feature_key not in _limits_cache:
        raise ValueError(f"Unknown feature_key: {feature_key}")

    free_limit, premium_limit = _limits_cache[feature_key]
    return premium_limit if is_premium_guild else free_limit