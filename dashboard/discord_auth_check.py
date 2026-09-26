import time
import aiohttp
import asyncio

_guild_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 60

async def get_user_guilds(access_token: str) -> list[dict]:
    cached = _guild_cache.get(access_token)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

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
                        raise ValueError("Discord-Token ungültig oder abgelaufen")
                    if resp.status != 200:
                        last_error = f"Discord antwortete mit Status {resp.status}"
                        await asyncio.sleep(0.5)
                        continue
                    guilds = await resp.json()
                    break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = str(e)
            await asyncio.sleep(0.5)
    else:
        raise ValueError(last_error or "Discord nicht erreichbar")

    _guild_cache[access_token] = (time.time(), guilds)
    if len(_guild_cache) > 1000:
        oldest_key = min(_guild_cache, key=lambda k: _guild_cache[k][0])
        del _guild_cache[oldest_key]

    return guilds

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