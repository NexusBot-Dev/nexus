"""Nexus-Log für AutoMod-Eingriffe (systems/automod_log.py) und das Umstellen älterer Regeln
(Discords Alarm raus, sonst stünde jede Meldung doppelt im Log)."""
import asyncio
import types
from datetime import timedelta

import discord
import pytest

from database import db_settings
from emojis import NexusEmojis
from systems import automod_links, automod_log, automod_rules
from tests.conftest import run, t

SPAM = discord.AutoModRuleTriggerType.spam
KEYWORD = discord.AutoModRuleTriggerType.keyword
BLOCK = discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)
ALERT_TYPE = discord.AutoModRuleActionType.send_alert_message


@pytest.fixture(autouse=True)
def schnell(monkeypatch):
    monkeypatch.setattr(automod_log, "COLLECT_SECONDS", 0.05)


@pytest.fixture
def log_channel(guild):
    channel = guild.add_text_channel("mod-log")
    run(db_settings.update_guild(guild.id, log_channel_id=channel.id))
    return channel


def execution(guild, rule, action=BLOCK, *, content="Das ist ein böses Wort", keyword="böse*", matched="böses", user=None):
    user = user or guild.member
    return types.SimpleNamespace(
        guild=guild, guild_id=guild.id, rule_id=rule.id, rule_trigger_type=rule.trigger.type,
        user_id=user.id, channel_id=next(iter(guild.channels_by_id), None), action=action,
        content=content, matched_keyword=keyword, matched_content=matched,
    )


def log_embeds(channel) -> list[discord.Embed]:
    return [m.embeds[0] for m in channel.messages.values() if m.embeds]


def field(embed, key) -> str:
    return next(f.value for f in embed.fields if f.name == t(key))


# ── Ein Eintrag pro Verstoß ─────────────────────────────────────────────────────────────────────

async def test_blockieren_und_timeout_ergeben_einen_eintrag(guild, log_channel):
    rule = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["böse*"])
    timeout = discord.AutoModRuleAction(duration=timedelta(minutes=10))
    await asyncio.gather(
        automod_log.handle_action(execution(guild, rule)),
        automod_log.handle_action(execution(guild, rule, timeout)),
    )

    [embed] = log_embeds(log_channel)
    assert embed.title == f"{NexusEmojis.SHIELD} {t('automod_log_title')}"
    assert field(embed, "automod_log_user") == guild.member.mention
    assert field(embed, "automod_log_rule") == t("automod_rule_name_keywords")
    assert field(embed, "automod_log_keyword") == "böse\\*"
    assert field(embed, "automod_log_measures") == "\n".join([
        t("automod_log_blocked"), t("automod_log_timeout", duration=t("automod_log_minutes", n=10)),
    ])
    assert field(embed, "automod_log_message") == "> Das ist ein **böses** Wort"


async def test_verschiedene_nutzer_getrennte_eintraege(guild, log_channel):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    await asyncio.gather(
        automod_log.handle_action(execution(guild, rule, keyword=None)),
        automod_log.handle_action(execution(guild, rule, keyword=None, user=guild.moderator)),
    )
    assert len(log_embeds(log_channel)) == 2


async def test_nachricht_wird_entschaerft_und_pingt_niemanden(guild, log_channel):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    await automod_log.handle_action(execution(guild, rule, content="**fett** @everyone\n" + "x" * 2000, keyword=None, matched=None))
    [message] = list(log_channel.messages.values())
    value = field(message.embeds[0], "automod_log_message")
    assert value.startswith("> \\*\\*fett\\*\\* @everyone\n> x")
    assert len(value) <= 1024
    assert message.allowed_mentions.everyone is False


@pytest.mark.parametrize("content", [
    "a\n" * 2000,                 # 4000 Zeichen mit Nitro, jede Zeile bekommt „> “ dazu
    "*" * 4000,                   # Entschärfen verdoppelt die Länge
    "**fett**\n" * 500,
    "x" * 996 + "*" + "x" * 50,   # „> “ + 996 x + „\\*“: gekürzt genau zwischen \\ und * — kein einzelner \\ am Ende
])
async def test_lange_nachrichten_passen_ins_embed(guild, log_channel, content):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    await automod_log.handle_action(execution(guild, rule, content=content, keyword=None, matched=None))
    [embed] = log_embeds(log_channel)
    value = field(embed, "automod_log_message")
    assert len(value) <= 1024
    assert value.startswith(">")
    assert not value.rstrip("…").endswith("\\")


# ── Ausschnitt statt Textwand ──────────────────────────────────────────────────────────────────

WALL = (
    "Hallo zusammen, ich wollte euch nur kurz mitteilen, dass wir für das kommende Community-Event am "
    "Wochenende schon alles vorbereitet haben. Neben den üblichen Getränken und Snacks habe ich heute "
    "Nachmittag auch noch zwei große Bleche frische Schoko Kekse gebacken, damit niemand während der langen "
    "Runde verhungert. Falls noch jemand spezielle Wünsche oder Allergien hat, schreibt mir bitte einfach "
    "kurz eine Nachricht in den allgemeinen Chat."
)


async def message_value(guild, channel, **kwargs) -> str:
    rule = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["kekse"])
    await automod_log.handle_action(execution(guild, rule, **kwargs))
    return field(log_embeds(channel)[-1], "automod_log_message")


async def test_ausschnitt_um_die_fundstelle(guild, log_channel):
    value = await message_value(guild, log_channel, content=WALL, keyword="kekse", matched="Kekse")
    assert "**Kekse**" in value
    assert value.startswith("> …") and value.endswith("…")
    assert len(value) < 300
    before, after = value[len("> …"):-1].split("**Kekse**")
    assert 80 <= len(before) <= 100 and 80 <= len(after) <= 100   # rund 100 Zeichen, an Wortgrenzen
    assert before[0] != " " and not after.endswith(" ")
    # kein halbes Wort an den Schnittstellen
    assert before.split()[0] in WALL.split() and after.split()[-1] in WALL.split()


async def test_kurze_nachricht_ganz_ohne_auslassung(guild, log_channel):
    value = await message_value(guild, log_channel, content="Ich mag Kekse.", matched="Kekse")
    assert value == "> Ich mag **Kekse**."


async def test_fundstelle_unabhaengig_von_gross_klein(guild, log_channel):
    value = await message_value(guild, log_channel, content="KEKSE für alle", matched="kekse")
    assert value == "> **KEKSE** für alle"


async def test_viele_leerzeilen_werden_zusammengefasst(guild, log_channel):
    content = "\n\nHALLO" + "\n" * 20 + "   \n\nKekse\n\n\n"
    value = await message_value(guild, log_channel, content=content, matched="Kekse")
    assert value == "> HALLO\n>\n> **Kekse**"


async def test_ohne_fundstelle_der_anfang(guild, log_channel):
    value = await message_value(guild, log_channel, content=WALL, keyword=None, matched=None)
    assert value.startswith("> Hallo zusammen") and value.endswith("…")
    assert len(value) <= 2 + 300 + 1
    assert value[2:-1].split()[-1] in WALL.split()


# ── Links im Ausschnitt und Auslöser beim Link-Filter ───────────────────────────────────────────

async def test_link_wird_code_und_als_ganzes_hervorgehoben(guild, log_channel):
    value = await message_value(guild, log_channel, content="Komm auf https://discord.gg/4Qp5Qn4bf jetzt", matched="discord.gg/4Qp5Qn4bf")
    # Kein ** mitten im Link (Discord machte daraus %2A%2A) und nicht anklickbar
    assert value == "> Komm auf **`https://discord.gg/4Qp5Qn4bf`** jetzt"


async def test_andere_links_werden_code_die_fundstelle_bleibt_fett(guild, log_channel):
    value = await message_value(guild, log_channel, content="Kekse gibt es auf https://example.com/*shop* und www.x.de", matched="Kekse")
    assert value == "> **Kekse** gibt es auf `https://example.com/*shop*` und `www.x.de`"


async def test_backticks_und_sehr_lange_links(guild, log_channel):
    value = await message_value(guild, log_channel, content="https://evil.com/`x`" + "a" * 500, keyword=None, matched=None)
    assert value.count("`") == 2                      # nur die Code-Klammern
    assert len(value) <= 2 + 2 + 200 + 1               # „> “ + Code-Klammern + höchstens 200 Zeichen Link + „…“ des Ausschnitts


async def test_ausloeser_beim_link_filter_im_klartext(dash, guild, log_channel):
    await dash.ok("automod", "toggle-rule", {"rule_key": "links"})
    rule = next(r for r in guild.automod_rules if r.trigger.regex_patterns)
    domain = automod_links.domain_patterns(["example.com"])[0]
    await asyncio.gather(
        automod_log.handle_action(execution(guild, rule, content="discord.gg/abc", keyword=automod_rules.INVITE_PATTERN, matched="discord.gg/abc")),
        automod_log.handle_action(execution(guild, rule, content="https://example.com", keyword=domain, matched="https://example.com", user=guild.moderator)),
    )
    triggers = sorted(field(e, "automod_log_keyword") for e in log_embeds(log_channel))
    assert triggers == sorted([t("automod_log_trigger_invite"), t("automod_log_trigger_domain")])


async def test_ausloeser_beim_wortfilter_bleibt_das_wort(guild, log_channel):
    rule = guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["kekse*"])
    await automod_log.handle_action(execution(guild, rule, content="Kekse!", keyword="kekse*", matched="Kekse"))
    assert field(log_embeds(log_channel)[0], "automod_log_keyword") == "kekse\\*"


# ── Wann nicht geloggt wird ─────────────────────────────────────────────────────────────────────

async def test_fremde_regel_wird_nicht_geloggt(guild, log_channel):
    fremd = guild.add_automod_rule("Vom Admin", KEYWORD, by_nexus=False, keyword_filter=["x"])
    await automod_log.handle_action(execution(guild, fremd))
    assert log_embeds(log_channel) == []


async def test_discords_alarm_allein_ist_kein_eintrag(guild, log_channel):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    alert = discord.AutoModRuleAction(channel_id=log_channel.id)
    await automod_log.handle_action(execution(guild, rule, alert))
    assert log_embeds(log_channel) == []


async def test_ohne_log_channel_oder_modul_aus_nichts(guild):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    await automod_log.handle_action(execution(guild, rule))   # kein Log-Channel gesetzt
    assert all(not c.messages for c in guild.channels_by_id.values())

    channel = guild.add_text_channel("mod-log")
    await db_settings.update_guild(guild.id, log_channel_id=channel.id)
    await db_settings.set_module(guild.id, "automod", False)
    await automod_log.handle_action(execution(guild, rule))
    assert log_embeds(channel) == []


async def test_ohne_schreibrecht_im_log_channel_kein_absturz(guild, log_channel):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    log_channel.bot_denied = {"send_messages"}
    await automod_log.handle_action(execution(guild, rule))
    assert log_embeds(log_channel) == []


# ── Regeln ohne Discords Alarm ──────────────────────────────────────────────────────────────────

async def test_neue_regeln_nur_blockieren(dash, guild, log_channel):
    await dash.ok("automod", "toggle-rule", {"rule_key": "spam"})
    [rule] = guild.automod_rules
    assert [a.type for a in rule.actions] == [discord.AutoModRuleActionType.block_message]


def mit_alarm(guild, name, trigger, channel, **kw):
    rule = guild.add_automod_rule(name, trigger, **kw)
    rule.actions = [BLOCK, discord.AutoModRuleAction(duration=timedelta(minutes=5)),
                    discord.AutoModRuleAction(channel_id=channel.id)]
    return rule


async def test_aeltere_regel_verliert_den_alarm_beim_naechsten_kontakt(dash, guild, log_channel):
    rule = mit_alarm(guild, "Nexus — Keyword-Filter", KEYWORD, log_channel, keyword_filter=["a"])
    await dash.ok("automod", "status")
    assert rule.name == t("automod_rule_name_keywords")
    assert [a.type for a in rule.actions] == [discord.AutoModRuleActionType.block_message, discord.AutoModRuleActionType.timeout]
    assert [c for c in guild.calls if c[0] == "automod.edit"] == [("automod.edit", rule.id, ("actions", "name"))]


async def test_log_kennt_neu_angelegte_regel_ohne_nachzufragen(dash, guild, log_channel):
    await dash.ok("automod", "status")                       # Zwischenspeicher für den Server füllen
    await dash.ok("automod", "toggle-rule", {"rule_key": "spam"})
    fetches = guild.automod_fetches
    [rule] = guild.automod_rules
    await automod_log.handle_action(execution(guild, rule, keyword=None))
    assert len(log_embeds(log_channel)) == 1
    assert guild.automod_fetches == fetches


async def test_fremde_regel_fragt_discord_nur_einmal(guild, log_channel):
    fremd = guild.add_automod_rule("Vom Admin", KEYWORD, by_nexus=False, keyword_filter=["x"])
    for _ in range(3):
        await automod_log.handle_action(execution(guild, fremd))
    assert guild.automod_fetches == 1
    assert log_embeds(log_channel) == []


async def test_regelart_aus_dem_zwischenspeicher(guild):
    rule = guild.add_automod_rule("Anti-Spam", SPAM)
    lang = {}
    assert await automod_rules.nexus_rule_kind(guild, rule.id, lang) == "spam"
    fetches = guild.automod_fetches
    assert await automod_rules.nexus_rule_kind(guild, rule.id, lang) == "spam"
    assert guild.automod_fetches == fetches
