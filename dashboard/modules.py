from .registry import register_action, DashCtx, DashboardError, InvalidRequest
from database import db_settings

ALLOWED_MODULES = set(db_settings.DEFAULT_MODULES.keys())

@register_action("modules", "toggle")
async def toggle_modules(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise InvalidRequest("Keine Änderungen übergeben")

    for key, enabled in changes.items():
        if key not in ALLOWED_MODULES:
            raise InvalidRequest(f"Unbekanntes Modul: {key}")
        # Strikt: bool("false") wäre True und würde das Modul einschalten
        if not isinstance(enabled, bool):
            raise InvalidRequest(f"Ungültiger Wert für {key} (true/false erwartet)")

    if changes.get("moderation") is False:
        raise DashboardError("dash_err_modules_core_moderation")

    for key, enabled in changes.items():
        await db_settings.set_module(guild_id, key, enabled)

    if changes.get("automod") is False:
        settings = await db_settings.get_guild(guild_id)
        if settings and settings.get("honeypot_channel_id"):
            from systems.honeypot import cleanup as cleanup_honeypot
            await cleanup_honeypot(ctx.guild, settings["honeypot_channel_id"])

    row = await db_settings.get_guild(guild_id)
    modules = {**db_settings.DEFAULT_MODULES, **row["modules"]}
    return {"modules": modules}
