"""/diagnose und Dashboard-Hinweise: fehlendes Recht „Events erstellen“ beim Streamplan-Abgleich
(systems/health_checks.py, cogs/diagnose.py)."""
import discord

from cogs.diagnose import Diagnose
from emojis import NexusEmojis
from tests.conftest import t
from tests.fakes import FakeInteraction
from tests.stream_schedule_fakes import follow


def ohne_events_recht(guild):
    perms = guild.bot_role.permissions
    guild.bot_role.permissions = discord.Permissions(perms.value & ~discord.Permissions(create_events=True).value)


async def health(dash) -> list[dict]:
    return (await dash.ok("guild", "health-issues"))["issues"]


async def diagnose_embed(bot, guild) -> discord.Embed:
    interaction = FakeInteraction(guild)
    await Diagnose.diagnose.callback(Diagnose(bot), interaction)
    return interaction.sent[-1]["embed"]


async def test_abgleich_an_ohne_recht_meldet_dashboard_und_diagnose(dash, bot, guild):
    await follow(guild)
    ohne_events_recht(guild)

    issue = {"section": "alerts", "severity": "error", "text": t("diag_schedule_missing_perm")}
    assert issue in await health(dash)

    fields = {f.name: f.value for f in (await diagnose_embed(bot, guild)).fields}
    alerts = fields[f"{NexusEmojis.Modules.BROADCAST} {t('diag_section_alerts')}"]
    assert f"{NexusEmojis.ERROR} {t('diag_schedule_missing_perm')}" in alerts


async def test_mit_recht_kein_hinweis(dash, guild):
    await follow(guild)
    assert all(i["text"] != t("diag_schedule_missing_perm") for i in await health(dash))


async def test_abgleich_aus_kein_hinweis_auch_ohne_recht(dash, guild):
    await follow(guild, sync=False)   # nur Stream-Alert, Streamplan nicht eingeschaltet
    ohne_events_recht(guild)
    assert all(i["text"] != t("diag_schedule_missing_perm") for i in await health(dash))


async def test_abschnitte_mit_eigenen_emojis(bot, guild):
    await follow(guild)
    names = [f.name for f in (await diagnose_embed(bot, guild)).fields]
    assert f"{NexusEmojis.Modules.TICKET} {t('diag_section_tickets')}" in names
    assert f"{NexusEmojis.Modules.BROADCAST} {t('diag_section_alerts')}" in names
    assert not any("🎫" in n or "📡" in n for n in names)
