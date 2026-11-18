"""Link-Filter (systems/automod_links.py): Einladungen auf fremde Server und gesperrte Domains blocken,
eigene Einladungen, Vanity, Partnerlinks und den Nexus-Support-Server erlauben — automatisch abgeglichen."""
import asyncio
import logging
import re
from datetime import timedelta

import discord
import pytest

from config import SUPPORT_INVITE_CODE
from systems import automod_links, automod_rules
from database import db_settings
from tests.conftest import query, t
from tests.fakes import FakeBot

KEYWORD = discord.AutoModRuleTriggerType.keyword
SUPPORT = f"*/{SUPPORT_INVITE_CODE.lower()}*"


@pytest.fixture(autouse=True)
def schnell(monkeypatch):
    monkeypatch.setattr(automod_links, "SYNC_DELAY", 0.05)


async def link_rule(dash, guild):
    await dash.ok("automod", "toggle-rule", {"rule_key": "links"})
    [rule] = [r for r in guild.automod_rules if r.trigger.type == KEYWORD and r.name == t("automod_rule_name_links")]
    return rule


async def content(dash, **fields):
    return await dash.ok("automod", "rule-content", {"rule_key": "links", **fields})


def edits(guild, rule) -> int:
    return len([c for c in guild.calls if c[0] == "automod.edit" and c[1] == rule.id])


# ── Anlegen ─────────────────────────────────────────────────────────────────────────────────────

async def test_link_filter_ist_eigene_regel_neben_dem_wortfilter(dash, guild):
    await dash.ok("automod", "set-keywords", {"keywords": ["kekse"]})
    rule = await link_rule(dash, guild)
    assert rule.trigger.regex_patterns == [automod_rules.INVITE_PATTERN]
    assert await query("SELECT kind FROM automod_rules WHERE rule_id = %s", rule.id) == [{"kind": "links"}]

    data = await dash.ok("automod", "status")
    assert data["keywords"] == ["kekse"]                                  # Wortfilter unverändert
    assert data["rules"]["links"]["block_invites"] is True
    assert data["rules"]["links"]["allow_support"] is True
    assert data["rules"]["keywords"]["keywords"] == ["kekse"]
    assert len(guild.automod_rules) == 2


async def test_support_server_vanity_und_eigene_einladungen_sofort_erlaubt(dash, guild):
    guild.vanity_url_code = "MeinServer"
    guild.add_invite("Dauer1")
    rule = await link_rule(dash, guild)
    assert rule.trigger.allow_list == [SUPPORT, "*/meinserver", "*/meinserver?*", "*/dauer1*"]


# ── Reihenfolge und 100er-Grenze ────────────────────────────────────────────────────────────────

async def test_reihenfolge_und_grenze(dash, guild, monkeypatch):
    monkeypatch.setattr(automod_links, "MAX_ALLOW_LIST", 6)
    guild.vanity_url_code = "vanity"
    guild.add_invite("bald", expires_in=timedelta(hours=1))
    guild.add_invite("lange", expires_in=timedelta(days=7))
    guild.add_invite("dauer")
    guild.add_invite("abgelaufen", expires_in=timedelta(seconds=-5))
    guild.add_invite("aufgebraucht", max_uses=3, uses=3)
    await link_rule(dash, guild)
    data = await content(dash, partner_links=["https://discord.gg/Partner1"])

    rule = await link_rule(dash, guild)
    # Support, Vanity (2), Partner, ohne Ablauf, dann die am längsten gültige — „bald“ fällt über die Grenze
    assert rule.trigger.allow_list == [SUPPORT, "*/vanity", "*/vanity?*", "*/partner1*", "*/dauer*", "*/lange*"]
    assert data["allow_list_count"] == 6


# ── Ereignisse und Abgleich ─────────────────────────────────────────────────────────────────────

async def test_neue_und_geloeschte_einladungen_gebuendelt(dash, guild, monkeypatch):
    rule = await link_rule(dash, guild)
    syncs = []
    real_sync = automod_links.sync
    monkeypatch.setattr(automod_links, "sync", lambda g, lang=None: syncs.append(g.id) or real_sync(g, lang))
    before = edits(guild, rule)
    a = guild.add_invite("neu1", expires_in=timedelta(days=1))
    guild.add_invite("neu2", expires_in=timedelta(days=2))
    automod_links.schedule_sync(guild)
    automod_links.schedule_sync(guild)   # zweites Ereignis im selben Zeitfenster
    await asyncio.sleep(0.2)
    assert syncs == [guild.id]                     # zwei Ereignisse, ein Abgleich
    assert edits(guild, rule) == before + 1
    assert rule.trigger.allow_list == [SUPPORT, "*/neu2*", "*/neu1*"]

    guild.invite_list.remove(a)
    automod_links.schedule_sync(guild)
    await asyncio.sleep(0.2)
    assert rule.trigger.allow_list == [SUPPORT, "*/neu2*"]


async def test_ohne_aenderung_kein_aufruf(dash, guild):
    rule = await link_rule(dash, guild)
    before = edits(guild, rule)
    assert await automod_links.sync(guild) is False
    assert edits(guild, rule) == before


async def test_regelmaessiger_abgleich_raeumt_abgelaufene_auf(dash, guild):
    invite = guild.add_invite("kurz", expires_in=timedelta(hours=1))
    rule = await link_rule(dash, guild)
    assert "*/kurz*" in rule.trigger.allow_list
    invite.expires_at = discord.utils.utcnow() - timedelta(seconds=1)   # abgelaufen, Discord meldet das nicht
    await automod_links.sync_all(FakeBot(guild), pause=0)
    assert "*/kurz*" not in rule.trigger.allow_list


async def test_ohne_recht_auf_einladungen_bleiben_support_und_partner(dash, guild):
    rule = await link_rule(dash, guild)
    await content(dash, partner_links=["abc123"])
    guild.add_invite("geheim")
    perms = guild.bot_role.permissions
    guild.bot_role.permissions = discord.Permissions(perms.value & ~discord.Permissions(manage_guild=True).value)
    assert await automod_links.allow_entries(guild, await automod_links.db_automod.get_link_settings(guild.id)) == [SUPPORT, "*/abc123*"]
    guild.bot_role.permissions = perms
    assert rule.trigger.allow_list == [SUPPORT, "*/abc123*"]


# ── Einstellungen ───────────────────────────────────────────────────────────────────────────────

async def test_support_server_abwaehlbar(dash, guild):
    rule = await link_rule(dash, guild)
    data = await content(dash, allow_support=False)
    assert data["allow_support"] is False
    assert SUPPORT not in rule.trigger.allow_list


async def test_einladungen_nicht_blocken(dash, guild):
    guild.add_invite("dauer")
    rule = await link_rule(dash, guild)
    await content(dash, block_invites=False, blocked_domains=["example.com"])
    assert automod_rules.INVITE_PATTERN not in rule.trigger.regex_patterns
    assert rule.trigger.allow_list == []   # ohne Einladungs-Sperre braucht es keine Ausnahmen


async def test_domains_werden_bereinigt_und_treffen_genau(dash, guild):
    rule = await link_rule(dash, guild)
    data = await content(dash, blocked_domains=["https://www.Example.com/pfad", "example.com", " evil.org "])
    assert data["blocked_domains"] == ["example.com", "evil.org"]
    [invite_pattern, domains] = rule.trigger.regex_patterns
    assert invite_pattern == automod_rules.INVITE_PATTERN
    assert len(domains) <= 260
    pattern = re.compile(domains)   # Rusts Regex verhält sich hier wie Pythons
    for hit in ("https://example.com", "http://sub.example.com/x", "HTTPS://EVIL.ORG?x=1", "https://example.com:8080"):
        assert pattern.search(hit), hit
    for miss in ("https://notexample.com", "https://example.com.fake.net", "example.com ohne Schema"):
        assert not pattern.search(miss), miss


async def test_viele_domains_auf_mehrere_muster(dash, guild):
    rule = await link_rule(dash, guild)
    domains = [f"domain-nummer-{i}.example" for i in range(40)]
    await content(dash, blocked_domains=domains)
    assert len(rule.trigger.regex_patterns) > 2
    assert all(len(p) <= 260 for p in rule.trigger.regex_patterns)
    assert await dash.error("automod", "rule-content", {"rule_key": "links", "blocked_domains": [f"d{i}-sehr-langer-name.example" for i in range(300)]}) == t("dash_err_am_too_many_domains")


async def test_eingaben_pruefen(dash, guild):
    await link_rule(dash, guild)
    err = lambda **f: dash.error("automod", "rule-content", {"rule_key": "links", **f})   # noqa: E731
    assert await err(blocked_domains=["kein domain"]) == t("dash_err_am_domain_invalid", domain="kein domain")
    assert await err(partner_links=["https://example.com/x"]) == t("dash_err_am_partner_invalid", link="https://example.com/x")
    assert await err(partner_links=[f"code{i}" for i in range(26)]) == t("dash_err_am_too_many_partners", max=25)
    assert await err(block_invites="ja") == t("dash_err_invalid_request")
    assert await dash.error("automod", "rule-content", {"rule_key": "links"}) == t("dash_err_invalid_request")


async def test_partnerlinks_in_allen_schreibweisen(dash, guild):
    await link_rule(dash, guild)
    data = await content(dash, partner_links=[
        "https://discord.gg/AbC123", "discord.com/invite/xyz-9", "https://discordapp.com/invite/q1w2", "plaincode",
        "https://discord.gg/AbC123?event=1",
    ])
    assert data["partner_codes"] == ["AbC123", "xyz-9", "q1w2", "plaincode"]


async def test_timeout_und_einstellungen_wie_beim_wortfilter(dash, guild):
    await link_rule(dash, guild)
    data = await dash.ok("automod", "rule-settings", {"rule_key": "links", "timeout_minutes": 15, "custom_message": "Keine Werbung!"})
    assert (data["timeout_minutes"], data["custom_message"]) == (15, "Keine Werbung!")


async def test_link_filter_wird_geloggt_als_link_filter(dash, guild):
    rule = await link_rule(dash, guild)
    assert await automod_rules.nexus_rule_kind(guild, rule.id, {}) == "links"


# ── Einladungen auf Servern ohne Link-Filter ────────────────────────────────────────────────────

def ohne_server_verwalten(guild):
    perms = guild.bot_role.permissions
    guild.bot_role.permissions = discord.Permissions(perms.value & ~discord.Permissions(manage_guild=True).value)


async def test_einladung_ohne_link_filter_fragt_discord_nicht(dash, guild, caplog):
    """Einladungs-Ereignisse kommen von jedem Server: ohne Link-Filter kein Abruf der Regeln (403 ohne Recht)."""
    await dash.ok("automod", "set-keywords", {"keywords": ["kekse"]})   # andere Nexus-Regel reicht nicht
    ohne_server_verwalten(guild)
    before = guild.automod_fetches
    with caplog.at_level(logging.WARNING, logger="systems.automod_links"):
        automod_links.schedule_sync(guild)
        await asyncio.sleep(0.2)
    assert guild.automod_fetches == before
    assert caplog.records == []


async def test_einladung_bei_pausiertem_automod_kein_abgleich(dash, guild):
    rule = await link_rule(dash, guild)
    await db_settings.set_module(guild.id, "automod", False)
    before = guild.automod_fetches
    guild.add_invite("neu")
    automod_links.schedule_sync(guild)
    await asyncio.sleep(0.2)
    assert guild.automod_fetches == before
    assert "*/neu*" not in rule.trigger.allow_list


async def test_link_filter_ohne_recht_nur_warnung(dash, guild, caplog):
    await link_rule(dash, guild)
    ohne_server_verwalten(guild)
    with caplog.at_level(logging.WARNING, logger="systems.automod_links"):
        automod_links.schedule_sync(guild)
        await asyncio.sleep(0.2)
        await automod_links.sync_all(FakeBot(guild), pause=0)
    assert [r.levelname for r in caplog.records] == ["WARNING", "WARNING"]
    assert all(r.exc_info is None for r in caplog.records)   # kein Stacktrace
