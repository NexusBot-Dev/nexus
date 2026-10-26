from .registry import register_action, get_bot
from database import db_settings

ALLOWED_MODULES = set(db_settings.DEFAULT_MODULES.keys())

@register_action("modules", "toggle")
async def toggle_modules(guild_id: int, payload: dict) -> dict:
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Keine Änderungen übergeben")

    for key, enabled in changes.items():
        if key not in ALLOWED_MODULES:
            raise ValueError(f"Unbekanntes Modul: {key}")
        # Strikt: bool("false") wäre True und würde das Modul einschalten
        if not isinstance(enabled, bool):
            raise ValueError(f"Ungültiger Wert für {key} (true/false erwartet)")

    if changes.get("moderation") is False:
        raise ValueError("Moderation ist ein Kern-Modul und kann nicht deaktiviert werden")

    for key, enabled in changes.items():
        await db_settings.set_module(guild_id, key, enabled)

    if changes.get("automod") is False:
        bot = get_bot()
        guild = bot.get_guild(guild_id)
        if guild is not None:
            settings = await db_settings.get_guild(guild_id)
            if settings and settings.get("honeypot_channel_id"):
                from cogs.honeypot import cleanup_honeypot
                await cleanup_honeypot(guild, settings["honeypot_channel_id"])

    row = await db_settings.get_guild(guild_id)
    modules = {**db_settings.DEFAULT_MODULES, **row["modules"]}
    return {"modules": modules}