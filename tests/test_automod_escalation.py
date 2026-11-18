"""Eskalation für Nexus-AutoMod (systems/automod_escalation.py) — Strikes aus dem AutoMod-Log
(systems/automod_log.py), Stufen, Maßnahmen, Log-Feld, Dashboard und Aufräumen."""
import asyncio
import types
from datetime import timedelta

import discord
import pytest

from database import db_automod, db_settings
from systems import automod_escalation, automod_log, automod_rules
from tests.conftest import query, run, set_premium, t

KEYWORD = discord.AutoModRuleTriggerType.keyword
PROFILE = discord.AutoModRuleTriggerType.member_profile
BLOCK = discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)
TIMEOUT_ACTION = discord.AutoModRuleAction(duration=timedelta(minutes=10))

WARN_3_IN_1H = {"strikes": 3, "window_minutes": 60, "action": "warn"}


@pytest.fixture(autouse=True)
def schnell(monkeypatch):
    monkeypatch.setattr(automod_log, "COLLECT_SECONDS", 0.01)


@pytest.fixture
def log_channel(guild):
    channel = guild.add_text_channel("mod-log")
    run(db_settings.update_guild(guild.id, log_channel_id=channel.id))
    return channel


@pytest.fixture
def rule(guild):
    return guild.add_automod_rule("Wortfilter", KEYWORD, keyword_filter=["böse*"])


def execution(guild, rule, action=BLOCK, *, user=None, content="Das ist böse"):
    user = user or guild.member
    return types.SimpleNamespace(
        guild=guild, guild_id=guild.id, rule_id=rule.id, rule_trigger_type=rule.trigger.type,
        user_id=user.id, channel_id=None, action=action, content=content, matched_keyword="böse*", matched_content="böse",
    )


async def violate(guild, rule, times=1, **kwargs):
    for i in range(times):
        await automod_log.handle_action(execution(guild, rule, content=f"böse {i}", **kwargs))


async def configure(guild, *steps, enabled=True):
    await db_automod.save_escalation(guild.id, enabled, list(steps))


async def strikes(guild, user=None):
    user = user or guild.member
    return await query("SELECT rule_kind FROM automod_strikes WHERE guild_id = %s AND user_id = %s", guild.id, user.id)


def calls(guild, name):
    return [c for c in guild.calls if c[0] == name]


def escalation_field(channel):
    embed = list(channel.messages.values())[-1].embeds[0]
    return next((f.value for f in embed.fields if f.name == t("automod_escalation_title")), None)


# ── Strikes ─────────────────────────────────────────────────────────────────────────────────────

async def test_ohne_eskalation_keine_strikes(guild, rule):
    await violate(guild, rule, 3)
    assert await strikes(guild) == []
    await configure(guild, WARN_3_IN_1H, enabled=False)
    await violate(guild, rule, 3)
    assert await strikes(guild) == []


async def test_blockieren_und_timeout_ein_strike(guild, rule):
    await configure(guild, WARN_3_IN_1H)
    await asyncio.gather(
        automod_log.handle_action(execution(guild, rule)),
        automod_log.handle_action(execution(guild, rule, TIMEOUT_ACTION)),
    )
    assert await strikes(guild) == [{"rule_kind": "keywords"}]


async def test_namensfilter_und_fremde_regeln_zaehlen_nicht(guild, rule):
    await configure(guild, WARN_3_IN_1H)
    names = guild.add_automod_rule("Namen", PROFILE, keyword_filter=["admin"])
    foreign = guild.add_automod_rule("Eigene", KEYWORD, by_nexus=False, keyword_filter=["x"])
    await violate(guild, names, 3)
    await violate(guild, foreign, 3)
    assert await strikes(guild) == []


async def test_testphase_keine_strikes(guild, rule, monkeypatch):
    monkeypatch.setattr(automod_rules, "RELEASED", False)
    await configure(guild, WARN_3_IN_1H)
    await violate(guild, rule, 3)
    assert await strikes(guild) == []


async def test_modul_aus_keine_strikes(guild, rule):
    await configure(guild, WARN_3_IN_1H)
    await db_settings.set_modules(guild.id, {"automod": False})
    await violate(guild, rule, 3)
    assert await strikes(guild) == []


# ── Stufen ──────────────────────────────────────────────────────────────────────────────────────

async def test_verwarnung_beim_dritten_verstoss(guild, rule, log_channel):
    await configure(guild, WARN_3_IN_1H)
    await violate(guild, rule, 2)
    assert await query("SELECT * FROM warnings") == []
    assert escalation_field(log_channel) is None

    await violate(guild, rule)
    [warning] = await query("SELECT * FROM warnings")
    reason = t("automod_escalation_reason", count=3, window=t("automod_log_hours", n=1))
    assert (warning["user_id"], warning["moderator_id"], warning["reason"]) == (guild.member.id, guild.me.id, reason)
    [dm] = calls(guild, "member.dm")
    assert dm[1] == guild.member.id and dm[2].fields[0].value == reason
    assert escalation_field(log_channel) == "\n".join([
        t("automod_escalation_step", step=1, total=1, measure=t("automod_escalation_warn")),
        t("automod_escalation_cause", count=3, window=t("automod_log_hours", n=1)),
    ])

    # Weiterzählen: der vierte Verstoß löst nicht erneut aus
    await violate(guild, rule)
    assert len(await query("SELECT * FROM warnings")) == 1


async def test_verwarnung_mit_geschlossenen_dms(guild, rule):
    guild.member.dms_closed = True
    await configure(guild, {"strikes": 1, "window_minutes": 10, "action": "warn"})
    await violate(guild, rule)
    assert len(await query("SELECT * FROM warnings")) == 1


async def test_nur_verstoesse_im_zeitfenster(guild, rule):
    await configure(guild, WARN_3_IN_1H)
    old = automod_escalation._now() - timedelta(minutes=61)
    await db_automod.add_strike(guild.id, guild.member.id, "keywords", old)
    await db_automod.add_strike(guild.id, guild.member.id, "keywords", old)
    await violate(guild, rule, 2)
    assert await query("SELECT * FROM warnings") == []
    await violate(guild, rule)
    assert len(await query("SELECT * FROM warnings")) == 1


async def test_jede_stufe_zaehlt_im_eigenen_zeitfenster(guild, rule):
    """Ein Strike von vor 30 Min. zählt für „5 in 24 Std.“, aber nicht für „2 in 10 Min.“."""
    await set_premium(guild)
    await configure(guild, {"strikes": 2, "window_minutes": 10, "action": "warn"}, {"strikes": 5, "window_minutes": 1440, "action": "ban"})
    await db_automod.add_strike(guild.id, guild.member.id, "spam", automod_escalation._now() - timedelta(minutes=30))
    await violate(guild, rule)
    assert await query("SELECT * FROM warnings") == []
    await violate(guild, rule)
    assert len(await query("SELECT * FROM warnings")) == 1


async def test_strikes_je_user(guild, rule):
    await configure(guild, WARN_3_IN_1H)
    await violate(guild, rule, 2)
    await violate(guild, rule, 2, user=guild.moderator)
    assert await query("SELECT * FROM warnings") == []


async def test_stufen_nacheinander(guild, rule, log_channel):
    await set_premium(guild)
    await configure(
        guild, WARN_3_IN_1H,
        {"strikes": 5, "window_minutes": 1440, "action": "timeout", "timeout_minutes": 60},
        {"strikes": 7, "window_minutes": 10080, "action": "kick"},
    )
    target = guild.add_member("spammer")
    await violate(guild, rule, 3, user=target)
    assert len(await query("SELECT * FROM warnings")) == 1
    await violate(guild, rule, 2, user=target)
    [(_, member_id, until)] = calls(guild, "member.timeout")
    assert member_id == target.id
    assert timedelta(minutes=59) < until - discord.utils.utcnow() <= timedelta(minutes=60)
    assert escalation_field(log_channel).startswith(t(
        "automod_escalation_step", step=2, total=3,
        measure=t("automod_escalation_timeout", duration=t("automod_log_hours", n=1)),
    ))
    await violate(guild, rule, 2, user=target)
    assert calls(guild, "guild.kick")[0][1] == target.id


async def test_gleichzeitig_nur_strengste_stufe(guild, rule):
    await set_premium(guild)
    await configure(guild, {"strikes": 2, "window_minutes": 60, "action": "warn"}, {"strikes": 2, "window_minutes": 1440, "action": "ban"})
    target = guild.add_member("spammer")
    await violate(guild, rule, 2, user=target)
    assert await query("SELECT * FROM warnings") == []
    assert calls(guild, "guild.ban")[0][1] == target.id


async def test_ohne_premium_nur_erste_stufe(guild, rule):
    await configure(guild, {"strikes": 1, "window_minutes": 60, "action": "warn"}, {"strikes": 2, "window_minutes": 60, "action": "ban"})
    target = guild.add_member("spammer")
    await violate(guild, rule, 2, user=target)
    assert calls(guild, "guild.ban") == []
    assert len(await query("SELECT * FROM warnings")) == 1


async def test_massnahme_scheitert_steht_im_log(guild, rule, log_channel):
    await configure(guild, {"strikes": 1, "window_minutes": 60, "action": "kick"})
    await violate(guild, rule, user=guild.admin)   # steht über Nexus
    assert calls(guild, "guild.kick") == []
    assert escalation_field(log_channel).endswith(t("automod_escalation_failed"))


async def test_kick_wenn_user_weg(guild, rule, log_channel):
    await configure(guild, {"strikes": 1, "window_minutes": 60, "action": "kick"})
    gone = types.SimpleNamespace(id=4242)
    await violate(guild, rule, user=gone)
    assert escalation_field(log_channel).endswith(t("automod_escalation_failed"))


async def test_bann_auch_wenn_user_weg(guild, rule):
    await configure(guild, {"strikes": 1, "window_minutes": 60, "action": "ban"})
    await violate(guild, rule, user=types.SimpleNamespace(id=4242))
    assert calls(guild, "guild.ban")[0][1] == 4242


async def test_eskalation_ohne_log_channel(guild, rule):
    await configure(guild, {"strikes": 1, "window_minutes": 60, "action": "warn"})
    await violate(guild, rule)
    assert len(await query("SELECT * FROM warnings")) == 1


async def test_fehler_in_eskalation_log_kommt_trotzdem(guild, rule, log_channel, monkeypatch):
    async def kaputt(*args):
        raise RuntimeError("DB weg")
    monkeypatch.setattr(automod_escalation, "on_violation", kaputt)
    await violate(guild, rule)
    assert len(log_channel.messages) == 1
    assert escalation_field(log_channel) is None


# ── Aufräumen ───────────────────────────────────────────────────────────────────────────────────

async def test_strikes_nach_30_tagen_geloescht(guild):
    now = automod_escalation._now()
    await db_automod.add_strike(guild.id, guild.member.id, "spam", now - timedelta(days=31))
    await db_automod.add_strike(guild.id, guild.member.id, "spam", now - timedelta(days=29))
    assert await automod_escalation.cleanup() == 1
    assert len(await strikes(guild)) == 1


# ── Dashboard ───────────────────────────────────────────────────────────────────────────────────

async def test_status(dash, guild):
    data = (await dash.ok("automod", "status"))["escalation"]
    assert data == {
        "enabled": False, "steps": [], "max_steps": 1, "premium_max_steps": 5,
        "actions": ["warn", "timeout", "kick", "ban"], "window_options": [10, 60, 360, 1440, 10080, 43200],
        "timeout_options": [5, 10, 60, 360, 1440, 10080, 40320], "max_strikes": 50,
    }
    await set_premium(guild)
    assert (await dash.ok("automod", "status"))["escalation"]["max_steps"] == 5


async def test_status_testphase(dash, monkeypatch):
    monkeypatch.setattr(automod_rules, "RELEASED", False)
    assert (await dash.ok("automod", "status"))["escalation"] is None


async def test_speichern(dash, guild):
    step = {"strikes": 3, "window_minutes": 60, "action": "timeout", "timeout_minutes": 10, "egal": 1}
    data = await dash.ok("automod", "escalation", {"steps": [step]})
    assert data["steps"] == [{"strikes": 3, "window_minutes": 60, "action": "timeout", "timeout_minutes": 10}]
    assert data["enabled"] is False
    data = await dash.ok("automod", "escalation", {"enabled": True})
    assert (data["enabled"], len(data["steps"])) == (True, 1)
    # Nicht-Timeout-Stufen ohne Timeout-Dauer
    data = await dash.ok("automod", "escalation", {"steps": [{**WARN_3_IN_1H, "timeout_minutes": 10}]})
    assert data["steps"] == [WARN_3_IN_1H]


async def test_einschalten_ohne_stufen(dash):
    assert await dash.error("automod", "escalation", {"enabled": True}) == t("dash_err_am_escalation_no_steps")
    await dash.ok("automod", "escalation", {"steps": [WARN_3_IN_1H], "enabled": True})
    assert await dash.error("automod", "escalation", {"steps": []}) == t("dash_err_am_escalation_no_steps")


async def test_stufen_grenze(dash, guild):
    two = [WARN_3_IN_1H, {"strikes": 5, "window_minutes": 60, "action": "ban"}]
    assert await dash.error("automod", "escalation", {"steps": two}) == t("dash_err_am_escalation_too_many_steps", max=1)
    await set_premium(guild)
    await dash.ok("automod", "escalation", {"steps": two})
    six = [WARN_3_IN_1H] * 6
    assert await dash.error("automod", "escalation", {"steps": six}) == t("dash_err_am_escalation_too_many_steps", max=5)


@pytest.mark.parametrize("strikes", [0, 51, "3", True])
def test_anzahl_verstoesse(dash, strikes):
    error = run(dash.error("automod", "escalation", {"steps": [{**WARN_3_IN_1H, "strikes": strikes}]}))
    assert error == t("dash_err_am_escalation_strikes", max=50)


@pytest.mark.parametrize("payload", [
    {},
    {"enabled": "true"},
    {"steps": "warn"},
    {"steps": ["warn"]},
    {"steps": [{**WARN_3_IN_1H, "window_minutes": 5}]},
    {"steps": [{**WARN_3_IN_1H, "action": "mute"}]},
    {"steps": [{**WARN_3_IN_1H, "action": "timeout"}]},
    {"steps": [{**WARN_3_IN_1H, "action": "timeout", "timeout_minutes": 99999}]},
])
def test_ungueltige_anfrage(dash, payload):
    assert run(dash.error("automod", "escalation", payload)) == t("dash_err_invalid_request")


async def test_speichern_testphase(dash, monkeypatch):
    monkeypatch.setattr(automod_rules, "RELEASED", False)
    assert await dash.error("automod", "escalation", {"steps": [WARN_3_IN_1H]}) == t("dash_err_am_v2_unavailable")
