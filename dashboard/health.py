import re

import discord

from database import db_settings
from cogs.utils import get_lang
from translations import TRANSLATIONS
from dashboard.registry import register_action, get_bot
from systems.health_checks import get_guild_issues

_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")


def _plain_mentions(guild: discord.Guild, text: str) -> str:
    """Discord-Mentions (<@&id>, <#id>) sind im Browser nur Rohtext —
    fürs Dashboard in lesbare Namen umwandeln."""
    def role(m):
        r = guild.get_role(int(m[1]))
        return f"@{r.name}" if r else "@deleted-role"

    def channel(m):
        c = guild.get_channel(int(m[1]))
        return f"#{c.name}" if c else "#deleted-channel"

    return _CHANNEL_MENTION.sub(channel, _ROLE_MENTION.sub(role, text))


@register_action("guild", "health-issues")
async def get_health_issues(guild_id: int, payload: dict) -> dict:
    """Dieselben strukturierten Probleme wie /diagnose — als JSON fürs
    Notifications-Dropdown im Dashboard statt als Discord-Embed."""
    bot = get_bot()
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")

    # Dashboard-Sprache hat Vorrang vor der Bot-Sprache des Servers
    lang_code = payload.get("lang")
    lang = TRANSLATIONS[lang_code] if lang_code in TRANSLATIONS else await get_lang(guild_id)

    settings = await db_settings.get_guild(guild_id) or {}
    issues = await get_guild_issues(guild, settings, lang)

    return {
        "issues": [
            {"section": i.section, "severity": i.severity, "text": _plain_mentions(guild, i.text)}
            for i in issues
        ],
    }