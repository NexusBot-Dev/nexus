import logging
import re
from zoneinfo import ZoneInfo
from database import db_settings
from .registry import get_guild_or_raise

log = logging.getLogger(__name__)

ALLOWED_LOCALES = {"de", "en", "es", "pt"}
CHANNEL_FIELDS = (
    "log_channel_id",
    "rules_channel_id",
    "welcome_channel_id",
    "boost_channel_id",
    "announcement_channel_id",
    "goodbye_channel_id",
    "ticket_log_channel_id",
)

BOOL_FIELDS = ("announcements_enabled", "goodbye_enabled")

ENABLE_NEEDS_CHANNEL = (
    ("goodbye_enabled",       "goodbye_channel_id",      "Bitte zuerst einen Goodbye-Channel wählen"),
    ("announcements_enabled", "announcement_channel_id", "Bitte zuerst einen Announcement-Channel wählen"),
)

HEX_COLOR_RE = re.compile(r"#?([0-9a-fA-F]{6})")

def _parse_channel_id(guild, value) -> int | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("Ungültige Channel-ID")
    channel_id = int(value)
    if guild.get_channel(channel_id) is None:
        raise ValueError(f"Channel {channel_id} existiert nicht (mehr) auf diesem Server")
    return channel_id

def _parse_color(value) -> int:
    """'#00f5d4' / '00F5D4' -> int. Wirft ValueError bei allem anderen (z.B. '#fff', '0x123456', 'zzz')."""
    match = HEX_COLOR_RE.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("Ungültiges Farbformat — erwartet z.B. '#00F5D4'")
    color = int(match.group(1), 16)
    if color == 0:
        raise ValueError("Schwarz (#000000) zeigt Discord als „keine Farbe“ — bitte eine andere Farbe wählen")
    return color

async def update_settings(guild_id: int, fields: dict) -> dict:
    guild = get_guild_or_raise(guild_id)

    updates: dict = {}
    warnings: list[str] = []

    # Farbe vor allen Schreibzugriffen prüfen — sonst wären die übrigen Felder bei
    # einer ungültigen Farbe schon gespeichert, obwohl der Request mit 400 endet
    color_int: int | None = _parse_color(fields["color"]) if fields.get("color") else None

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

    for key in BOOL_FIELDS:
        if key in fields:
            # Strikt: bool("false") wäre True und würde die Funktion einschalten
            if not isinstance(fields[key], bool):
                raise ValueError(f"Ungültiger Wert für {key} (true/false erwartet)")
            updates[key] = fields[key]

    if any(flag in updates or channel in updates for flag, channel, _ in ENABLE_NEEDS_CHANNEL):
        stored = await db_settings.get_guild(guild_id) or {}
        for flag, channel, message in ENABLE_NEEDS_CHANNEL:
            effective = updates[channel] if channel in updates else stored.get(channel)
            if updates.get(flag) and not effective:
                raise ValueError(message)
            if channel in updates and updates[channel] is None and flag not in updates:
                updates[flag] = False

    if updates:
        await db_settings.update_guild(guild_id, **updates)

    if color_int is not None:
        color_set = await db_settings.set_color(guild_id, color_int)
        if not color_set:
            warnings.append("Farbe konnte nicht gesetzt werden — Premium erforderlich.")

    return {"updated_fields": list(updates.keys()), "warnings": warnings}