"""Boost-Nachricht beim Server-Boost (cogs/boosts.py)."""
import pytest

from cogs.boosts import Boosts
from database import db_settings
from tests.conftest import query, run


@pytest.fixture
def boost(guild, bot):
    """Server mit Boost-Kanal; ob das Modul an ist, entscheidet jeder Test selbst."""
    guild.premium_subscription_count = 3
    guild.boosts = guild.add_text_channel("boosts")
    run(query("UPDATE guild_settings SET boost_channel_id = %s WHERE guild_id = %s", guild.boosts.id, guild.id))
    guild.cog = Boosts(bot)
    return guild


async def test_boost_nachricht_bei_aktivem_modul(boost):
    await db_settings.set_module(boost.id, "boosts", True)
    await boost.cog._handle_boost(boost.member)
    [message] = boost.boosts.messages.values()
    assert boost.member.mention in message.embeds[0].description


async def test_keine_boost_nachricht_bei_ausgeschaltetem_modul(boost):
    await db_settings.set_module(boost.id, "boosts", False)
    await boost.cog._handle_boost(boost.member)
    assert boost.boosts.messages == {}


async def test_standard_ist_aus(boost):
    """Fehlt das Modul in den Einstellungen, gilt der Standard des Bots: Boosts aus."""
    await query("UPDATE guild_settings SET modules = %s WHERE guild_id = %s", '{"moderation": true}', boost.id)
    db_settings._settings_cache.clear()
    await boost.cog._handle_boost(boost.member)
    assert boost.boosts.messages == {}
