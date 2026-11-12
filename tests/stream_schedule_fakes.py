"""Nachgebaute Twitch-Streampläne für die Tests von systems/stream_schedule.py und /stream_schedule."""
from datetime import datetime, timedelta, timezone

import io

import aiohttp
from PIL import Image

from database import db_stream_alerts, db_stream_schedule
from systems import stream_schedule, twitch_api

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
IMAGE = b"\x89PNG offline-bild"


def at(days: float = 0, hours: float = 0) -> datetime:
    return NOW + timedelta(days=days, hours=hours)


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def segment(seg_id: str, start: datetime, hours: float = 3, *, title="Elden Ring Blind-Run", category="Elden Ring",
            category_id="1", canceled=False, end=True) -> dict:
    return {
        "id": seg_id, "start_time": iso(start), "end_time": iso(start + timedelta(hours=hours)) if end else None,
        "title": title, "category": {"id": category_id, "name": category} if category else None,
        "canceled_until": iso(start + timedelta(hours=hours)) if canceled else None, "is_recurring": True,
    }


def cover(color=(200, 40, 40), size=(600, 800)) -> bytes:
    """Ein Kategorie-Cover wie von Twitch (hochkant, JPEG)."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeTwitch:
    """Streampläne, Offline-Bilder und Kategorie-Cover; zählt die Abrufe.
    Ohne Eintrag in covers hat eine Kategorie kein Cover — dann nimmt der Bot das Offline-Bild."""

    def __init__(self, monkeypatch):
        self.plans: dict[str, dict | None] = {}
        self.images: dict[str, str | None] = {}
        self.covers: dict[str, bytes] = {}   # Kategorie-ID → Cover
        self.fail: set[str] = set()
        self.schedule_calls: list[str] = []
        self.image_calls: list[str] = []
        monkeypatch.setattr(twitch_api, "get_schedule", self.get_schedule)
        monkeypatch.setattr(twitch_api, "get_offline_image_url", self.get_offline_image_url)
        monkeypatch.setattr(twitch_api, "get_box_art_url", self.get_box_art_url)

        async def download(url):
            if url.startswith("https://boxart/"):
                return self.covers.get(url.split("/")[3])
            return IMAGE if url == "https://img/offline.png" else None
        monkeypatch.setattr(stream_schedule, "download_image", download)

    def plan(self, broadcaster_id: str, *segments, login="rusirda", vacation=None):
        self.plans[broadcaster_id] = {"segments": list(segments), "broadcaster_login": login, "vacation": vacation}

    async def get_schedule(self, broadcaster_id, start_time, until):
        self.schedule_calls.append(broadcaster_id)
        if broadcaster_id in self.fail:
            raise aiohttp.ClientError("Twitch nicht erreichbar")
        return self.plans.get(broadcaster_id)

    async def get_offline_image_url(self, broadcaster_id):
        self.image_calls.append(broadcaster_id)
        return self.images.get(broadcaster_id)

    async def get_box_art_url(self, game_id, width, height):
        return f"https://boxart/{game_id}/{width}x{height}.jpg" if game_id in self.covers else None


async def follow(guild, broadcaster_id="tw-1", name="Rusirda", *, sync=True) -> int:
    """Stream-Alert für einen Twitch-Kanal anlegen (und den Streamplan-Schalter einschalten)."""
    channel = guild.add_text_channel(f"live-{broadcaster_id}")
    monitored = await db_stream_alerts.get_or_create_monitored_channel("twitch", broadcaster_id, name)
    await db_stream_alerts.add_subscription(guild.id, channel.id, monitored["id"])
    if sync:
        await db_stream_schedule.enable(guild.id, monitored["id"])
    return monitored["id"]
