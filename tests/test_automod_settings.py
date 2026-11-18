"""Einstellungen je AutoMod-Regel (systems/automod_rules.update_settings, dashboard/automod.py):
Hinweis an den Nutzer, ausgenommene Rollen und Channels, Timeout, Mention-Limit und Raid-Schutz."""
from datetime import timedelta

import discord

from cogs.automod import _automod_embed, _load_state
from systems import automod_rules
from tests.conftest import t
from translations import TRANSLATIONS

SPAM = discord.AutoModRuleTriggerType.spam
MENTIONS = discord.AutoModRuleTriggerType.mention_spam
KEYWORD = discord.AutoModRuleTriggerType.keyword
BLOCK = discord.AutoModRuleActionType.block_message
TIMEOUT = discord.AutoModRuleActionType.timeout


def setup_rules(guild):
    spam = guild.add_automod_rule("Anti-Spam", SPAM)
    mentions = guild.add_automod_rule("Mention-Spam", MENTIONS, mention_limit=5)
    words = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["kekse"])
    for rule in (spam, mentions, words):
        rule.actions = [discord.AutoModRuleAction(type=BLOCK)]
    return spam, mentions, words


async def settings(dash, rule_key, **fields):
    return await dash.ok("automod", "rule-settings", {"rule_key": rule_key, **fields})


async def error(dash, rule_key, **fields):
    return await dash.error("automod", "rule-settings", {"rule_key": rule_key, **fields})


# ── Status ──────────────────────────────────────────────────────────────────────────────────────

async def test_status_zeigt_den_heutigen_stand(dash, guild):
    spam, mentions, _ = setup_rules(guild)
    spam.exempt_role_ids = {guild.mod_role.id}   # vom Admin direkt in Discord gesetzt
    data = await dash.ok("automod", "status")

    assert data["rules"]["spam"] == {
        "enabled": True, "custom_message": None, "exempt_role_ids": [str(guild.mod_role.id)],
        "exempt_channel_ids": [], "timeout_minutes": 0,
    }
    assert (data["rules"]["mentions"]["mention_limit"], data["rules"]["mentions"]["raid_protection"]) == (5, False)
    assert data["rule_limits"] == {
        "custom_message": 150, "exempt_roles": 20, "exempt_channels": 50, "mention_limit": 50,
        "timeout_minutes": 40320, "timeout_kinds": ["keywords", "links", "mentions"],
        "words": 1000, "allow_list": 1000, "word_length": 60, "partners": 25, "link_allow_list": 100,
    }
    # alte Felder fürs heutige Dashboard bleiben
    assert (data["spam_enabled"], data["keywords"]) == (True, ["kekse"])


async def test_status_ohne_regel_none(dash, guild):
    assert (await dash.ok("automod", "status"))["rules"] == dict.fromkeys(("spam", "mentions", "keywords", "presets", "names", "links"))


# ── Hinweis, Ausnahmen, Timeout ─────────────────────────────────────────────────────────────────

async def test_hinweis_ausnahmen_und_timeout_in_einem_aufruf(dash, guild):
    _, _, words = setup_rules(guild)
    memes = guild.add_text_channel("memes")
    data = await settings(
        dash, "keywords", custom_message="  Bitte keine Kekse hier.  ", exempt_role_ids=[str(guild.mod_role.id)],
        exempt_channel_ids=[str(memes.id)], timeout_minutes=10,
    )
    assert data == {
        "rule_key": "keywords", "enabled": True, "custom_message": "Bitte keine Kekse hier.",
        "exempt_role_ids": [str(guild.mod_role.id)], "exempt_channel_ids": [str(memes.id)], "timeout_minutes": 10,
    }
    assert [(a.type, a.custom_message) for a in words.actions if a.type == BLOCK] == [(BLOCK, "Bitte keine Kekse hier.")]
    assert [a.duration for a in words.actions if a.type == TIMEOUT] == [timedelta(minutes=10)]
    assert [c for c in guild.calls if c[0] == "automod.edit"] == [
        ("automod.edit", words.id, ("actions", "exempt_channels", "exempt_roles")),
    ]


async def test_nur_mitgeschickte_felder_aendern_sich(dash, guild):
    _, _, words = setup_rules(guild)
    await settings(dash, "keywords", custom_message="Hinweis", timeout_minutes=60)
    data = await settings(dash, "keywords", exempt_role_ids=[str(guild.mod_role.id)])
    assert (data["custom_message"], data["timeout_minutes"]) == ("Hinweis", 60)

    data = await settings(dash, "keywords", custom_message="")   # leer = Discords Standardtext
    assert (data["custom_message"], data["timeout_minutes"]) == (None, 60)
    data = await settings(dash, "keywords", timeout_minutes=0)   # 0 = kein Timeout
    assert data["timeout_minutes"] == 0
    assert [a.type for a in words.actions] == [BLOCK]


async def test_fremde_aktionen_bleiben_erhalten(dash, guild):
    _, _, words = setup_rules(guild)
    sperre = discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_member_interactions)
    words.actions.append(sperre)   # vom Admin in Discord ergänzt
    await settings(dash, "keywords", timeout_minutes=5)
    assert sperre in words.actions


async def test_grenzen_und_fehler(dash, guild):
    setup_rules(guild)
    assert await error(dash, "keywords", custom_message="x" * 151) == t("dash_err_am_message_too_long", max=150)
    assert await error(dash, "spam", timeout_minutes=5) == t("dash_err_am_timeout_unsupported")
    assert await error(dash, "keywords", timeout_minutes=40321) == t("dash_err_am_timeout_range", max=40320)
    assert await error(dash, "keywords", timeout_minutes=-1) == t("dash_err_am_timeout_range", max=40320)
    assert await error(dash, "keywords", exempt_role_ids=["123"]) == t("dash_err_am_unknown_role")
    assert await error(dash, "keywords", exempt_role_ids=[str(guild.id)]) == t("dash_err_am_unknown_role")   # @everyone
    assert await error(dash, "keywords", exempt_channel_ids=["123"]) == t("dash_err_am_unknown_channel")
    assert await error(dash, "mentions", mention_limit=51) == t("dash_err_am_mention_limit_range", max=50)
    assert await error(dash, "mentions", mention_limit=0) == t("dash_err_am_mention_limit_range", max=50)
    assert await error(dash, "keywords", mention_limit=3) == t("dash_err_invalid_request")
    assert await error(dash, "keywords", timeout_minutes="5") == t("dash_err_invalid_request")
    assert await error(dash, "keywords", exempt_role_ids="123") == t("dash_err_invalid_request")
    assert await error(dash, "keywords", custom_message=5) == t("dash_err_invalid_request")
    assert await error(dash, "mentions", raid_protection="ja") == t("dash_err_invalid_request")
    assert await error(dash, "keywords") == t("dash_err_invalid_request")   # nichts übergeben
    assert not [c for c in guild.calls if c[0] == "automod.edit"]       # nichts halb geschrieben


async def test_zu_viele_ausnahmen(dash, guild, monkeypatch):
    setup_rules(guild)
    monkeypatch.setattr(automod_rules, "MAX_EXEMPT_ROLES", 1)
    monkeypatch.setattr(automod_rules, "MAX_EXEMPT_CHANNELS", 1)
    roles = [str(guild.mod_role.id), str(guild.admin_role.id)]
    channels = [str(guild.add_text_channel("a").id), str(guild.add_text_channel("b").id)]
    assert await error(dash, "keywords", exempt_role_ids=roles) == t("dash_err_am_too_many_roles", max=1)
    assert await error(dash, "keywords", exempt_channel_ids=channels) == t("dash_err_am_too_many_channels", max=1)


async def test_timeout_ohne_recht(dash, guild):
    setup_rules(guild)
    guild.bot_role.permissions = discord.Permissions(guild.bot_role.permissions.value & ~discord.Permissions(moderate_members=True).value)
    assert await error(dash, "keywords", timeout_minutes=10) == t("dash_err_am_timeout_no_permission")
    assert (await settings(dash, "keywords", timeout_minutes=0))["timeout_minutes"] == 0   # ausschalten geht immer


async def test_regel_noch_nicht_angelegt(dash):
    assert await error(dash, "spam", custom_message="Hallo") == t("dash_err_am_rule_missing")


# ── Mention-Spam ────────────────────────────────────────────────────────────────────────────────

async def test_mention_limit_und_raid_schutz(dash, guild):
    _, mentions, _ = setup_rules(guild)
    data = await settings(dash, "mentions", mention_limit=10, raid_protection=True)
    assert (data["mention_limit"], data["raid_protection"]) == (10, True)
    assert (mentions.trigger.type, mentions.trigger.mention_limit) == (MENTIONS, 10)

    data = await settings(dash, "mentions", mention_limit=15)   # Raid-Schutz bleibt an
    assert (data["mention_limit"], data["raid_protection"]) == (15, True)

    data = await settings(dash, "mentions", raid_protection=False)   # Limit bleibt
    assert (data["mention_limit"], data["raid_protection"]) == (15, False)


async def test_discord_uebersicht_zeigt_das_echte_limit(guild):
    _, mentions, _ = setup_rules(guild)
    mentions.trigger = discord.AutoModTrigger(type=MENTIONS, mention_limit=12)
    lang = TRANSLATIONS["de"]
    embed = _automod_embed(await _load_state(guild, lang), lang)
    assert t("automod_summary_limit", count=12) in embed.fields[0].value.split("\n")[1]
