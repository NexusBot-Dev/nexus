import aiomysql
import database.database as db
from database import db_limits
from systems.premium import is_premium

async def get_embed_count(guild_id: int) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM saved_embeds WHERE guild_id = %s",
                (guild_id,)
            )
            row = await cur.fetchone()
            return row[0]

async def can_save_embed(guild_id: int) -> bool:
    """Prüft ob die Guild noch Embeds speichern kann."""
    count   = await get_embed_count(guild_id)
    premium = await is_premium(guild_id)
    limit   = await db_limits.get_limit(guild_id, "saved_embeds", premium)
    return count < limit

def active_embed_ids(embeds: list[dict], limit: int) -> set[int]:
    """FIFO: Die ältesten `limit` Embeds (created_at, id) einer Guild sind aktiv, der Rest ist inaktiv.
    Wird zur Laufzeit berechnet (kein gespeicherter Status): Beim Löschen rücken Embeds automatisch nach.
    Reihenfolge muss mit is_embed_active() übereinstimmen."""
    ordered = sorted(embeds, key=lambda e: (e["created_at"], e["id"]))
    return {e["id"] for e in ordered[:max(limit, 0)]}

async def is_embed_active(embed_id: int, guild_id: int, limit: int) -> bool:
    """Einzelprüfung zu active_embed_ids(), ohne alle Embeds zu laden (z.B. für den Scheduler)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT id FROM saved_embeds
                   WHERE guild_id = %s
                   ORDER BY created_at ASC, id ASC
                   LIMIT %s""",
                (guild_id, max(limit, 0)),
            )
            return embed_id in {row[0] for row in await cur.fetchall()}

async def save_embed(
    guild_id: int,
    name: str,
    title: str,
    description: str,
    color: int | None = None,
    channel_id: int | None = None,
    scheduled_at=None,
) -> int:
    """Speichert einen Embed. Gibt die ID zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO saved_embeds
                   (guild_id, name, title, description, color, channel_id, scheduled_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (guild_id, name, title, description, color, channel_id, scheduled_at),
            )
            return cur.lastrowid

async def get_embeds(guild_id: int) -> list[dict]:
    """Gibt alle gespeicherten Embeds einer Guild zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM saved_embeds WHERE guild_id = %s ORDER BY created_at DESC",
                (guild_id,)
            )
            return await cur.fetchall()

async def get_embed_summaries(guild_id: int) -> list[dict]:
    """Leichtgewichtige Embed-Liste für /embed_list und Autocomplete (Beschreibung auf 80 Zeichen gekürzt).
    Sortiert FIFO (älteste zuerst) — dieselbe Reihenfolge wie active_embed_ids()."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT id, name, title, LEFT(description, 80) AS description, scheduled_at, created_at
                   FROM saved_embeds WHERE guild_id = %s
                   ORDER BY created_at ASC, id ASC""",
                (guild_id,)
            )
            return await cur.fetchall()

async def get_embed(embed_id: int, guild_id: int) -> dict | None:
    """Gibt einen einzelnen Embed zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM saved_embeds WHERE id = %s AND guild_id = %s",
                (embed_id, guild_id)
            )
            return await cur.fetchone()

async def delete_embed(embed_id: int, guild_id: int) -> bool:
    """Löscht einen gespeicherten Embed."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM saved_embeds WHERE id = %s AND guild_id = %s",
                (embed_id, guild_id)
            )
            return cur.rowcount > 0

async def get_scheduled_embeds() -> list[dict]:
    """Gibt alle Embeds zurück die gepostet werden sollen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM saved_embeds
                   WHERE scheduled_at IS NOT NULL
                   AND scheduled_at <= NOW()
                   AND channel_id IS NOT NULL"""
            )
            return await cur.fetchall()

async def set_schedule(embed_id: int, guild_id: int, scheduled_at) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE saved_embeds SET scheduled_at = %s WHERE id = %s AND guild_id = %s",
                (scheduled_at, embed_id, guild_id),
            )
            return cur.rowcount > 0

async def clear_scheduled(embed_id: int):
    """Löscht den scheduled_at nach dem Posten."""

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE saved_embeds
                SET scheduled_at = NULL,
                    name = REPLACE(name, 'Scheduled: ', '')
                WHERE id = %s
                """,
                (embed_id,)
            )

async def update_embed(
    embed_id: int,
    guild_id: int,
    title: str,
    description: str,
    channel_id: int | None,
    name: str | None = None,
) -> bool:
    """Aktualisiert einen Embed. `name` bleibt unverändert, wenn None (z.B. beim Dashboard).
    Gibt False zurück, wenn der Embed nicht (mehr) existiert."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            if name is None:
                await cur.execute(
                    """UPDATE saved_embeds SET title = %s, description = %s, channel_id = %s
                       WHERE id = %s AND guild_id = %s""",
                    (title, description, channel_id, embed_id, guild_id),
                )
            else:
                await cur.execute(
                    """UPDATE saved_embeds SET name = %s, title = %s, description = %s, channel_id = %s
                       WHERE id = %s AND guild_id = %s""",
                    (name, title, description, channel_id, embed_id, guild_id),
                )
            if cur.rowcount > 0:
                return True

            # rowcount zählt nur *geänderte* Zeilen: Speichern ohne Änderung liefert 0, obwohl der Embed existiert.
            await cur.execute(
                "SELECT 1 FROM saved_embeds WHERE id = %s AND guild_id = %s",
                (embed_id, guild_id),
            )
            return await cur.fetchone() is not None