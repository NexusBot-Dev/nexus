"""Notbremse: Mit RELEASED = False ist der Streamplan nur auf dem Dev-Server nutzbar (stream_schedule.is_available)."""
from datetime import datetime, timedelta, timezone

import pytest

from database import db_stream_schedule
from systems import stream_schedule
from tests.conftest import t
from tests.stream_schedule_fakes import follow, segment

SA = "stream_alerts"
SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)


@pytest.fixture(autouse=True)
def gesperrt(monkeypatch):
    monkeypatch.setattr(stream_schedule, "RELEASED", False)


@pytest.fixture
def dev_server(guild, monkeypatch):
    monkeypatch.setattr(stream_schedule, "DEV_GUILD_ID", guild.id)


async def test_anderer_server_kann_nicht_einschalten(dash, guild, twitch):
    mc_id = await follow(guild, sync=False)
    error = await dash.error(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": True})
    assert error == t("dash_err_sched_unavailable")
    assert await db_stream_schedule.get_syncs_for_guild(guild.id) == []
    assert (await dash.ok(SA, "schedule"))["available"] is False


async def test_anderer_server_ausschalten_geht(bot, guild, twitch):
    mc_id = await follow(guild)   # z. B. eingeschaltet, bevor die Sperre kam
    await stream_schedule.set_enabled(bot, guild.id, mc_id, False)
    assert await db_stream_schedule.get_syncs_for_guild(guild.id) == []


async def test_abgleich_raeumt_auf_anderen_servern_ab(bot, guild, twitch, monkeypatch):
    twitch.plan("tw-1", segment("a", SOON))
    await follow(guild)
    monkeypatch.setattr(stream_schedule, "RELEASED", True)
    await stream_schedule.sync_all(bot)
    assert len(guild.events_by_id) == 1

    monkeypatch.setattr(stream_schedule, "RELEASED", False)   # Sperre kommt dazu
    await stream_schedule.sync_all(bot)
    assert guild.events_by_id == {}
    assert len(await db_stream_schedule.get_syncs_for_guild(guild.id)) == 1   # Schalter bleibt für die Freigabe


async def test_dev_server_geht(dash, guild, twitch, dev_server):
    twitch.plan("tw-1", segment("a", SOON))
    mc_id = await follow(guild, sync=False)
    data = await dash.ok(SA, "schedule-toggle", {"monitored_channel_id": str(mc_id), "enabled": True})
    assert data["available"] is True
    assert data["entries"][0]["events"] == 1


def test_meldung_in_allen_sprachen():
    from translations import TRANSLATIONS
    for code, lang in TRANSLATIONS.items():
        assert lang["dash_err_sched_unavailable"], code
