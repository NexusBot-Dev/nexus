"""AutoMod-Regeln in Discord (systems/automod_rules.py): Erkennung an der Ersteller-ID, neutrale
Namen, rückwirkendes Umbenennen und Pausieren beim Ausschalten des Moduls."""
import discord

from cogs.utils import toggle_module
from dashboard import registry
from tests.conftest import query, t
from tests.fakes import FakeGuild
from translations import TRANSLATIONS

SPAM = discord.AutoModRuleTriggerType.spam
MENTIONS = discord.AutoModRuleTriggerType.mention_spam
KEYWORD = discord.AutoModRuleTriggerType.keyword
LANG = TRANSLATIONS["de"]


def rule_names(guild) -> list[str]:
    return sorted(r.name for r in guild.automod_rules)


# ── Erkennen und Umbenennen ─────────────────────────────────────────────────────────────────────

async def test_status_erkennt_nexus_regeln_am_ersteller_nicht_am_namen(dash, guild):
    guild.add_automod_rule("Mein Spamschutz", SPAM)                          # von Nexus, vom Admin umbenannt
    guild.add_automod_rule("Nexus — Keyword-Filter", KEYWORD, by_nexus=False, keyword_filter=["x"])   # fremd

    data = await dash.ok("automod", "status")
    assert data["spam_enabled"] is True
    assert (data["keywords_enabled"], data["keywords"]) == (False, [])
    # Weder die vom Admin umbenannte noch die fremde Regel wurde angefasst
    assert rule_names(guild) == ["Mein Spamschutz", "Nexus — Keyword-Filter"]


async def test_alte_namen_werden_beim_naechsten_kontakt_neutral(dash, guild):
    guild.add_automod_rule("Nexus — Anti-Spam", SPAM)
    guild.add_automod_rule("Nexus — Mention-Spam", MENTIONS, mention_limit=5, enabled=False)
    guild.add_automod_rule("Nexus — Keyword-Filter", KEYWORD, keyword_filter=["spam"])

    data = await dash.ok("automod", "status")
    assert rule_names(guild) == sorted([
        t("automod_rule_name_spam"), t("automod_rule_name_mentions"), t("automod_rule_name_keywords"),
    ])
    # Zustand und Inhalt bleiben erhalten
    assert (data["spam_enabled"], data["mentions_enabled"], data["keywords"]) == (True, False, ["spam"])


async def test_neue_regeln_bekommen_neutrale_namen_in_der_serversprache(dash, guild):
    await dash.ok("automod", "toggle-rule", {"rule_key": "spam"})
    await dash.ok("automod", "set-keywords", {"keywords": ["Böse"]})
    assert rule_names(guild) == sorted([t("automod_rule_name_spam"), t("automod_rule_name_keywords")])
    assert not any("Nexus" in name for name in rule_names(guild))


async def test_fremde_regel_belegt_den_platz(dash, guild):
    guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    assert await dash.error("automod", "toggle-rule", {"rule_key": "spam"}) == t("dash_err_am_foreign_rule")
    assert len(guild.automod_rules) == 1

    # Wortfilter darf Discord sechs Mal pro Server — eine fremde Wortfilter-Regel stört nicht
    guild.add_automod_rule("Eigene Wörter", KEYWORD, by_nexus=False, keyword_filter=["a"])
    assert (await dash.ok("automod", "toggle-rule", {"rule_key": "keywords"}))["enabled"] is True
    assert len(guild.automod_rules) == 3


async def test_umschalten_findet_umbenannte_nexus_regel(dash, guild):
    rule = guild.add_automod_rule("Spam weg", SPAM)
    assert (await dash.ok("automod", "toggle-rule", {"rule_key": "spam"}))["enabled"] is False
    assert (await dash.ok("automod", "toggle-rule", {"rule_key": "spam"}))["enabled"] is True
    assert guild.automod_rules == [rule]   # keine zweite Regel angelegt


# ── Modul aus und wieder an ─────────────────────────────────────────────────────────────────────

async def test_modul_aus_pausiert_regeln_und_an_stellt_genau_sie_wieder_her(dash, guild):
    spam = guild.add_automod_rule("Anti-Spam", SPAM)
    mentions = guild.add_automod_rule("Mention-Spam", MENTIONS, mention_limit=5, enabled=False)   # war schon aus
    words = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["a", "b"])
    fremd = guild.add_automod_rule("Fremd", KEYWORD, by_nexus=False, keyword_filter=["c"])

    await dash.ok("modules", "toggle", {"changes": {"automod": False}})
    assert (spam.enabled, mentions.enabled, words.enabled, fremd.enabled) == (False, False, False, True)
    assert words.trigger.keyword_filter == ["a", "b"]   # nur deaktiviert, nichts gelöscht
    assert len(guild.automod_rules) == 4

    await dash.ok("modules", "toggle", {"changes": {"automod": True}})
    assert (spam.enabled, mentions.enabled, words.enabled, fremd.enabled) == (True, False, True, True)

    # Ein zweites Einschalten ändert nichts mehr (nichts mehr gemerkt)
    calls_before = len(guild.calls)
    await dash.ok("modules", "toggle", {"changes": {"automod": True}})
    assert len(guild.calls) == calls_before


async def test_modul_umschalten_per_slash_command(bot, guild):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    assert await toggle_module(bot, guild, "automod", False, LANG) == (True, "")
    assert rule.enabled is False
    assert await toggle_module(bot, guild, "automod", True, LANG) == (True, "")
    assert rule.enabled is True


async def test_modul_aus_klappt_auch_ohne_recht_auf_die_regeln(dash):
    # Fehlt „Server verwalten“, bleiben die Regeln wie sie sind — das Modul wird trotzdem ausgeschaltet
    guild = FakeGuild(bot_permissions=discord.Permissions(view_channel=True, send_messages=True))
    await query(
        "INSERT INTO guild_settings (guild_id, locale, modules) VALUES (%s, 'de', %s)",
        guild.id, '{"automod": true}',
    )
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    registry.get_bot().add_guild(guild)
    data = await dash.ok("modules", "toggle", {"changes": {"automod": False}}, guild=guild, user=guild.owner)
    assert data["modules"]["automod"] is False
    assert rule.enabled is True
