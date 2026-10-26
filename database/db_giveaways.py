import aiomysql
import random
import database.database as db
from datetime import datetime
from typing import Literal

async def create_giveaway(guild_id: int, message_id: int, prize: str, end_time: datetime, host_id: int, channel_id: int, winners_count: int = 1) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO giveaways (guild_id, message_id, prize, end_time, host_id, channel_id, winners_count)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (guild_id, message_id, prize, end_time, host_id, channel_id, winners_count),
            )
            return cur.lastrowid

async def get_giveaway(guild_id: int, message_id: int) -> dict | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM giveaways WHERE guild_id = %s AND message_id = %s""",
                (guild_id, message_id),
            )
            return await cur.fetchone()

async def get_active_giveaways(guild_id: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM giveaways WHERE guild_id = %s AND status = 'running'",
                (guild_id,),
            )
            return await cur.fetchall()

async def get_due_giveaways() -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM giveaways WHERE status = 'running' and end_time <= UTC_TIMESTAMP()",
            )
            return await cur.fetchall()

async def update_giveaway(
        guild_id: int,
        message_id: int,
        prize: str | None = None,
        end_time: datetime | None = None,
        winners_count: int | None = None,
    ) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE giveaways SET prize = COALESCE(%s, prize), end_time = COALESCE(%s, end_time), winners_count = COALESCE(%s, winners_count)
                   WHERE guild_id = %s AND message_id = %s AND status = 'running'""",
                (prize, end_time, winners_count, guild_id, message_id),
            )
            return cur.rowcount > 0

async def end_giveaway(guild_id: int, message_id: int, status: Literal["ended", "cancelled"]) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE giveaways SET status = %s WHERE guild_id = %s AND message_id = %s AND status = 'running'""",
                (status, guild_id, message_id),
            )
            return cur.rowcount > 0

async def add_participant(giveaway_id: int, user_id: int) -> bool:
    """Trägt einen User als Teilnehmer ein. False, falls er schon dabei ist."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES (%s, %s)""",
                    (giveaway_id, user_id),
                )
                return True
            except aiomysql.IntegrityError:
                return False

async def get_participant_count(giveaway_id: int) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = %s""",
                (giveaway_id,),
            )
            row = await cur.fetchone()
            return row[0]

async def draw_winners(giveaway_id: int, winners_count: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM giveaway_entries WHERE giveaway_id = %s",
                (giveaway_id,),
            )
            entries = await cur.fetchall()
            return random.sample(entries, min(winners_count, len(entries)))

async def delete_user(guild_id: int, user_id: int) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """DELETE ge FROM giveaway_entries ge
                   INNER JOIN giveaways g ON g.id = ge.giveaway_id
                   WHERE g.guild_id = %s AND ge.user_id = %s""",
                (guild_id, user_id),
            )
            return cur.rowcount

async def get_giveaway_by_id(guild_id: int, giveaway_id: int) -> dict | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM giveaways WHERE guild_id = %s AND id = %s",
                (guild_id, giveaway_id),
            )
            return await cur.fetchone()

async def get_giveaways_overview(guild_id: int, running: bool, limit: int = 25) -> list[dict]:
    """Giveaways inkl. Teilnehmerzahl für das Dashboard (eine Query statt N+1).

    running=True: laufende, nächstes Ende zuerst.
    running=False: beendete/abgebrochene, neueste zuerst.
    """
    status_clause = "g.status = 'running'" if running else "g.status <> 'running'"
    order = "ASC" if running else "DESC"
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"""SELECT g.*, COUNT(ge.user_id) AS participant_count
                    FROM giveaways g
                    LEFT JOIN giveaway_entries ge ON ge.giveaway_id = g.id
                    WHERE g.guild_id = %s AND {status_clause}
                    GROUP BY g.id
                    ORDER BY g.end_time {order}
                    LIMIT %s""",
                (guild_id, limit),
            )
            return await cur.fetchall()