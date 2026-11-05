import asyncio
import logging
import aiohttp
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

API_URL = "https://mee6.xyz/api/plugins/levels/leaderboard/{guild_id}"
PAGE_LIMIT = 1000
MAX_PLAYERS = 50_000
REQUEST_PAUSE = 0.5
MAX_XP = 2_000_000_000
TIMEOUT = aiohttp.ClientTimeout(total=15)
HEADERS = {"User-Agent": "NexusBot-Importer (+https://trynexus.de)"}


class Mee6Error(Exception):
    """Basis für alle Import-Fehler."""

class Mee6Private(Mee6Error):
    """401: Leaderboard ist nicht öffentlich."""

class Mee6NotFound(Mee6Error):
    """404: Kein MEE6-Leaderboard für diesen Server."""

class Mee6Unavailable(Mee6Error):
    """429 (nach Retry), 5xx, Timeout oder kaputte Antwort."""

@dataclass
class Mee6Data:
    players: list[tuple[int, int]] = field(default_factory=list)   # (user_id, xp), absteigend nach XP
    rewards: list[tuple[int, int]] = field(default_factory=list)   # (mee6_level, role_id)
    truncated: bool = False

def parse_page(payload: dict) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Reine Funktion: (Spieler, Rollenbelohnungen) aus einer API-Antwort. Kaputte Einträge werden übersprungen."""
    players: list[tuple[int, int]] = []
    for p in payload.get("players") or []:
        try:
            user_id, xp = int(p["id"]), int(p["xp"])
        except (KeyError, TypeError, ValueError):
            continue
        if xp > 0:
            players.append((user_id, min(xp, MAX_XP)))

    rewards: list[tuple[int, int]] = []
    for r in payload.get("role_rewards") or []:
        try:
            rewards.append((int(r["rank"]), int(r["role"]["id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return players, rewards

def _raise_for_body(payload: dict) -> None:
    """MEE6 meldet Fehler teils nur im Body ({"status_code": 401, ...})."""
    code = payload.get("status_code")
    if code == 401:
        raise Mee6Private()
    if code == 404:
        raise Mee6NotFound()
    if isinstance(code, int) and code >= 400:
        raise Mee6Unavailable(f"MEE6 status_code {code}")

async def _get_page(session: aiohttp.ClientSession, guild_id: int, page: int, *, _retry: bool = True) -> dict:
    url = API_URL.format(guild_id=guild_id)
    async with session.get(url, params={"page": page, "limit": PAGE_LIMIT}) as resp:
        if resp.status == 401:
            raise Mee6Private()
        if resp.status == 404:
            raise Mee6NotFound()
        if resp.status == 429:
            if not _retry:
                raise Mee6Unavailable("rate limited")
            try:
                delay = float(resp.headers.get("Retry-After", 5))
            except ValueError:
                delay = 5.0
            await asyncio.sleep(min(delay, 30))
            return await _get_page(session, guild_id, page, _retry=False)
        if resp.status != 200:
            raise Mee6Unavailable(f"HTTP {resp.status}")
        try:
            payload = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as e:
            raise Mee6Unavailable("invalid response") from e

    if not isinstance(payload, dict):
        raise Mee6Unavailable("unexpected response")
    _raise_for_body(payload)
    return payload

async def fetch_leaderboard(
    guild_id: int,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
) -> Mee6Data:
    """Lädt alle Seiten bis zur leeren Antwort. on_progress(anzahl_bisher) wird nach jeder Seite aufgerufen."""
    data = Mee6Data()
    seen: set[int] = set()
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
            page = 0
            while True:
                payload = await _get_page(session, guild_id, page)
                players, rewards = parse_page(payload)
                if page == 0:
                    data.rewards = rewards

                new = [(uid, xp) for uid, xp in players if uid not in seen]
                if not new:
                    break   # leere Seite (oder nur Wiederholungen) = Ende

                seen.update(uid for uid, _ in new)
                data.players.extend(new)

                if len(data.players) >= MAX_PLAYERS:
                    data.players = data.players[:MAX_PLAYERS]
                    data.truncated = True
                    break

                if on_progress:
                    await on_progress(len(data.players))
                page += 1
                await asyncio.sleep(REQUEST_PAUSE)
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        raise Mee6Unavailable(str(e)) from e

    log.info("MEE6-Leaderboard von %s geladen: %s Spieler, %s Rollenbelohnungen.", guild_id, len(data.players), len(data.rewards))
    return data