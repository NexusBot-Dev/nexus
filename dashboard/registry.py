from typing import Callable, Awaitable, Optional

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
