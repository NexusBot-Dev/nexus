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
    lang:    Übersetzungen in der Sprache des Dashboards (payload["lang"]), sonst
             in der Bot-Sprache des Servers — für alles, was der Admin dort liest
    """
    guild: discord.Guild
    actor: discord.Member
    payload: dict
    lang: dict[str, str]

    @property
    def guild_id(self) -> int:
        return self.guild.id

class DashboardError(Exception):
    """Fehler, den der Admin im Dashboard angezeigt bekommt. Trägt nur den Übersetzungs-Key;
    übersetzt wird zentral in der Sprache des Dashboards (handle_module_action / ctx.lang)."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def render(self, lang: dict[str, str]) -> str:
        return lang.get(self.key, self.key).format(**self.params)

class InvalidRequest(DashboardError):
    """Request ist falsch aufgebaut (fehlende/ungültige Felder) — über die Oberfläche nicht
    erreichbar. Der Admin sieht nur "Ungültige Anfrage", das Detail landet im Bot-Log."""

    def __init__(self, detail: str):
        super().__init__("dash_err_invalid_request")
        self.detail = detail

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
        raise DashboardError("dash_err_bot_not_in_guild")
    return guild

def parse_id(value, error: str) -> int:
    """Discord-/DB-ID aus dem Payload. Wirft InvalidRequest (error landet im Log) statt
    'invalid literal for int()' bzw. einem 500er bei Listen/Objekten."""
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidRequest(error)
    return value