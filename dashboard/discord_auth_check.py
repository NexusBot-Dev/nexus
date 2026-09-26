import time
import logging
import asyncio
import aiohttp
from cachetools import LRUCache

log = logging.getLogger(__name__)

FRESH_TTL = 60
MAX_STALE = 600

_guild_cache: LRUCache = LRUCache(maxsize=1000)  # token -> (fetched_at, guilds)
_pending: dict[str, asyncio.Task] = {}

class InvalidDiscordToken(ValueError):
    """Token ungültig oder abgelaufen."""

async def _fetch_from_discord(access_token: str) -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=5)
    last_error = None
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://discord.com/api/users/@me/guilds",
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as resp:
                    if resp.status == 401:
                        raise InvalidDiscordToken("Discord-Token ungültig oder abgelaufen")
                    if resp.status != 200:
                        last_error = f"Discord antwortete mit Status {resp.status}"
                        await asyncio.sleep(0.5)
                        continue
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = str(e)
            await asyncio.sleep(0.5)

    raise ValueError(last_error or "Discord nicht erreichbar")

async def _fetch_and_store(access_token: str) -> list[dict]:
    try:
        guilds = await _fetch_from_discord(access_token)
    except InvalidDiscordToken:
        # Token widerrufen/abgelaufen: nichts Veraltetes mehr ausliefern
        _guild_cache.pop(access_token, None)
        raise
    _guild_cache[access_token] = (time.time(), guilds)
    return guilds

def _start_fetch(access_token: str) -> asyncio.Task:
    """Startet einen Discord-Abruf — oder gibt den bereits laufenden zurück,
    damit parallele Requests sich einen Abruf teilen."""
    task = _pending.get(access_token)
    if task is None:
        task = asyncio.create_task(_fetch_and_store(access_token))
        _pending[access_token] = task

        def _cleanup(t: asyncio.Task):
            _pending.pop(access_token, None)
            if not t.cancelled() and (exc := t.exception()):
                log.warning("Guild-Abruf für Dashboard fehlgeschlagen: %s", exc)

        task.add_done_callback(_cleanup)
    return task


async def get_user_guilds(access_token: str) -> list[dict]:
    cached = _guild_cache.get(access_token)
    if cached:
        age = time.time() - cached[0]
        if age < FRESH_TTL:
            return cached[1]
        if age < MAX_STALE:
            _start_fetch(access_token)  # im Hintergrund aktualisieren, nicht warten
            return cached[1]

    # shield: bricht ein einzelner Request ab, läuft der Abruf für die anderen weiter
    return await asyncio.shield(_start_fetch(access_token))

ADMINISTRATOR = 0x8
MANAGE_GUILD = 0x20

async def verify_guild_access(access_token: str, guild_id: int) -> bool:
    guilds = await get_user_guilds(access_token)
    guild = next((g for g in guilds if int(g["id"]) == guild_id), None)
    if guild is None:
        return False
    if guild.get("owner"):
        return True
    perms = int(guild["permissions"])
    return bool(perms & ADMINISTRATOR) or bool(perms & MANAGE_GUILD)