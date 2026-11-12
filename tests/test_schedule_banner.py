"""Event-Banner aus dem Kategorie-Cover (systems/schedule_banner.py) und das Cover von Twitch (get_box_art_url)."""
import io

import pytest
from PIL import Image

from systems import schedule_banner, twitch_api
from tests.stream_schedule_fakes import cover

RED = (200, 40, 40)


def render(title="Grand Theft Auto V", subtitle="Rusirda  ·  twitch.tv/rusirda", data=None) -> Image.Image:
    return Image.open(io.BytesIO(schedule_banner.render_banner(data or cover(RED), title, subtitle)))


def test_banner_ist_ein_jpeg_in_eventgroesse():
    image = render()
    assert (image.format, image.size) == ("JPEG", (800, 320))


def test_cover_unverzerrt_links_und_text_rechts():
    image = render().convert("RGB")
    left = image.getpixel((150, 160))           # mitten im Cover
    assert all(abs(a - b) < 40 for a, b in zip(left, RED))
    background = image.getpixel((780, 300))     # weichgezeichneter, abgedunkelter Hintergrund
    assert background[0] < RED[0] * 0.75


def test_lange_namen_werden_kleiner_und_notfalls_gekuerzt():
    width = 400
    font, text = schedule_banner.fit_text("GTA V", width, 58, 30)
    assert (font.size, text) == (58, "GTA V")
    font, text = schedule_banner.fit_text("TOM CLANCY'S RAINBOW SIX SIEGE X", width, 58, 30)
    assert text == "TOM CLANCY'S RAINBOW SIX SIEGE X" and font.size < 58
    font, text = schedule_banner.fit_text("X" * 200, width, 58, 30)
    assert text.endswith("…") and font.size == 30
    assert font.getlength(text) <= width
    render(title="X" * 200)   # rechnet ohne Fehler


def test_kein_bild_oder_zu_gross_wird_abgelehnt(monkeypatch):
    with pytest.raises(Exception):
        render(data=b"kein Bild")
    monkeypatch.setattr(schedule_banner, "MAX_COVER_PIXELS", 1000)
    with pytest.raises(ValueError):
        render()


async def test_cover_url_von_twitch(monkeypatch):
    calls = []

    async def helix_get(endpoint, params):
        calls.append((endpoint, params))
        if params["id"] == "32982":
            return {"data": [{"id": "32982", "box_art_url": "https://static-cdn.jtvnw.net/ttv-boxart/32982_IGDB-{width}x{height}.jpg"}]}
        return {"data": []}

    monkeypatch.setattr(twitch_api, "helix_get", helix_get)
    monkeypatch.setattr(twitch_api, "_box_art_urls", {})
    assert await twitch_api.get_box_art_url("32982", 600, 800) == "https://static-cdn.jtvnw.net/ttv-boxart/32982_IGDB-600x800.jpg"
    assert await twitch_api.get_box_art_url("32982", 600, 800) is not None
    assert await twitch_api.get_box_art_url("999", 600, 800) is None
    assert [c[1]["id"] for c in calls] == ["32982", "999"]   # je Kategorie nur einmal gefragt
