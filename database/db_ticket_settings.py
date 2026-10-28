import copy

import aiomysql
import database.database as db
from cachetools import TTLCache

# Feste Auswahl für die zwei Fristen (Stunden)
CLOSURE_OPTIONS  = (12, 24, 48, 72, 168)   # automatisch schließen ohne Reaktion
DELETION_OPTIONS = (1, 12, 24, 48, 168)    # Channel löschen nach dem Schließen
DEFAULT_CLOSURE_HOURS  = 72
DEFAULT_DELETION_HOURS = 24
PANEL_MODES = ("buttons", "dropdown")      # dropdown: Vorbereitung, noch nicht genutzt

_cache: TTLCache[int, dict] = TTLCache(maxsize=2000, ttl=900)

UPDATABLE_TICKET_COLUMNS = frozenset({
    "panel_channel_id", "panel_message_id", "panel_title", "panel_description", "panel_mode",
    "closure_timeout_hours", "deletion_delay_hours",
})

_SELECT = """SELECT panel_channel_id, panel_message_id, panel_title, panel_description, panel_mode,
                    closure_timeout_hours, deletion_delay_hours
             FROM ticket_settings WHERE guild_id = %s"""

_DEFAULTS = {
    "panel_channel_id": None, "panel_message_id": None, "panel_title": None, "panel_description": None,
    "panel_mode": "buttons", "closure_timeout_hours": DEFAULT_CLOSURE_HOURS, "deletion_delay_hours": DEFAULT_DELETION_HOURS,
}

async def get_ticket_settings(guild_id: int) -> dict:
    """Ticket-Einstellungen einer Guild (Panel + Fristen). Legt bei Bedarf eine Zeile mit Defaults an."""
    cached = _cache.get(guild_id)
    if cached is not None:
        return copy.deepcopy(cached)

    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(_SELECT, (guild_id,))
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """INSERT INTO ticket_settings (guild_id) VALUES (%s)
                       ON DUPLICATE KEY UPDATE guild_id = guild_id""",
                    (guild_id,),
                )
                await cur.execute(_SELECT, (guild_id,))
                row = await cur.fetchone()

    row = row or dict(_DEFAULTS)
    _cache[guild_id] = copy.deepcopy(row)
    return row

async def update_ticket_settings(guild_id: int, **fields) -> dict:
    """Aktualisiert einzelne Spalten (nur Whitelist) und prüft die Werte."""
    if not fields:
        return await get_ticket_settings(guild_id)

    unknown = fields.keys() - UPDATABLE_TICKET_COLUMNS
    if unknown:
        raise ValueError(f"update_ticket_settings: unbekannte oder nicht erlaubte Spalte(n): {', '.join(sorted(unknown))}")
    if "closure_timeout_hours" in fields and fields["closure_timeout_hours"] not in CLOSURE_OPTIONS:
        raise ValueError(f"closure_timeout_hours muss einer dieser Werte sein: {CLOSURE_OPTIONS}")
    if "deletion_delay_hours" in fields and fields["deletion_delay_hours"] not in DELETION_OPTIONS:
        raise ValueError(f"deletion_delay_hours muss einer dieser Werte sein: {DELETION_OPTIONS}")
    if "panel_mode" in fields and fields["panel_mode"] not in PANEL_MODES:
        raise ValueError(f"panel_mode muss einer dieser Werte sein: {PANEL_MODES}")
    if fields.get("panel_title") is not None and len(fields["panel_title"]) > 256:
        raise ValueError("panel_title zu lang (max. 256 Zeichen)")
    if fields.get("panel_description") is not None and len(fields["panel_description"]) > 1000:
        raise ValueError("panel_description zu lang (max. 1000 Zeichen)")

    await get_ticket_settings(guild_id)   # stellt sicher, dass die Zeile existiert

    columns = ", ".join(f"`{key}` = %s" for key in fields)
    values = list(fields.values()) + [guild_id]

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"UPDATE ticket_settings SET {columns} WHERE guild_id = %s", values)

    _cache.pop(guild_id, None)
    return await get_ticket_settings(guild_id)
