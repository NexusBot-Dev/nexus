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

async def log_shadow_result(
    guild_id: int,
    user_id: int,
    is_bot: bool,
    action_type: str,
    old_score: float,
    old_triggered: bool,
    new_score: float,
    new_would_restore: bool,
    new_would_punish: bool,
    tempo_avg_ms: int | None,
    tempo_multiplier: float | None,
    channel_ratio: float | None,
    dangerous_perm_change: bool | None,
    actions_in_window: int,
) -> None:
    """Schreibt einen Shadow-Score-Vergleich. Rein informativ, nie live wirksam."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO shield_shadow_log (
                    guild_id, user_id, is_bot, action_type,
                    old_score, old_triggered,
                    new_score, new_would_restore, new_would_punish,
                    tempo_avg_ms, tempo_multiplier, channel_ratio,
                    dangerous_perm_change, actions_in_window
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    guild_id, user_id, int(is_bot), action_type,
                    old_score, int(old_triggered),
                    new_score, int(new_would_restore), int(new_would_punish),
                    tempo_avg_ms, tempo_multiplier, channel_ratio,
                    None if dangerous_perm_change is None else int(dangerous_perm_change),
                    actions_in_window,
                ),
            )

async def get_shadow_stats(guild_id: int | None = None, days: int = 7) -> dict:
    """Aggregierte Auswertung für den Owner-Command. guild_id=None -> serverübergreifend."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = "WHERE created_at >= NOW() - INTERVAL %s DAY"
            params: list = [days]
            if guild_id is not None:
                where += " AND guild_id = %s"
                params.append(guild_id)

            await cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_events,
                    SUM(old_triggered) AS old_triggers,
                    SUM(new_would_restore) AS new_restores,
                    SUM(new_would_punish) AS new_punishes,
                    SUM(CASE WHEN old_triggered = 1 AND new_would_punish = 0 THEN 1 ELSE 0 END) AS old_only,
                    SUM(CASE WHEN old_triggered = 0 AND new_would_punish = 1 THEN 1 ELSE 0 END) AS new_only
                FROM shield_shadow_log
                {where}
                """,
                params,
            )
            row = await cur.fetchone()
            keys = ["total_events", "old_triggers", "new_restores", "new_punishes", "old_only", "new_only"]
            return dict(zip(keys, row))

async def get_diverging_cases(guild_id: int | None = None, limit: int = 20) -> list[dict]:
    """Fälle, in denen altes und neues System unterschiedlich entschieden haben."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = "WHERE old_triggered != new_would_punish"
            params: list = []
            if guild_id is not None:
                where += " AND guild_id = %s"
                params.append(guild_id)
            params.append(limit)

            await cur.execute(
                f"""
                SELECT guild_id, user_id, is_bot, action_type,
                       old_score, old_triggered, new_score, new_would_punish,
                       tempo_avg_ms, channel_ratio, actions_in_window, created_at
                FROM shield_shadow_log
                {where}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = await cur.fetchall()
            cols = ["guild_id", "user_id", "is_bot", "action_type", "old_score", "old_triggered",
                    "new_score", "new_would_punish", "tempo_avg_ms", "channel_ratio",
                    "actions_in_window", "created_at"]
            return [dict(zip(cols, r)) for r in rows]

async def cleanup_shadow_log(retention_days: int) -> int:
    """Löscht Shadow-Log-Einträge älter als retention_days. Gibt Anzahl gelöschter Zeilen zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM shield_shadow_log WHERE created_at < NOW() - INTERVAL %s DAY",
                (retention_days,),
            )
            return cur.rowcount