from .registry import register_action
from database import db_welcome, db_limits
from systems.premium import is_premium

@register_action("welcome", "list")
async def list_messages(guild_id: int, payload: dict) -> dict:
    messages = await db_welcome.get_welcome_messages(guild_id)
    return {
        "messages": [{"id": m["id"], "message": m["message"]} for m in messages],
        "limit_free": await db_limits.get_limit(guild_id, "welcome_messages", False),
        "limit_premium": await db_limits.get_limit(guild_id, "welcome_messages", True),
    }

@register_action("welcome", "add-message")
async def add_message(guild_id: int, payload: dict) -> dict:
    text = (payload.get("message") or "").strip()
    if not text:
        raise ValueError("Nachricht darf nicht leer sein")
    if len(text) > 1000:
        raise ValueError("Nachricht ist zu lang (max. 1000 Zeichen)")

    existing = await db_welcome.get_welcome_messages(guild_id)
    limit = await db_limits.get_limit(guild_id, "welcome_messages", await is_premium(guild_id))
    if len(existing) >= limit:
        raise ValueError(f"Limit erreicht ({limit} Nachrichten). Lösche zuerst eine bestehende Nachricht.")

    message_id = await db_welcome.add_welcome_message(guild_id, text)
    return {"id": message_id, "message": text}

@register_action("welcome", "edit-message")
async def edit_message(guild_id: int, payload: dict) -> dict:
    message_id = payload.get("message_id")
    text = (payload.get("message") or "").strip()
    if not message_id or not text:
        raise ValueError("message_id und message werden benötigt")
    if len(text) > 1000:
        raise ValueError("Nachricht ist zu lang (max. 1000 Zeichen)")

    updated = await db_welcome.update_welcome_message(int(message_id), guild_id, text)
    if not updated:
        raise ValueError("Nachricht wurde nicht gefunden")
    return {"id": message_id, "message": text}

@register_action("welcome", "delete-message")
async def delete_message(guild_id: int, payload: dict) -> dict:
    message_id = payload.get("message_id")
    if not message_id:
        raise ValueError("message_id wird benötigt")

    deleted = await db_welcome.delete_welcome_message(int(message_id), guild_id)
    if not deleted:
        raise ValueError("Nachricht wurde nicht gefunden")
    return {"id": message_id}