import logging
import aiohttp
import discord
from discord.ext import commands
from config import DBL_TOKEN
from translations import COMMAND_LOCALES

log = logging.getLogger(__name__)

def _resolve_description(cmd: discord.app_commands.Command | discord.app_commands.Group) -> str:
    """Holt die englische Beschreibung aus den Locales oder den Standard-Docstring."""
    desc = str(cmd.description)
    en_desc = COMMAND_LOCALES.get("en", {}).get(desc)
    return en_desc or desc or "No description provided"

def build_dbl_commands(tree: discord.app_commands.CommandTree) -> list[dict]:
    """Liest alle registrierten Slash-Commands aus dem Tree und strukturiert sie für DBL."""
    payload = []

    for item in tree.get_commands():
        if isinstance(item, discord.app_commands.Command):
            payload.append({
                "name": item.name,
                "description": _resolve_description(item)[:100],
                "type": 1,
            })

        elif isinstance(item, discord.app_commands.Group):
            for sub_cmd in item.commands:
                if isinstance(sub_cmd, discord.app_commands.Command):
                    payload.append({
                        "name": f"{item.name} {sub_cmd.name}",
                        "description": _resolve_description(sub_cmd)[:100],
                        "type": 1,
                    })
                elif isinstance(sub_cmd, discord.app_commands.Group):
                    for deep_cmd in sub_cmd.commands:
                        payload.append({
                            "name": f"{item.name} {sub_cmd.name} {deep_cmd.name}",
                            "description": _resolve_description(deep_cmd)[:100],
                            "type": 1,
                        })

    return payload

async def post_commands_to_dbl(bot: commands.Bot):
    if not bot.user:
        log.warning("Bot ist noch nicht eingeloggt; DBL Command-Upload abgebrochen.")
        return

    commands_payload = build_dbl_commands(bot.tree)
    url = f"https://discordbotlist.com/api/v1/bots/{bot.user.id}/commands"
    headers = {"Authorization": f"Bot {DBL_TOKEN}", "Content-Type": "application/json"}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(url, json=commands_payload, headers=headers) as resp:
                if resp.status in (200, 204):
                    log.info("Erfolgreich %d Commands an discordbotlist.com übertragen.", len(commands_payload))
                else:
                    text = await resp.text()
                    log.warning("DBL Command-Upload fehlgeschlagen (HTTP %d): %s", resp.status, text)
    except aiohttp.ClientError:
        log.exception("DBL Command-Upload fehlgeschlagen (Netzwerkfehler).")

async def post_server_count_to_dbl(bot: commands.Bot):
    """Pusht die aktuelle Server- und Nutzeranzahl an discordbotlist.com."""
    if not bot.user:
        return

    guild_count = len(bot.guilds)
    user_count = sum(g.member_count for g in bot.guilds if g.member_count)

    payload = {
        "guilds": guild_count,
        "users": user_count,
    }

    url = f"https://discordbotlist.com/api/v1/bots/{bot.user.id}/stats"
    headers = {
        "Authorization": f"Bot {DBL_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 204):
                    log.info("[DBL] Serveranzahl (%d) & User (%d) erfolgreich aktualisiert.", guild_count, user_count)
                else:
                    text = await resp.text()
                    log.warning("[DBL] Stats-Push fehlgeschlagen (HTTP %d): %s", resp.status, text)
    except aiohttp.ClientError:
        log.exception("DBL Command-Upload fehlgeschlagen (Netzwerkfehler).")