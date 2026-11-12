"""/xp_settings: Chat- und Voice-XP per Slash-Command (cogs/levels.py)."""
import pytest

from cogs.help import MODULE_INFO
from cogs.levels import Levels, XpRangeModal
from database import db_levels
from emojis import NexusEmojis
from tests.conftest import run, t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


@pytest.fixture
def cog(bot):
    return Levels(bot)


async def open_panel(cog, guild) -> tuple[FakeInteraction, object]:
    interaction = FakeInteraction(guild)
    await Levels.xp_settings.callback(cog, interaction)
    [message] = interaction.sent
    return interaction, message


def field_values(embed) -> list[str]:
    return [f.value for f in embed.fields]


async def submit(view, kind, low, high, guild) -> FakeInteraction:
    modal = XpRangeModal(view, kind)
    modal.min_input._value, modal.max_input._value = low, high
    interaction = FakeInteraction(guild)
    await modal.on_submit(interaction)
    return interaction


async def test_uebersicht(cog, guild):
    _, message = await open_panel(cog, guild)
    assert message["embed"].title == t("xps_title")
    assert field_values(message["embed"]) == [
        t("xps_chat_value", min=15, max=25, cooldown=60),
        t("xps_voice_value", status=f"{NexusEmojis.OFF} {t('xps_voice_off')}", min=5, max=8),
    ]
    assert message["view"].btn_toggle.label == t("xps_btn_voice_enable")


async def test_voice_xp_ein_und_ausschalten(cog, guild):
    _, message = await open_panel(cog, guild)
    interaction = FakeInteraction(guild)
    await message["view"].btn_toggle.callback(interaction)
    assert (await db_levels.get_levels_settings(guild.id))["voice_xp_enabled"]

    [edit] = interaction.edits   # dieselbe Nachricht, neu aufgebaut
    assert field_values(edit["embed"])[1] == t("xps_voice_value", status=f"{NexusEmojis.ON} {t('xps_voice_on')}", min=5, max=8)
    assert edit["view"].btn_toggle.label == t("xps_btn_voice_disable")

    await edit["view"].btn_toggle.callback(FakeInteraction(guild))
    assert not (await db_levels.get_levels_settings(guild.id))["voice_xp_enabled"]


async def test_chat_und_voice_werte_aendern(cog, guild):
    _, message = await open_panel(cog, guild)
    view = message["view"]

    interaction = await submit(view, "chat", "10", " 40 ", guild)
    assert field_values(interaction.edits[0]["embed"])[0] == t("xps_chat_value", min=10, max=40, cooldown=60)
    interaction = await submit(view, "voice", "3", "150", guild)
    assert interaction.sent == []

    settings = await db_levels.get_levels_settings(guild.id)
    assert (settings["xp_min"], settings["xp_max"], settings["voice_xp_min"], settings["voice_xp_max"]) == (10, 40, 3, 150)


@pytest.mark.parametrize("kind, low, high, error", [
    ("chat", "0", "10", ("dash_err_lvl_xp_range", {})),
    ("chat", "5", "1001", ("dash_err_lvl_xp_range", {})),
    ("chat", "abc", "10", ("dash_err_lvl_xp_range", {})),
    ("voice", "5", "151", ("dash_err_lvl_voice_xp_range", {"max": 150})),
    ("voice", "9", "8", ("dash_err_lvl_xp_min_gt_max", {})),
])
async def test_ungueltige_werte(cog, guild, kind, low, high, error):
    _, message = await open_panel(cog, guild)
    interaction = await submit(message["view"], kind, low, high, guild)
    key, params = error
    assert [m["content"] for m in interaction.sent] == [f"{NexusEmojis.ERROR} " + t(key, **params)]
    assert interaction.edits == []
    settings = await db_levels.get_levels_settings(guild.id)
    assert (settings["xp_min"], settings["xp_max"], settings["voice_xp_min"], settings["voice_xp_max"]) == (15, 25, 5, 8)


def test_formular_zeigt_aktuelle_werte_und_grenzen(guild):
    run(db_levels.update_levels_settings(guild.id, voice_xp_min=7, voice_xp_max=12))
    settings = run(db_levels.get_levels_settings(guild.id))
    from cogs.levels import XpSettingsView
    modal = XpRangeModal(XpSettingsView(LANG, settings), "voice")
    assert (modal.min_input.default, modal.max_input.default) == ("7", "12")
    assert modal.max_xp == 150


def test_in_der_hilfe():
    assert MODULE_INFO["Levels"]["commands"]["xp_settings"] == "help_cmd_xp_settings"
