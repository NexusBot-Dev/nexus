import asyncio
import discord
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from cogs.giveaways import MAX_WINNERS, post_giveaway
from cogs.utils import get_lang
from database import db_giveaways, db_settings
from .registry import register_action, get_bot, parse_id, DashCtx

MAX_PRIZE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
HISTORY_LIMIT = 20

def _iso_utc(dt: datetime) -> str:
    """Naive UTC-Datetime aus der DB -> ISO-String mit 'Z'."""
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

def _serialize(guild: discord.Guild, g: dict) -> dict:
    channel = guild.get_channel(g["channel_id"])
    return {
        "id": g["id"],
        "prize": g["prize"],
        "status": g["status"],
        "winners_count": g["winners_count"],
        "participant_count": g["participant_count"],
        "end_time": _iso_utc(g["end_time"]),
        "channel_id": str(g["channel_id"]),
        "channel_name": channel.name if channel else None,
        "message_url": f"https://discord.com/channels/{g['guild_id']}/{g['channel_id']}/{g['message_id']}",
    }

@register_action("giveaways", "list")
async def list_giveaways(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    active, history = await asyncio.gather(
        db_giveaways.get_giveaways_overview(guild_id, running=True),
        db_giveaways.get_giveaways_overview(guild_id, running=False, limit=HISTORY_LIMIT),
    )
    return {
        "active": [_serialize(guild, g) for g in active],
        "history": [_serialize(guild, g) for g in history],
        "limits": {
            "max_prize": MAX_PRIZE_LENGTH,
            "max_description": MAX_DESCRIPTION_LENGTH,
            "max_winners": MAX_WINNERS,
        },
    }

@register_action("giveaways", "create")
async def create_giveaway(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload

    prize = str(payload.get("prize") or "").strip()
    if not prize or len(prize) > MAX_PRIZE_LENGTH:
        raise ValueError(f"Preis muss 1–{MAX_PRIZE_LENGTH} Zeichen lang sein")

    description = str(payload.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Beschreibung darf max. {MAX_DESCRIPTION_LENGTH} Zeichen lang sein")

    # Anzahl, keine ID: Zahl oder Ziffern-String, aber kein Float/bool (int(2.7) wäre still 2)
    winners_count = payload.get("winners_count")
    if isinstance(winners_count, str) and winners_count.isascii() and winners_count.isdigit():
        winners_count = int(winners_count)
    if not isinstance(winners_count, int) or isinstance(winners_count, bool) or not 1 <= winners_count <= MAX_WINNERS:
        raise ValueError(f"Gewinneranzahl muss zwischen 1 und {MAX_WINNERS} liegen")

    # Channel immer über die Guild auflösen -> verhindert fremde Channel-IDs
    channel = guild.get_channel(parse_id(payload.get("channel_id"), "Ungültiger Channel"))
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Channel nicht gefunden oder kein Text-Channel")
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise ValueError("Dem Bot fehlen Rechte in diesem Channel (Senden / Links einbetten)")

    # datetime-local ("2026-10-05T18:00") in der Guild-Timezone interpretieren, wie im Modal
    tz = ZoneInfo(await db_settings.get_timezone(guild_id) or "Europe/Berlin")
    try:
        end_local = datetime.strptime(str(payload.get("end_time")), "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    except ValueError:
        raise ValueError("Ungültiges Datumsformat") from None
    if end_local <= datetime.now(tz):
        raise ValueError("Die Endzeit liegt in der Vergangenheit")
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)

    # Host ist der eingeloggte Dashboard-User (von der Auth-Middleware geprüft)
    host_id = ctx.actor.id
    lang = await get_lang(guild_id)

    try:
        giveaway_id = await post_giveaway(
            guild_id=guild_id,
            host_id=host_id,
            channel=channel,
            prize=prize,
            description=description or None,
            winners_count=winners_count,
            end_utc=end_utc,
            lang=lang,
        )
    except discord.Forbidden:
        raise ValueError("Dem Bot fehlen Rechte in diesem Channel") from None
    return {"id": giveaway_id}

@register_action("giveaways", "cancel")
async def cancel_giveaway(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    giveaway_id = parse_id(payload.get("id"), "Ungültige Giveaway-ID")

    # Über guild_id mitprüfen, dass das Giveaway wirklich zu diesem Server gehört
    giveaway = await db_giveaways.get_giveaway_by_id(guild_id, giveaway_id)
    if giveaway is None or giveaway["status"] != "running":
        raise ValueError("Giveaway nicht gefunden oder bereits beendet")

    cog = get_bot().get_cog("Giveaways")
    if cog is None:
        raise ValueError("Giveaway-Modul ist nicht geladen")

    if not await cog._finish_giveaway(giveaway, status="cancelled"):
        raise ValueError("Giveaway-Nachricht nicht erreichbar (fehlende Rechte?), bitte später erneut versuchen")
    return {"cancelled": True}
