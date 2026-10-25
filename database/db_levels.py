import aiomysql
import database.database as db
import copy
from systems.exp_system import calculate_level, xp_for_level
from cachetools import TTLCache

IMPORT_CHUNK = 500
IMPORT_MAX_XP = 2_000_000_000
_levels_settings_cache: TTLCache[int, dict] = TTLCache(maxsize=2000, ttl=900)
_excluded_cache: TTLCache[int, frozenset[int]] = TTLCache(maxsize=2000, ttl=900)

UPDATABLE_LEVELS_COLUMNS = frozenset({
    "xp_min", "xp_max", "currency_name", "points_min", "points_max",
    "cooldown", "shop_enabled", "levelup_channel_id",
})

async def get_or_create_user(guild_id: int, user_id: int) -> dict:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM levels WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    "INSERT INTO levels (guild_id, user_id) VALUES (%s, %s)",
                    (guild_id, user_id),
                )

                return {"guild_id": guild_id, "user_id": user_id, "xp": 0, "level": 1, "last_xp": None}
            return row

async def add_xp(guild_id: int, user_id: int, amount: int) -> dict | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT xp, level FROM levels WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = await cur.fetchone()

            if row is None:
                await cur.execute(
                    "INSERT INTO levels (guild_id, user_id, xp, level, last_xp) VALUES (%s, %s, %s, %s, NOW())",
                    (guild_id, user_id, amount, 1),
                )
                return {"old_level": 1, "new_level": 1, "xp": amount}

            old_level = row["level"]
            new_xp    = row["xp"] + amount
            new_level = calculate_level(new_xp)

            await cur.execute(
                "UPDATE levels SET xp = %s, level = %s, last_xp = NOW() WHERE guild_id = %s AND user_id = %s",
                (new_xp, new_level, guild_id, user_id),
            )
            return {"old_level": old_level, "new_level": new_level, "xp": new_xp}

async def get_user_xp(guild_id: int, user_id: int) -> dict:
    return await get_or_create_user(guild_id, user_id)

async def set_level(guild_id: int, user_id: int, level: int) -> dict:
    xp = xp_for_level(level)
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO levels (guild_id, user_id, xp, level)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE xp = %s, level = %s""",
                (guild_id, user_id, xp, level, xp, level),
            )

    return {"xp": xp, "level": level}

async def add_xp_mod(guild_id: int, user_id: int, amount: int) -> dict:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT xp FROM levels WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = await cur.fetchone()
            current_xp = row["xp"] if row else 0
            old_level  = calculate_level(current_xp)

            new_xp     = max(0, current_xp + amount)
            new_level  = calculate_level(new_xp)

            await cur.execute(
                """INSERT INTO levels (guild_id, user_id, xp, level)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE xp = %s, level = %s""",
                (guild_id, user_id, new_xp, new_level, new_xp, new_level),
            )

            return {"xp": new_xp, "old_level": old_level, "new_level": new_level}

async def remove_xp_mod(guild_id: int, user_id: int, amount: int) -> dict:
    return await add_xp_mod(guild_id, user_id, -amount)

async def wipe_xp(guild_id: int, user_id: int) -> dict:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT level FROM levels WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = await cur.fetchone()

            old_level = row["level"] if row else 1
            new_xp = 0
            new_level = 1

            await cur.execute(
                "UPDATE levels SET xp = %s, level = %s, last_xp = NULL WHERE guild_id = %s AND user_id = %s",
                (new_xp, new_level, guild_id, user_id),
            )

            return {"old_level": old_level, "new_level": new_level, "xp": new_xp}

async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT user_id, xp, level FROM levels WHERE guild_id = %s ORDER BY xp DESC LIMIT %s",
                (guild_id, limit),
            )
            return await cur.fetchall()

async def get_user_rank(guild_id: int, user_id: int) -> int:
    """Gibt die 1-basierte Rangposition des Nutzers zurück (mehr XP = besserer Rang)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT COUNT(*) + 1 FROM levels
                   WHERE guild_id = %s AND xp > (
                       SELECT xp FROM levels WHERE guild_id = %s AND user_id = %s
                   )""",
                (guild_id, guild_id, user_id),
            )
            (rank,) = await cur.fetchone()
            return rank

async def get_level_role(guild_id: int, level: int) -> int | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT role_id FROM level_roles WHERE guild_id = %s AND level = %s",
                (guild_id, level),
            )
            row = await cur.fetchone()
            if row and row.get("role_id"):
                return int(row["role_id"])
            return None

async def get_all_level_roles(guild_id: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT role_id, level FROM level_roles WHERE guild_id = %s ORDER BY level ASC",
                (guild_id,),
            )
            rows = await cur.fetchall()
            return rows if rows else []

async def set_level_role(guild_id: int, level: int, role_id: int):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO level_roles (guild_id, level, role_id)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE role_id = %s""",
                (guild_id, level, role_id, role_id),
            )

async def remove_level_role(guild_id: int, level: int):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM level_roles WHERE guild_id = %s AND level = %s",
                (guild_id, level),
            )

# ─── Levels-Settings (XP-Range, Punkte, Cooldown) ─────────────────────────────

async def get_levels_settings(guild_id: int) -> dict:
    """Gibt die Level-Einstellungen einer Guild zurück (XP-Range, Punkte-Range,
    Currency-Name, Cooldown). Legt bei Bedarf eine Zeile mit den Tabellen-Defaults an."""
    if guild_id in _levels_settings_cache:
        return copy.deepcopy(_levels_settings_cache[guild_id])

    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT xp_min, xp_max, currency_name, points_min, points_max, cooldown, shop_enabled, levelup_channel_id, mee6_imported_at
                   FROM levels_settings WHERE guild_id = %s""",
                (guild_id,),
            )
            row = await cur.fetchone()

            if row is None:
                await cur.execute(
                    """INSERT INTO levels_settings (guild_id) VALUES (%s)
                       ON DUPLICATE KEY UPDATE guild_id = guild_id""",
                    (guild_id,),
                )
                await cur.execute(
                    """SELECT xp_min, xp_max, currency_name, points_min, points_max, cooldown, shop_enabled, levelup_channel_id, mee6_imported_at
                       FROM levels_settings WHERE guild_id = %s""",
                    (guild_id,),
                )
                row = await cur.fetchone()

            if row:
                _levels_settings_cache[guild_id] = copy.deepcopy(row)
            return row or {}

async def update_levels_settings(guild_id: int, **fields) -> dict:
    """Aktualisiert einzelne Spalten von levels_settings (z.B. xp_min=.., cooldown=..).
    Legt die Zeile bei Bedarf mit Defaults an."""
    if not fields:
        return await get_levels_settings(guild_id)

    unknown = fields.keys() - UPDATABLE_LEVELS_COLUMNS
    if unknown:
        raise ValueError(f"update_levels_settings: unbekannte oder nicht erlaubte Spalte(n): {', '.join(sorted(unknown))}")

    await get_levels_settings(guild_id)  # stellt sicher, dass die Zeile existiert

    columns = ", ".join(f"`{key}` = %s" for key in fields)
    values = list(fields.values()) + [guild_id]

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE levels_settings SET {columns} WHERE guild_id = %s",
                values,
            )

    _levels_settings_cache.pop(guild_id, None)

    return await get_levels_settings(guild_id)

async def get_excluded_channels(guild_id: int) -> frozenset[int]:
    """Channels/Kategorien ohne XP. Gecacht (hot path: wird bei jeder Nachricht gefragt)."""
    cached = _excluded_cache.get(guild_id)
    if cached is not None:
        return cached
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT channel_id FROM levels_excluded_channels WHERE guild_id = %s", (guild_id,),
            )
            rows = await cur.fetchall()
    result = frozenset(int(r[0]) for r in rows)
    _excluded_cache[guild_id] = result
    return result

async def set_excluded_channels(guild_id: int, channel_ids: list[int]) -> None:
    """Ersetzt die komplette Liste in einer Transaktion."""
    async with db.pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM levels_excluded_channels WHERE guild_id = %s", (guild_id,))
                if channel_ids:
                    await cur.executemany(
                        "INSERT INTO levels_excluded_channels (guild_id, channel_id) VALUES (%s, %s)",
                        [(guild_id, cid) for cid in channel_ids],
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    _excluded_cache.pop(guild_id, None)

async def import_xp(guild_id: int, entries: list[tuple[int, int]]) -> int | None:
    """Addiert importierte XP zu den bestehenden (einmalig pro Guild), alles in einer Transaktion.
    Das Flag wird atomar gesetzt; bei einem Fehler wird alles zurückgerollt, inklusive Flag.
    Gibt die Anzahl importierter Mitglieder zurück, None wenn schon importiert wurde."""
    await get_levels_settings(guild_id)   # stellt sicher, dass die Zeile existiert
    async with db.pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE levels_settings SET mee6_imported_at = UTC_TIMESTAMP()
                       WHERE guild_id = %s AND mee6_imported_at IS NULL""",
                    (guild_id,),
                )
                if cur.rowcount == 0:
                    await conn.rollback()
                    return None

                for i in range(0, len(entries), IMPORT_CHUNK):
                    chunk = entries[i:i + IMPORT_CHUNK]
                    await cur.executemany(
                        f"""INSERT INTO levels (guild_id, user_id, xp, level)
                            VALUES (%s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE xp = LEAST(xp + VALUES(xp), {IMPORT_MAX_XP})""",
                        [(guild_id, uid, xp, 1) for uid, xp in chunk],
                    )
                    ids = [uid for uid, _ in chunk]
                    placeholders = ", ".join(["%s"] * len(ids))
                    await cur.execute(
                        f"SELECT user_id, xp FROM levels WHERE guild_id = %s AND user_id IN ({placeholders})",
                        (guild_id, *ids),
                    )
                    rows = await cur.fetchall()
                    await cur.executemany(
                        """INSERT INTO levels (guild_id, user_id, xp, level)
                           VALUES (%s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE level = VALUES(level)""",
                        [(guild_id, uid, xp, calculate_level(xp)) for uid, xp in rows],
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    _levels_settings_cache.pop(guild_id, None)
    return len(entries)