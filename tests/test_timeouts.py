"""Timeouts mit Sicherheitsabstand zu Discords 28-Tage-Grenze (systems/timeouts.py)."""
from datetime import timedelta

import discord

from cogs.moderation import Moderation
from emojis import NexusEmojis
from systems import honeypot
from systems.timeouts import MAX_TIMEOUT, MAX_TIMEOUT_MINUTES, timeout_until
from tests.conftest import t
from tests.fakes import FakeInteraction


def remaining(until) -> timedelta:
    return until - discord.utils.utcnow()


def test_kuerzere_dauer_bleibt_laengere_wird_gekappt():
    assert abs(remaining(timeout_until(timedelta(hours=2))) - timedelta(hours=2)) < timedelta(seconds=5)
    for duration in (timedelta(days=28), timedelta(days=60)):
        assert remaining(timeout_until(duration)) <= MAX_TIMEOUT
    assert MAX_TIMEOUT == timedelta(days=28) - timedelta(minutes=5)


async def test_honeypot_28_tage_timeout_wird_von_discord_angenommen(guild):
    result = await honeypot.punish(guild, guild.member, "timeout", 28)
    assert result.applied
    assert timedelta(days=27, hours=23) < remaining(guild.member.timed_out_until) <= MAX_TIMEOUT


async def test_slash_timeout_ueber_28_tage_gibt_klare_meldung(guild, bot):
    interaction = FakeInteraction(guild)
    cog = Moderation(bot)
    cog._hierarchy_error = lambda *args, **kwargs: None   # Rangprüfung ist hier nicht Thema
    await Moderation.timeout.callback(cog, interaction, member=guild.member, duration=MAX_TIMEOUT_MINUTES + 1)
    assert [m["content"] for m in interaction.sent] == [
        f"{NexusEmojis.ERROR} {t('timeout_too_long', max=MAX_TIMEOUT_MINUTES)}"
    ]
    assert not any(c[0] == "member.timeout" for c in guild.calls)


async def test_slash_timeout_genau_28_tage_klappt(guild, bot):
    interaction = FakeInteraction(guild)
    cog = Moderation(bot)
    cog._hierarchy_error = lambda *args, **kwargs: None
    await Moderation.timeout.callback(cog, interaction, member=guild.member, duration=MAX_TIMEOUT_MINUTES)
    assert remaining(guild.member.timed_out_until) <= MAX_TIMEOUT
    assert NexusEmojis.ERROR not in interaction.sent[0]["content"]
