import discord
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass(frozen=True, slots=True)
class DashCtx:
    """Kontext eines Dashboard-Requests — von der Auth-Middleware gebaut.

    guild:   die Guild, auf die der Request zielt (Bot ist garantiert drauf)
    actor:   der im Dashboard eingeloggte User als Member dieser Guild; hat
             Administrator oder Server verwalten (von der Middleware geprüft)
    payload: der Request-Body; guild_id ist darin bereits als int normalisiert
    """
    guild: discord.Guild
    actor: discord.Member
    payload: dict

    @property
    def guild_id(self) -> int:
        return self.guild.id

ModuleHandler = Callable[[DashCtx], Awaitable[dict]]

_MODULE_ACTIONS: dict[str, dict[str, ModuleHandler]] = {}
_bot = None

def register_action(module: str, action: str):
    def decorator(fn: ModuleHandler):
        _MODULE_ACTIONS.setdefault(module, {})[action] = fn
        return fn
    return decorator

def get_handler(module: str, action: str) -> ModuleHandler | None:
    return _MODULE_ACTIONS.get(module, {}).get(action)

def set_bot(bot):
    global _bot
    _bot = bot

def get_bot():
    if _bot is None:
        raise RuntimeError("Bot-Referenz wurde noch nicht gesetzt")
    return _bot

def get_guild_or_raise(guild_id: int):
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")
    return guild

def parse_id(value, error: str) -> int:
    """Discord-/DB-ID aus dem Payload. Wirft ValueError mit lesbarer Meldung statt
    'invalid literal for int()' bzw. einem 500er bei Listen/Objekten."""
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(error)
    return value