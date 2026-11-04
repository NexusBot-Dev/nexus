import asyncio
from .registry import register_action, parse_id, DashCtx, DashboardError
from database import db_welcome, db_limits
from systems.premium import is_premium

@register_action("welcome", "list")
async def list_messages(ctx: DashCtx) -> dict:
    guild_id = ctx.guild_id
    messages, limit_free, limit_premium = await asyncio.gather(
        db_welcome.get_welcome_messages(guild_id),
        db_limits.get_limit(guild_id, "welcome_messages", False),
        db_limits.get_limit(guild_id, "welcome_messages", True),
    )
    return {
        "messages": [{"id": m["id"], "message": m["message"]} for m in messages],
        "limit_free": limit_free,
        "limit_premium": limit_premium,
    }

@register_action("welcome", "add-message")
async def add_message(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    text = (payload.get("message") or "").strip()
    if not text:
        raise DashboardError("dash_err_msg_empty")
    if len(text) > 1000:
        raise DashboardError("dash_err_msg_too_long")

    existing = await db_welcome.get_welcome_messages(guild_id)
    limit = await db_limits.get_limit(guild_id, "welcome_messages", await is_premium(guild_id))
    if len(existing) >= limit:
        raise DashboardError("dash_err_msg_limit", limit=limit)

    message_id = await db_welcome.add_welcome_message(guild_id, text)
    return {"id": message_id, "message": text}

@register_action("welcome", "edit-message")
async def edit_message(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    message_id = parse_id(payload.get("message_id"), "message_id wird benötigt")
    text = (payload.get("message") or "").strip()
    if not text:
        raise DashboardError("dash_err_msg_empty")
    if len(text) > 1000:
        raise DashboardError("dash_err_msg_too_long")

    updated = await db_welcome.update_welcome_message(message_id, guild_id, text)
    if not updated:
        raise DashboardError("dash_err_message_not_found")
    return {"id": message_id, "message": text}

@register_action("welcome", "delete-message")
async def delete_message(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    message_id = parse_id(payload.get("message_id"), "message_id wird benötigt")

    deleted = await db_welcome.delete_welcome_message(message_id, guild_id)
    if not deleted:
        raise DashboardError("dash_err_message_not_found")
    return {"id": message_id}
