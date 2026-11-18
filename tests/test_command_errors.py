"""Zentraler Fehler-Handler für Slash-Commands (bot.Nexus.on_application_command_error)."""

import logging

import discord
from discord import app_commands
from discord.app_commands.transformers import MemberTransformer

from bot import Nexus
from systems.ui import error_msg
from tests.conftest import TRANSLATIONS
from tests.fakes import FakeGuild, FakeInteraction


class _Command:
    qualified_name = "warn"
    binding = None   # kein Cog mit eigenem Handler


class _User:
    """Was Discord schickt, wenn die ausgewählte Person kein Mitglied ist: nur ein User."""

    def __str__(self):
        return "mysterious3988"


def _interaction(locale: str = "de") -> FakeInteraction:
    interaction = FakeInteraction(FakeGuild(locale=locale))
    interaction.command = _Command()
    return interaction


async def _not_member_error(interaction) -> app_commands.TransformerError:
    # Echter Weg von discord.py: Der Member-Transformer lehnt einen User ab
    try:
        await MemberTransformer().transform(interaction, _User())
    except app_commands.TransformerError as e:
        return e
    raise AssertionError("MemberTransformer hat einen User angenommen")


async def test_person_nicht_auf_dem_server_bekommt_passende_meldung(caplog):
    interaction = _interaction()
    error = await _not_member_error(interaction)

    with caplog.at_level(logging.INFO, logger="__main__"), caplog.at_level(logging.INFO, logger="bot"):
        await Nexus.on_application_command_error(None, interaction, error)

    assert interaction.sent == [{"content": error_msg(TRANSLATIONS["de"]["error_not_member"]), "ephemeral": True}]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("/warn" in r.getMessage() and "mysterious3988" in r.getMessage() for r in caplog.records)


async def test_meldung_in_der_sprache_des_servers():
    interaction = _interaction("en")
    await Nexus.on_application_command_error(None, interaction, await _not_member_error(interaction))
    assert interaction.sent[0]["content"] == error_msg(TRANSLATIONS["en"]["error_not_member"])


async def test_andere_umwandlungsfehler_bleiben_unbehandelte_fehler(caplog):
    interaction = _interaction()
    error = app_commands.TransformerError("abc", discord.AppCommandOptionType.integer, MemberTransformer())

    with caplog.at_level(logging.ERROR):
        await Nexus.on_application_command_error(None, interaction, error)

    assert interaction.sent[0]["content"] == error_msg(TRANSLATIONS["de"]["error_generic"])
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_meldung_in_allen_sprachen():
    for lang, texts in TRANSLATIONS.items():
        assert texts.get("error_not_member"), lang
