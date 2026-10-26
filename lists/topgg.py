import aiohttp
import asyncio
import logging
import discord
from config import TOPGG_TOKEN
from translations import COMMAND_LOCALES

log = logging.getLogger(__name__)

def prepare_command_payload(item: dict) -> dict:
    """Ersetzt Lokalisierungs-Keys rekursiv durch EN (Root) und DE (Localizations)."""
    desc_key = item.get("description")

    if desc_key:
        en_text = COMMAND_LOCALES.get("en", {}).get(desc_key)
        de_text = COMMAND_LOCALES.get("de", {}).get(desc_key)

        if en_text:
            item["description"] = en_text

        if de_text:
            if not item.get("description_localizations"):
                item["description_localizations"] = {}
            item["description_localizations"]["de"] = de_text

    for option in item.get("options", []):
        prepare_command_payload(option)

    return item

async def _topgg_request(method: str, url: str, json_data: dict | list):
    """Zentrale Helfer-Funktion für alle Top.gg API-Calls."""
    headers = {
        "Authorization": f"Bearer {TOPGG_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, json=json_data, headers=headers) as resp:
                text = await resp.text()
                return resp.status, text
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        log.warning("⚠️ Top.gg API nicht erreichbar oder Timeout: %s", e)
        return None, str(e)

async def post_commands_to_topgg(bot: discord.Client):
    """Pusht alle Slash-Commands an Top.gg (API v1)."""
    raw_commands = [cmd.to_dict(bot.tree) for cmd in bot.tree.get_commands()]
    resolved_commands = [prepare_command_payload(cmd) for cmd in raw_commands]

    url = "https://top.gg/api/v1/projects/@me/commands"
    status, text = await _topgg_request("PUT", url, resolved_commands)
    if status is None:
        log.warning("Upload der Commands zu Top.gg übersprungen (Timeout/Netzwerkfehler).")
        return
    if status in (200, 204):
        log.info("[Top.gg] %d Commands erfolgreich synchronisiert.", len(resolved_commands))
    else:
        log.error("[Top.gg] Fehler beim Command-Sync (HTTP %d): %s", status, text)

async def post_server_count(bot: discord.Client):
    """Pusht die aktuelle Serveranzahl an Top.gg."""
    if not bot.user:
        return

    guild_count = len(bot.guilds)
    payload = {"server_count": guild_count}

    url = f"https://top.gg/api/bots/{bot.user.id}/stats"
    status, text = await _topgg_request("POST", url, payload)

    if status in (200, 204):
        log.info("[Top.gg] Serveranzahl (%d) erfolgreich aktualisiert.", guild_count)
    else:
        log.error("[Top.gg] Fehler beim Stats-Push (HTTP %d): %s", status, text)