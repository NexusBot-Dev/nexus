import logging
from zoneinfo import ZoneInfo
from database import db_settings

log = logging.getLogger(__name__)

ALLOWED_LOCALES = {"de", "en"}
CHANNEL_FIELDS = (
    "log_channel_id",
    "rules_channel_id",
    "welcome_channel_id",
    "boost_channel_id",
    "announcement_channel_id",
    "goodbye_channel_id",
    "ticket_log_channel_id",
)


def _parse_channel_id(guild, value) -> int | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("Ungültige Channel-ID")
    channel_id = int(value)
    if guild.get_channel(channel_id) is None:
        raise ValueError(f"Channel {channel_id} existiert nicht (mehr) auf diesem Server")
    return channel_id


async def update_settings(bot, guild_id: int, fields: dict) -> dict:
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    updates: dict = {}
    warnings: list[str] = []

    if "locale" in fields:
        locale = fields["locale"]
        if locale not in ALLOWED_LOCALES:
            raise ValueError("Ungültige Sprache")
        updates["locale"] = locale

    if "timezone" in fields:
        tz = fields["timezone"]
        try:
            ZoneInfo(tz)
        except Exception:
            raise ValueError("Ungültige Zeitzone")
        updates["timezone"] = tz

    for key in CHANNEL_FIELDS:
        if key in fields:
            updates[key] = _parse_channel_id(guild, fields[key])

    if "announcements_enabled" in fields:
        updates["announcements_enabled"] = bool(fields["announcements_enabled"])

    if "goodbye_enabled" in fields:
        updates["goodbye_enabled"] = bool(fields["goodbye_enabled"])

    if updates:
        await db_settings.update_guild(guild_id, **updates)

    if fields.get("color"):
        hex_color = str(fields["color"]).lstrip("#")
        try:
            color_int = int(hex_color, 16)
        except ValueError:
            raise ValueError("Ungültiges Farbformat — erwartet z.B. '#00F5D4'")

        color_set = await db_settings.set_color(guild_id, color_int)
        if not color_set:
            warnings.append("Farbe konnte nicht gesetzt werden — Premium erforderlich.")

    return {"updated_fields": list(updates.keys()), "warnings": warnings}
