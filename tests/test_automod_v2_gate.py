"""Testphase AutoMod 2.0: Mit RELEASED = False gibt es neue Regeln, Einstellungen je Regel und das
Übernehmen fremder Regeln nur auf dem Dev-Server (systems/automod_rules.v2_available). Die Sperre sitzt im Bot."""
import discord
import pytest

from systems import automod_links, automod_rules
from tests.conftest import t

SPAM = discord.AutoModRuleTriggerType.spam
PRESET = discord.AutoModRuleTriggerType.keyword_preset
GESPERRT = t("dash_err_am_v2_unavailable")


@pytest.fixture(autouse=True)
def testphase(monkeypatch):
    monkeypatch.setattr(automod_rules, "RELEASED", False)


def als_dev_server(monkeypatch, guild):
    monkeypatch.setattr(automod_rules, "DEV_GUILD_ID", guild.id)


async def test_normaler_server_nur_die_bisherigen_regeln(dash, guild):
    assert (await dash.ok("automod", "status"))["v2"] is False
    # Bisheriges geht weiter: Schalter und Wortliste
    assert (await dash.ok("automod", "toggle-rule", {"rule_key": "spam"}))["enabled"] is True
    assert (await dash.ok("automod", "set-keywords", {"keywords": ["kekse"]}))["keywords"] == ["kekse"]

    for kind in ("presets", "names", "links"):
        assert await dash.error("automod", "toggle-rule", {"rule_key": kind}) == GESPERRT
    assert await dash.error("automod", "rule-settings", {"rule_key": "spam", "custom_message": "Hi"}) == GESPERRT
    assert await dash.error("automod", "rule-content", {"rule_key": "presets", "presets": ["slurs"]}) == GESPERRT
    assert await dash.error("automod", "rule-content", {"rule_key": "links", "block_invites": False}) == GESPERRT
    guild.add_automod_rule("Wortlisten vom Admin", PRESET, by_nexus=False)
    assert await dash.error("automod", "adopt-rule", {"rule_key": "presets"}) == GESPERRT
    assert len([r for r in guild.automod_rules if r.creator_id == guild.me.id]) == 2   # nur Spam und Wortfilter


async def test_vorhandene_neue_regel_ausschalten_geht_einschalten_nicht(dash, guild):
    rule = guild.add_automod_rule("Wortlisten", PRESET)   # z. B. vor der Testphase angelegt
    assert (await dash.ok("automod", "toggle-rule", {"rule_key": "presets"}))["enabled"] is False
    assert await dash.error("automod", "toggle-rule", {"rule_key": "presets"}) == GESPERRT
    assert rule.enabled is False


async def test_dev_server_hat_alles(dash, guild, monkeypatch):
    als_dev_server(monkeypatch, guild)
    assert (await dash.ok("automod", "status"))["v2"] is True
    await dash.ok("automod", "toggle-rule", {"rule_key": "presets"})
    await dash.ok("automod", "toggle-rule", {"rule_key": "links"})
    await dash.ok("automod", "rule-content", {"rule_key": "links", "partner_links": ["abc123"]})
    await dash.ok("automod", "rule-settings", {"rule_key": "presets", "custom_message": "Bitte freundlich bleiben."})


async def test_freigabe_fuer_alle(dash, monkeypatch):
    monkeypatch.setattr(automod_rules, "RELEASED", True)
    assert (await dash.ok("automod", "status"))["v2"] is True
    await dash.ok("automod", "toggle-rule", {"rule_key": "names"})


async def test_link_sync_und_event_ohne_link_filter_tun_nichts(guild):
    # Auf normalen Servern gibt es keinen Link-Filter — Einladungs-Ereignisse ändern dort nichts
    assert await automod_links.sync(guild) is False
    assert not [c for c in guild.calls if c[0].startswith("automod")]
