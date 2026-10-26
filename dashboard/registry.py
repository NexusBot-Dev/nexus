from typing import Callable, Awaitable

ModuleHandler = Callable[[int, dict], Awaitable[dict]]

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
    if isinstance(value, bool):
        raise ValueError(error)
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(error)
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError(error)
