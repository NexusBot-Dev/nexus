"""Twitch-Streamplan als Discord-Events.

Ein Abgleich (sync_all) läuft regelmäßig für alle Server mit eingeschaltetem Schalter:
  - kommende Termine im Zeitfenster (7 Tage, aus feature_limits) → Event anlegen bzw. bei Änderungen anpassen,
  - abgesagt, aus dem Plan entfernt, im Urlaub oder außerhalb des Fensters → Event löschen,
  - Schalter aus, Stream-Alert entfernt, Modul aus oder über dem Limit → alle kommenden Events löschen.

Der Bot fasst nur Events an, die er selbst angelegt hat (gespeichert in stream_schedule_events). Laufende
und vergangene Events bleiben, wie sie sind — Discord startet und beendet Events mit Link als Ort selbst.
Löscht jemand im Server ein Event von Hand, legt der Bot es nicht wieder an.

Nötiges Recht: „Events erstellen“ (create_events) — damit darf der Bot auch seine eigenen Events ändern und löschen.
"""
import asyncio
import hashlib
import json
import logging
import re
import aiohttp
import discord
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from cogs.utils import get_lang
from config import DEV_GUILD_ID
from database import db_limits, db_settings, db_stream_alerts, db_stream_schedule
from emojis import NexusEmojis
from systems import schedule_banner, schedule_images, twitch_api
from systems.premium import is_premium
from systems.stream_alert_locks import schedule_lock

log = logging.getLogger(__name__)

PLATFORM = "twitch"
# Freigegeben. Auf False gesetzt nur noch auf dem Dev-Server nutzbar (Notbremse; der Abgleich räumt dann
# die Events anderer Server ab, die Schalter bleiben gespeichert).
RELEASED = True
LIMIT_STREAMERS = "stream_schedule_streamers"   # feature_limits: wie viele Kanäle pro Server
LIMIT_DAYS = "stream_schedule_days"             # feature_limits: wie viele Tage im Voraus
DEFAULT_DURATION = timedelta(hours=3)           # falls ein Termin keine Endzeit hat (Discord verlangt eine)
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Fehlerzustände für Dashboard und Befehl (stream_schedule_sync.last_error)
ERR_PERMISSION = "missing_permission"
ERR_NO_SCHEDULE = "no_schedule"
ERR_TWITCH = "twitch_error"
ERR_DISCORD = "discord_error"


def is_available(guild_id: int) -> bool:
    """Ohne Freigabe nur auf dem Dev-Server — anderswo lässt sich nichts einschalten, und der Abgleich
    räumt dort Events ab, falls vorher schon jemand eingeschaltet hat."""
    return RELEASED or guild_id == DEV_GUILD_ID


# Bildquelle pro Streamer (stream_schedule_sync.image_source)
IMAGE_BANNER = "banner"     # Banner aus dem Kategorie-Cover, ohne Kategorie das Offline-Bild
IMAGE_OFFLINE = "offline"   # immer das Offline-Bild des Kanals
IMAGE_NONE = "none"         # kein Bild
IMAGE_CUSTOM = "custom"     # eigenes Bild (Premium) — ohne Premium oder ohne Bild wie IMAGE_BANNER
IMAGE_SOURCES = (IMAGE_BANNER, IMAGE_OFFLINE, IMAGE_NONE, IMAGE_CUSTOM)

# Eigener Text (Premium): Platzhalter und Länge der Vorlage
TEXT_PLACEHOLDERS = ("title", "category", "link", "streamer")
MAX_TEXT_LENGTH = 800   # Vorlage; die fertige Beschreibung darf bei Discord bis 1000 Zeichen haben

BANNER_VERSION = 1   # hochzählen, wenn sich das Aussehen des Banners ändert — dann bekommen alle Events das neue


@dataclass(frozen=True)
class PlannedEvent:
    segment_id: str
    name: str
    description: str
    start: datetime
    end: datetime
    location: str
    streamer: str = ""
    login: str = ""
    category_id: str | None = None
    category: str | None = None
    image_source: str = IMAGE_BANNER
    custom_image_hash: str | None = None   # nur gesetzt, wenn das eigene Bild gilt (Premium, Quelle „custom“)

    @property
    def image_key(self) -> str:
        """Woher das Bild kommt — gehört in den Fingerabdruck, damit ein Wechsel das Event neu bebildert."""
        if self.image_source == IMAGE_NONE:
            return "none"
        if self.custom_image_hash:
            return f"custom:{self.custom_image_hash}"
        if self.image_source == IMAGE_BANNER and self.category_id:
            return f"category:{self.category_id}:v{BANNER_VERSION}"
        return "offline"

    @property
    def fingerprint(self) -> str:
        data = [self.name, self.description, self.start.isoformat(), self.end.isoformat(), self.location, self.image_key]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()


# ── Twitch-Plan → geplante Events ────────────────────────────────────────────────────────────────

def _in_vacation(start: datetime, end: datetime, vacation: dict | None) -> bool:
    if not vacation or not vacation.get("start_time") or not vacation.get("end_time"):
        return False
    return start < twitch_api.parse_time(vacation["end_time"]) and end > twitch_api.parse_time(vacation["start_time"])


def render_text(template: str, *, title: str, category: str | None, link: str, streamer: str) -> str:
    """Eigenen Text füllen. Unbekannte Platzhalter und einzelne Klammern bleiben, wie sie sind —
    kein str.format, das an jedem „{“ scheitern oder Attribute auslesen würde."""
    values = {"title": title, "category": category or "", "link": link, "streamer": streamer}
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


def plan_events(schedule: dict, *, streamer: str, lang: dict, now: datetime, days: int,
                image_source: str = IMAGE_BANNER, custom_text: str | None = None,
                custom_image_hash: str | None = None) -> list[PlannedEvent]:
    """custom_text / custom_image_hash: nur übergeben, wenn sie gelten (Premium) — sonst Standardtext und -bild."""
    login = schedule.get("broadcaster_login") or streamer.lower()
    location = f"https://twitch.tv/{login}"
    until = now + timedelta(days=days)
    planned = []
    for segment in schedule.get("segments") or []:
        if segment.get("canceled_until"):
            continue
        start = twitch_api.parse_time(segment["start_time"])
        end = twitch_api.parse_time(segment["end_time"]) if segment.get("end_time") else start + DEFAULT_DURATION
        if end <= start:
            end = start + DEFAULT_DURATION
        if start <= now or start > until or _in_vacation(start, end, schedule.get("vacation")):
            continue
        category = (segment.get("category") or {}).get("name")
        category_id = (segment.get("category") or {}).get("id")
        title = (segment.get("title") or "").strip()
        if not title:
            title = (lang["schedule_event_title"].format(streamer=streamer, category=category) if category
                     else lang["schedule_event_title_plain"].format(streamer=streamer))
        if custom_text:
            description = render_text(custom_text, title=title, category=category, link=location, streamer=streamer)
        else:
            lines = [f"🎮 {category}"] if category else []
            lines += [f"{NexusEmojis.Integrations.TWITCH} twitch.tv/{login}", "", lang["schedule_event_source"].format(streamer=streamer)]
            description = "\n".join(lines)
        planned.append(PlannedEvent(
            segment_id=segment["id"], name=title[:100], description=description[:1000],
            start=start, end=end, location=location[:100],
            streamer=streamer, login=login, category_id=category_id if category else None, category=category,
            image_source=image_source, custom_image_hash=custom_image_hash if image_source == IMAGE_CUSTOM else None,
        ))
    return planned


# ── Abgleich ─────────────────────────────────────────────────────────────────────────────────────

def _as_utc(value: datetime) -> datetime:
    """Aus der Datenbank kommen Zeiten ohne Zeitzone — gespeichert ist UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _get_event(guild: discord.Guild, event_id: int) -> discord.ScheduledEvent | None:
    event = guild.get_scheduled_event(event_id)
    if event is not None:
        return event
    try:
        return await guild.fetch_scheduled_event(event_id)
    except discord.NotFound:
        return None


async def _delete_event(guild: discord.Guild, event_id: int | None) -> None:
    if event_id is None:
        return
    event = await _get_event(guild, event_id)
    if event is None or event.status is not discord.EventStatus.scheduled:
        return   # schon weg, oder läuft bzw. ist vorbei — dann lässt der Bot es in Ruhe
    try:
        await event.delete()
    except discord.NotFound:
        pass


async def remove_events(guild: discord.Guild, monitored_channel_id: int, now: datetime) -> None:
    """Alle kommenden Events eines Kanals löschen (Schalter aus, Alert entfernt, Modul aus, Limit überschritten)."""
    for row in await db_stream_schedule.get_events(guild.id, monitored_channel_id):
        if _as_utc(row["start_time"]) > now:
            await _delete_event(guild, row["event_id"])
        await db_stream_schedule.forget_event(guild.id, monitored_channel_id, row["segment_id"])


class _Images:
    """Bilder für die Events eines Abgleichs — jedes höchstens einmal geladen bzw. gerechnet, und nur bei Bedarf.

    Mit Kategorie: Banner aus dem Twitch-Cover (systems/schedule_banner.py). Ohne Kategorie, oder wenn das
    Cover fehlt: das Offline-Bild des Kanals. Fehlt auch das, bleibt das Event ohne Bild."""

    def __init__(self):
        self._offline: dict[str, bytes | None] = {}
        self._banners: dict[tuple, bytes | None] = {}
        self._custom: dict[tuple, bytes | None] = {}

    async def for_event(self, event: PlannedEvent, broadcaster_id: str, guild_id: int,
                        monitored_channel_id: int) -> bytes | None:
        if event.image_source == IMAGE_NONE:
            return None
        if event.custom_image_hash:
            key = (guild_id, monitored_channel_id, event.custom_image_hash)
            if key not in self._custom:
                self._custom[key] = await db_stream_schedule.get_custom_image(guild_id, monitored_channel_id)
            if self._custom[key]:
                return self._custom[key]
        if event.image_source in (IMAGE_BANNER, IMAGE_CUSTOM) and event.category_id:
            key = (event.category_id, event.category, event.streamer, event.login)
            if key not in self._banners:
                self._banners[key] = await self._banner(event)
            if self._banners[key]:
                return self._banners[key]
        return await self.offline(broadcaster_id)

    async def offline(self, broadcaster_id: str) -> bytes | None:
        if broadcaster_id not in self._offline:
            self._offline[broadcaster_id] = await self._load_offline(broadcaster_id)
        return self._offline[broadcaster_id]

    @staticmethod
    async def _banner(event: PlannedEvent) -> bytes | None:
        try:
            url = await twitch_api.get_box_art_url(event.category_id, *schedule_banner.COVER_SIZE)
            cover = await download_image(url) if url else None
            if cover is None:
                log.info("Kein Cover für Kategorie %s (%s) — Offline-Bild stattdessen.", event.category_id, event.category)
                return None
            subtitle = f"{event.streamer}  ·  twitch.tv/{event.login}"
            # Pillow rechnet ein paar Zehntelsekunden — nicht im Thread des Bots
            return await asyncio.to_thread(schedule_banner.render_banner, cover, event.category, subtitle)
        except Exception:
            log.warning("Banner für Kategorie %s nicht erstellt — Offline-Bild stattdessen.", event.category_id, exc_info=True)
            return None

    @staticmethod
    async def _load_offline(broadcaster_id: str) -> bytes | None:
        try:
            url = await twitch_api.get_offline_image_url(broadcaster_id)
            if not url:
                log.info("Twitch-Kanal %s hat kein Offline-Bild — Events ohne Bild.", broadcaster_id)
                return None
            image = await download_image(url)
            if image is None:
                log.warning("Offline-Bild von Twitch-Kanal %s nicht geladen (%s) — Events ohne Bild.", broadcaster_id, url)
            return image
        except Exception:
            log.warning("Offline-Bild von Twitch-Kanal %s nicht ladbar — Event ohne Bild.", broadcaster_id, exc_info=True)
            return None


async def download_image(url: str) -> bytes | None:
    """Ganze Datei laden — None bei Fehlerstatus oder über MAX_IMAGE_BYTES.
    Nicht resp.content.read(n): Das liefert nur, was gerade im Puffer liegt (bei großen Bildern ein abgeschnittenes)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200 or (resp.content_length or 0) > MAX_IMAGE_BYTES:
                return None
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    return None
            return bytes(data)


async def sync_channel(guild: discord.Guild, monitored_channel_id: int, broadcaster_id: str,
                       planned: list[PlannedEvent], now: datetime, images: _Images) -> str | None:
    """Events eines Kanals auf den Plan bringen. Gibt einen Fehlerzustand zurück (oder None)."""
    stored = {row["segment_id"]: row for row in await db_stream_schedule.get_events(guild.id, monitored_channel_id)}
    wanted = {p.segment_id: p for p in planned}

    # Nicht mehr im Plan: kommende Events löschen; vergangene nur vergessen
    for segment_id, row in stored.items():
        if segment_id in wanted:
            continue
        if _as_utc(row["start_time"]) > now:
            await _delete_event(guild, row["event_id"])
        await db_stream_schedule.forget_event(guild.id, monitored_channel_id, segment_id)

    for event in sorted(planned, key=lambda p: p.start):
        row = stored.get(event.segment_id)
        fields = dict(name=event.name, description=event.description, start_time=event.start,
                      end_time=event.end, location=event.location)
        try:
            if row is None:
                image = await images.for_event(event, broadcaster_id, guild.id, monitored_channel_id)
                created = await guild.create_scheduled_event(
                    **fields, entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only,
                    image=image if image else discord.utils.MISSING,
                )
                await db_stream_schedule.save_event(guild.id, monitored_channel_id, event.segment_id, created.id,
                                                    event.start, event.fingerprint)
            elif row["event_id"] is not None and row["fingerprint"] != event.fingerprint:
                existing = await _get_event(guild, row["event_id"])
                if existing is None:
                    # Im Server gelöscht — merken und nicht wieder anlegen
                    await db_stream_schedule.save_event(guild.id, monitored_channel_id, event.segment_id, None,
                                                        event.start, event.fingerprint)
                    continue
                if existing.status is discord.EventStatus.scheduled:
                    # Bild immer mitschicken: Die Änderung kann auch die Kategorie (und damit das Banner) sein
                    await existing.edit(**fields, image=await images.for_event(event, broadcaster_id, guild.id, monitored_channel_id))
                await db_stream_schedule.save_event(guild.id, monitored_channel_id, event.segment_id, existing.id,
                                                    event.start, event.fingerprint)
        except discord.Forbidden:
            return ERR_PERMISSION
        except discord.HTTPException:
            # z. B. 100 geplante Events erreicht — die übrigen Termine folgen beim nächsten Abgleich
            log.warning("Event für Guild %s / Kanal %s nicht angelegt oder geändert.", guild.id, monitored_channel_id, exc_info=True)
            return ERR_DISCORD
    return None


async def sync_all(bot, *, now: datetime | None = None) -> None:
    """Ein Durchlauf für alle Server. Fehler bleiben auf den jeweiligen Server bzw. Kanal begrenzt."""
    await _sync(bot, None, now or datetime.now(timezone.utc))


async def sync_guild(bot, guild_id: int, *, now: datetime | None = None) -> None:
    """Sofort abgleichen, z. B. direkt nach dem Ein- oder Ausschalten — nicht erst beim nächsten stündlichen Lauf."""
    await _sync(bot, guild_id, now or datetime.now(timezone.utc))


async def _sync(bot, only_guild_id: int | None, now: datetime) -> None:
    targets = await db_stream_schedule.get_targets(only_guild_id)
    if not targets:
        return
    # Einmal pro Streamer bei Twitch fragen, so weit voraus, wie es das größte Fenster (Premium) braucht
    fetch_until = now + timedelta(days=await db_limits.get_limit(0, LIMIT_DAYS, True))
    schedules: dict[str, dict | None | Exception] = {}   # Plan je Streamer (None: kein Plan, Exception: nicht abrufbar)
    images = _Images()

    by_guild: dict[int, list[dict]] = {}
    for target in targets:
        by_guild.setdefault(target["guild_id"], []).append(target)

    for guild_id, guild_targets in by_guild.items():
        guild = bot.get_guild(guild_id)
        try:
            async with schedule_lock(guild_id):
                await _sync_guild(guild, guild_id, guild_targets, schedules, images, now, fetch_until)
        except Exception:
            log.exception("Streamplan-Abgleich für Guild %s fehlgeschlagen.", guild_id)


async def _sync_guild(guild: discord.Guild | None, guild_id: int, targets: list[dict],
                      schedules: dict, images: _Images, now: datetime, fetch_until: datetime) -> None:
    if guild is None:
        return   # Bot nicht (mehr) auf dem Server — nichts zu tun, Events kann er dort ohnehin nicht löschen

    enabled = [t for t in targets if t["sync_created_at"] is not None and t["subscription_id"] is not None
               and t["platform"] == PLATFORM]
    premium = await is_premium(guild_id)
    limit = await db_limits.get_limit(guild_id, LIMIT_STREAMERS, premium)
    days = await db_limits.get_limit(guild_id, LIMIT_DAYS, premium)
    module_on = await db_settings.is_module_enabled(guild_id, "stream_alerts")
    usable = module_on and is_available(guild_id)
    active = {t["monitored_channel_id"] for t in enabled[:limit]} if usable else set()   # die ältesten zuerst

    can_create = guild.me.guild_permissions.create_events
    custom = await db_stream_schedule.get_custom_meta(guild_id)
    lang = None
    for target in targets:
        mc_id = target["monitored_channel_id"]
        if target["subscription_id"] is None and mc_id in custom:
            await db_stream_schedule.delete_custom(guild_id, mc_id)   # Stream-Alert entfernt: eigener Text und Bild auch
        if mc_id not in active:
            if can_create:
                await remove_events(guild, mc_id, now)
            continue
        if not can_create:
            await db_stream_schedule.set_status(guild_id, mc_id, ERR_PERMISSION)
            continue

        broadcaster_id = target["platform_channel_id"]
        if broadcaster_id not in schedules:
            try:
                schedules[broadcaster_id] = await twitch_api.get_schedule(broadcaster_id, now, fetch_until)
            except Exception as e:   # Twitch nicht erreichbar: vorhandene Events bleiben, nächster Versuch beim nächsten Lauf
                log.warning("Streamplan von Twitch-Kanal %s nicht abrufbar.", broadcaster_id, exc_info=True)
                schedules[broadcaster_id] = e
        schedule = schedules[broadcaster_id]
        if isinstance(schedule, Exception):
            await db_stream_schedule.set_status(guild_id, mc_id, ERR_TWITCH)
            continue

        if lang is None:
            lang = await get_lang(guild_id)
        own = custom.get(mc_id, {}) if premium else {}   # Premium abgelaufen: gespeichert, aber nicht benutzt
        planned = plan_events(
            schedule, streamer=target["streamer_name"], lang=lang, now=now, days=days,
            image_source=target.get("image_source") or IMAGE_BANNER,
            custom_text=own.get("description"), custom_image_hash=own.get("image_hash"),
        ) if schedule else []
        error = await sync_channel(guild, mc_id, broadcaster_id, planned, now, images)
        await db_stream_schedule.set_status(guild_id, mc_id, error or (None if schedule else ERR_NO_SCHEDULE))


# ── Ein- und Ausschalten, Übersicht (für /stream_schedule und das Dashboard) ─────────────────────

class ScheduleError(Exception):
    """Erwarteter Abbruch. reason: "unavailable" (nicht freigegeben, siehe is_available), "not_monitored" (kein Twitch-Alert
    für den Kanal), "limit" (limit=, max= — max ist das Limit mit Premium), "not_enabled" (Einstellung für einen
    ausgeschalteten Kanal), "premium" (eigener Text oder eigenes Bild ohne Premium), "no_custom_image",
    "text_too_long" (max=) oder "image_rejected" (problem=)."""

    def __init__(self, reason: str, **params):
        super().__init__(reason)
        self.reason = reason
        self.params = params


@dataclass(frozen=True)
class ScheduleEntry:
    monitored_channel_id: int
    streamer_name: str
    enabled: bool
    active: bool          # eingeschaltet und im Limit — nur dann gleicht der Bot ab
    error: str | None     # ERR_* aus dem letzten Abgleich
    events: int           # kommende Events, die der Bot angelegt hat
    image_source: str = IMAGE_BANNER
    custom_text: str | None = None          # gespeichert — gilt nur mit Premium
    custom_image_hash: str | None = None    # Prüfsumme des eigenen Bildes (für Vorschau und Caching)


@dataclass(frozen=True)
class ScheduleOverview:
    entries: list[ScheduleEntry]   # alle Twitch-Kanäle mit Stream-Alert auf dem Server
    limit: int
    premium_limit: int
    days: int
    premium_days: int
    premium: bool
    can_create_events: bool
    available: bool                # False: nicht freigegeben (RELEASED), auf diesem Server nicht nutzbar


def effective_image_source(entry: ScheduleEntry, premium: bool) -> str:
    """Bildquelle, die der Abgleich wirklich nutzt: eigenes Bild nur mit Premium und gespeichertem Bild, sonst
    das Spiel-Banner. Für Übersichten (Discord-Panel, Dashboard)."""
    if entry.image_source == IMAGE_CUSTOM and not (premium and entry.custom_image_hash):
        return IMAGE_BANNER
    return entry.image_source


async def _twitch_subscriptions(guild_id: int) -> list[dict]:
    subs = await db_stream_alerts.get_subscriptions_for_guild(guild_id)
    return sorted((s for s in subs if s["platform"] == PLATFORM), key=lambda s: s["created_at"])


async def get_overview(guild: discord.Guild, *, now: datetime | None = None) -> ScheduleOverview:
    now = now or datetime.now(timezone.utc)
    premium = await is_premium(guild.id)
    limit = await db_limits.get_limit(guild.id, LIMIT_STREAMERS, premium)
    syncs = {row["monitored_channel_id"]: row for row in await db_stream_schedule.get_syncs_for_guild(guild.id)}
    subs = await _twitch_subscriptions(guild.id)
    events = await db_stream_schedule.count_upcoming_events(guild.id, now)
    custom = await db_stream_schedule.get_custom_meta(guild.id)

    enabled_ids = [mc for mc in syncs if any(s["monitored_channel_id"] == mc for s in subs)]   # nach created_at
    active_ids = set(enabled_ids[:limit])
    entries = [
        ScheduleEntry(
            monitored_channel_id=s["monitored_channel_id"], streamer_name=s["streamer_name"],
            enabled=s["monitored_channel_id"] in syncs, active=s["monitored_channel_id"] in active_ids,
            error=syncs[s["monitored_channel_id"]]["last_error"] if s["monitored_channel_id"] in syncs else None,
            events=events.get(s["monitored_channel_id"], 0),
            image_source=syncs[s["monitored_channel_id"]]["image_source"] if s["monitored_channel_id"] in syncs else IMAGE_BANNER,
            custom_text=custom.get(s["monitored_channel_id"], {}).get("description"),
            custom_image_hash=custom.get(s["monitored_channel_id"], {}).get("image_hash"),
        )
        for s in subs
    ]
    return ScheduleOverview(
        entries=entries, limit=limit, premium_limit=await db_limits.get_limit(guild.id, LIMIT_STREAMERS, True),
        days=await db_limits.get_limit(guild.id, LIMIT_DAYS, premium),
        premium_days=await db_limits.get_limit(guild.id, LIMIT_DAYS, True),
        premium=premium, can_create_events=guild.me.guild_permissions.create_events,
        available=is_available(guild.id),
    )


async def set_enabled(bot, guild_id: int, monitored_channel_id: int, enabled: bool) -> None:
    """Schalter umlegen und sofort abgleichen (Events anlegen bzw. löschen). Wirft ScheduleError."""
    async with schedule_lock(guild_id):
        if enabled:
            if not is_available(guild_id):
                raise ScheduleError("unavailable")
            subs = await _twitch_subscriptions(guild_id)
            if not any(s["monitored_channel_id"] == monitored_channel_id for s in subs):
                raise ScheduleError("not_monitored")
            sub_ids = {s["monitored_channel_id"] for s in subs}
            syncs = [r for r in await db_stream_schedule.get_syncs_for_guild(guild_id) if r["monitored_channel_id"] in sub_ids]
            if all(r["monitored_channel_id"] != monitored_channel_id for r in syncs):
                premium = await is_premium(guild_id)
                limit = await db_limits.get_limit(guild_id, LIMIT_STREAMERS, premium)
                if len(syncs) >= limit:
                    raise ScheduleError("limit", limit=limit,
                                        max=await db_limits.get_limit(guild_id, LIMIT_STREAMERS, True))
                await db_stream_schedule.enable(guild_id, monitored_channel_id)
        else:
            await db_stream_schedule.disable(guild_id, monitored_channel_id)
    await sync_guild(bot, guild_id)


async def set_image_source(bot, guild_id: int, monitored_channel_id: int, source: str) -> None:
    """Bildquelle eines eingeschalteten Kanals wählen und sofort abgleichen (kommende Events bekommen das neue Bild).
    Wirft ScheduleError("not_enabled"), wenn der Kanal nicht eingeschaltet ist."""
    if source not in IMAGE_SOURCES:
        raise ValueError(f"Unbekannte Bildquelle: {source!r}")
    if source == IMAGE_CUSTOM:
        if not await is_premium(guild_id):
            raise ScheduleError("premium")
        if not (await db_stream_schedule.get_custom_meta(guild_id)).get(monitored_channel_id, {}).get("image_hash"):
            raise ScheduleError("no_custom_image")
    async with schedule_lock(guild_id):
        if not await db_stream_schedule.set_image_source(guild_id, monitored_channel_id, source):
            raise ScheduleError("not_enabled")
    await sync_guild(bot, guild_id)


async def _require_enabled(guild_id: int, monitored_channel_id: int) -> None:
    if all(r["monitored_channel_id"] != monitored_channel_id for r in await db_stream_schedule.get_syncs_for_guild(guild_id)):
        raise ScheduleError("not_enabled")


async def set_custom_text(bot, guild_id: int, monitored_channel_id: int, text: str | None, *, sync: bool = True) -> None:
    """Eigene Beschreibung (Premium) mit Platzhaltern; leer = Standardtext. Gleicht sofort ab (sync=False: der
    Aufrufer gleicht selbst ab, z. B. einmal nach Text und Bild).
    Wirft ScheduleError: "premium", "not_enabled", "text_too_long" (max=)."""
    text = (text or "").strip() or None
    if text is not None:
        if not await is_premium(guild_id):
            raise ScheduleError("premium")
        if len(text) > MAX_TEXT_LENGTH:
            raise ScheduleError("text_too_long", max=MAX_TEXT_LENGTH)
    async with schedule_lock(guild_id):
        await _require_enabled(guild_id, monitored_channel_id)
        await db_stream_schedule.set_custom_description(guild_id, monitored_channel_id, text)
    if sync:
        await sync_guild(bot, guild_id)


async def set_custom_image(bot, guild_id: int, monitored_channel_id: int, data: bytes | None, *, sync: bool = True) -> None:
    """Eigenes Bild (Premium): geprüft, auf 800×320 zugeschnitten, als JPEG gespeichert und gleich als Bildquelle
    gewählt. None entfernt es (die Bildquelle fällt dann auf das Spiel-Banner zurück). Gleicht sofort ab (sync wie
    bei set_custom_text).
    Wirft ScheduleError: "premium", "not_enabled", "image_rejected" (problem= aus schedule_images.ImageRejected)."""
    image = image_hash = None
    if data is not None:
        if not await is_premium(guild_id):
            raise ScheduleError("premium")
        await _require_enabled(guild_id, monitored_channel_id)   # vor dem Rechnen: nichts umsonst verarbeiten
        try:
            image, image_hash = await schedule_images.process_upload(data)
        except schedule_images.ImageRejected as e:
            raise ScheduleError("image_rejected", problem=e.reason) from None
    async with schedule_lock(guild_id):
        await _require_enabled(guild_id, monitored_channel_id)
        await db_stream_schedule.set_custom_image(guild_id, monitored_channel_id, image, image_hash)
        source = next(r["image_source"] for r in await db_stream_schedule.get_syncs_for_guild(guild_id)
                      if r["monitored_channel_id"] == monitored_channel_id)
        if image is not None:
            await db_stream_schedule.set_image_source(guild_id, monitored_channel_id, IMAGE_CUSTOM)
        elif source == IMAGE_CUSTOM:
            await db_stream_schedule.set_image_source(guild_id, monitored_channel_id, IMAGE_BANNER)
    if sync:
        await sync_guild(bot, guild_id)
