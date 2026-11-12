"""AutoMod- und Honeypot-Übersicht in Discord — Status-Emojis aus emojis.py."""
from cogs.automod import _automod_embed
from cogs.honeypot import _hub_embed
from emojis import NexusEmojis
from tests.conftest import t
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]
OFF = f"{NexusEmojis.OFF} {t('module_inactive')}"


def test_automod_uebersicht_alles_aus():
    embed = _automod_embed({}, shield_enabled=False, lang=LANG)
    values = [f.value for f in embed.fields]
    # Jede Regel, Anti-Nuke und Honeypot zeigen „aus“ mit dem OFF-Emoji
    assert sum(OFF in v for v in values) == 5, values


async def test_honeypot_hub_ohne_honeypot(guild):
    embed, _ = await _hub_embed(guild, LANG)
    assert f"{NexusEmojis.OFF} {t('honeypot_hub_inactive')}" in [f.value for f in embed.fields]
