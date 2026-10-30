import time
import logging
import aiohttp
import re
from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
from systems.platform_registry import register_platform, PlatformHandler

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE  = "https://api.twitch.tv/helix"
TWITCH_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{4,25}$")
CALLBACK_URL = "https://hooks.trynexus.de/webhooks/twitch"

_app_token: str | None = None
_token_expires_at: float = 0.0

# Verhindert, dass mehrere gleichzeitige Requests parallel je einen neuen Token holen
import asyncio
_token_lock = asyncio.Lock()

def normalize_twitch_input(raw: str) -> str | None:
    value = raw.strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(www\.)?twitch\.tv/", "", value, flags=re.IGNORECASE)
    value = value.split("?")[0].split("/")[0].strip().lower()
    if not TWITCH_USERNAME_PATTERN.match(value):
        return None
    return value

async def _twitch_create_subscriptions(broadcaster_id: str) -> dict:
    from config import TWITCH_WEBHOOK_SECRET
    online_id = await create_eventsub_subscription("stream.online", broadcaster_id, CALLBACK_URL, TWITCH_WEBHOOK_SECRET)
    offline_id = await create_eventsub_subscription("stream.offline", broadcaster_id, CALLBACK_URL, TWITCH_WEBHOOK_SECRET)
    return {"online_id": online_id, "offline_id": offline_id}

async def _fetch_new_token(session: aiohttp.ClientSession) -> str:
    async with session.post(
        TOKEN_URL,
        data={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Twitch-Token-Anfrage fehlgeschlagen ({resp.status}): {body}")
        data = await resp.json()
        return data["access_token"], data["expires_in"]

async def get_app_token() -> str:
    """Gibt einen gültigen App-Access-Token zurück — holt nur bei Bedarf einen neuen.
    Twitch-Tokens laufen üblicherweise ~60 Tage, wir puffern 5 Minuten Sicherheitsabstand ein."""
    global _app_token, _token_expires_at

    now = time.monotonic()
    if _app_token and now < _token_expires_at:
        return _app_token

    async with _token_lock:
        # Doppelcheck nach dem Lock-Erwerb — falls eine andere Coroutine
        # währenddessen schon einen neuen Token geholt hat
        now = time.monotonic()
        if _app_token and now < _token_expires_at:
            return _app_token

        async with aiohttp.ClientSession() as session:
            token, expires_in = await _fetch_new_token(session)

        _app_token = token
        _token_expires_at = now + expires_in - 300  # 5 Min Sicherheitspuffer
        log.info("Neuer Twitch App-Access-Token geholt (gültig ~%s Sekunden).", expires_in)
        return _app_token

async def helix_get(endpoint: str, params: dict) -> dict:
    """Generischer GET-Helper gegen die Helix-API mit automatischem Token-Handling."""
    token = await get_app_token()
    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}/{endpoint}", params=params, headers=headers) as resp:
            if resp.status == 401:
                # Token evtl. serverseitig invalidiert — einmal erzwungen neu holen und retry
                log.warning("Twitch-API 401 — erzwinge Token-Refresh und wiederhole einmalig.")
                await _force_refresh()
                token = await get_app_token()
                headers["Authorization"] = f"Bearer {token}"
                async with session.get(f"{API_BASE}/{endpoint}", params=params, headers=headers) as retry_resp:
                    retry_resp.raise_for_status()
                    return await retry_resp.json()
            resp.raise_for_status()
            return await resp.json()

async def helix_post(endpoint: str, json_body: dict) -> dict:
    token = await get_app_token()
    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API_BASE}/{endpoint}", json=json_body, headers=headers) as resp:
            if resp.status == 401:
                await _force_refresh()
                token = await get_app_token()
                headers["Authorization"] = f"Bearer {token}"
                async with session.post(f"{API_BASE}/{endpoint}", json=json_body, headers=headers) as retry_resp:
                    body = await retry_resp.json()
                    if retry_resp.status >= 400:
                        raise RuntimeError(f"Twitch-API-Fehler ({retry_resp.status}): {body}")
                    return body
            body = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"Twitch-API-Fehler ({resp.status}): {body}")
            return body

async def helix_delete(endpoint: str, params: dict) -> None:
    token = await get_app_token()
    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.delete(f"{API_BASE}/{endpoint}", params=params, headers=headers) as resp:
            if resp.status not in (200, 204, 404):
                body = await resp.text()
                raise RuntimeError(f"Twitch-API-Löschung fehlgeschlagen ({resp.status}): {body}")

async def create_eventsub_subscription(sub_type: str, broadcaster_id: str, callback_url: str, secret: str) -> str:
    """Legt eine EventSub-Subscription an (stream.online/stream.offline). Gibt die Subscription-ID zurück."""
    body = {
        "type": sub_type,
        "version": "1",
        "condition": {"broadcaster_user_id": broadcaster_id},
        "transport": {
            "method": "webhook",
            "callback": callback_url,
            "secret": secret,
        },
    }
    result = await helix_post("eventsub/subscriptions", body)
    return result["data"][0]["id"]

async def delete_eventsub_subscription(subscription_id: str):
    await helix_delete("eventsub/subscriptions", {"id": subscription_id})

async def _force_refresh():
    global _app_token, _token_expires_at
    _app_token = None
    _token_expires_at = 0.0

async def get_user_by_login(login: str) -> dict | None:
    """Löst einen Twitch-Login-Namen zu den vollen User-Daten auf (u.a. id, display_name)."""
    data = await helix_get("users", {"login": login})
    results = data.get("data", [])
    return results[0] if results else None

async def get_stream_by_user_id(user_id: str) -> dict | None:
    """Holt aktuelle Stream-Details, falls der Kanal gerade live ist."""
    data = await helix_get("streams", {"user_id": user_id})
    results = data.get("data", [])
    return results[0] if results else None

async def _twitch_delete_subscriptions(monitored: dict) -> None:
    if monitored.get("eventsub_online_id"):
        await delete_eventsub_subscription(monitored["eventsub_online_id"])
    if monitored.get("eventsub_offline_id"):
        await delete_eventsub_subscription(monitored["eventsub_offline_id"])

register_platform("twitch", PlatformHandler(
    normalize_input=normalize_twitch_input,
    resolve_user=get_user_by_login,
    create_subscriptions=_twitch_create_subscriptions,
    delete_subscriptions=_twitch_delete_subscriptions,
))