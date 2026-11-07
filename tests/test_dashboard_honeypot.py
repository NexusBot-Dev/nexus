"""Honeypot im Dashboard (dashboard/automod.py → systems/honeypot.py)."""
import discord

from tests.conftest import query_one, t

CREATE = {"name": "Nicht Schreiben!", "title": "Finger weg", "warn_text": "Wer hier schreibt, fliegt."}


async def settings_row(guild):
    return await query_one("SELECT honeypot_channel_id FROM guild_settings WHERE guild_id = %s", guild.id)


async def honeypot_row(guild):
    return await query_one("SELECT * FROM honeypot_settings WHERE guild_id = %s", guild.id)


async def create(dash, **overrides):
    return await dash.ok("automod", "create-honeypot", {**CREATE, **overrides})


# ── Status ──────────────────────────────────────────────────────────────────────────────────────

async def test_status_ohne_honeypot(dash):
    data = await dash.ok("automod", "status")
    assert data["honeypot_channel_id"] is None
    assert data["honeypot_title"] == t("honeypot_channel_title")   # Standardtitel in der Server-Sprache
    assert (data["honeypot_punish_members"], data["honeypot_punish_bots"], data["honeypot_timeout_days"]) == ("timeout", "kick", 7)
    assert data["honeypot_catches"] == 0
    assert data["honeypot_day_options"] == [1, 3, 7, 14, 28]


# ── Anlegen ─────────────────────────────────────────────────────────────────────────────────────

async def test_anlegen(dash, guild):
    data = await create(dash)
    channel = guild.get_channel(int(data["channel_id"]))

    assert channel.name == "nicht-schreiben" == data["channel_name"]
    assert (data["title"], data["text"]) == ("Finger weg", "Wer hier schreibt, fliegt.")
    # Jeder darf den Kanal sehen und schreiben — sonst tappt niemand in die Falle
    everyone = channel.overwrites[guild.default_role]
    assert everyone.view_channel and everyone.send_messages

    [message] = channel.messages.values()
    embed = message.embeds[0]
    assert (embed.title, embed.description) == ("Finger weg", "Wer hier schreibt, fliegt.")
    assert "0" in embed.footer.text   # Fang-Zähler

    assert (await settings_row(guild))["honeypot_channel_id"] == channel.id
    row = await honeypot_row(guild)
    assert (row["message_id"], row["warn_title"], row["warn_text"]) == (message.id, "Finger weg", "Wer hier schreibt, fliegt.")

    status = await dash.ok("automod", "status")
    assert status["honeypot_channel_id"] == str(channel.id)
    assert (status["honeypot_title"], status["honeypot_text"]) == ("Finger weg", "Wer hier schreibt, fliegt.")


async def test_anlegen_ohne_titel_nimmt_standard(dash, guild):
    data = await create(dash, title="")
    assert data["title"] == t("honeypot_channel_title")


async def test_anlegen_ohne_name_oder_text(dash, guild):
    assert await dash.error("automod", "create-honeypot", {**CREATE, "warn_text": "  "}) == t("dash_err_am_honeypot_text_required")
    assert await dash.error("automod", "create-honeypot", {**CREATE, "name": ""}) == t("dash_err_am_honeypot_text_required")
    assert guild.channels == []


async def test_anlegen_zu_langer_text(dash, guild):
    assert await dash.error("automod", "create-honeypot", {**CREATE, "warn_text": "x" * 1001}) == t("dash_err_am_honeypot_text_too_long")
    assert guild.channels == []


async def test_zweiter_honeypot_abgelehnt(dash, guild):
    await create(dash)
    assert await dash.error("automod", "create-honeypot", CREATE) == t("dash_err_am_honeypot_exists")
    assert len(guild.channels) == 1


async def test_anlegen_ohne_kanalrechte(dash, guild):
    guild.bot_role.permissions = discord.Permissions(send_messages=True, embed_links=True)
    error = await dash.error("automod", "create-honeypot", CREATE)
    assert error == t("perm_issue_missing_perms", perms="`Manage Channels`, `Manage Roles`")
    assert guild.channels == []
    assert (await settings_row(guild))["honeypot_channel_id"] is None


# ── Text ändern ─────────────────────────────────────────────────────────────────────────────────

async def test_text_aendern_bearbeitet_nachricht(dash, guild):
    data = await create(dash)
    channel = guild.get_channel(int(data["channel_id"]))
    [message] = channel.messages.values()

    result = await dash.ok("automod", "set-honeypot-text", {"title": "Neu", "warn_text": "Neuer Text"})
    assert result == {"title": "Neu", "text": "Neuer Text", "status": "updated"}
    assert (message.embeds[0].title, message.embeds[0].description) == ("Neu", "Neuer Text")
    assert len(channel.messages) == 1   # bearbeitet, nicht neu gepostet
    row = await honeypot_row(guild)
    assert (row["warn_title"], row["warn_text"]) == ("Neu", "Neuer Text")


async def test_text_aendern_postet_neu_wenn_nachricht_weg(dash, guild):
    data = await create(dash)
    channel = guild.get_channel(int(data["channel_id"]))
    channel.messages.clear()   # jemand hat die Warn-Nachricht gelöscht

    result = await dash.ok("automod", "set-honeypot-text", {"title": "", "warn_text": "Wieder da"})
    assert result["status"] == "reposted"
    [message] = channel.messages.values()
    assert message.embeds[0].description == "Wieder da"
    assert (await honeypot_row(guild))["message_id"] == message.id


async def test_text_aendern_ohne_text_oder_ohne_honeypot(dash):
    assert await dash.error("automod", "set-honeypot-text", {"warn_text": ""}) == t("dash_err_am_honeypot_warn_text_required")
    assert await dash.error("automod", "set-honeypot-text", {"warn_text": "x"}) == t("dash_err_am_honeypot_missing")


# ── Bestrafung ──────────────────────────────────────────────────────────────────────────────────

async def test_bestrafung_speichern(dash, guild):
    assert await dash.ok("automod", "set-honeypot-punishment", {"members": "ban", "days": 14}) == \
        {"members": "ban", "bots": "kick", "days": 14}
    row = await honeypot_row(guild)
    assert (row["punish_members"], row["punish_bots"], row["timeout_days"]) == ("ban", "kick", 14)

    status = await dash.ok("automod", "status")
    assert (status["honeypot_punish_members"], status["honeypot_timeout_days"]) == ("ban", 14)


async def test_bestrafung_ungueltig(dash, guild):
    invalid = t("dash_err_invalid_request")
    assert await dash.error("automod", "set-honeypot-punishment", {"members": "kill"}) == invalid
    assert await dash.error("automod", "set-honeypot-punishment", {}) == invalid
    days_error = t("dash_err_am_honeypot_days_range", max=28)
    for days in (0, 29, "7", True):
        assert await dash.error("automod", "set-honeypot-punishment", {"days": days}) == days_error, days
    row = await honeypot_row(guild)
    assert row is None or (row["punish_members"], row["timeout_days"]) == ("timeout", 7)


# ── Löschen ─────────────────────────────────────────────────────────────────────────────────────

async def test_loeschen_behaelt_einstellungen(dash, guild):
    data = await create(dash)
    channel_id = int(data["channel_id"])
    await dash.ok("automod", "set-honeypot-punishment", {"members": "kick"})

    assert await dash.ok("automod", "delete-honeypot") == {"deleted": True}
    assert guild.get_channel(channel_id) is None
    assert ("channel.delete", channel_id) in guild.calls
    assert (await settings_row(guild))["honeypot_channel_id"] is None
    row = await honeypot_row(guild)
    assert row["message_id"] is None
    assert (row["warn_title"], row["warn_text"], row["punish_members"]) == ("Finger weg", "Wer hier schreibt, fliegt.", "kick")

    # Danach lässt sich wieder einer anlegen
    await create(dash)


async def test_loeschen_ohne_honeypot(dash):
    assert await dash.error("automod", "delete-honeypot") == t("dash_err_am_honeypot_missing")


async def test_von_hand_geloeschter_kanal_gilt_als_kein_honeypot(dash, guild):
    data = await create(dash)
    del guild.channels_by_id[int(data["channel_id"])]   # in Discord gelöscht, Bot hat es nicht mitbekommen

    status = await dash.ok("automod", "status")
    assert status["honeypot_channel_id"] is None
    assert (await settings_row(guild))["honeypot_channel_id"] is None
    assert (await honeypot_row(guild))["message_id"] is None
