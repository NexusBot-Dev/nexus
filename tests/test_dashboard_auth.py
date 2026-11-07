"""Zugriffsschutz der Dashboard-Schnittstelle (dashboard/dashboard_auth.py) — gilt für alle Module."""
from tests.conftest import t
from tests.fakes import FakeGuild

STATUS = "/api/dashboard/module/automod/status"


async def test_ohne_token_abgelehnt(dash, guild):
    status, body = await dash.raw(STATUS, {"guild_id": str(guild.id), "acting_user_id": str(guild.admin.id)}, token=None)
    assert (status, body["error"]) == (401, "missing_token")


async def test_falscher_token_abgelehnt(dash, guild):
    status, body = await dash.raw(STATUS, {"guild_id": str(guild.id), "acting_user_id": str(guild.admin.id)}, token="x" * 40)
    assert (status, body["error"]) == (401, "invalid_token")


async def test_kaputter_body_abgelehnt(dash):
    assert await dash.raw(STATUS, ["keine", "dict"]) == (400, {"ok": False, "error": "invalid_request"})
    assert await dash.raw(STATUS, {"acting_user_id": "1"}) == (400, {"ok": False, "error": "invalid_request"})


async def test_ohne_user_abgelehnt(dash, guild):
    status, body = await dash.raw(STATUS, {"guild_id": str(guild.id)})
    assert (status, body["error"]) == (401, "missing_user")


async def test_mitglied_ohne_rechte_abgelehnt(dash, guild):
    assert await dash.error("automod", "status", user=guild.member, status=403) == "guild_access_denied"


async def test_server_verwalten_reicht(dash, guild):
    await dash.ok("automod", "status", user=guild.moderator)


async def test_owner_ohne_rollen_darf(dash, guild):
    await dash.ok("automod", "status", user=guild.owner)


async def test_fremder_server_abgelehnt(dash, guild):
    """Der Bot ist auf dem anderen Server nicht — auch ein Admin von hier kommt nicht dran."""
    other = FakeGuild(name="Fremd")
    assert await dash.error("automod", "status", guild=other, user=guild.admin, status=403) == "guild_access_denied"


async def test_unbekannte_aktion(dash):
    assert await dash.error("automod", "gibt-es-nicht", status=404) == "unknown_action"


async def test_fehler_in_dashboard_sprache(dash):
    """Fehlermeldungen kommen in der Sprache des Dashboards, nicht der Bot-Sprache des Servers."""
    assert await dash.error("automod", "set-honeypot-text", {"warn_text": "x"}, lang="en") == t("dash_err_am_honeypot_missing", "en")
    assert await dash.error("automod", "set-honeypot-text", {"warn_text": "x"}, lang="de") == t("dash_err_am_honeypot_missing", "de")
