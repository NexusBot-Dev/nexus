"""Module ein- und ausschalten (dashboard/modules.py, cogs/utils.toggle_module, db_settings.set_modules)."""
import asyncio
import json

from cogs.utils import toggle_module
from database import db_settings
from tests.conftest import add_guild, query, query_one, t
from translations import TRANSLATIONS


async def stored_modules(guild) -> dict:
    """Was wirklich in der Datenbank steht — am Cache vorbei."""
    row = await query_one("SELECT modules FROM guild_settings WHERE guild_id = %s", guild.id)
    return json.loads(row["modules"])


async def toggle(dash, **changes):
    return await dash.ok("modules", "toggle", {"changes": changes})


async def test_umschalten(dash, guild):
    data = await toggle(dash, levels=True, automod=False)
    assert data["modules"] == {**db_settings.DEFAULT_MODULES, "levels": True, "automod": False}
    # Echte JSON-Booleans, keine 1/0 — und die übrigen Module bleiben, wie sie waren
    assert await stored_modules(guild) == {"moderation": True, "automod": False, "stream_alerts": True,
                                           "tickets": True, "levels": True}
    assert await db_settings.is_module_enabled(guild.id, "levels") is True


async def test_gleichzeitige_aenderungen_ueberschreiben_sich_nicht(dash, guild):
    """Zwei Admins schalten fast zeitgleich verschiedene Module um."""
    await toggle(dash, levels=True)   # Einstellungen liegen jetzt im Cache — so wie im Betrieb
    await asyncio.gather(toggle(dash, welcome=True), toggle(dash, embeds=True), toggle(dash, automod=False))
    modules = await stored_modules(guild)
    assert (modules["levels"], modules["welcome"], modules["embeds"], modules["automod"]) == (True, True, True, False)


async def test_dashboard_und_befehl_gleichzeitig(dash, guild, bot):
    lang = TRANSLATIONS["de"]
    await db_settings.get_guild(guild.id)   # Cache füllen
    results = await asyncio.gather(
        toggle_module(bot, guild, "reaction_roles", True, lang),
        toggle(dash, boosts=True),
        toggle_module(bot, guild, "welcome", True, lang),
    )
    assert results[0] == results[2] == (True, "")
    modules = await stored_modules(guild)
    assert (modules["reaction_roles"], modules["boosts"], modules["welcome"]) == (True, True, True)


async def test_server_ohne_einstellungen(dash, bot):
    neu = await add_guild(bot)
    await query("DELETE FROM guild_settings WHERE guild_id = %s", neu.id)
    data = await dash.ok("modules", "toggle", {"changes": {"levels": True}}, guild=neu, user=neu.admin)
    assert data["modules"]["levels"] is True
    assert (await stored_modules(neu))["levels"] is True


async def test_ungueltig(dash, guild):
    before = await stored_modules(guild)
    invalid = t("dash_err_invalid_request")
    for changes in ({}, {"gibt_es_nicht": True}, {"levels": "false"}, {"levels": 1}, None):
        assert await dash.error("modules", "toggle", {"changes": changes}) == invalid, changes
    assert await dash.error("modules", "toggle", {"changes": {"moderation": False, "levels": True}}) == \
        t("dash_err_modules_always_on", module="Moderation")
    assert await dash.error("modules", "toggle", {"changes": {"embeds": False}}) == t("dash_err_modules_always_on", module="Embeds")
    assert await dash.error("modules", "toggle", {"changes": {"giveaways": False}}) == invalid   # hat keinen Schalter
    assert await stored_modules(guild) == before
