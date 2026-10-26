import logging
import aiohttp
import discord
from config import BOTBOARD_TOKEN

log = logging.getLogger(__name__)

async def post_commands_to_botboard(bot: discord.Client):
    """Pusht die aktuelle Server- und Nutzeranzahl an discordbotlist.com."""
    if not bot.user:
        return

    guild_count = len(bot.guilds)

    payload = {
        "server_count": guild_count,
    }

    url = f"https://www.botboard.gg/api/v1/bots/{bot.user.id}/stats"
    headers = {
        "Authorization": f"{BOTBOARD_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 204):
                    log.info("[BotBoard] Serveranzahl (%d) erfolgreich aktualisiert.", guild_count)
                else:
                    text = await resp.text()
                    log.warning("[BotBoard] Stats-Push fehlgeschlagen (HTTP %d): %s", resp.status, text)
    except aiohttp.ClientError:
        log.exception("DBL Command-Upload fehlgeschlagen (Netzwerkfehler).")