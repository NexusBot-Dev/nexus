import hmac
import math
import time
import discord
from collections import deque
from aiohttp import web
from .discord_auth_check import resolve_actor
from .registry import DashCtx

RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_REQUESTS = 60
_PURGE_THRESHOLD = 1000
_request_log: dict[int, deque[float]] = {}

def _check_rate_limit(user_id: int) -> int | None:
    """None = erlaubt, sonst Sekunden bis wieder Platz im Fenster ist."""
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    if len(_request_log) > _PURGE_THRESHOLD:
        for uid in [u for u, q in _request_log.items() if not q or q[-1] < window_start]:
            del _request_log[uid]

    log = _request_log.setdefault(user_id, deque())
    while log and log[0] < window_start:
        log.popleft()

    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return max(1, math.ceil(log[0] + RATE_LIMIT_WINDOW_SECONDS - now))

    log.append(now)
    return None

@web.middleware
async def dashboard_auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/dashboard/"):
        return await handler(request)

    if request.path == "/api/dashboard/health":
        return await handler(request)

    expected = request.app["dashboard_token"]
    if not expected:
        return web.json_response({"ok": False, "error": "dashboard_disabled"}, status=503)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response({"ok": False, "error": "missing_token"}, status=401)

    token = auth_header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        return web.json_response({"ok": False, "error": "invalid_token"}, status=401)

    if request.method != "POST":
        return await handler(request)  # aktuell nur /health als GET

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "invalid_request"}, status=400)

    try:
        guild_id = int(body["guild_id"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid_request"}, status=400)
    if guild_id <= 0:
        return web.json_response({"ok": False, "error": "invalid_request"}, status=400)

    raw_user_id = body.get("acting_user_id")
    if not isinstance(raw_user_id, str) or not (raw_user_id.isascii() and raw_user_id.isdigit()):
        return web.json_response({"ok": False, "error": "missing_user"}, status=401)
    user_id = int(raw_user_id)

    retry_after = _check_rate_limit(user_id)
    if retry_after is not None:
        return web.json_response(
            {"ok": False, "error": "rate_limited", "retry_after": retry_after},
            status=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        resolved = await resolve_actor(user_id, guild_id)
    except discord.HTTPException:
        return web.json_response({"ok": False, "error": "discord_unavailable"}, status=503)
    if resolved is None:
        return web.json_response({"ok": False, "error": "guild_access_denied"}, status=403)

    guild, actor = resolved
    body["guild_id"] = guild_id
    request["dash_ctx"] = DashCtx(guild=guild, actor=actor, payload=body)
    return await handler(request)