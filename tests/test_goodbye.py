"""Goodbye-Einstellungen in Discord (cogs/goodbye.py) — Status und Meldungen mit Emojis aus emojis.py."""
from cogs.goodbye import GoodbyeConfigView, _goodbye_status_embed
from database import db_settings
from emojis import NexusEmojis
from tests.conftest import t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


async def test_status_aus_und_an(guild):
    embed = await _goodbye_status_embed(guild.id, LANG)
    assert embed.fields[0].value == f"{NexusEmojis.OFF} {t('setup_off')}"

    await db_settings.update_guild(guild.id, goodbye_enabled=True)
    embed = await _goodbye_status_embed(guild.id, LANG)
    assert embed.fields[0].value == f"{NexusEmojis.CHECKMARK} {t('setup_on')}"


async def test_einschalten_ohne_kanal(guild):
    interaction = FakeInteraction(guild)
    view = GoodbyeConfigView(LANG, enabled=False)
    await view.toggle_goodbye.callback(interaction)
    assert [m["content"] for m in interaction.sent] == [f"{NexusEmojis.ERROR} {t('announcement_need_channel')}"]
    assert not (await db_settings.get_guild(guild.id))["goodbye_enabled"]
