import discord
from .registry import register_action, get_bot
from database import db_stream_alerts, db_limits
from systems.premium import is_premium
from systems.platform_registry import PLATFORM_HANDLERS

def _guild_or_raise(guild_id: int) -> discord.Guild:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")
    return guild

@register_action("stream_alerts", "list")
async def list_alerts(guild_id: int, payload: dict) -> dict:
    guild = _guild_or_raise(guild_id)
    subs = await db_stream_alerts.get_subscriptions_for_guild(guild_id)
    premium = await is_premium(guild_id)

    grouped: dict[str, list] = {"twitch": [], "youtube": [], "kick": []}
    limits = {}
    for platform in grouped:
        feature_key = f"stream_alerts_{platform}"
        limit = await db_limits.get_limit(guild_id, feature_key, premium)
        limits[platform] = {
            "free": await db_limits.get_limit(guild_id, feature_key, False),
            "premium": await db_limits.get_limit(guild_id, feature_key, True),
        }
        entries = [s for s in subs if s["platform"] == platform]
        entries.sort(key=lambda s: s["created_at"])
        for i, s in enumerate(entries):
            channel = guild.get_channel(s["channel_id"])
            grouped[platform].append({
                "monitored_channel_id": s["monitored_channel_id"],
                "subscription_id": s["id"],
                "streamer_name": s["streamer_name"],
                "channel_id": str(s["channel_id"]),
                "channel_name": channel.name if channel else None,
                "message": s.get("message"),
                "live": bool(s["live"]),
                "active": i < limit,
            })

    ping_roles = {}
    for platform in ("all", "twitch", "youtube", "kick"):
        role_id = await db_stream_alerts.get_ping_role(guild_id, platform) if platform != "all" else None
        ping_roles[platform] = str(role_id) if role_id else None

    return {
        "subscriptions": grouped,
        "limits": limits,          # ersetzt limit_free/limit_premium — jetzt pro Plattform
        "premium_active": premium,
        "ping_roles": ping_roles,
    }

@register_action("stream_alerts", "add")
async def add_alert(guild_id: int, payload: dict) -> dict:
    platform = payload.get("platform")
    login_raw = (payload.get("username") or "").strip()
    channel_id = payload.get("channel_id")
    message = (payload.get("message") or "").strip() or None

    handler = PLATFORM_HANDLERS.get(platform)
    if handler is None:
        raise ValueError("Unbekannte Plattform")
    if not login_raw or not channel_id or not str(channel_id).isdigit():
        raise ValueError("Username und Channel werden benötigt")
    if message and len(message) > 200:
        raise ValueError("Nachricht zu lang (max. 200 Zeichen)")

    guild = _guild_or_raise(guild_id)
    if guild.get_channel(int(channel_id)) is None:
        raise ValueError("Channel existiert nicht (mehr) auf diesem Server")

    login = handler.normalize_input(login_raw)
    if login is None:
        raise ValueError("Ungültiger Username/Link")

    current_count = await db_stream_alerts.count_subscriptions_for_guild(guild_id, platform)
    premium = await is_premium(guild_id)
    limit = await db_limits.get_limit(guild_id, f"stream_alerts_{platform}", premium)
    if current_count >= limit:
        raise ValueError(f"Limit erreicht ({limit} Kanäle für {platform})")

    user = await handler.resolve_user(login)
    if user is None:
        raise ValueError(f"User '{login}' wurde nicht gefunden")

    external_id = user["id"]
    monitored = await db_stream_alerts.get_monitored_channel(platform, external_id)

    if monitored is None:
        sub_ids = {}
        if handler.create_subscriptions:
            try:
                sub_ids = await handler.create_subscriptions(external_id)
            except Exception:
                raise ValueError("Abo konnte nicht eingerichtet werden — bitte später erneut versuchen")

        monitored = await db_stream_alerts.get_or_create_monitored_channel(platform, external_id, user["display_name"])
        if sub_ids:
            await db_stream_alerts.set_eventsub_ids(monitored["id"], sub_ids.get("online_id"), sub_ids.get("offline_id"))
        if handler.on_channel_created:
            try:
                await handler.on_channel_created(monitored["id"], user)
            except Exception:
                pass
    else:
        existing = await db_stream_alerts.get_subscription(guild_id, monitored["id"])
        if existing is not None:
            raise ValueError(f"'{login}' wird bereits überwacht")

    await db_stream_alerts.add_subscription(
        guild_id=guild_id, channel_id=int(channel_id), monitored_channel_id=monitored["id"], message=message,
    )

    return {
        "monitored_channel_id": monitored["id"],
        "streamer_name": user["display_name"],
        "platform": platform,
    }

@register_action("stream_alerts", "edit")
async def edit_alert(guild_id: int, payload: dict) -> dict:
    monitored_channel_id = payload.get("monitored_channel_id")
    channel_id = payload.get("channel_id")
    message = payload.get("message", ...)

    if not monitored_channel_id:
        raise ValueError("monitored_channel_id wird benötigt")

    channel_id_int = None
    if channel_id and str(channel_id).isdigit():
        guild = _guild_or_raise(guild_id)
        if guild.get_channel(int(channel_id)) is None:
            raise ValueError("Channel existiert nicht (mehr) auf diesem Server")
        channel_id_int = int(channel_id)

    if message is not ... and message:
        message = str(message).strip()[:200] or None

    updated = await db_stream_alerts.update_subscription(
        guild_id=guild_id, monitored_channel_id=int(monitored_channel_id),
        channel_id=channel_id_int, message=message,
    )
    if not updated:
        raise ValueError("Nichts zu aktualisieren oder Eintrag nicht gefunden")
    return {"monitored_channel_id": monitored_channel_id}

@register_action("stream_alerts", "remove")
async def remove_alert(guild_id: int, payload: dict) -> dict:
    monitored_channel_id = payload.get("monitored_channel_id")
    if not monitored_channel_id:
        raise ValueError("monitored_channel_id wird benötigt")

    monitored = await db_stream_alerts.get_monitored_channel_by_id(int(monitored_channel_id))
    if monitored is None:
        raise ValueError("Kanal wurde nicht gefunden")

    removed = await db_stream_alerts.remove_subscription(guild_id, int(monitored_channel_id))
    if not removed:
        raise ValueError("Abo wurde nicht gefunden")

    still_referenced = await db_stream_alerts.is_channel_still_referenced(int(monitored_channel_id))
    if not still_referenced:
        handler = PLATFORM_HANDLERS.get(monitored["platform"])
        if handler and handler.delete_subscriptions:
            try:
                await handler.delete_subscriptions(monitored)
            except Exception:
                pass
        await db_stream_alerts.delete_monitored_channel(int(monitored_channel_id))

    return {"monitored_channel_id": monitored_channel_id}

@register_action("stream_alerts", "set-ping-role")
async def set_ping_role(guild_id: int, payload: dict) -> dict:
    platform = payload.get("platform", "all")
    role_id = payload.get("role_id")

    if platform != "all":
        premium = await is_premium(guild_id)
        if not premium:
            raise ValueError("Plattform-spezifische Rollen erfordern Premium")

    if role_id:
        if not str(role_id).isdigit():
            raise ValueError("Ungültige role_id")
        guild = _guild_or_raise(guild_id)
        role = guild.get_role(int(role_id))
        if role is None:
            raise ValueError("Rolle existiert nicht auf diesem Server")
        await db_stream_alerts.set_ping_role(guild_id, platform, int(role_id))
    else:
        await db_stream_alerts.remove_ping_role(guild_id, platform)

    return {"platform": platform, "role_id": role_id}

@register_action("stream_alerts", "list-links")
async def list_links(guild_id: int, payload: dict) -> dict:
    rows = await db_stream_alerts.get_links_for_guild(guild_id)
    return {
        "links": [
            {
                "id": r["id"],
                "a": {"subscription_id": r["sub_a_id"], "platform": r["platform_a"], "streamer_name": r["name_a"]},
                "b": {"subscription_id": r["sub_b_id"], "platform": r["platform_b"], "streamer_name": r["name_b"]},
            }
            for r in rows
        ]
    }

@register_action("stream_alerts", "create-link")
async def create_link(guild_id: int, payload: dict) -> dict:
    sub_a_id = payload.get("subscription_a_id")
    sub_b_id = payload.get("subscription_b_id")

    if not sub_a_id or not sub_b_id:
        raise ValueError("Beide Kanäle werden benötigt")
    if int(sub_a_id) == int(sub_b_id):
        raise ValueError("Ein Kanal kann nicht mit sich selbst verknüpft werden")

    subs = await db_stream_alerts.get_subscriptions_for_guild(guild_id)
    sub_ids = {s["id"] for s in subs}
    if int(sub_a_id) not in sub_ids or int(sub_b_id) not in sub_ids:
        raise ValueError("Kanal gehört nicht zu diesem Server")

    try:
        link_id = await db_stream_alerts.create_link(guild_id, int(sub_a_id), int(sub_b_id))
    except Exception:
        raise ValueError("Diese Verknüpfung existiert bereits")

    return {"id": link_id}

@register_action("stream_alerts", "delete-link")
async def delete_link(guild_id: int, payload: dict) -> dict:
    link_id = payload.get("link_id")
    if not link_id:
        raise ValueError("link_id wird benötigt")
    deleted = await db_stream_alerts.delete_link(int(link_id), guild_id)
    if not deleted:
        raise ValueError("Verknüpfung wurde nicht gefunden")
    return {"id": link_id}