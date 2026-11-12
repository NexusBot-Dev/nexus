"""Giveaways in Discord (cogs/giveaways.py): Abbrechen, Beitreten, Formular-Prüfungen."""
import types
from datetime import datetime, timedelta, timezone

from cogs.giveaways import DURATIONS, AddGiveawayModal, Giveaways, GiveawayJoinView, post_giveaway
from database import db_giveaways
from emojis import NexusEmojis
from tests.conftest import t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


def cog(bot) -> Giveaways:
    """Cog ohne Konstruktor — der würde die Minuten-Schleife starten."""
    instance = Giveaways.__new__(Giveaways)
    instance.bot = bot
    return instance


async def start(guild, channel) -> dict:
    end = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    await post_giveaway(guild_id=guild.id, host_id=guild.admin.id, channel=channel, prize="Steam-Key",
                        description=None, winners_count=1, end_utc=end, lang=LANG)
    [message] = channel.messages.values()
    return await db_giveaways.get_giveaway(guild.id, message.id)


async def test_abbrechen_markiert_das_embed(guild, bot):
    channel = guild.add_text_channel("giveaways")
    bot.add_guild(guild)
    giveaway = await start(guild, channel)

    assert await cog(bot)._finish_giveaway(giveaway, "cancelled") is True
    [message] = channel.messages.values()
    embed = message.embeds[0]
    assert embed.title == f"{NexusEmojis.CANCELLED} Steam-Key"
    assert embed.fields[1].value == f"{NexusEmojis.CANCELLED} {t('giveaway_cancelled')}"
    assert (await db_giveaways.get_giveaway(guild.id, message.id))["status"] == "cancelled"


async def test_beitreten_zu_geloeschtem_giveaway(guild):
    interaction = FakeInteraction(guild)
    interaction.message = types.SimpleNamespace(id=123)
    await GiveawayJoinView(LANG, enabled=True).join_giveaway.callback(interaction)
    assert [m["content"] for m in interaction.sent] == [f"{NexusEmojis.ERROR} {t('giveaway_not_found')}"]


async def test_formular_ungueltige_gewinnerzahl(guild):
    modal = AddGiveawayModal(guild.id, LANG)
    modal.prize_input._value = "Steam-Key"
    modal.duration_select._values = [str(DURATIONS[0][0])]
    modal.winners_count_input._value = "0"
    interaction = FakeInteraction(guild)
    await modal.on_submit(interaction)
    assert [m["content"] for m in interaction.sent] == [f"{NexusEmojis.ERROR} {t('giveaway_winners_count_invalid')}"]
