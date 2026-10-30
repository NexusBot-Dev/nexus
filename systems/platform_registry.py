from dataclasses import dataclass
from typing import Callable, Awaitable, Optional

@dataclass
class PlatformHandler:
    normalize_input: Callable[[str], Optional[str]]
    resolve_user: Callable[[str], Awaitable[Optional[dict]]]
    create_subscriptions: Optional[Callable[[str], Awaitable[dict]]] = None
    delete_subscriptions: Optional[Callable[[dict], Awaitable[None]]] = None
    on_channel_created: Optional[Callable[[int, dict], Awaitable[None]]] = None

PLATFORM_HANDLERS: dict[str, PlatformHandler] = {}

def register_platform(name: str, handler: PlatformHandler):
    PLATFORM_HANDLERS[name] = handler
