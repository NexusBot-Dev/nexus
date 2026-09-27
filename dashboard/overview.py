import asyncio
from datetime import timezone

from .registry import register_action, get_bot
from .guild_info import get_guild_info
from .moderation import ensure_members_cached
from database import db_tickets, db_ticket_categories, db_stream_alerts, db_levels

RECENT_TICKETS = 3
TOP_LEVELS = 3


def _iso_utc(value) -> str | None:
    """DB-Zeitstempel kommen ohne Zeitzone zurück — als UTC markieren, damit der
    Browser sie korrekt in die lokale Zeit umrechnet."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@register_action("guild", "overview")
async def get_guild_overview(guild_id: int, payload: dict) -> dict:
    """Alles für die Übersichtsseite in einem Aufruf: die normalen Guild-Infos
    (Name, Icon, Mitgliederzahl, Online-Status, ...), die Kennzahlen für die
    Kacheln, die neuesten offenen Tickets und die Top-Level."""
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    info = await get_guild_info(guild_id, payload)  # nur Cache-Zugriffe, parallelisieren bringt nichts

    async with asyncio.TaskGroup() as tg:
        tickets_task = tg.create_task(db_tickets.get_open_tickets_for_guild(guild_id))
        categories_task = tg.create_task(db_ticket_categories.get_categories(guild_id))
        subs_task = tg.create_task(db_stream_alerts.get_subscriptions_for_guild(guild_id))
        levels_task = tg.create_task(db_levels.get_leaderboard(guild_id, limit=TOP_LEVELS))
        # Für Timeouts und Namen muss der Bot jedes Mitglied kennen, nicht nur die gecachten
        tg.create_task(ensure_members_cached(guild))

    open_tickets = tickets_task.result()
    categories = {c["id"]: c["name"] for c in categories_task.result()}
    subs = subs_task.result()

    live_streamers = sorted({s["streamer_name"] for s in subs if s["live"]})
    active_timeouts = sum(1 for m in guild.members if m.is_timed_out())

    # Neueste offene Tickets (höchste Ticketnummer zuerst)
    recent_tickets = []
    for t in sorted(open_tickets, key=lambda t: t["ticket_number"], reverse=True)[:RECENT_TICKETS]:
        member = guild.get_member(t["user_id"])
        recent_tickets.append({
            "ticket_number": t["ticket_number"],
            "status": t["status"],
            "category_name": categories.get(t["category_id"]),
            "user_name": member.display_name if member else None,
            "created_at": _iso_utc(t.get("created_at")),
        })

    top_levels = []
    for row in levels_task.result():
        member = guild.get_member(row["user_id"])
        top_levels.append({
            "user_id": str(row["user_id"]),
            "username": member.display_name if member else None,
            "avatar_url": member.display_avatar.url if member else None,
            "level": row["level"],
            "xp": row["xp"],
        })

    return {
        **info,
        "stats": {
            "open_tickets": len(open_tickets),
            "live_streamers": live_streamers,
            "active_timeouts": active_timeouts,
        },
        "recent_tickets": recent_tickets,
        "top_levels": top_levels,
    }