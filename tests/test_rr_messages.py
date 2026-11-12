"""Fehlermeldungen bei Reaction Roles in Discord (cogs/reaction_roles.py) — Emoji aus emojis.py."""
import types

from cogs.reaction_roles import _limit_text, _panel_gone_text, _role_issue
from emojis import NexusEmojis
from tests.conftest import t
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


def test_panel_voll_und_weg():
    assert _limit_text(LANG, 20) == f"{NexusEmojis.ERROR} {t('rr_edit_limit_reached', max=20)}"
    assert _panel_gone_text(LANG) == f"{NexusEmojis.ERROR} {t('rr_edit_panel_gone')}"


def test_verwaltete_rolle(guild):
    role = guild.add_role("Booster", 2)
    role.managed = True
    interaction = types.SimpleNamespace(guild=guild, user=guild.owner)
    assert _role_issue(interaction, role, LANG) == f"{NexusEmojis.ERROR} {t('rr_role_managed')}"
