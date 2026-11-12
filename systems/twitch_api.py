import time
from datetime import datetime, timezone
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
SCHEDULE_MAX_PAGES = 4   # je 25 Termine — reicht auch bei mehreren Streams am Tag für zwei Wochen

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

async def get_schedule(broadcaster_id: str, start_time: datetime, until: datetime) -> dict | None:
    """Streamplan von start_time bis until: {"segments": [...], "vacation": {...} | None, "broadcaster_login": ...}.

    Twitch liefert die Termine sortiert, 25 pro Seite — weitergeblättert wird nur, solange die Seite vor until
    endet (höchstens SCHEDULE_MAX_PAGES Seiten). None, wenn der Streamer keinen Plan angelegt hat (404)."""
    params = {"broadcaster_id": broadcaster_id, "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "first": 25}
    segments: list[dict] = []
    schedule: dict = {}
    for _ in range(SCHEDULE_MAX_PAGES):
        try:
            body = await helix_get("schedule", params)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise
        schedule = body.get("data") or {}
        segments.extend(schedule.get("segments") or [])
        cursor = (body.get("pagination") or {}).get("cursor")
        if not cursor or not segments or parse_time(segments[-1]["start_time"]) > until:
            break
        params = {**params, "after": cursor}
    return {**schedule, "segments": segments}

def parse_time(value: str) -> datetime:
    """RFC3339 von Twitch ("2026-11-10T18:00:00Z") → datetime in UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

_box_art_urls: dict[str, str | None] = {}   # je Kategorie — ändert sich praktisch nie


async def get_box_art_url(game_id: str, width: int, height: int) -> str | None:
    """Cover einer Kategorie in der gewünschten Größe (Get Games, box_art_url mit {width}x{height}).
    None, wenn Twitch die Kategorie nicht kennt."""
    if game_id not in _box_art_urls:
        data = await helix_get("games", {"id": game_id})
        results = data.get("data", [])
        _box_art_urls[game_id] = (results[0].get("box_art_url") or None) if results else None
    template = _box_art_urls[game_id]
    return template.replace("{width}x{height}", f"{width}x{height}") if template else None

async def get_offline_image_url(user_id: str) -> str | None:
    """Offline-Bild des Kanals (16:9) — None, wenn der Streamer keins hinterlegt hat."""
    data = await helix_get("users", {"id": user_id})
    results = data.get("data", [])
    return (results[0].get("offline_image_url") or None) if results else None

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