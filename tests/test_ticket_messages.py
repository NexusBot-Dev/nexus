"""Fehlermeldungen im Ticketsystem (cogs/tickets.py) — Emoji kommt aus emojis.py, nicht aus dem Text."""
import types

from cogs.tickets import Tickets, _category_gone_text, _panel_error_text
from emojis import NexusEmojis
from systems.ticket_panel import PanelError
from tests.conftest import query, set_premium, t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


def test_panel_fehler():
    assert _panel_error_text(PanelError("no_categories"), LANG) == f"{NexusEmojis.ERROR} {t('ticket_panel_no_categories')}"
    channel = types.SimpleNamespace(mention="<#1>")
    assert _panel_error_text(PanelError("no_permission"), LANG, channel) == \
        f"{NexusEmojis.ERROR} {t('ticket_panel_no_permission', channel='<#1>')}"
    assert _panel_error_text(PanelError("unbekannt"), LANG) == f"{NexusEmojis.ERROR} {t('error_generic')}"


def test_kategorie_weg():
    assert _category_gone_text(LANG) == f"{NexusEmojis.ERROR} {t('ticket_category_not_found')}"


async def test_kategorie_limit_mit_premium(guild):
    """Mit Premium am Limit: normale Fehlermeldung (ohne Premium kommt der Premium-Hinweis, siehe test_premium_ui)."""
    await set_premium(guild)
    for i in range(10):
        await query("INSERT INTO ticket_categories (guild_id, name, category_channel_id) VALUES (%s, %s, 1)", guild.id, f"Kat {i}")
    interaction = FakeInteraction(guild)
    await Tickets.add_category.callback(types.SimpleNamespace(), interaction)
    assert [m["content"] for m in interaction.sent] == \
        [f"{NexusEmojis.ERROR} {t('ticket_category_limit_reached', limit=10, premium_hint='')}"]
