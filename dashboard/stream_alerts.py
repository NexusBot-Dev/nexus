import asyncio
import aiomysql
from .registry import register_action, parse_id, DashCtx, DashboardError, InvalidRequest
from database import db_stream_alerts, db_limits
from systems.premium import is_premium
from systems.platform_registry import PLATFORM_HANDLERS
from systems.alert_dispatch import ALERT_CHANNEL_PERMS
from systems.hierarchy import missing_perms, perm_label
from systems import stream_alert_service as alert_service

PLATFORMS = ("twitch", "youtube", "kick")
PING_ROLE_SCOPES = ("all", *PLATFORMS)
PLATFORM_NAMES = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}   # für Meldungen


# Abbruchgründe aus systems/stream_alert_service → Dashboard-Meldung
_ALERT_ERRORS = {
    "limit": "dash_err_sa_limit",
    "user_not_found": "dash_err_sa_user_not_found",
    "subscription_failed": "dash_err_sa_subscription_failed",
    "already_monitored": "dash_err_sa_already_monitored",
    "not_monitored": "dash_err_sa_not_found",
}


def _alert_error(e: "alert_service.AlertError", login: str = "", platform: str = "") -> DashboardError:
    return DashboardError(_ALERT_ERRORS[e.reason], name=login, platform=PLATFORM_NAMES.get(platform, platform), **e.params)


def channel_warning(ctx: DashCtx, channel) -> str | None:
    """Hinweis fürs Dashboard, wenn Nexus im Alert-Kanal nicht posten kann (Alert wird trotzdem gespeichert).
    Reiner Text ohne Markdown, weil das Dashboard ihn so anzeigt."""
    miss = missing_perms(channel, *ALERT_CHANNEL_PERMS)
    if not miss:
        return None
    return ctx.lang.get(
        "alert_channel_missing_perms",
        "⚠️ Heads up: I'm missing permissions in {channel} ({perms}). Until that's fixed, no alerts will show up there.",
    ).format(channel=f"#{channel.name}", perms=", ".join(perm_label(p) for p in miss))

@register_action("stream_alerts", "list")
async def list_alerts(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    # Alles parallel: vorher liefen hier bis zu 12 Abfragen nacheinander
    subs, premium, stored_ping_roles, *limit_values = await asyncio.gather(
        db_stream_alerts.get_subscriptions_for_guild(guild_id),
        is_premium(guild_id),
        db_stream_alerts.get_ping_roles(guild_id),
        *(
            db_limits.get_limit(guild_id, f"stream_alerts_{platform}", tier)
            for platform in PLATFORMS
            for tier in (False, True)
        ),
    )

    grouped: dict[str, list] = {platform: [] for platform in PLATFORMS}
    limits = {}
    for i, platform in enumerate(PLATFORMS):
        limits[platform] = {"free": limit_values[2 * i], "premium": limit_values[2 * i + 1]}
        limit = limits[platform]["premium" if premium else "free"]
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

    # Explizit gesetzte Rollen je Scope — "all" wurde vorher nie zurückgegeben,
    # und Plattformen zeigten fälschlich die "all"-Rolle als eigene an
    ping_roles = {
        scope: str(stored_ping_roles[scope]) if stored_ping_roles.get(scope) else None
        for scope in PING_ROLE_SCOPES
    }

    return {
        "subscriptions": grouped,
        "limits": limits,          # ersetzt limit_free/limit_premium — jetzt pro Plattform
        "premium_active": premium,
        "ping_roles": ping_roles,
    }

@register_action("stream_alerts", "add")
async def add_alert(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    platform = payload.get("platform")
    login_raw = (payload.get("username") or "").strip()
    channel_id = payload.get("channel_id")
    message = (payload.get("message") or "").strip() or None

    handler = PLATFORM_HANDLERS.get(platform)
    if handler is None:
        raise InvalidRequest(f"Unbekannte Plattform: {platform!r}")
    if not login_raw or not channel_id or not str(channel_id).isdigit():
        raise DashboardError("dash_err_sa_username_channel_required")
    if message and len(message) > 200:
        raise DashboardError("dash_err_sa_message_too_long")

    channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise DashboardError("dash_err_channel_gone")

    login = handler.normalize_input(login_raw)
    if login is None:
        raise DashboardError("dash_err_sa_invalid_username")

    try:
        added = await alert_service.add_alert(guild_id, platform, login, channel.id, message)
    except alert_service.AlertError as e:
        raise _alert_error(e, login, platform) from None

    return {
        "monitored_channel_id": added.monitored_channel_id,
        "subscription_id": added.subscription_id,   # damit das Dashboard den Kanal sofort verknüpfen kann
        "streamer_name": added.streamer_name,
        "platform": platform,
        "warning": channel_warning(ctx, channel),
    }

@register_action("stream_alerts", "edit")
async def edit_alert(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    monitored_channel_id = parse_id(payload.get("monitored_channel_id"), "monitored_channel_id wird benötigt")
    channel_id = payload.get("channel_id")
    message = payload.get("message", ...)

    channel_id_int = None
    channel = None
    if channel_id and str(channel_id).isdigit():
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            raise DashboardError("dash_err_channel_gone")
        channel_id_int = int(channel_id)

    if message is not ... and message:
        message = str(message).strip()[:200] or None

    updated = await db_stream_alerts.update_subscription(
        guild_id=guild_id, monitored_channel_id=monitored_channel_id,
        channel_id=channel_id_int, message=message,
    )
    if not updated:
        raise DashboardError("dash_err_sa_not_found")
    return {
        "monitored_channel_id": monitored_channel_id,
        "warning": channel_warning(ctx, channel) if channel else None,
    }

@register_action("stream_alerts", "remove")
async def remove_alert(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    monitored_channel_id = parse_id(payload.get("monitored_channel_id"), "monitored_channel_id wird benötigt")

    monitored = await db_stream_alerts.get_monitored_channel_by_id(monitored_channel_id)
    if monitored is None:
        raise DashboardError("dash_err_sa_not_found")

    try:
        await alert_service.remove_alert(guild_id, monitored)
    except alert_service.AlertError as e:
        raise _alert_error(e) from None

    return {"monitored_channel_id": monitored_channel_id}

@register_action("stream_alerts", "set-ping-role")
async def set_ping_role(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    platform = payload.get("platform", "all")
    role_id = payload.get("role_id")

    if platform not in PING_ROLE_SCOPES:
        raise InvalidRequest(f"Unbekannte Plattform: {platform!r}")
    if platform != "all":
        premium = await is_premium(guild_id)
        if not premium:
            raise DashboardError("dash_err_sa_platform_roles_premium")

    if role_id:
        if not str(role_id).isdigit():
            raise InvalidRequest(f"Ungültige role_id: {role_id!r}")
        role = guild.get_role(int(role_id))
        if role is None:
            raise DashboardError("dash_err_role_gone")
        await db_stream_alerts.set_ping_role(guild_id, platform, int(role_id))
    else:
        await db_stream_alerts.remove_ping_role(guild_id, platform)

    return {"platform": platform, "role_id": role_id}

@register_action("stream_alerts", "list-links")
async def list_links(ctx: DashCtx) -> dict:
    rows = await db_stream_alerts.get_links_for_guild(ctx.guild_id)
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
async def create_link(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    sub_a_id = payload.get("subscription_a_id")
    sub_b_id = payload.get("subscription_b_id")

    if not sub_a_id or not sub_b_id:
        raise DashboardError("dash_err_sa_link_both_required")
    sub_a_id = parse_id(sub_a_id, "Ungültige Kanal-ID")
    sub_b_id = parse_id(sub_b_id, "Ungültige Kanal-ID")
    if sub_a_id == sub_b_id:
        raise DashboardError("dash_err_sa_link_self")

    subs = await db_stream_alerts.get_subscriptions_for_guild(guild_id)
    sub_ids = {s["id"] for s in subs}
    if sub_a_id not in sub_ids or sub_b_id not in sub_ids:
        raise DashboardError("dash_err_sa_not_found")   # z. B. in einem anderen Tab entfernt

    try:
        link_id = await db_stream_alerts.create_link(guild_id, sub_a_id, sub_b_id)
    except aiomysql.IntegrityError:
        raise DashboardError("dash_err_sa_link_exists") from None

    return {"id": link_id}

@register_action("stream_alerts", "delete-link")
async def delete_link(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    link_id = parse_id(payload.get("link_id"), "link_id wird benötigt")
    deleted = await db_stream_alerts.delete_link(link_id, guild_id)
    if not deleted:
        raise DashboardError("dash_err_sa_link_not_found")
    return {"id": link_id}
