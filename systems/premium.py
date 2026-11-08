import logging
import discord
import database.database as db
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from cachetools import TTLCache

log = logging.getLogger(__name__)

_premium_cache = TTLCache[int, bool](maxsize=10_000, ttl=300)

EARLY_BIRD_DAYS = 90
EARLY_BIRD_END = datetime(2026, 11, 30, 22, 59, tzinfo=timezone.utc)

@dataclass(frozen=True)
class GrantResult:
    applied: bool                  # False = bestehendes Premium war mindestens gleich lang, nichts geändert
    expires_at: Optional[datetime]  # Ablauf nach dem Aufruf (None = unbegrenzt)
    source: str                    # Quelle des Premiums nach dem Aufruf

@dataclass(frozen=True)
class PremiumInfo:
    active: bool
    expires_at: Optional[datetime]  # None = unbegrenzt
    source: str

def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """DB liefert naive Datetimes (UTC) — für Vergleiche timezone-aware machen."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def _existing_wins(existing_expires: Optional[datetime], new_expires: Optional[datetime], now: datetime) -> bool:
    """True, wenn ein BESTEHENDES, noch aktives Premium mindestens so lange läuft wie das neue.
    None = unbegrenzt. Abgelaufenes Premium wird nie geschützt."""
    if existing_expires is None:
        return True
    existing_expires = _as_utc(existing_expires)
    if existing_expires <= now:
        return False
    return new_expires is not None and existing_expires >= _as_utc(new_expires)

def invalidate_cache(guild_id: int) -> None:
    """Löscht einen Server explizit aus dem Cache."""
    _premium_cache.pop(guild_id, None)

async def is_premium(guild_id: int) -> bool:
    # 1. Schneller In-Memory Return (TTLCache prüft Ablaufzeit automatisch)
    if guild_id in _premium_cache:
        return _premium_cache[guild_id]

    # 2. Datenbank abfragen
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT expires_at FROM premium WHERE guild_id = %s",
                (guild_id,)
            )
            row = await cur.fetchone()

            if row is None:
                res = False
            elif row[0] is None:
                res = True  # Kein Ablaufdatum = Lifetime / Unbegrenzt aktiv
            else:
                expires_at = row[0]
                # Sicherstellen, dass expires_at timezone-aware in UTC verglichen wird
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                res = expires_at > datetime.now(timezone.utc)

            _premium_cache[guild_id] = res
            return res

async def get_premium_info(guild_id: int) -> Optional[PremiumInfo]:
    """Liefert Quelle und Ablauf des Premiums (für Support/Dashboard). None = nie Premium gehabt."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT expires_at, source FROM premium WHERE guild_id = %s", (guild_id,))
            row = await cur.fetchone()
    if row is None:
        return None
    expires_at = _as_utc(row[0])
    active = expires_at is None or expires_at > datetime.now(timezone.utc)
    return PremiumInfo(active=active, expires_at=expires_at, source=row[1])

async def set_premium_from_discord(
    bot: discord.Client,
    guild_id: int,
    entitlement: discord.Entitlement,
    reason: str = "Discord Store Kauf",
) -> Optional[datetime]:
    """Trägt ein Discord-Abo aus dem Entitlement in die DB ein.
    Ein bezahltes Abo gewinnt immer: es überschreibt ein laufendes Gift/Manual-Premium
    (ends_at = None bei laufenden Abos = unbegrenzt bis zur Beendigung)."""
    now = datetime.now(timezone.utc)
    expires_at = entitlement.ends_at

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO premium (guild_id, expires_at, source, updated_at)
                VALUES (%s, %s, 'discord', %s)
                ON DUPLICATE KEY UPDATE
                    expires_at = VALUES(expires_at),
                    source = VALUES(source),
                    updated_at = VALUES(updated_at)
                """,
                (guild_id, expires_at, now)
            )

    # Cache erst NACH dem Schreiben verwerfen, sonst kann ein paralleler is_premium() den alten Stand neu cachen
    invalidate_cache(guild_id)
    log.info("Premium für Guild %s gesetzt (Quelle: discord). Läuft ab: %s. Grund: %s", guild_id, expires_at, reason)
    bot.dispatch("premium_update", guild_id, expires_at, reason)
    return expires_at

async def grant_premium_until(
    bot, guild_id, expires_at, source, reason, stack: bool = False,
) -> GrantResult:
    ...
    now = datetime.now(timezone.utc)
    async with db.pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT expires_at, source FROM premium WHERE guild_id = %s FOR UPDATE",
                    (guild_id,),
                )
                row = await cur.fetchone()

                if row is not None:
                    existing = _as_utc(row[0])
                    skip = False
                    if existing is None:
                        skip = True  # unbegrenzt (laufendes Abo): nichts zu verschenken
                    elif stack:
                        if existing > now:
                            expires_at = existing + (expires_at - now)  # obendrauf
                    elif _existing_wins(row[0], expires_at, now):
                        skip = True
                    if skip:
                        await conn.commit()
                        log.info("Premium-Vergabe für Guild %s übersprungen (bestehend: %s bis %s). Grund der Vergabe: %s",
                                 guild_id, row[1], row[0], reason)
                        return GrantResult(applied=False, expires_at=existing, source=row[1])

                await cur.execute(
                    """INSERT INTO premium (guild_id, expires_at, source, updated_at)
                       VALUES (%s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                           expires_at = VALUES(expires_at),
                           source = VALUES(source),
                           updated_at = VALUES(updated_at)""",
                    (guild_id, expires_at, source, now),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    invalidate_cache(guild_id)
    log.info("Premium für Guild %s gesetzt (Quelle: %s). Läuft ab: %s. Grund: %s", guild_id, source, expires_at, reason)
    bot.dispatch("premium_update", guild_id, expires_at, reason)
    return GrantResult(applied=True, expires_at=expires_at, source=source)

async def grant_early_bird_premium(bot: discord.Client, guild_id: int) -> Optional[GrantResult]:
    """Early-Bird-Gift beim Beitritt. Nach EARLY_BIRD_END wird nichts mehr vergeben (None)."""
    now = datetime.now(timezone.utc)
    if now >= EARLY_BIRD_END:
        return None
    return await grant_premium_until(
        bot, guild_id,
        expires_at=now + timedelta(days=EARLY_BIRD_DAYS),
        source="gift",
        reason=f"Early-Bird {EARLY_BIRD_DAYS} Days Gift",
    )

async def remove_premium(bot: discord.Client, guild_id: int, reason: str = "Abo abgelaufen") -> None:
    """Beendet Discord-Abo-Premium, indem expires_at auf 'jetzt' gesetzt wird.
    Betrifft NUR source='discord' — Gift/Manual laufen über ihr eigenes expires_at."""
    now = datetime.now(timezone.utc)

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE premium SET expires_at = %s, updated_at = %s WHERE guild_id = %s AND source = 'discord'",
                (now, now, guild_id)
            )
            changed = cur.rowcount

    invalidate_cache(guild_id)
    if changed:
        bot.dispatch("premium_update", guild_id, None, reason)
    else:
        log.info("remove_premium für Guild %s ohne Wirkung (kein Discord-Premium vorhanden). Grund: %s", guild_id, reason)

async def set_discord_period_end(bot, guild_id, expires_at, reason) -> bool:
    now = datetime.now(timezone.utc)
    async with db.pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT expires_at, source FROM premium WHERE guild_id = %s FOR UPDATE",
                    (guild_id,),
                )
                row = await cur.fetchone()
                if row and row[1] != "discord" and _existing_wins(row[0], expires_at, now):
                    await conn.commit()
                    return False
                await cur.execute(
                    """INSERT INTO premium (guild_id, expires_at, source, updated_at)
                       VALUES (%s, %s, 'discord', %s)
                       ON DUPLICATE KEY UPDATE
                           expires_at = VALUES(expires_at),
                           source = 'discord',
                           updated_at = VALUES(updated_at)""",
                    (guild_id, expires_at, now),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    invalidate_cache(guild_id)
    log.info("Premium-Ablauf für Guild %s auf %s gesetzt. Grund: %s", guild_id, expires_at, reason)
    bot.dispatch("premium_update", guild_id, expires_at, reason)
    return True

def describe_subscription(subscription: discord.Subscription) -> str:
    """Einzeiler für Logs: genau das, was Discord geschickt hat."""
    return (
        f"id={subscription.id} user={subscription.user_id} status={subscription.status.name} "
        f"canceled_at={subscription.canceled_at} period_end={subscription.current_period_end} "
        f"entitlements={subscription.entitlement_ids}"
    )

async def _active_guild_ids(bot: discord.Client, subscription: discord.Subscription) -> set[int]:
    """Guilds mit noch aktivem Entitlement dieser Subscription (die Subscription selbst hat keine guild_id).
    Beendete/gelöschte Entitlements zählen nicht, damit ein verspätetes Event kein Premium wiederbelebt."""
    guild_ids: set[int] = set()
    for entitlement_id in subscription.entitlement_ids:
        try:
            entitlement = await bot.fetch_entitlement(entitlement_id)
        except discord.HTTPException as e:
            log.warning("Entitlement %s zu Subscription %s nicht abrufbar: %s", entitlement_id, subscription.id, e)
            continue
        if entitlement.guild_id is not None and not entitlement.deleted and not entitlement.is_expired():
            guild_ids.add(entitlement.guild_id)
    return guild_ids

async def apply_subscription_update(bot: discord.Client, subscription: discord.Subscription) -> None:
    """Übernimmt Kündigung/Wiederaufnahme aus der Subscription in premium.expires_at.

    ACTIVE            -> unbegrenzt (verlängert sich)
    ENDING / INACTIVE -> Ende der bezahlten Periode (current_period_end)

    Laut Discord-Doku ist 'gekündigt, läuft noch' = ENDING. In der Praxis kam eine normale Kündigung
    in den Abo-Einstellungen aber als INACTIVE (mit canceled_at, current_period_end in der Zukunft),
    während das Entitlement noch bis zum Periodenende aktiv war. Beides bedeutet: bezahlt ist nur bis
    current_period_end. Ist das Datum schon vorbei, endet das Premium damit sofort."""
    if subscription.status == discord.SubscriptionStatus.active:
        new_expiry, reason = None, "Discord Abo aktiv (verlängert sich)"
    elif subscription.status in (discord.SubscriptionStatus.ending, discord.SubscriptionStatus.inactive):
        new_expiry = subscription.current_period_end
        reason = f"Discord Abo gekündigt/beendet (Status {subscription.status.name}, läuft bis Periodenende)"
    else:
        log.warning("Unbekannter Subscription-Status %s bei Subscription %s", subscription.status, subscription.id)
        return

    for guild_id in await _active_guild_ids(bot, subscription):
        await set_discord_period_end(bot, guild_id, new_expiry, reason)

async def sync_entitlements(bot: discord.Client) -> int:
    """
    Gleicht NUR Discord-Store-Käufe (source='discord') gegen die aktiven
    Discord-Entitlements ab. Gift- und Manual-Premium bleiben davon unberührt.
    """
    active_guild_ids = set()
    async for entitlement in bot.entitlements(exclude_ended=True, limit=None):
        if entitlement.guild_id is not None:
            active_guild_ids.add(entitlement.guild_id)

    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT guild_id FROM premium
                   WHERE source = 'discord'
                   AND (expires_at IS NULL OR expires_at > NOW())"""
            )
            db_discord_premium_guild_ids = {row[0] for row in await cur.fetchall()}

    stale = db_discord_premium_guild_ids - active_guild_ids
    for guild_id in stale:
        await remove_premium(bot, guild_id, reason="Tägl. Entitlement-Abgleich: kein aktives Discord-Abo mehr gefunden")

    return len(stale)