import re
import logging
import aiohttp
from config import YOUTUBE_API_KEY
from systems.platform_registry import register_platform, PlatformHandler

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"

CHANNEL_ID_PATTERN = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")
HANDLE_PATTERN = re.compile(r"^@?[a-zA-Z0-9_.-]{3,30}$")

def normalize_youtube_input(raw: str) -> str | None:
    """Gibt entweder eine Channel-ID (UC...) oder ein Handle (@...) zurück,
    je nachdem was erkannt wurde. Die eigentliche Auflösung passiert in resolve_user."""
    value = raw.strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(www\.)?youtube\.com/", "", value, flags=re.IGNORECASE)
    value = value.split("?")[0].strip()

    # Format: channel/UCxxxxxxxx
    if value.startswith("channel/"):
        value = value[len("channel/"):]

    # Format: @handle oder c/customname oder einfach nur ein Name
    if value.startswith("@"):
        value = value  # Handle bleibt mit @-Präfix
    elif value.startswith("c/"):
        value = "@" + value[len("c/"):]  # alte Custom-URL grob als Handle behandeln

    value = value.split("/")[0].strip()

    if CHANNEL_ID_PATTERN.match(value):
        return value
    if HANDLE_PATTERN.match(value):
        return value if value.startswith("@") else f"@{value}"

    return None

async def _api_get(endpoint: str, params: dict) -> dict:
    params = {**params, "key": YOUTUBE_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}/{endpoint}", params=params) as resp:
            try:
                body = await resp.json()
            except aiohttp.ContentTypeError:
                raise RuntimeError(f"YouTube-API lieferte kein valides JSON. Status: {resp.status}")

            if resp.status == 403 and body.get("error", {}).get("errors", [{}])[0].get("reason") == "quotaExceeded":
                log.error("YouTube-API-Quota erschöpft — Poll-Zyklus wird pausiert.")
                raise QuotaExceededError()
            if resp.status == 404 and body.get("error", {}).get("errors", [{}])[0].get("reason") == "playlistNotFound":
                raise PlaylistNotFoundError()
            if resp.status >= 400:
                raise RuntimeError(f"YouTube-API-Fehler ({resp.status}): {body}")
            return body

class QuotaExceededError(Exception):
    pass

class PlaylistNotFoundError(Exception):
    pass

async def get_channel_by_input(identifier: str) -> dict | None:
    """Löst Handle ODER Channel-ID zur vollen Channel-Info auf. 1 Unit."""
    if CHANNEL_ID_PATTERN.match(identifier):
        params = {"part": "snippet,contentDetails", "id": identifier}
    else:
        params = {"part": "snippet,contentDetails", "forHandle": identifier}

    data = await _api_get("channels", params)
    results = data.get("items", [])
    if not results:
        return None

    channel = results[0]
    return {
        "id": channel["id"],
        "display_name": channel["snippet"]["title"],
        "uploads_playlist_id": channel["contentDetails"]["relatedPlaylists"]["uploads"],
    }

async def get_latest_video_status(uploads_playlist_id: str) -> dict | None:
    """Holt das neueste Video + Live-Status. 2 Units (playlistItems + videos)."""
    try:
        playlist_data = await _api_get("playlistItems", {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": 1,
        })
    except PlaylistNotFoundError:
        log.info("Uploads-Playlist %s existiert noch nicht (Kanal ohne Uploads) — überspringe.", uploads_playlist_id)
        return None
    items = playlist_data.get("items", [])
    if not items:
        return None

    video_id = items[0]["contentDetails"]["videoId"]

    video_data = await _api_get("videos", {
        "part": "snippet,liveStreamingDetails",
        "id": video_id,
    })
    videos = video_data.get("items", [])
    if not videos:
        return None

    video = videos[0]
    return {
        "video_id": video_id,
        "title": video["snippet"]["title"],
        "thumbnail_url": video["snippet"]["thumbnails"].get("high", {}).get("url"),
        "live_broadcast_content": video["snippet"]["liveBroadcastContent"],  # "live" | "upcoming" | "none"
    }

# ─── Platform-Registry-Anbindung ───

async def _resolve_youtube_user(identifier: str) -> dict | None:
    channel = await get_channel_by_input(identifier)
    if channel is None:
        return None
    return {
        "id": channel["id"],
        "display_name": channel["display_name"],
        "uploads_playlist_id": channel["uploads_playlist_id"],  # zusätzliches Feld, Registry-Vertrag toleriert Extra-Keys
    }

async def _store_youtube_playlist_id(monitored_channel_id: int, user: dict) -> None:
    from database import db_stream_alerts
    playlist_id = user.get("uploads_playlist_id")
    if playlist_id:
        await db_stream_alerts.set_youtube_playlist_id(monitored_channel_id, playlist_id)

register_platform("youtube", PlatformHandler(
    normalize_input=normalize_youtube_input,
    resolve_user=_resolve_youtube_user,
    create_subscriptions=None,
    on_channel_created=_store_youtube_playlist_id,
))
