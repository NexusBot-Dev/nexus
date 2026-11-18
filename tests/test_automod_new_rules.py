"""Neue AutoMod-Regeln (systems/automod_rules.py): fertige Wortlisten, Namensfilter und das Übernehmen
fremder Regeln auf Plätzen, die Discord nur einmal pro Server vergibt."""
import types

import discord
import pytest

from database import db_settings
from systems import automod_log
from tests.conftest import query, run, t

PRESET = discord.AutoModRuleTriggerType.keyword_preset
PROFILE = discord.AutoModRuleTriggerType.member_profile
SPAM = discord.AutoModRuleTriggerType.spam
KEYWORD = discord.AutoModRuleTriggerType.keyword
BLOCK = discord.AutoModRuleActionType.block_message
INTERACTIONS = discord.AutoModRuleActionType.block_member_interactions


async def toggle(dash, kind):
    return await dash.ok("automod", "toggle-rule", {"rule_key": kind})


async def content(dash, kind, **fields):
    return await dash.ok("automod", "rule-content", {"rule_key": kind, **fields})


def only(guild, trigger_type):
    [rule] = [r for r in guild.automod_rules if r.trigger.type == trigger_type]
    return rule


@pytest.fixture
def log_channel_for_log(guild):
    channel = guild.add_text_channel("mod-log")
    run(db_settings.update_guild(guild.id, log_channel_id=channel.id))
    return channel


# ── Fertige Wortlisten ──────────────────────────────────────────────────────────────────────────

async def test_wortlisten_starten_nur_mit_slurs(dash, guild):
    await toggle(dash, "presets")
    rule = only(guild, PRESET)
    assert rule.name == t("automod_rule_name_presets")
    assert (rule.trigger.presets.slurs, rule.trigger.presets.profanity, rule.trigger.presets.sexual_content) == (True, False, False)
    assert [a.type for a in rule.actions] == [BLOCK]
    data = await dash.ok("automod", "status")
    assert (data["rules"]["presets"]["presets"], data["rules"]["presets"]["allow_list"]) == (["slurs"], [])
    assert data["presets"] == ["profanity", "sexual_content", "slurs"]


async def test_wortlisten_waehlen_und_erlaubte_woerter(dash, guild):
    await toggle(dash, "presets")
    data = await content(dash, "presets", presets=["profanity", "slurs"], allow_list=["  Scheiße ", "scheiße", "Mist"])
    assert data == {"rule_key": "presets", "presets": ["profanity", "slurs"], "allow_list": ["scheiße", "mist"]}

    data = await content(dash, "presets", presets=["sexual_content"])   # erlaubte Wörter bleiben
    assert data["allow_list"] == ["scheiße", "mist"]
    assert only(guild, PRESET).trigger.presets.sexual_content


async def test_wortlisten_fehler(dash, guild):
    await toggle(dash, "presets")
    assert await dash.error("automod", "rule-content", {"rule_key": "presets", "presets": []}) == t("dash_err_am_presets_empty")
    assert await dash.error("automod", "rule-content", {"rule_key": "presets", "presets": ["nsfw"]}) == t("dash_err_invalid_request")
    assert await dash.error("automod", "rule-content", {"rule_key": "presets", "allow_list": ["x" * 61]}) == t("dash_err_am_word_too_long", max=60)
    assert await dash.error("automod", "rule-content", {"rule_key": "presets", "allow_list": [str(i) for i in range(1001)]}) == t("dash_err_am_too_many_words", max=1000)
    assert await dash.error("automod", "rule-content", {"rule_key": "presets", "keywords": ["x"]}) == t("dash_err_invalid_request")
    # Timeout erlaubt Discord bei fertigen Wortlisten nicht
    assert await dash.error("automod", "rule-settings", {"rule_key": "presets", "timeout_minutes": 5}) == t("dash_err_am_timeout_unsupported")


# ── Namensfilter ────────────────────────────────────────────────────────────────────────────────

async def test_namensfilter_sperrt_interaktionen(dash, guild):
    await toggle(dash, "names")
    rule = only(guild, PROFILE)
    assert rule.name == t("automod_rule_name_names")
    assert [a.type for a in rule.actions] == [INTERACTIONS]
    assert rule.event_type == discord.AutoModRuleEventType.member_update

    data = await content(dash, "names", keywords=["Admin", "MOD*"])
    assert data == {"rule_key": "names", "keywords": ["admin", "mod*"]}


async def test_namensfilter_ohne_hinweis_timeout_und_channels(dash, guild):
    await toggle(dash, "names")
    for field, value in (("custom_message", "Hi"), ("timeout_minutes", 0), ("exempt_channel_ids", [])):
        assert await dash.error("automod", "rule-settings", {"rule_key": "names", field: value}) == t("dash_err_am_setting_unsupported")
    # Ausgenommene Rollen gehen (z. B. das Team darf „Admin“ im Namen tragen)
    data = await dash.ok("automod", "rule-settings", {"rule_key": "names", "exempt_role_ids": [str(guild.mod_role.id)]})
    assert data["exempt_role_ids"] == [str(guild.mod_role.id)]
    assert [a.type for a in only(guild, PROFILE).actions] == [INTERACTIONS]


async def test_inhalt_nur_fuer_vorhandene_regel(dash):
    assert await dash.error("automod", "rule-content", {"rule_key": "names", "keywords": ["x"]}) == t("dash_err_am_rule_missing")


# ── Fremde Regeln übernehmen ────────────────────────────────────────────────────────────────────

async def test_status_zeigt_fremde_regeln_auf_einzelplaetzen(dash, guild):
    guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    guild.add_automod_rule("Eigene Wörter", KEYWORD, by_nexus=False, keyword_filter=["x"])   # kein Einzelplatz
    data = await dash.ok("automod", "status")
    assert data["foreign_rules"] == {"spam": "Spamschutz vom Admin"}
    assert data["rules"]["spam"] is None


async def test_uebernehmen_macht_die_regel_zur_nexus_regel(dash, guild, log_channel_for_log):
    fremd = guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    alert = discord.AutoModRuleAction(channel_id=log_channel_for_log.id)
    fremd.actions = [discord.AutoModRuleAction(type=BLOCK), alert]
    fremd.exempt_role_ids = {guild.mod_role.id}

    data = await dash.ok("automod", "adopt-rule", {"rule_key": "spam"})
    assert data["exempt_role_ids"] == [str(guild.mod_role.id)]   # Einstellungen des Admins bleiben
    assert fremd.name == t("automod_rule_name_spam")
    assert alert not in fremd.actions                             # Nexus-Log statt Discord-Alarm
    assert await query("SELECT kind FROM automod_rules WHERE rule_id = %s", fremd.id) == [{"kind": "spam"}]

    status = await dash.ok("automod", "status")
    assert status["foreign_rules"] == {}
    assert status["rules"]["spam"]["enabled"] is True
    # Ab jetzt wie eine eigene Regel: umschalten, Einstellungen, Log
    assert (await toggle(dash, "spam"))["enabled"] is False
    assert guild.automod_rules == [fremd]


async def test_uebernommene_regel_wird_geloggt(dash, guild, log_channel_for_log, monkeypatch):
    monkeypatch.setattr(automod_log, "COLLECT_SECONDS", 0)
    fremd = guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    await dash.ok("automod", "adopt-rule", {"rule_key": "spam"})
    await automod_log.handle_action(types.SimpleNamespace(
        guild=guild, guild_id=guild.id, rule_id=fremd.id, user_id=guild.member.id, channel_id=None,
        action=discord.AutoModRuleAction(type=BLOCK), content="spam", matched_keyword=None, matched_content=None,
    ))
    assert len(log_channel_for_log.messages) == 1


async def test_uebernehmen_ohne_fremde_regel_oder_falscher_typ(dash, guild):
    assert await dash.error("automod", "adopt-rule", {"rule_key": "spam"}) == t("dash_err_am_nothing_to_adopt")
    assert await dash.error("automod", "adopt-rule", {"rule_key": "keywords"}) == t("dash_err_invalid_request")


async def test_eintrag_geloeschter_regel_wird_aufgeraeumt(dash, guild):
    fremd = guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    await dash.ok("automod", "adopt-rule", {"rule_key": "spam"})
    guild.automod_rules.remove(fremd)   # in Discord gelöscht
    await dash.ok("automod", "status")
    assert await query("SELECT * FROM automod_rules WHERE guild_id = %s", guild.id) == []


async def test_modul_aus_pausiert_auch_neue_und_uebernommene_regeln(dash, guild):
    await toggle(dash, "presets")
    await toggle(dash, "names")
    guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    await dash.ok("automod", "adopt-rule", {"rule_key": "spam"})

    await dash.ok("modules", "toggle", {"changes": {"automod": False}})
    assert not any(r.enabled for r in guild.automod_rules)
    await dash.ok("modules", "toggle", {"changes": {"automod": True}})
    assert all(r.enabled for r in guild.automod_rules)
