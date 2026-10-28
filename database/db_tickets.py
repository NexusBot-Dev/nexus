import aiomysql
import database.database as db

# Fallbacks, falls für eine Guild (noch) keine Zeile in ticket_settings existiert
CLOSURE_TIMEOUT_HOURS  = 72
DELETION_DELAY_HOURS   = 24

# ─── Erstellung & Abfrage ─────────────────────────────────────────────────────

async def get_open_ticket(guild_id: int, user_id: int, category_id: int) -> dict | None:
    """Prüft, ob der User bereits ein offenes Ticket in dieser Kategorie hat.
    'Offen' zählt hier sowohl 'open' als auch 'pending_closure' — ein Ticket,
    das gerade auf Bestätigung wartet, ist ja noch nicht wirklich weg."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM tickets
                   WHERE guild_id = %s AND user_id = %s AND category_id = %s
                   AND status IN ('open', 'pending_closure')""",
                (guild_id, user_id, category_id),
            )
            return await cur.fetchone()

async def create_ticket(
    guild_id: int,
    channel_id: int,
    user_id: int,
    category_id: int,
    ticket_number: int,
) -> int:
    """Legt einen neuen Ticket-Eintrag an. Gibt die ID zurück.
    Wird aufgerufen, NACHDEM der Discord-Channel bereits erstellt wurde."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO tickets (guild_id, channel_id, user_id, category_id, ticket_number, status)
                   VALUES (%s, %s, %s, %s, %s, 'open')""",
                (guild_id, channel_id, user_id, category_id, ticket_number),
            )
            return cur.lastrowid

async def get_ticket_by_channel(channel_id: int) -> dict | None:
    """Zentraler Lookup — fast jede Interaktion im Ticket-Channel (Buttons, Modals)
    braucht zuerst den zugehörigen DB-Eintrag über die channel_id."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM tickets WHERE channel_id = %s",
                (channel_id,),
            )
            return await cur.fetchone()

async def get_ticket(ticket_id: int) -> dict | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM tickets WHERE id = %s",
                (ticket_id,),
            )
            return await cur.fetchone()

async def has_open_tickets_for_category(category_id: int) -> bool:
    """Für den Löschschutz bei /edit_category — 'offen' zählt hier bewusst NICHT
    'closed', aber SEHR WOHL 'pending_closure', da das Ticket ja noch nicht final ist."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT COUNT(*) FROM tickets
                   WHERE category_id = %s AND status IN ('open', 'pending_closure')""",
                (category_id,),
            )
            row = await cur.fetchone()
            return row[0] > 0

async def get_open_tickets_for_guild(guild_id: int) -> list[dict]:
    """Für die Dashboard-Monitoring-Ansicht — alle offenen/pending Tickets einer Guild."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM tickets
                   WHERE guild_id = %s AND status IN ('open', 'pending_closure')
                   ORDER BY id DESC""",
                (guild_id,),
            )
            return await cur.fetchall()

# ─── Status-Übergänge ─────────────────────────────────────────────────────────

# Alle Übergänge prüfen den erwarteten Ausgangsstatus im UPDATE selbst und melden per
# Rückgabewert, ob sie gegriffen haben. Damit können veraltete Buttons/Modals oder
# gleichzeitige Klicks keinen Status mehr überschreiben (z.B. ein geschlossenes Ticket
# über einen alten "Ablehnen"-Button wieder öffnen).

async def request_closure(ticket_id: int) -> bool:
    """Support klickt 'Ticket schließen' — Status wechselt zu pending_closure,
    der Zeitstempel ist die Referenz für den späteren Timeout-Check.
    False, wenn das Ticket nicht (mehr) 'open' ist."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE tickets
                   SET status = 'pending_closure', closure_requested_at = NOW()
                   WHERE id = %s AND status = 'open'""",
                (ticket_id,),
            )
            return cur.rowcount > 0

async def reopen_ticket(ticket_id: int) -> bool:
    """User lehnt die Schließung ab — zurück auf 'open', Timeout-Referenz wird gelöscht,
    damit ein späteres erneutes 'schließen' wieder frisch bei 0h startet.
    False, wenn das Ticket nicht (mehr) auf 'pending_closure' steht — ein geschlossenes
    Ticket lässt sich so nie wieder öffnen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE tickets
                   SET status = 'open', closure_requested_at = NULL
                   WHERE id = %s AND status = 'pending_closure'""",
                (ticket_id,),
            )
            return cur.rowcount > 0

async def close_ticket(
    ticket_id: int,
    closed_by_type: str,
    closed_by_user_id: int | None = None,
    close_reason: str | None = None,
    deletion_hours: int = DELETION_DELAY_HOURS,
) -> bool:
    """Finales Schließen — egal ob durch User-Bestätigung, Auto-Timeout oder
    Support-Force-Close. Setzt gleichzeitig die Löschfrist (jetzt + deletion_hours,
    pro Server einstellbar), damit der Deletion-Task unabhängig vom Schließ-Grund greift.

    closed_by_type: 'user_confirmed' | 'auto_timeout' | 'support_forced'
    False, wenn das Ticket bereits geschlossen ist.
    """
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE tickets
                   SET status = 'closed',
                       closed_at = NOW(),
                       closed_by_type = %s,
                       closed_by_user_id = %s,
                       close_reason = %s,
                       scheduled_deletion_at = NOW() + INTERVAL %s HOUR
                   WHERE id = %s AND status <> 'closed'""",
                (closed_by_type, closed_by_user_id, close_reason, int(deletion_hours), ticket_id),
            )
            return cur.rowcount > 0

# ─── Für die beiden tasks.loop (Timeout-Check & Deletion-Check) ──────────────

async def get_overdue_pending_closures() -> list[dict]:
    """Alle Tickets, die länger als die Frist ihres Servers auf eine User-Reaktion warten —
    Basis für den automatischen Timeout-Close. Ohne Zeile in ticket_settings gilt der Fallback."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT t.* FROM tickets t
                   LEFT JOIN ticket_settings ts ON ts.guild_id = t.guild_id
                   WHERE t.status = 'pending_closure'
                   AND t.closure_requested_at <= NOW() - INTERVAL COALESCE(ts.closure_timeout_hours, %s) HOUR""",
                (CLOSURE_TIMEOUT_HOURS,),
            )
            return await cur.fetchall()

async def get_due_for_deletion() -> list[dict]:
    """Alle geschlossenen Tickets, deren Löschfrist abgelaufen ist —
    Basis für die automatische Channel-Löschung."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM tickets
                   WHERE status = 'closed'
                   AND scheduled_deletion_at IS NOT NULL
                   AND scheduled_deletion_at <= NOW()""",
                (),
            )
            return await cur.fetchall()

async def delete_ticket_record(ticket_id: int):
    """Entfernt die DB-Zeile, NACHDEM der zugehörige Discord-Channel bereits
    gelöscht wurde. Der Channel selbst enthält keine für uns relevanten Inhalte
    mehr (Transcript liegt bereits separat im Log-Channel/DM) — die Zeile hier
    zu behalten würde nur die Tabelle unnötig aufblähen, ohne Mehrwert."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM tickets WHERE id = %s",
                (ticket_id,),
            )

async def delete_user(guild_id: int, user_id: int):
    """DSGVO — Tickets selbst enthalten keine Konversationsinhalte in der DB
    (die liegen nur temporär als Discord-Nachricht/Anhang, nie bei uns),
    daher hier nichts zu tun außer ggf. offene Ticket-Zeilen mit dem User
    zu verwaisen zu lassen. Bewusst KEIN Löschen laufender Tickets bei
    Account-Löschung, da das offene Support-Anliegen sonst kommentarlos
    verschwinden würde."""
    pass
