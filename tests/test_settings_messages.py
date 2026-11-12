"""Einstellungen in Discord (cogs/admin_commands.py, cogs/diagnose.py) — Emojis aus emojis.py."""
from cogs import diagnose
from cogs.admin_commands import EmbedColorModal
from emojis import NexusEmojis
from tests.conftest import t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


async def test_ungueltige_farbe(guild):
    modal = EmbedColorModal(LANG, None)
    modal.color_input._value = "kein-hex"
    interaction = FakeInteraction(guild)
    await modal.on_submit(interaction)
    assert [m["content"] for m in interaction.sent] == [f"{NexusEmojis.ERROR} {t('color_invalid')}"]


def test_diagnose_symbole():
    assert (diagnose.OK, diagnose.WARN, diagnose.ERR) == (NexusEmojis.CHECKMARK, NexusEmojis.WARNING, NexusEmojis.ERROR)
