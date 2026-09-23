import aiomysql
import database.database as db

async def create_group(guild_id: int, name: str, mode: str = "toggle") -> int:
    """Erstellt eine neue Reaktionsrollen-Gruppe. Gibt die ID zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO reaction_role_groups (guild_id, name, mode) VALUES (%s, %s, %s)",
                (guild_id, name, mode),
            )
            return cur.lastrowid

async def get_groups(guild_id: int) -> list[dict]:
    """Gibt alle Gruppen einer Guild zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM reaction_role_groups WHERE guild_id = %s",
                (guild_id,)
            )
            return await cur.fetchall()

async def delete_group(group_id: int, guild_id: int) -> bool:
    """Löscht eine Gruppe und alle zugehörigen Reaktionsrollen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM reaction_roles WHERE group_id = %s AND guild_id = %s",
                (group_id, guild_id),
            )
            await cur.execute(
                "DELETE FROM reaction_role_groups WHERE id = %s AND guild_id = %s",
                (group_id, guild_id),
            )
            return cur.rowcount > 0

async def add_reaction_role(
    guild_id: int,
    channel_id: int,
    message_id: int,
    role_id: int,
    emoji: str,
    mode: str = "toggle",
    group_id: int | None = None,
) -> int:
    """Fügt eine Reaktionsrolle hinzu. Gibt die ID zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO reaction_roles
                   (guild_id, channel_id, message_id, role_id, emoji, mode, group_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (guild_id, channel_id, message_id, role_id, emoji, mode, group_id),
            )
            return cur.lastrowid

async def get_reaction_roles(guild_id: int, message_id: int) -> list[dict]:
    """Gibt alle Reaktionsrollen für eine Nachricht zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM reaction_roles WHERE guild_id = %s AND message_id = %s",
                (guild_id, message_id),
            )
            return await cur.fetchall()

async def get_reaction_role(guild_id: int, message_id: int, emoji: str) -> dict | None:
    """Gibt eine einzelne Reaktionsrolle zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM reaction_roles
                   WHERE guild_id = %s AND message_id = %s AND emoji = %s""",
                (guild_id, message_id, emoji),
            )
            return await cur.fetchone()

async def get_group_roles(guild_id: int, group_id: int) -> list[dict]:
    """Gibt alle Rollen einer Gruppe zurück — für Unique-Modus."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM reaction_roles
                   WHERE guild_id = %s AND group_id = %s""",
                (guild_id, group_id),
            )
            return await cur.fetchall()

async def delete_reaction_role(role_id: int, guild_id: int) -> bool:
    """Löscht eine einzelne Reaktionsrolle."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM reaction_roles WHERE id = %s AND guild_id = %s",
                (role_id, guild_id),
            )
            return cur.rowcount > 0

async def create_message_ref(
    guild_id: int,
    channel_id: int,
    message_id: int,
    title: str,
    description: str,
    display_mode: str = "reaction",
) -> int:
    """Legt den Metadaten-Eintrag für eine neu erstellte Reaction-Role-Nachricht an."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO reaction_role_messages
                   (guild_id, channel_id, message_id, title, description, display_mode)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (guild_id, channel_id, message_id, title, description, display_mode),
            )
            return cur.lastrowid

async def get_message_ref(guild_id: int, message_id: int) -> dict | None:
    """Gibt Titel/Beschreibung einer Reaction-Role-Nachricht zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM reaction_role_messages WHERE guild_id = %s AND message_id = %s",
                (guild_id, message_id),
            )
            return await cur.fetchone()

async def update_message_ref(guild_id: int, message_id: int, title: str, description: str) -> bool:
    """Aktualisiert Titel/Beschreibung einer bestehenden Reaction-Role-Nachricht."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE reaction_role_messages SET title = %s, description = %s
                   WHERE guild_id = %s AND message_id = %s""",
                (title, description, guild_id, message_id),
            )
            return cur.rowcount > 0

async def delete_message_ref(guild_id: int, message_id: int) -> bool:
    """Löscht den Metadaten-Eintrag einer Reaction-Role-Nachricht."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM reaction_role_messages WHERE guild_id = %s AND message_id = %s",
                (guild_id, message_id),
            )
            return cur.rowcount > 0

async def move_message_ref(guild_id: int, old_message_id: int, new_channel_id: int, new_message_id: int) -> bool:
    """
    Zieht eine Reaction-Role-Nachricht auf eine neue Discord-Message um (z.B. nach Neu-Posten).
    Aktualisiert sowohl den Metadaten-Eintrag als auch alle zugehörigen Zuordnungen in einem Rutsch.
    """
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE reaction_role_messages SET channel_id = %s, message_id = %s
                   WHERE guild_id = %s AND message_id = %s""",
                (new_channel_id, new_message_id, guild_id, old_message_id),
            )
            moved = cur.rowcount > 0
            await cur.execute(
                """UPDATE reaction_roles SET channel_id = %s, message_id = %s
                   WHERE guild_id = %s AND message_id = %s""",
                (new_channel_id, new_message_id, guild_id, old_message_id),
            )
            return moved

async def get_all_reaction_role_messages(guild_id: int) -> list[dict]:
    """
    Gibt alle Nachrichten mit Reaktionsrollen zurück (inkl. Titel/Beschreibung/Display-Modus, falls vorhanden).
    Quelle der Wahrheit ist reaction_roles — auch Nachrichten ohne (oder mit verlorenem)
    Metadaten-Eintrag in reaction_role_messages werden hier zuverlässig gelistet.
    """
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT DISTINCT
                       rr.channel_id,
                       rr.message_id,
                       rrm.title,
                       rrm.description,
                       COALESCE(rrm.display_mode, 'reaction') AS display_mode
                   FROM reaction_roles rr
                   LEFT JOIN reaction_role_messages rrm
                       ON rrm.guild_id = rr.guild_id AND rrm.message_id = rr.message_id
                   WHERE rr.guild_id = %s""",
                (guild_id,)
            )
            return await cur.fetchall()

async def delete_user(guild_id: int, user_id: int):
    """DSGVO — keine User-spezifischen Daten in Reaktionsrollen."""
    pass  # Reaktionsrollen speichern keine User-Daten