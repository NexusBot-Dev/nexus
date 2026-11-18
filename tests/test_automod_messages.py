"""Honeypot-Übersicht in Discord — Status-Emojis aus emojis.py (die AutoMod-Übersicht: test_automod_overview.py)."""
from cogs.honeypot import _hub_embed
from emojis import NexusEmojis
from tests.conftest import t
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


async def test_honeypot_hub_ohne_honeypot(guild):
    embed, _ = await _hub_embed(guild, LANG)
    assert f"{NexusEmojis.OFF} {t('honeypot_hub_inactive')}" in [f.value for f in embed.fields]
