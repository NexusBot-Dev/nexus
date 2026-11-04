import re

import discord

from database import db_settings
from dashboard.registry import register_action, DashCtx
from systems.health_checks import get_guild_issues

_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")


def plain_mentions(guild: discord.Guild, text: str) -> str:
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
async def get_health_issues(ctx: DashCtx) -> dict:
    """Dieselben strukturierten Probleme wie /diagnose — als JSON fürs
    Notifications-Dropdown im Dashboard statt als Discord-Embed."""
    guild, guild_id = ctx.guild, ctx.guild_id

    settings = await db_settings.get_guild(guild_id) or {}
    issues = await get_guild_issues(guild, settings, ctx.lang)

    return {
        "issues": [
            {"section": i.section, "severity": i.severity, "text": plain_mentions(guild, i.text)}
            for i in issues
        ],
    }
