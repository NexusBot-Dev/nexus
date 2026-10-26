import aiomysql
import database.database as db


async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (%s, %s, %s, %s)",
                (guild_id, user_id, moderator_id, reason),
            )
            await cur.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = await cur.fetchone()
            return row[0]


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, moderator_id, reason, created_at FROM warnings WHERE guild_id = %s AND user_id = %s ORDER BY created_at DESC",
                (guild_id, user_id),
            )
            return await cur.fetchall()


async def remove_warning(warning_id: int, guild_id: int, user_id: int | None = None) -> bool:
    """Löscht eine Verwarnung. Mit user_id nur, wenn sie auch diesem User gehört."""
    query = "DELETE FROM warnings WHERE id = %s AND guild_id = %s"
    params: list = [warning_id, guild_id]
    if user_id is not None:
        query += " AND user_id = %s"
        params.append(user_id)
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return cur.rowcount > 0


async def clear_warnings(guild_id: int, user_id: int) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM warnings WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            return cur.rowcount