import hmac
import discord
from aiohttp import web
from .discord_auth_check import verify_guild_access

@web.middleware
async def dashboard_auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/dashboard/"):
        return await handler(request)

    if request.path == "/api/dashboard/health":
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response({"ok": False, "error": "missing_token"}, status=401)

    token = auth_header.removeprefix("Bearer ").strip()
    expected = request.app["dashboard_token"]
    if not hmac.compare_digest(token, expected):
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
    if not isinstance(raw_user_id, str) or not raw_user_id.isdigit():
        return web.json_response({"ok": False, "error": "missing_user"}, status=401)

    try:
        has_access = await verify_guild_access(int(raw_user_id), guild_id)
    except discord.HTTPException:
        return web.json_response({"ok": False, "error": "discord_unavailable"}, status=503)
    if not has_access:
        return web.json_response({"ok": False, "error": "guild_access_denied"}, status=403)

    body["guild_id"] = guild_id
    request["_cached_json"] = body
    return await handler(request)