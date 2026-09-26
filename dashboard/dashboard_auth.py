import hmac
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
        return await handler(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    guild_id = body.get("guild_id")
    access_token = body.get("discord_access_token")

    if guild_id:
        if not access_token:
            return web.json_response({"ok": False, "error": "missing_discord_session"}, status=401)
        try:
            has_access = await verify_guild_access(access_token, int(guild_id))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid_discord_session"}, status=401)
        if not has_access:
            return web.json_response({"ok": False, "error": "guild_access_denied"}, status=403)

    # Body erneut lesbar machen, da aiohttp Requests nur einmal auslesbar sind
    request["_cached_json"] = body

    return await handler(request)