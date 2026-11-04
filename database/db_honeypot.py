import copy

import aiomysql
import database.database as db
from cachetools import TTLCache

DEFAULT_TIMEOUT_DAYS = 7
MAX_TIMEOUT_DAYS = 28   # Discord begrenzt Timeouts auf 28 Tage
TIMEOUT_DAY_OPTIONS = (1, 3, 7, 14, 28)   # Auswahl in /honeypot und im Dashboard

PUNISHMENTS = ("timeout", "kick", "ban")
DEFAULT_PUNISH_MEMBERS = "timeout"
DEFAULT_PUNISH_BOTS = "kick"

WARN_TITLE_MAX = 256    # Discord-Limit für Embed-Titel
WARN_TEXT_MAX = 1000

_cache: TTLCache[int, dict] = TTLCache(maxsize=2000, ttl=900)

def forget_guild(guild_id: int) -> None:
    """Cache-Eintrag verwerfen — nach dem Löschen aller Guild-Daten (db_settings.delete_guild_data)."""
    _cache.pop(guild_id, None)

UPDATABLE_HONEYPOT_COLUMNS = frozenset({
    "message_id", "timeout_days", "warn_title", "warn_text", "punish_members", "punish_bots",
})

# warn_title/warn_text: NULL = noch nicht aus der Warn-Nachricht übernommen (siehe systems.honeypot.load_texts)
_SELECT = """SELECT message_id, catches, timeout_days, warn_title, warn_text, punish_members, punish_bots
             FROM honeypot_settings WHERE guild_id = %s"""
_DEFAULT_ROW = {
    "message_id": None, "catches": 0, "timeout_days": DEFAULT_TIMEOUT_DAYS,
    "warn_title": None, "warn_text": None,
    "punish_members": DEFAULT_PUNISH_MEMBERS, "punish_bots": DEFAULT_PUNISH_BOTS,
}

async def get_honeypot_settings(guild_id: int) -> dict:
    """Honeypot-Einstellungen einer Guild (Warn-Embed-ID und -Texte, Fang-Zähler, Bestrafung, Timeout-Dauer).
    Legt bei Bedarf eine Zeile mit den Tabellen-Defaults an."""
    cached = _cache.get(guild_id)
    if cached is not None:
        return copy.deepcopy(cached)

    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(_SELECT, (guild_id,))
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """INSERT INTO honeypot_settings (guild_id) VALUES (%s)
                       ON DUPLICATE KEY UPDATE guild_id = guild_id""",
                    (guild_id,),
                )
                await cur.execute(_SELECT, (guild_id,))
                row = await cur.fetchone()

    row = row or dict(_DEFAULT_ROW)
    _cache[guild_id] = copy.deepcopy(row)
    return row

async def update_honeypot_settings(guild_id: int, **fields) -> dict:
    """Aktualisiert einzelne Spalten (nur Whitelist). Prüft Timeout-Dauer (1-28 Tage), Bestrafung und Textlängen."""
    if not fields:
        return await get_honeypot_settings(guild_id)

    unknown = fields.keys() - UPDATABLE_HONEYPOT_COLUMNS
    if unknown:
        raise ValueError(f"update_honeypot_settings: unbekannte oder nicht erlaubte Spalte(n): {', '.join(sorted(unknown))}")

    if "timeout_days" in fields:
        days = fields["timeout_days"]
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_TIMEOUT_DAYS:
            raise ValueError(f"timeout_days muss eine Zahl zwischen 1 und {MAX_TIMEOUT_DAYS} sein")
    for key in ("punish_members", "punish_bots"):
        if key in fields and fields[key] not in PUNISHMENTS:
            raise ValueError(f"{key} muss einer dieser Werte sein: {PUNISHMENTS}")
    if len(fields.get("warn_title") or "") > WARN_TITLE_MAX or len(fields.get("warn_text") or "") > WARN_TEXT_MAX:
        raise ValueError(f"warn_title max. {WARN_TITLE_MAX}, warn_text max. {WARN_TEXT_MAX} Zeichen")

    await get_honeypot_settings(guild_id)   # stellt sicher, dass die Zeile existiert

    columns = ", ".join(f"`{key}` = %s" for key in fields)
    values = list(fields.values()) + [guild_id]

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE honeypot_settings SET {columns} WHERE guild_id = %s",
                values,
            )

    _cache.pop(guild_id, None)
    return await get_honeypot_settings(guild_id)

async def increment_catches(guild_id: int) -> int:
    """Zählt einen Fang atomar hoch und gibt den neuen Stand zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO honeypot_settings (guild_id, catches) VALUES (%s, 1)
                   ON DUPLICATE KEY UPDATE catches = catches + 1""",
                (guild_id,),
            )
            await cur.execute("SELECT catches FROM honeypot_settings WHERE guild_id = %s", (guild_id,))
            (total,) = await cur.fetchone()
    _cache.pop(guild_id, None)
    return int(total)

async def get_total_catches() -> int:
    """Summe aller Fänge über alle Server (z. B. für /info oder die Website)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(SUM(catches), 0) FROM honeypot_settings")
            (total,) = await cur.fetchone()
    return int(total)