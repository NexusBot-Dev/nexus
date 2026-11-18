"""Whitelist fürs Anti-Nuke-Shield (systems/shield_whitelist.py, cogs/shield.py, dashboard/automod.py).

Nachgestellter Anlass: Ein Moderations-Bot kickt/bannt drei Accounts innerhalb von Sekundenbruchteilen
(3 × 0.75 Punkte × Tempo 2.5 = 5.62) und löst damit die Vorwarnung aus.
"""
import types

import discord
import pytest

from cogs import shield
from database import db_automod, db_settings
from systems import shield_whitelist
from tests.conftest import query, run, t
from tests.fakes import FakeMember

KICK = discord.AuditLogAction.kick


@pytest.fixture(autouse=True)
def shield_state(monkeypatch):
    """Shield-Zustand im Speicher leeren und das verzögerte Freigeben nach der Quarantäne abschalten."""
    for state in (shield._shield_action_tracker, shield._restore_cache, shield._restore_flagged,
                  shield._restore_actors, shield._shield_initiated_bans, shield._quarantined_users):
        state.clear()

    async def nichts(*args):
        return None
    monkeypatch.setattr(shield, "_release_quarantine", nichts)


@pytest.fixture
def log_channel(guild):
    channel = guild.add_text_channel("mod-log")
    run(db_settings.update_guild(guild.id, log_channel_id=channel.id))
    run(db_automod.set_antinuke(guild.id, True))
    return channel


@pytest.fixture
def cog(bot):
    c = shield.Shield.__new__(shield.Shield)   # ohne __init__: der startet die Aufräum-Schleife
    c.bot = bot
    return c


def add_bot(guild, name: str, *roles) -> FakeMember:
    member = FakeMember(guild, name, list(roles), bot=True)
    guild.members_by_id[member.id] = member
    return member


def entry(guild, user, action=KICK):
    return types.SimpleNamespace(
        guild=guild, user=user, action=action, target=types.SimpleNamespace(id=12345),
        extra=None, before=None, after=None,
    )


async def three_kicks(cog, guild, user):
    for _ in range(3):
        await cog.on_audit_log_entry_create(entry(guild, user))


def embeds(channel):
    return [e for m in channel.messages.values() for e in m.embeds]


# ── Bewertung ───────────────────────────────────────────────────────────────────────────────────

async def test_mod_bot_ohne_whitelist_loest_vorwarnung_aus(cog, guild, log_channel):
    mod_bot = add_bot(guild, "Mod-Bot")
    await three_kicks(cog, guild, mod_bot)

    [embed] = embeds(log_channel)
    assert "5.62" in embed.description
    # Die Vorwarnung behauptet nicht mehr, es sei etwas wiederhergestellt worden
    assert embed.description == t("shield_warn_desc", user=mod_bot.mention, score="5.62", minutes=2)
    assert "wiederhergestellt" not in embed.description
    # Bei Bots: Hinweis auf die Whitelist
    assert embed.fields[-1].name == t("shield_warn_trusted_title")
    assert (guild.id, mod_bot.id) in shield._restore_flagged


async def test_vorwarnung_fuer_menschen_ohne_whitelist_hinweis(cog, guild, log_channel):
    await three_kicks(cog, guild, guild.moderator)
    [embed] = embeds(log_channel)
    assert t("shield_warn_trusted_title") not in [f.name for f in embed.fields]


async def test_bot_auf_whitelist_wird_nicht_bewertet(cog, guild, log_channel):
    mod_bot = add_bot(guild, "Mod-Bot")
    await db_automod.add_shield_whitelist(guild.id, "bot", [mod_bot.id], guild.owner_id)

    for _ in range(3):   # auch eine zweite Runde bleibt still (ohne Whitelist: Bann)
        await three_kicks(cog, guild, mod_bot)

    assert embeds(log_channel) == []
    assert not shield._shield_action_tracker[guild.id].get(mod_bot.id)


async def test_rolle_auf_whitelist_nimmt_menschen_aus(cog, guild, log_channel):
    team = guild.add_role("Team", 6, kick_members=True)
    mod = guild.add_member("mod", team)
    await db_automod.add_shield_whitelist(guild.id, "role", [team.id], guild.owner_id)

    await three_kicks(cog, guild, mod)
    assert embeds(log_channel) == []

    # Ohne die Rolle wird wieder bewertet
    await three_kicks(cog, guild, guild.moderator)
    assert len(embeds(log_channel)) == 1


async def test_bot_id_eines_menschen_nimmt_ihn_nicht_aus(cog, guild, log_channel):
    """Bots über die ID — trüge jemand die ID eines Menschen ein (z. B. direkt in der DB), zählt sie nicht."""
    await db_automod.add_shield_whitelist(guild.id, "bot", [guild.moderator.id], guild.owner_id)
    await three_kicks(cog, guild, guild.moderator)
    assert len(embeds(log_channel)) == 1


async def test_whitelist_rolle_ohne_mitglied_im_cache_nimmt_nicht_aus(guild):
    team = guild.add_role("Team", 6)
    await db_automod.add_shield_whitelist(guild.id, "role", [team.id], guild.owner_id)
    fremder = types.SimpleNamespace(id=999, bot=False)   # User statt Member, nicht im Cache
    assert not await shield_whitelist.is_exempt(guild, fremder)


# ── Aufräumen ───────────────────────────────────────────────────────────────────────────────────

async def test_bot_verlaesst_server_eintrag_weg(cog, guild):
    mod_bot = add_bot(guild, "Mod-Bot")
    other = add_bot(guild, "Andere")
    await db_automod.add_shield_whitelist(guild.id, "bot", [mod_bot.id, other.id], guild.owner_id)

    await cog.on_member_remove(mod_bot)
    assert (await db_automod.get_shield_whitelist(guild.id)).bots == {other.id}
    rows = await query("SELECT target_id FROM shield_whitelist WHERE guild_id = %s", guild.id)
    assert [r["target_id"] for r in rows] == [other.id]


async def test_rolle_geloescht_eintrag_weg(cog, guild):
    team = guild.add_role("Team", 6)
    await db_automod.add_shield_whitelist(guild.id, "role", [team.id], guild.owner_id)
    await cog.on_guild_role_delete(team)
    assert (await db_automod.get_shield_whitelist(guild.id)).roles == set()
    assert shield._restore_cache[guild.id]   # fürs Restore trotzdem gemerkt


# ── Dashboard: Übersicht ────────────────────────────────────────────────────────────────────────

async def test_uebersicht_bots_mit_mod_rechten_zuerst(dash, guild):
    harmlos = add_bot(guild, "Aaa Musikbot")
    mod_role = guild.add_role("Bot-Rolle", 4, ban_members=True)
    mod_bot = add_bot(guild, "Mod-Bot", mod_role)
    await db_automod.add_shield_whitelist(guild.id, "bot", [mod_bot.id], guild.owner_id)

    data = await dash.ok("automod", "shield-whitelist", user=guild.admin)
    assert [b["id"] for b in data["bots"]] == [str(mod_bot.id), str(harmlos.id)]   # Nexus selbst fehlt
    assert data["bots"][0] == {
        "id": str(mod_bot.id), "name": "Mod-Bot", "avatar": str(mod_bot.display_avatar.url),
        "mod_permissions": True, "whitelisted": True,
    }
    assert data["bots"][1]["mod_permissions"] is False and data["bots"][1]["whitelisted"] is False
    assert (data["can_manage_bots"], data["is_owner"]) == (True, False)
    assert data["limits"] == {"bots": 25, "roles": 10}


async def test_uebersicht_zeigt_eingetragenen_bot_der_nicht_im_cache_ist(dash, guild):
    await db_automod.add_shield_whitelist(guild.id, "bot", [4242], guild.owner_id)
    data = await dash.ok("automod", "shield-whitelist")
    assert data["bots"] == [{"id": "4242", "name": None, "avatar": None, "mod_permissions": False, "whitelisted": True}]


async def test_uebersicht_rechte_je_nach_person(dash, guild):
    owner = await dash.ok("automod", "shield-whitelist", user=guild.owner_member)
    mod = await dash.ok("automod", "shield-whitelist", user=guild.moderator)
    assert (owner["can_manage_bots"], owner["is_owner"]) == (True, True)
    assert (mod["can_manage_bots"], mod["is_owner"]) == (False, False)


async def test_uebersicht_laedt_mitgliederliste_einmal_nach(dash, guild):
    guild.chunked = False
    mod_bot = FakeMember(guild, "Mod-Bot", [], bot=True)
    guild.members_by_id[mod_bot.id] = mod_bot   # kommt erst mit dem Nachladen in den Cache — hier vereinfacht
    await dash.ok("automod", "shield-whitelist")
    await dash.ok("automod", "shield-whitelist")
    assert guild.calls.count(("chunk",)) == 1


# ── Dashboard: Bots ─────────────────────────────────────────────────────────────────────────────

async def test_admin_traegt_bot_ein_und_aus(dash, guild):
    mod_bot = add_bot(guild, "Mod-Bot")
    data = await dash.ok("automod", "shield-bot", {"bot_id": str(mod_bot.id), "whitelisted": True}, user=guild.admin)
    assert data["bots"][0]["whitelisted"] is True
    row = (await query("SELECT * FROM shield_whitelist WHERE guild_id = %s", guild.id))[0]
    assert (row["target_id"], row["kind"], row["added_by"]) == (mod_bot.id, "bot", guild.admin.id)

    # Doppelt eintragen schadet nicht
    await dash.ok("automod", "shield-bot", {"bot_id": str(mod_bot.id), "whitelisted": True}, user=guild.owner_member)

    data = await dash.ok("automod", "shield-bot", {"bot_id": str(mod_bot.id), "whitelisted": False}, user=guild.admin)
    assert data["bots"][0]["whitelisted"] is False
    assert await query("SELECT * FROM shield_whitelist") == []


async def test_server_verwalten_reicht_nicht_fuer_bots(dash, guild):
    mod_bot = add_bot(guild, "Mod-Bot")
    for whitelisted in (True, False):
        error = await dash.error("automod", "shield-bot", {"bot_id": str(mod_bot.id), "whitelisted": whitelisted}, user=guild.moderator)
        assert error == t("dash_err_shield_admin_only")
    assert await query("SELECT * FROM shield_whitelist") == []


async def test_nur_bots_koennen_eingetragen_werden(dash, guild):
    for target in (guild.moderator.id, 777, guild.me.id):   # Mensch, nicht auf dem Server, Nexus selbst
        error = await dash.error("automod", "shield-bot", {"bot_id": str(target), "whitelisted": True})
        assert error == t("dash_err_shield_not_a_bot")


async def test_hoechstens_25_bots(dash, guild):
    bots = [add_bot(guild, f"Bot {i}") for i in range(26)]
    await db_automod.add_shield_whitelist(guild.id, "bot", [b.id for b in bots[:25]], guild.owner_id)
    error = await dash.error("automod", "shield-bot", {"bot_id": str(bots[25].id), "whitelisted": True})
    assert error == t("dash_err_shield_too_many_bots", max=25)


@pytest.mark.parametrize("payload", [
    {"bot_id": 123, "whitelisted": True},
    {"bot_id": "abc", "whitelisted": True},
    {"bot_id": "123", "whitelisted": "true"},
])
def test_bot_ungueltige_anfrage(dash, payload):
    error = run(dash.error("automod", "shield-bot", payload))
    assert error == t("dash_err_invalid_request")


# ── Dashboard: Rollen ───────────────────────────────────────────────────────────────────────────

async def test_owner_setzt_rollen(dash, guild):
    a, b, c = (guild.add_role(n, 3) for n in "ABC")
    data = await dash.ok("automod", "shield-roles", {"role_ids": [str(a.id), str(b.id), str(a.id)]}, user=guild.owner_member)
    assert set(data["roles"]) == {str(a.id), str(b.id)}

    # Ersetzen: b bleibt, a raus, c rein
    data = await dash.ok("automod", "shield-roles", {"role_ids": [str(b.id), str(c.id)]}, user=guild.owner_member)
    assert set(data["roles"]) == {str(b.id), str(c.id)}
    rows = await query("SELECT target_id, kind FROM shield_whitelist ORDER BY target_id")
    assert {(r["target_id"], r["kind"]) for r in rows} == {(b.id, "role"), (c.id, "role")}

    await dash.ok("automod", "shield-roles", {"role_ids": []}, user=guild.owner_member)
    assert await query("SELECT * FROM shield_whitelist") == []


async def test_admin_darf_keine_rollen_setzen(dash, guild):
    team = guild.add_role("Team", 3)
    error = await dash.error("automod", "shield-roles", {"role_ids": [str(team.id)]}, user=guild.admin)
    assert error == t("dash_err_shield_owner_only")
    assert await query("SELECT * FROM shield_whitelist") == []


async def test_ungueltige_rollen(dash, guild):
    bot_role = guild.add_role("Bot-Rolle", 3)
    bot_role.managed = True
    for role_id in (guild.default_role.id, bot_role.id, 31337):
        error = await dash.error("automod", "shield-roles", {"role_ids": [str(role_id)]}, user=guild.owner_member)
        assert error == t("dash_err_shield_role_invalid")


async def test_hoechstens_10_rollen(dash, guild):
    roles = [guild.add_role(f"R{i}", 2) for i in range(11)]
    error = await dash.error("automod", "shield-roles", {"role_ids": [str(r.id) for r in roles]}, user=guild.owner_member)
    assert error == t("dash_err_shield_too_many_roles", max=10)


# ── Shield ein/aus ──────────────────────────────────────────────────────────────────────────────

async def test_admin_schaltet_ein_aber_nicht_aus(dash, guild):
    assert (await dash.ok("automod", "toggle-shield", user=guild.moderator))["shield_enabled"] is True
    for user in (guild.moderator, guild.admin):
        assert await dash.error("automod", "toggle-shield", user=user) == t("dash_err_shield_owner_only")
    assert await db_automod.get_antinuke(guild.id) == 1

    assert (await dash.ok("automod", "toggle-shield", user=guild.owner_member))["shield_enabled"] is False


async def test_status_sagt_wer_ausschalten_darf(dash, guild):
    assert (await dash.ok("automod", "status", user=guild.admin))["shield_can_disable"] is False
    assert (await dash.ok("automod", "status", user=guild.owner_member))["shield_can_disable"] is True


async def test_automod_button_ausschalten_nur_owner(bot, guild):
    from cogs.automod import AutoModView, _load_state
    from tests.fakes import FakeInteraction

    await db_automod.set_antinuke(guild.id, True)
    lang = {}
    view = AutoModView(guild, guild.admin.id, lang, await _load_state(guild, lang))

    admin = FakeInteraction(guild)   # Admin
    await view._toggle_shield(admin)
    assert admin.sent[-1]["content"].endswith("Only the server owner can turn off the Anti-Nuke Shield.")
    assert await db_automod.get_antinuke(guild.id) == 1

    owner = FakeInteraction(guild)
    owner.user = guild.owner_member
    await view._toggle_shield(owner)
    assert await db_automod.get_antinuke(guild.id) == 0

    # Einschalten darf der Admin wieder
    await view._toggle_shield(admin)
    assert await db_automod.get_antinuke(guild.id) == 1
