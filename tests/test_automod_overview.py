"""/automod als Übersicht mit Schnellzugriff (cogs/automod.py): alle Regeln mit Status und Kurzzeile,
an/aus per Auswahlmenü, Anti-Nuke per Button, Wortfilter per Fenster, Link ins Dashboard."""
from datetime import timedelta

import discord

from cogs.automod import AutoModView, KeywordModal, _automod_embed, _load_state
from cogs.utils import get_lang
from config import DASHBOARD_URL
from database import db_automod
from emojis import NexusEmojis
from systems import automod_rules
from tests.conftest import t
from tests.fakes import FakeInteraction

SPAM = discord.AutoModRuleTriggerType.spam
MENTIONS = discord.AutoModRuleTriggerType.mention_spam
KEYWORD = discord.AutoModRuleTriggerType.keyword
OFF, ON = NexusEmojis.OFF, NexusEmojis.CHECKMARK


async def open_overview(guild, user=None):
    lang = await get_lang(guild.id)
    state = await _load_state(guild, lang)
    return AutoModView(guild, (user or guild.admin).id, lang, state), _automod_embed(state, lang)


def field(embed, name) -> str:
    return next(f.value for f in embed.fields if f.name == name)


def rule_lines(embed) -> list[str]:
    return field(embed, t("automod_rules_title")).split("\n")


def interaction(guild, user=None, values=None):
    inter = FakeInteraction(guild)
    inter.user = user or guild.admin
    inter.data = {"values": values or []}
    return inter


def select(view):
    return next(i for i in view.children if isinstance(i, discord.ui.Select))


def buttons(view):
    return [i for i in view.children if isinstance(i, discord.ui.Button)]


# ── Embed ───────────────────────────────────────────────────────────────────────────────────────

async def test_testphase_nur_drei_regeln_ohne_eskalation(guild, monkeypatch):
    monkeypatch.setattr(automod_rules, "RELEASED", False)
    view, embed = await open_overview(guild)
    lines = rule_lines(embed)
    assert len(lines) == 3
    assert lines[0] == f"{OFF} 🛡️ **{t('automod_rule_name_spam')}**"
    assert all(f.name != f"⚖️ {t('automod_escalation_title')}" for f in embed.fields)
    assert [o.value for o in select(view).options] == ["spam", "mentions", "keywords"]


async def test_alle_regeln_mit_kurzzeile(guild):
    lang = await get_lang(guild.id)
    spam = guild.add_automod_rule("Anti-Spam", SPAM)
    spam.exempt_role_ids = {guild.mod_role.id}
    guild.add_automod_rule("Mention-Spam", MENTIONS, mention_limit=7, enabled=False)
    words = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["kekse"])
    words.actions = [
        discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message),
        discord.AutoModRuleAction(duration=timedelta(days=1)),
    ]
    await automod_rules.enable_rule(guild, "presets", lang, reason="Test")
    await automod_rules.enable_rule(guild, "links", lang, reason="Test")
    guild.add_automod_rule("Namen vom Admin", discord.AutoModRuleTriggerType.member_profile, by_nexus=False, keyword_filter=["x"])

    _, embed = await open_overview(guild)
    assert rule_lines(embed) == [
        f"{ON} 🛡️ **{t('automod_rule_name_spam')}** — {t('automod_summary_roles_one')}",
        f"{OFF} 📢 **{t('automod_rule_name_mentions')}** — {t('automod_summary_limit', count=7)}",
        f"{ON} 🔤 **{t('automod_rule_name_keywords')}** — {t('automod_summary_words_one')} · "
        f"{t('automod_summary_timeout', duration=t('automod_log_day_one'))}",
        f"{ON} 📚 **{t('automod_rule_name_presets')}** — {t('automod_preset_slurs')}",
        f"{OFF} 🪪 **{t('automod_rule_name_names')}** — {t('automod_rule_foreign', name='Namen vom Admin')}",
        f"{ON} 🔗 **{t('automod_rule_name_links')}** — {t('automod_summary_invites')}",
    ]


async def test_mehrzahl_in_kurzzeile(guild):
    rule = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["a", "b", "c"])
    rule.exempt_role_ids = {guild.mod_role.id, guild.admin_role.id}
    _, embed = await open_overview(guild)
    assert rule_lines(embed)[2].endswith(f"{t('automod_summary_words', count=3)} · {t('automod_summary_roles', count=2)}")


async def test_shield_eskalation_honeypot(guild):
    await db_automod.set_antinuke(guild.id, True)
    await db_automod.add_shield_whitelist(guild.id, "bot", [111, 222], guild.owner_id)
    await db_automod.save_escalation(guild.id, True, [{"strikes": 3, "window_minutes": 60, "action": "warn"}])
    _, embed = await open_overview(guild)
    shield = field(embed, "🚨 Anti-Nuke Shield")
    assert shield.startswith(f"{ON} {t('module_active')}\n{t('automod_shield_whitelist', bots=2, roles=0)}")
    assert field(embed, f"⚖️ {t('automod_escalation_title')}") == (
        f"{ON} {t('module_active')} — {t('automod_escalation_levels_one')}"
    )
    assert field(embed, "🍯 Honeypot").startswith(f"{OFF} {t('module_inactive')}")


async def test_eskalation_ohne_stufen_gilt_als_aus(guild):
    await db_automod.save_escalation(guild.id, True, [])
    _, embed = await open_overview(guild)
    assert field(embed, f"⚖️ {t('automod_escalation_title')}") == f"{OFF} {t('module_inactive')}"


async def test_fremder_name_wird_entschaerft(guild):
    guild.add_automod_rule("**Spam** vom _Admin_", SPAM, by_nexus=False)
    _, embed = await open_overview(guild)
    assert rule_lines(embed)[0].endswith(t("automod_rule_foreign", name=r"\*\*Spam\*\* vom \_Admin\_"))


# ── Komponenten ─────────────────────────────────────────────────────────────────────────────────

async def test_auswahl_zeigt_status(guild):
    guild.add_automod_rule("Anti-Spam", SPAM)
    guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["x"], enabled=False)
    guild.add_automod_rule("Namen vom Admin", discord.AutoModRuleTriggerType.member_profile, by_nexus=False, keyword_filter=["x"])
    view, _ = await open_overview(guild)
    options = {o.value: o for o in select(view).options}
    assert list(options) == list(automod_rules.RULE_KINDS)
    assert options["spam"].description == t("automod_select_on")
    assert options["keywords"].description == t("automod_select_off")
    assert options["presets"].description == t("automod_select_off")
    assert options["names"].description == t("automod_select_foreign")
    assert str(options["spam"].emoji) == ON


async def test_buttons(guild):
    view, _ = await open_overview(guild)
    labels = [b.label for b in buttons(view)]
    assert labels == [f"Anti-Nuke: {t('setup_off')}", t("automod_open_dashboard")]   # ohne Wortfilter kein Keywords-Button
    link = buttons(view)[-1]
    assert link.url == f"{DASHBOARD_URL}/server/{guild.id}/automod"

    guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["x"])
    view, _ = await open_overview(guild)
    assert [b.label for b in buttons(view)][1] == t("automod_manage_keywords")


async def test_regel_einschalten_und_ausschalten(guild):
    view, _ = await open_overview(guild)
    inter = interaction(guild, values=["spam"])
    await view._rule_chosen(inter)
    [rule] = [r for r in guild.automod_rules if r.trigger.type == SPAM]
    assert rule.enabled
    # Nachricht zeigt den neuen Stand, Auswahl auch
    embed = inter.edits[-1]["embed"]
    assert rule_lines(embed)[0].startswith(f"{ON} ")
    assert {o.value: o for o in select(inter.edits[-1]["view"]).options}["spam"].description == t("automod_select_on")

    await view._rule_chosen(interaction(guild, values=["spam"]))
    assert not rule.enabled


async def test_regel_neu_anlegen_wortlisten(guild):
    view, _ = await open_overview(guild)
    await view._rule_chosen(interaction(guild, values=["presets"]))
    assert any(r.trigger.type == discord.AutoModRuleTriggerType.keyword_preset for r in guild.automod_rules)


async def test_regel_von_fremder_belegt(guild):
    guild.add_automod_rule("Spamschutz vom Admin", SPAM, by_nexus=False)
    view, _ = await open_overview(guild)
    inter = interaction(guild, values=["spam"])
    await view._rule_chosen(inter)
    assert inter.sent[0]["content"].endswith(t("automod_rule_exists"))
    assert inter.edits == []   # nichts geändert


async def test_regel_ohne_recht(guild):
    view, _ = await open_overview(guild)
    guild.bot_role.permissions = discord.Permissions(send_messages=True)   # Recht inzwischen entzogen
    inter = interaction(guild, values=["spam"])
    await view._rule_chosen(inter)
    assert inter.sent[0]["content"].endswith(t("automod_no_permission"))


async def test_testphase_regel_gesperrt(guild, monkeypatch):
    view, _ = await open_overview(guild)
    monkeypatch.setattr(automod_rules, "RELEASED", False)
    inter = interaction(guild, values=["links"])   # Menü noch von vor der Sperre
    await view._rule_chosen(inter)
    assert inter.sent[0]["content"].endswith(t("dash_err_am_v2_unavailable"))
    assert not any(r.trigger.regex_patterns for r in guild.automod_rules)


async def test_shield_button(guild):
    view, _ = await open_overview(guild)
    inter = interaction(guild)
    await view._toggle_shield(inter)
    assert await db_automod.get_antinuke(guild.id) == 1
    assert buttons(inter.edits[-1]["view"])[0].label == f"Anti-Nuke: {t('setup_on')}"
    # Ohne Administrator-Recht: Hinweis zum Wiederherstellen
    assert any(t("shield_admin_hint") in (s["content"] or "") for s in inter.sent)


async def test_shield_button_mit_admin_kein_hinweis(guild):
    guild.bot_role.permissions = discord.Permissions(administrator=True)
    view, _ = await open_overview(guild)
    inter = interaction(guild)
    await view._toggle_shield(inter)
    assert not any(t("shield_admin_hint") in (s["content"] or "") for s in inter.sent)


async def test_keywords_fenster_speichert_und_aktualisiert(guild):
    rule = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["alt"])
    view, _ = await open_overview(guild)
    modal = KeywordModal(view.lang, guild, rule, view)
    modal.keywords_input._value = "Kekse\n  Torte \n\n"
    inter = interaction(guild)
    await modal.on_submit(inter)
    assert list(rule.trigger.keyword_filter) == ["kekse", "torte"]
    assert rule_lines(inter.edits[-1]["embed"])[2].endswith(t("automod_summary_words", count=2))
