"""Fehlermeldungen der Moderations-Befehle (cogs/moderation.py) — Emoji aus emojis.py."""
import types

from cogs.moderation import Moderation
from emojis import NexusEmojis
from tests.conftest import t
from tests.fakes import FakeInteraction, not_found


def contents(interaction) -> list[str]:
    return [m["content"] for m in interaction.sent]


async def test_unban_ohne_gueltigen_nutzer(guild, bot):
    interaction = FakeInteraction(guild)
    await Moderation.unban.callback(Moderation(bot), interaction, user="kein-nutzer")
    assert contents(interaction) == [f"{NexusEmojis.ERROR} {t('unban_invalid_user')}"]


async def test_unban_nicht_gebannt(guild, bot):
    async def fetch_user(user_id):
        return types.SimpleNamespace(id=user_id)

    async def unban(user, reason=None):
        raise not_found(10026)   # Unknown Ban

    bot.fetch_user = fetch_user
    guild.unban = unban
    interaction = FakeInteraction(guild)
    await Moderation.unban.callback(Moderation(bot), interaction, user="123")
    assert contents(interaction) == [f"{NexusEmojis.ERROR} {t('unban_not_found')}"]


async def test_timeout_negative_dauer(guild, bot):
    interaction = FakeInteraction(guild)
    cog = Moderation(bot)
    cog._hierarchy_error = lambda *args, **kwargs: None   # Rangprüfung ist hier nicht Thema
    await Moderation.timeout.callback(cog, interaction, member=guild.member, duration=-5)
    assert contents(interaction) == [f"{NexusEmojis.ERROR} {t('timeout_negative')}"]
