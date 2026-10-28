import aiomysql
import database.database as db

# ─── Kategorien ──────────────────────────────────────────────────────────────

async def get_categories(guild_id: int) -> list[dict]:
    """Gibt alle Ticket-Kategorien einer Guild zurück, sortiert nach Position."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM ticket_categories WHERE guild_id = %s ORDER BY position ASC, id ASC",
                (guild_id,),
            )
            return await cur.fetchall()

async def get_category(category_id: int, guild_id: int) -> dict | None:
    """Gibt eine einzelne Kategorie zurück — guild_id wird zur Sicherheit mitgeprüft,
    damit niemand über eine fremde category_id auf eine andere Guild zugreifen kann."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM ticket_categories WHERE id = %s AND guild_id = %s",
                (category_id, guild_id),
            )
            return await cur.fetchone()

async def get_category_count(guild_id: int) -> int:
    """Für den Free/Premium-Limit-Check vor dem Anlegen einer neuen Kategorie."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM ticket_categories WHERE guild_id = %s",
                (guild_id,),
            )
            row = await cur.fetchone()
            return row[0]

async def create_category(guild_id: int, name: str, category_channel_id: int, position: int = 0) -> int:
    """Legt eine neue Ticket-Kategorie an. Gibt die ID zurück.
    Wird aufgerufen, NACHDEM die Discord-Kategorie bereits erstellt wurde."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO ticket_categories (guild_id, name, category_channel_id, position)
                   VALUES (%s, %s, %s, %s)""",
                (guild_id, name, category_channel_id, position),
            )
            return cur.lastrowid

async def rename_category(category_id: int, guild_id: int, new_name: str) -> bool:
    """Ändert nur den Namen in der DB. Das Umbenennen der Discord-Kategorie selbst
    muss separat im Cog passieren (discord.CategoryChannel.edit)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ticket_categories SET name = %s WHERE id = %s AND guild_id = %s",
                (new_name, category_id, guild_id),
            )
            return cur.rowcount > 0

async def delete_category(category_id: int, guild_id: int) -> bool:
    """Löscht eine Kategorie samt verknüpfter Rollen (CASCADE via FK).
    Offene/pending Tickets werden bereits VOR diesem Call durch
    has_open_tickets_for_category() im Cog blockiert. Geschlossene Tickets,
    die noch auf ihre 24h-Löschfrist warten, blockieren NICHT mehr —
    deren category_id wird durch ON DELETE SET NULL einfach aufgelöst
    (siehe Migration in schema.sql)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM ticket_categories WHERE id = %s AND guild_id = %s",
                (category_id, guild_id),
            )
            return cur.rowcount > 0

# ─── Atomarer Ticket-Counter ─────────────────────────────────────────────────

async def get_next_ticket_number(category_id: int) -> int:
    """Erhöht den Counter atomar direkt in der DB und gibt die neue Nummer zurück.
    Race-Condition-sicher bei gleichzeitigen Klicks: LAST_INSERT_ID(expr) merkt sich den
    neuen Wert pro Verbindung. Ein separates SELECT auf ticket_counter könnte dagegen
    schon die Erhöhung eines parallelen Klicks sehen (autocommit) — doppelte Nummern."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ticket_categories SET ticket_counter = LAST_INSERT_ID(ticket_counter + 1) WHERE id = %s",
                (category_id,),
            )
            if cur.rowcount == 0:
                # Sonst lieferte LAST_INSERT_ID() einen alten Wert dieser (gepoolten) Verbindung
                raise LookupError(f"Ticket-Kategorie {category_id} existiert nicht")
            await cur.execute("SELECT LAST_INSERT_ID()")
            row = await cur.fetchone()
            return row[0]

# ─── Rollen-Zuordnung (n:m) ──────────────────────────────────────────────────

async def get_category_roles(category_id: int) -> list[int]:
    """Gibt alle role_ids zurück, die für diese Kategorie berechtigt sind."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id FROM ticket_category_roles WHERE category_id = %s",
                (category_id,),
            )
            rows = await cur.fetchall()
            return [row[0] for row in rows]

async def get_category_roles_for_guild(guild_id: int) -> dict[int, list[int]]:
    """category_id -> role_ids für alle Kategorien einer Guild in einer Abfrage."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT tcr.category_id, tcr.role_id
                   FROM ticket_category_roles tcr
                   JOIN ticket_categories tc ON tc.id = tcr.category_id
                   WHERE tc.guild_id = %s""",
                (guild_id,),
            )
            result: dict[int, list[int]] = {}
            for category_id, role_id in await cur.fetchall():
                result.setdefault(category_id, []).append(role_id)
            return result

async def set_category_roles(category_id: int, role_ids: list[int]):
    """Ersetzt die komplette Rollen-Zuordnung einer Kategorie durch die übergebene Liste.
    Einfacher und robuster als einzelne add/remove-Calls, da RoleSelect im UI
    ohnehin immer den kompletten neuen Zustand liefert (keine Diff-Logik nötig)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM ticket_category_roles WHERE category_id = %s",
                (category_id,),
            )
            if role_ids:
                values = [(category_id, role_id) for role_id in role_ids]
                await cur.executemany(
                    "INSERT INTO ticket_category_roles (category_id, role_id) VALUES (%s, %s)",
                    values,
                )

async def delete_user(guild_id: int, user_id: int):
    """DSGVO — keine User-spezifischen Daten in den Kategorien selbst, nur Guild-Konfiguration."""
    pass
