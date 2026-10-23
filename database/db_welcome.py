import aiomysql
import database.database as db

async def get_welcome_messages(guild_id: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM welcome_messages WHERE guild_id = %s ORDER BY created_at ASC, id ASC",
                (guild_id,)
            )
            return await cur.fetchall()

async def count_welcome_messages(guild_id: int) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM welcome_messages WHERE guild_id = %s", (guild_id,)
            )
            row = await cur.fetchone()
            return row[0]

async def add_welcome_message(guild_id: int, message: str) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO welcome_messages (guild_id, message) VALUES (%s, %s)",
                (guild_id, message)
            )
            return cur.lastrowid

async def delete_welcome_message(message_id: int, guild_id: int) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM welcome_messages WHERE id = %s AND guild_id = %s",
                (message_id, guild_id)
            )
            return cur.rowcount > 0

async def update_welcome_message(message_id: int, guild_id: int, new_message: str) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE welcome_messages SET message = %s WHERE id = %s AND guild_id = %s",
                (new_message, message_id, guild_id)
            )
            return cur.rowcount > 0

async def delete_user(guild_id: int, user_id: int):
    """DSGVO — keine User-Daten in Welcome-Messages, nur Guild-Konfiguration."""
    pass