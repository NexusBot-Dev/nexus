"""Streamplan als Discord-Events: welcher Twitch-Kanal pro Server abgeglichen wird und welche Events dazugehören.

stream_schedule_sync    — Schalter pro Server und überwachtem Kanal (Zeile da = eingeschaltet)
stream_schedule_events  — Twitch-Termin (segment_id) → Discord-Event, das der Bot dafür angelegt hat

Beide Tabellen haben bewusst keine Fremdschlüssel: Wird ein Stream-Alert entfernt, bleiben die Zeilen
stehen, und der nächste Abgleich räumt die Discord-Events dazu ab (siehe systems/stream_schedule.py).
"""
from datetime import datetime

import aiomysql

import database.database as db


async def enable(guild_id: int, monitored_channel_id: int) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO stream_schedule_sync (guild_id, monitored_channel_id) VALUES (%s, %s)",
                (guild_id, monitored_channel_id),
            )


async def disable(guild_id: int, monitored_channel_id: int) -> None:
    """Nur der Schalter — die Events räumt der Abgleich ab."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM stream_schedule_sync WHERE guild_id = %s AND monitored_channel_id = %s",
                (guild_id, monitored_channel_id),
            )


async def get_syncs_for_guild(guild_id: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT monitored_channel_id, created_at, last_synced_at, last_error, image_source
                   FROM stream_schedule_sync WHERE guild_id = %s ORDER BY created_at, monitored_channel_id""",
                (guild_id,),
            )
            return list(await cur.fetchall())


async def get_targets(guild_id: int | None = None) -> list[dict]:
    """Alles, was ein Abgleich anfassen muss: eingeschaltete Kanäle und Kanäle, zu denen noch Events gespeichert sind.

    sync_created_at ist None, wenn der Schalter aus ist (dann nur noch aufräumen); subscription_id ist None,
    wenn der Stream-Alert inzwischen entfernt wurde."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT t.guild_id, t.monitored_channel_id, s.created_at AS sync_created_at, s.image_source,
                          gs.id AS subscription_id, mc.platform, mc.platform_channel_id, mc.streamer_name
                   FROM (SELECT guild_id, monitored_channel_id FROM stream_schedule_sync
                         UNION
                         SELECT DISTINCT guild_id, monitored_channel_id FROM stream_schedule_events) t
                   LEFT JOIN stream_schedule_sync s
                          ON s.guild_id = t.guild_id AND s.monitored_channel_id = t.monitored_channel_id
                   LEFT JOIN guild_subscriptions gs
                          ON gs.guild_id = t.guild_id AND gs.monitored_channel_id = t.monitored_channel_id
                   LEFT JOIN monitored_channels mc ON mc.id = t.monitored_channel_id
                   WHERE %s IS NULL OR t.guild_id = %s
                   ORDER BY t.guild_id, s.created_at, t.monitored_channel_id""",
                (guild_id, guild_id),
            )
            return list(await cur.fetchall())


async def set_image_source(guild_id: int, monitored_channel_id: int, source: str) -> bool:
    """Bildquelle eines eingeschalteten Kanals. False, wenn der Kanal nicht eingeschaltet ist."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM stream_schedule_sync WHERE guild_id = %s AND monitored_channel_id = %s",
                (guild_id, monitored_channel_id),
            )
            if await cur.fetchone() is None:
                return False
            await cur.execute(
                "UPDATE stream_schedule_sync SET image_source = %s WHERE guild_id = %s AND monitored_channel_id = %s",
                (source, guild_id, monitored_channel_id),
            )
            return True


async def set_status(guild_id: int, monitored_channel_id: int, error: str | None) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE stream_schedule_sync SET last_synced_at = UTC_TIMESTAMP(), last_error = %s
                   WHERE guild_id = %s AND monitored_channel_id = %s""",
                (error, guild_id, monitored_channel_id),
            )


async def get_events(guild_id: int, monitored_channel_id: int) -> list[dict]:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT segment_id, event_id, start_time, fingerprint FROM stream_schedule_events
                   WHERE guild_id = %s AND monitored_channel_id = %s""",
                (guild_id, monitored_channel_id),
            )
            return list(await cur.fetchall())


async def save_event(guild_id: int, monitored_channel_id: int, segment_id: str, event_id: int | None,
                     start_time: datetime, fingerprint: str) -> None:
    """event_id None: Das Event wurde im Server gelöscht — der Bot legt es nicht wieder an."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO stream_schedule_events
                       (guild_id, monitored_channel_id, segment_id, event_id, start_time, fingerprint)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE event_id = VALUES(event_id), start_time = VALUES(start_time),
                                           fingerprint = VALUES(fingerprint)""",
                (guild_id, monitored_channel_id, segment_id, event_id, start_time.replace(tzinfo=None), fingerprint),
            )


async def forget_event(guild_id: int, monitored_channel_id: int, segment_id: str) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM stream_schedule_events WHERE guild_id = %s AND monitored_channel_id = %s AND segment_id = %s",
                (guild_id, monitored_channel_id, segment_id),
            )


async def count_upcoming_events(guild_id: int, now: datetime) -> dict[int, int]:
    """Kommende Events, die der Bot angelegt hat, je überwachtem Kanal."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT monitored_channel_id, COUNT(*) FROM stream_schedule_events
                   WHERE guild_id = %s AND event_id IS NOT NULL AND start_time > %s
                   GROUP BY monitored_channel_id""",
                (guild_id, now.replace(tzinfo=None)),
            )
            return {row[0]: row[1] for row in await cur.fetchall()}


# ── Eigener Text und eigenes Bild (Premium) ─────────────────────────────────────────────────────
# Eigene Tabelle: Übersichten lesen nur Text und Prüfsumme — das Bild (MEDIUMBLOB) nur, wenn ein Event es braucht.

async def get_custom_meta(guild_id: int) -> dict[int, dict]:
    """Je Kanal: description und image_hash (ohne das Bild selbst)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT monitored_channel_id, description, image_hash FROM stream_schedule_custom WHERE guild_id = %s",
                (guild_id,),
            )
            return {row["monitored_channel_id"]: row for row in await cur.fetchall()}


async def get_custom_image(guild_id: int, monitored_channel_id: int) -> bytes | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT image FROM stream_schedule_custom WHERE guild_id = %s AND monitored_channel_id = %s",
                (guild_id, monitored_channel_id),
            )
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def set_custom_description(guild_id: int, monitored_channel_id: int, description: str | None) -> None:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO stream_schedule_custom (guild_id, monitored_channel_id, description) VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE description = VALUES(description)""",
                (guild_id, monitored_channel_id, description),
            )


async def set_custom_image(guild_id: int, monitored_channel_id: int, image: bytes | None, image_hash: str | None) -> None:
    """Das Bild geht als Hex-Text an die Datenbank (UNHEX): aiomysql 0.3 kann mit aktuellem PyMySQL keine Bytes als
    Parameter übergeben (escape_bytes_prefixed gibt es dort nicht mehr)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO stream_schedule_custom (guild_id, monitored_channel_id, image, image_hash)
                   VALUES (%s, %s, UNHEX(%s), %s)
                   ON DUPLICATE KEY UPDATE image = VALUES(image), image_hash = VALUES(image_hash)""",
                (guild_id, monitored_channel_id, image.hex() if image is not None else None, image_hash),
            )


async def delete_custom(guild_id: int, monitored_channel_id: int) -> None:
    """Text und Bild weg — wenn der Stream-Alert des Kanals entfernt wurde."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM stream_schedule_custom WHERE guild_id = %s AND monitored_channel_id = %s",
                (guild_id, monitored_channel_id),
            )
