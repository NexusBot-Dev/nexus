import aiomysql
import database.database as db

DEDUP_WINDOW_SECONDS = 90

# ─── Monitored Channels ───────────────────────────────────────────────────

async def get_monitored_channel(platform: str, platform_channel_id: str) -> dict | None:
    """Holt einen überwachten Kanal anhand Plattform + externer Channel-ID."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM monitored_channels WHERE platform = %s AND platform_channel_id = %s",
                (platform, platform_channel_id),
            )
            return await cur.fetchone()

async def get_monitored_channel_by_id(monitored_channel_id: int) -> dict | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM monitored_channels WHERE id = %s",
                (monitored_channel_id,),
            )
            return await cur.fetchone()

async def get_or_create_monitored_channel(
    platform: str, platform_channel_id: str, streamer_name: str
) -> dict:
    """Holt einen überwachten Kanal oder legt ihn an — Basis für das Shared-Resource-Pattern.
    Mehrere Guilds können denselben Twitch-Kanal überwachen, ohne dass doppelte
    EventSub-Abos angelegt werden."""
    existing = await get_monitored_channel(platform, platform_channel_id)
    if existing:
        return existing

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO monitored_channels (platform, platform_channel_id, streamer_name)
                   VALUES (%s, %s, %s)""",
                (platform, platform_channel_id, streamer_name),
            )
    return await get_monitored_channel(platform, platform_channel_id)

async def set_live_status(monitored_channel_id: int, is_live: bool):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE monitored_channels SET live = %s, last_checked = NOW() WHERE id = %s",
                (is_live, monitored_channel_id),
            )

async def is_channel_still_referenced(monitored_channel_id: int) -> bool:
    """Prüft, ob noch irgendeine Guild diesen Kanal überwacht — wichtig vor dem Löschen,
    damit ein EventSub-Abo nicht entfernt wird, während es noch gebraucht wird."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM guild_subscriptions WHERE monitored_channel_id = %s",
                (monitored_channel_id,),
            )
            row = await cur.fetchone()
            return row[0] > 0

async def delete_monitored_channel(monitored_channel_id: int):
    """Löscht einen überwachten Kanal komplett — nur aufrufen, wenn keine Guild
    mehr referenziert (siehe is_channel_still_referenced)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM monitored_channels WHERE id = %s",
                (monitored_channel_id,),
            )

# ─── Guild Subscriptions ───────────────────────────────────────────────────

async def get_subscriptions_for_channel(monitored_channel_id: int) -> list[dict]:
    """Gibt alle Guild-Abos für einen überwachten Kanal zurück — genutzt vom Webhook,
    um alle Server zu benachrichtigen, die diesen Streamer überwachen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM guild_subscriptions WHERE monitored_channel_id = %s",
                (monitored_channel_id,),
            )
            return await cur.fetchall()

async def get_subscription(guild_id: int, monitored_channel_id: int) -> dict | None:
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM guild_subscriptions
                   WHERE guild_id = %s AND monitored_channel_id = %s""",
                (guild_id, monitored_channel_id),
            )
            return await cur.fetchone()

async def get_subscriptions_for_guild(guild_id: int) -> list[dict]:
    """Für /alert_list — zeigt alle überwachten Streamer/Kanäle einer Guild."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT gs.*, mc.platform, mc.platform_channel_id, mc.streamer_name, mc.live
                   FROM guild_subscriptions gs
                   JOIN monitored_channels mc ON mc.id = gs.monitored_channel_id
                   WHERE gs.guild_id = %s
                   ORDER BY gs.created_at DESC""",
                (guild_id,),
            )
            return await cur.fetchall()

async def add_subscription(
    guild_id: int,
    channel_id: int,
    monitored_channel_id: int,
    message: str | None = None,
) -> int:
    """Verknüpft eine Guild mit einem überwachten Kanal. Gibt die neue ID zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO guild_subscriptions
                   (guild_id, channel_id, monitored_channel_id, message)
                   VALUES (%s, %s, %s, %s)""",
                (guild_id, channel_id, monitored_channel_id, message),
            )
            return cur.lastrowid

async def remove_subscription(guild_id: int, monitored_channel_id: int) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """DELETE FROM guild_subscriptions
                   WHERE guild_id = %s AND monitored_channel_id = %s""",
                (guild_id, monitored_channel_id),
            )
            return cur.rowcount > 0

async def update_subscription(
    guild_id: int,
    monitored_channel_id: int,
    channel_id: int | None = None,
    message: str | None = ...,
) -> bool:
    """Aktualisiert Channel und/oder Custom-Message einer bestehenden Subscription.
    message=... (Default) bedeutet 'unverändert lassen', message=None heißt 'explizit löschen'."""
    updates = []
    params = []

    if channel_id is not None:
        updates.append("channel_id = %s")
        params.append(channel_id)

    if message is not ...:
        updates.append("message = %s")
        params.append(message)

    if not updates:
        return False

    params.extend([guild_id, monitored_channel_id])

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""UPDATE guild_subscriptions SET {", ".join(updates)}
                    WHERE guild_id = %s AND monitored_channel_id = %s""",
                tuple(params),
            )
            return cur.rowcount > 0

async def count_subscriptions_for_guild(guild_id: int, platform: str) -> int:
    """Für Free/Premium-Limits — pro Plattform getrennt gezählt."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT COUNT(*) FROM guild_subscriptions gs
                   JOIN monitored_channels mc ON mc.id = gs.monitored_channel_id
                   WHERE gs.guild_id = %s AND mc.platform = %s""",
                (guild_id, platform),
            )
            row = await cur.fetchone()
            return row[0]

async def set_eventsub_ids(monitored_channel_id: int, online_id: str, offline_id: str):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE monitored_channels SET eventsub_online_id = %s, eventsub_offline_id = %s WHERE id = %s",
                (online_id, offline_id, monitored_channel_id),
            )

async def is_subscription_active(guild_id: int, platform: str, monitored_channel_id: int, limit: int) -> bool:
    """FIFO: die ältesten Abos einer Plattform (nach created_at) gelten als aktiv, bis zum Limit.
    Wird bei jedem Alert-Versand geprüft, damit abgelaufenes Premium nicht mehr Kanäle beliefert,
    als das aktuelle Limit erlaubt — ohne dass überzählige Abos gelöscht werden müssen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT gs.monitored_channel_id FROM guild_subscriptions gs
                   JOIN monitored_channels mc ON mc.id = gs.monitored_channel_id
                   WHERE gs.guild_id = %s AND mc.platform = %s
                   ORDER BY gs.created_at ASC
                   LIMIT %s""",
                (guild_id, platform, limit),
            )
            rows = await cur.fetchall()
            active_ids = {row[0] for row in rows}
            return monitored_channel_id in active_ids

async def get_ping_role(guild_id: int, platform: str) -> int | None:
    """Sucht erst eine plattform-spezifische Rolle, fällt sonst auf die 'all'-Rolle zurück."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT platform, role_id FROM guild_ping_roles WHERE guild_id = %s AND platform IN (%s, 'all')",
                (guild_id, platform),
            )
            rows = await cur.fetchall()
            for row in rows:
                if row["platform"] == platform:
                    return row["role_id"]
            for row in rows:
                if row["platform"] == "all":
                    return row["role_id"]
            return None

async def set_ping_role(guild_id: int, platform: str, role_id: int):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO guild_ping_roles (guild_id, platform, role_id)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE role_id = %s""",
                (guild_id, platform, role_id, role_id),
            )

async def remove_ping_role(guild_id: int, platform: str):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM guild_ping_roles WHERE guild_id = %s AND platform = %s",
                (guild_id, platform),
            )

async def count_configured_platforms(guild_id: int) -> int:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(DISTINCT platform) FROM guild_ping_roles WHERE guild_id = %s",
                (guild_id,),
            )
            row = await cur.fetchone()
            return row[0]

async def set_youtube_last_video_id(monitored_channel_id: int, video_id: str):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE monitored_channels SET youtube_last_video_id = %s WHERE id = %s",
                (video_id, monitored_channel_id),
            )

async def set_youtube_playlist_id(monitored_channel_id: int, playlist_id: str):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE monitored_channels SET youtube_uploads_playlist_id = %s WHERE id = %s",
                (playlist_id, monitored_channel_id),
            )

async def get_monitored_channels_by_platform(platform: str) -> list[dict]:
    """Gibt alle überwachten Kanäle einer Plattform zurück — genutzt vom Polling-Task,
    um bei jedem Zyklus alle YouTube-Kanäle durchzugehen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM monitored_channels WHERE platform = %s",
                (platform,),
            )
            return await cur.fetchall()

async def get_linked_subscription(subscription_id: int) -> dict | None:
    """Gibt die verknüpfte Subscription zurück, falls vorhanden — inkl. deren
    monitored_channel-Infos für die Alert-Zusammenführung."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT
                       CASE WHEN sl.subscription_a_id = %s THEN sl.subscription_b_id
                            ELSE sl.subscription_a_id END AS linked_sub_id
                   FROM streamer_links sl
                   WHERE sl.subscription_a_id = %s OR sl.subscription_b_id = %s""",
                (subscription_id, subscription_id, subscription_id),
            )
            row = await cur.fetchone()
            if not row:
                return None

            await cur.execute(
                """SELECT gs.*, mc.platform, mc.streamer_name, mc.live
                   FROM guild_subscriptions gs
                   JOIN monitored_channels mc ON mc.id = gs.monitored_channel_id
                   WHERE gs.id = %s""",
                (row["linked_sub_id"],),
            )
            return await cur.fetchone()

async def mark_alert_posted(subscription_id: int):
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE guild_subscriptions SET last_live_alert_at = NOW() WHERE id = %s",
                (subscription_id,),
            )

async def should_suppress_duplicate(subscription_id: int) -> bool:
    """True, wenn die verlinkte Subscription innerhalb des Dedup-Fensters
    bereits einen Live-Alert gepostet hat."""
    linked = await get_linked_subscription(subscription_id)
    if not linked or not linked.get("last_live_alert_at"):
        return False

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT TIMESTAMPDIFF(SECOND, %s, NOW()) AS diff",
                (linked["last_live_alert_at"],),
            )
            row = await cur.fetchone()
            return row[0] is not None and row[0] < DEDUP_WINDOW_SECONDS

async def get_links_for_guild(guild_id: int) -> list[dict]:
    """Für die Dashboard-Übersicht — alle Verknüpfungen einer Guild mit Streamer-Namen."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT
                       sl.id,
                       gs_a.id AS sub_a_id, mc_a.platform AS platform_a, mc_a.streamer_name AS name_a,
                       gs_b.id AS sub_b_id, mc_b.platform AS platform_b, mc_b.streamer_name AS name_b
                   FROM streamer_links sl
                   JOIN guild_subscriptions gs_a ON gs_a.id = sl.subscription_a_id
                   JOIN monitored_channels mc_a ON mc_a.id = gs_a.monitored_channel_id
                   JOIN guild_subscriptions gs_b ON gs_b.id = sl.subscription_b_id
                   JOIN monitored_channels mc_b ON mc_b.id = gs_b.monitored_channel_id
                   WHERE sl.guild_id = %s""",
                (guild_id,),
            )
            return await cur.fetchall()

async def create_link(guild_id: int, sub_a_id: int, sub_b_id: int) -> int:
    a, b = sorted((sub_a_id, sub_b_id))
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO streamer_links (guild_id, subscription_a_id, subscription_b_id) VALUES (%s, %s, %s)",
                (guild_id, a, b),
            )
            return cur.lastrowid

async def delete_link(link_id: int, guild_id: int) -> bool:
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM streamer_links WHERE id = %s AND guild_id = %s",
                (link_id, guild_id),
            )
            return cur.rowcount > 0

async def delete_user(guild_id: int, user_id: int):
    """DSGVO — keine User-spezifischen Daten in Stream-Alerts, nur Guild-Konfiguration."""
    pass