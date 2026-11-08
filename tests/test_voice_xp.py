"""Voice-XP (systems/voice_xp.py, Levels-Cog award_voice_xp, Dashboard leveling)."""
import pytest

from cogs.levels import Levels
from database import db_levels, db_settings
from systems.exp_system import xp_for_level
from tests.conftest import query, query_one, run, t
from tests.fakes import FakeMember


@pytest.fixture
def voice(guild, bot):
    """Server mit Level-Modul, eingeschaltetem Voice-XP (Standard 5–8 XP) und einem Voice-Kanal."""
    run(db_settings.set_module(guild.id, "levels", True))
    run(db_levels.update_levels_settings(guild.id, voice_xp_enabled=True))
    guild.zocken = guild.add_voice_channel("Zocken")
    guild.cog = Levels(bot)
    return guild


async def xp_of(guild, member) -> int | None:
    row = await query_one("SELECT xp FROM levels WHERE guild_id = %s AND user_id = %s", guild.id, member.id)
    return row["xp"] if row else None


async def tick(guild):
    await guild.cog.award_voice_xp(guild)


async def test_zwei_aktive_bekommen_xp(voice):
    a, b = voice.zocken.join(voice.admin), voice.zocken.join(voice.member)
    await tick(voice)
    assert 5 <= await xp_of(voice, a) <= 8
    assert 5 <= await xp_of(voice, b) <= 8

    await tick(voice)   # jede Minute kommt etwas dazu
    assert 10 <= await xp_of(voice, a) <= 16


@pytest.mark.parametrize("state", [
    {"self_mute": True}, {"mute": True}, {"self_deaf": True}, {"deaf": True},
])
async def test_stumm_oder_taub_zaehlt_nicht(voice, state):
    """Gemutete bekommen nichts — und zählen auch nicht als zweite Person."""
    voice.zocken.join(voice.admin)
    stumm = voice.zocken.join(voice.member, **state)
    await tick(voice)
    assert await xp_of(voice, voice.admin) is None
    assert await xp_of(voice, stumm) is None

    # Mit einer weiteren aktiven Person gibt es XP — für die beiden Aktiven, nicht für den Gemuteten
    voice.zocken.join(voice.moderator)
    await tick(voice)
    assert await xp_of(voice, voice.admin) and await xp_of(voice, voice.moderator)
    assert await xp_of(voice, stumm) is None


async def test_bots_zaehlen_nicht(voice):
    voice.zocken.join(voice.admin)
    voice.zocken.join(FakeMember(voice, "Musikbot", [], bot=True))
    await tick(voice)
    assert await xp_of(voice, voice.admin) is None


async def test_afk_und_ausgeschlossene_kanaele(voice):
    afk = voice.add_voice_channel("AFK")
    voice.afk_channel_obj = afk
    kategorie_id = 4242
    ausgeschlossen = voice.add_voice_channel("Ruhe")
    in_kategorie = voice.add_voice_channel("Lounge", category_id=kategorie_id)
    await db_levels.set_excluded_channels(voice.id, [ausgeschlossen.id, kategorie_id])

    for channel, (m1, m2) in ((afk, (voice.admin, voice.member)),
                              (ausgeschlossen, (voice.moderator, voice.owner)),
                              (in_kategorie, (voice.add_member("a"), voice.add_member("b")))):
        channel.join(m1)
        channel.join(m2)
    await tick(voice)
    assert await query("SELECT * FROM levels WHERE guild_id = %s", voice.id) == []


async def test_aus_wenn_voice_xp_oder_modul_aus(voice):
    voice.zocken.join(voice.admin)
    voice.zocken.join(voice.member)

    await db_levels.update_levels_settings(voice.id, voice_xp_enabled=False)
    await tick(voice)
    assert await xp_of(voice, voice.admin) is None

    await db_levels.update_levels_settings(voice.id, voice_xp_enabled=True)
    await db_settings.set_module(voice.id, "levels", False)
    await tick(voice)
    assert await xp_of(voice, voice.admin) is None


async def test_level_up_im_voice_chat(voice):
    """Ohne Level-Up-Kanal kommt die Nachricht in den Text-Chat des Voice-Kanals."""
    await db_levels.update_levels_settings(voice.id, voice_xp_min=50, voice_xp_max=50)
    await query("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (%s, %s, %s, 1)",
                voice.id, voice.admin.id, xp_for_level(2) - 10)
    voice.zocken.join(voice.admin)
    voice.zocken.join(voice.member)
    await tick(voice)

    assert (await query_one("SELECT level FROM levels WHERE guild_id = %s AND user_id = %s", voice.id, voice.admin.id))["level"] == 2
    [message] = voice.zocken.messages.values()
    assert voice.admin.mention in message.embeds[0].description


# ── Dashboard ───────────────────────────────────────────────────────────────────────────────────

async def test_dashboard_einstellungen(dash, guild):
    data = await dash.ok("leveling", "get-xp-settings")
    assert (data["voice_xp_enabled"], data["voice_xp_min"], data["voice_xp_max"]) == (False, 5, 8)   # Standard: aus

    assert await dash.ok("leveling", "set-voice-xp", {"enabled": True, "xp_min": 3, "xp_max": 6}) == \
        {"voice_xp_enabled": True, "voice_xp_min": 3, "voice_xp_max": 6}
    data = await dash.ok("leveling", "get-xp-settings")
    assert (data["voice_xp_enabled"], data["voice_xp_min"], data["voice_xp_max"]) == (True, 3, 6)
    assert (data["xp_min"], data["xp_max"]) == (15, 25)   # Chat-XP unverändert


async def test_dashboard_ungueltige_werte(dash, guild):
    for payload, error in (
        ({"enabled": True, "xp_min": 0, "xp_max": 5}, t("dash_err_lvl_voice_xp_range", max=150)),
        ({"enabled": True, "xp_min": 5, "xp_max": 151}, t("dash_err_lvl_voice_xp_range", max=150)),
        ({"enabled": True, "xp_min": "5", "xp_max": 8}, t("dash_err_lvl_voice_xp_range", max=150)),
        ({"enabled": True, "xp_min": 9, "xp_max": 8}, t("dash_err_lvl_xp_min_gt_max")),
        ({"enabled": "ja", "xp_min": 5, "xp_max": 8}, t("dash_err_invalid_request")),
    ):
        assert await dash.error("leveling", "set-voice-xp", payload) == error, payload
    assert (await dash.ok("leveling", "get-xp-settings"))["voice_xp_enabled"] is False


async def test_dashboard_voice_kanal_ausschliessen(dash, guild):
    zocken = guild.add_voice_channel("Zocken")
    targets = (await dash.ok("leveling", "get-excluded"))["targets"]
    assert {"id": str(zocken.id), "name": "Zocken", "type": "channel", "category": None} in targets

    assert await dash.ok("leveling", "set-excluded", {"channel_ids": [str(zocken.id)]}) == {"excluded_ids": [str(zocken.id)]}


async def test_hoechstwert_gilt_auch_fuer_alte_werte(voice):
    """Vor dem Höchstwert gespeicherte 1000 XP/min werden beim Vergeben auf 150 begrenzt."""
    await db_levels.update_levels_settings(voice.id, voice_xp_min=1000, voice_xp_max=1000)
    voice.zocken.join(voice.admin)
    voice.zocken.join(voice.member)
    await tick(voice)
    assert await xp_of(voice, voice.admin) == 150


async def test_dashboard_voice_hoechstwert(dash, guild):
    assert (await dash.ok("leveling", "set-voice-xp", {"enabled": True, "xp_min": 150, "xp_max": 150}))["voice_xp_max"] == 150
