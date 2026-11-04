import asyncio
from .registry import register_action, parse_id, DashCtx, DashboardError
from database import db_boosts, db_limits
from systems.premium import is_premium

@register_action("boosts", "list")
async def list_messages(ctx: DashCtx) -> dict:
    guild_id = ctx.guild_id
    messages, limit_free, limit_premium = await asyncio.gather(
        db_boosts.get_boost_messages(guild_id),
        db_limits.get_limit(guild_id, "boost_messages", False),
        db_limits.get_limit(guild_id, "boost_messages", True),
    )
    return {
        "messages": [{"id": m["id"], "message": m["message"]} for m in messages],
        "limit_free": limit_free,
        "limit_premium": limit_premium,
    }

@register_action("boosts", "add-message")
async def add_message(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    text = (payload.get("message") or "").strip()
    if not text:
        raise DashboardError("dash_err_msg_empty")
    if len(text) > 1000:
        raise DashboardError("dash_err_msg_too_long")

    current_count = await db_boosts.count_boost_messages(guild_id)
    limit = await db_limits.get_limit(guild_id, "boost_messages", await is_premium(guild_id))
    if current_count >= limit:
        raise DashboardError("dash_err_msg_limit", limit=limit)

    message_id = await db_boosts.add_boost_message(guild_id, text)
    return {"id": message_id, "message": text}

@register_action("boosts", "edit-message")
async def edit_message(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    message_id = parse_id(payload.get("message_id"), "message_id wird benötigt")
    text = (payload.get("message") or "").strip()
    if not text:
        raise DashboardError("dash_err_msg_empty")
    if len(text) > 1000:
        raise DashboardError("dash_err_msg_too_long")

    updated = await db_boosts.update_boost_message(message_id, guild_id, text)
    if not updated:
        raise DashboardError("dash_err_message_not_found")
    return {"id": message_id, "message": text}

@register_action("boosts", "delete-message")
async def delete_message(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    message_id = parse_id(payload.get("message_id"), "message_id wird benötigt")

    deleted = await db_boosts.delete_boost_message(message_id, guild_id)
    if not deleted:
        raise DashboardError("dash_err_message_not_found")
    return {"id": message_id}
