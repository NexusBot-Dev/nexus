"""Stream-Alerts im Dashboard (dashboard/stream_alerts.py, db_stream_alerts, Plattform-Registry)."""
import asyncio

import pytest

from tests.conftest import add_guild, query, query_one, set_premium, t

SA = "stream_alerts"


@pytest.fixture
def sa(guild, platforms):
    guild.alerts = guild.add_text_channel("live")
    guild.other = guild.add_text_channel("streams")
    guild.ping = guild.add_role("Live-Ping", 2)
    platforms.add("twitch", "shroud", "tw-1", "shroud")
    platforms.add("twitch", "pokimane", "tw-2", "Pokimane")
    platforms.add("kick", "trainwreck", "k-1", "Trainwreck")
    platforms.add("youtube", "@mkbhd", "UCyt1", "MKBHD")
    return guild


async def add(dash, guild, platform="twitch", username="shroud", **extra) -> dict:
    """Kanal über das Dashboard hinzufügen — als Admin des jeweiligen Servers, Alerts in dessen #live."""
    return await dash.ok(SA, "add", {"platform": platform, "username": username,
                                     "channel_id": str(guild.alerts.id), **extra}, guild=guild, user=guild.admin)


async def subscriptions(guild):
    return await query("""SELECT gs.channel_id, gs.message, mc.platform, mc.platform_channel_id, mc.streamer_name
                          FROM guild_subscriptions gs JOIN monitored_channels mc ON mc.id = gs.monitored_channel_id
                          WHERE gs.guild_id = %s ORDER BY gs.id""", guild.id)


# ── Übersicht ───────────────────────────────────────────────────────────────────────────────────

async def test_uebersicht_leer(dash, sa):
    data = await dash.ok(SA, "list")
    assert data["subscriptions"] == {"twitch": [], "youtube": [], "kick": []}
    # Grenzwerte kommen aus feature_limits — dieselben Zahlen wie auf der Premium-Seite
    assert data["limits"] == {"twitch": {"free": 1, "premium": 5}, "youtube": {"free": 1, "premium": 3},
                              "kick": {"free": 1, "premium": 5}}
    assert data["premium_active"] is False
    assert data["ping_roles"] == {"all": None, "twitch": None, "youtube": None, "kick": None}


# ── Hinzufügen ──────────────────────────────────────────────────────────────────────────────────

async def test_twitch_kanal_per_link_hinzufuegen(dash, sa, platforms):
    result = await add(dash, sa, username="https://www.twitch.tv/Shroud?ref=x", message="Los geht's!")
    assert (result["streamer_name"], result["platform"]) == ("shroud", "twitch")
    assert ("resolve", "twitch", "shroud") in platforms.calls          # Link → Login, klein geschrieben
    assert ("subscribe", "twitch", "tw-1") in platforms.calls          # EventSub-Abo angelegt

    assert await subscriptions(sa) == [{"channel_id": sa.alerts.id, "message": "Los geht's!", "platform": "twitch",
                                        "platform_channel_id": "tw-1", "streamer_name": "shroud"}]
    monitored = await query_one("SELECT eventsub_online_id, eventsub_offline_id FROM monitored_channels")
    assert monitored == {"eventsub_online_id": "twitch-on-tw-1", "eventsub_offline_id": "twitch-off-tw-1"}

    [entry] = (await dash.ok(SA, "list"))["subscriptions"]["twitch"]
    assert (entry["streamer_name"], entry["channel_name"], entry["message"], entry["live"], entry["active"]) == \
        ("shroud", "live", "Los geht's!", False, True)
    assert entry["subscription_id"] == result["subscription_id"]


async def test_youtube_speichert_upload_playlist(dash, sa):
    await add(dash, sa, platform="youtube", username="https://youtube.com/@mkbhd")
    row = await query_one("SELECT platform_channel_id, youtube_uploads_playlist_id FROM monitored_channels")
    assert row == {"platform_channel_id": "UCyt1", "youtube_uploads_playlist_id": "UUyt1"}


async def test_hinzufuegen_ungueltig(dash, sa, platforms):
    base = {"platform": "twitch", "username": "shroud", "channel_id": str(sa.alerts.id)}
    assert await dash.error(SA, "add", {**base, "platform": "tiktok"}) == t("dash_err_invalid_request")
    assert await dash.error(SA, "add", {**base, "username": " "}) == t("dash_err_sa_username_channel_required")
    assert await dash.error(SA, "add", {**base, "channel_id": ""}) == t("dash_err_sa_username_channel_required")
    assert await dash.error(SA, "add", {**base, "channel_id": "999"}) == t("dash_err_channel_gone")
    assert await dash.error(SA, "add", {**base, "message": "x" * 201}) == t("dash_err_sa_message_too_long")
    assert await dash.error(SA, "add", {**base, "username": "kein name!"}) == t("dash_err_sa_invalid_username")
    assert await dash.error(SA, "add", {**base, "username": "gibtsnicht"}) == t("dash_err_sa_user_not_found", name="gibtsnicht")
    assert await subscriptions(sa) == []
    assert not [c for c in platforms.calls if c[0] == "subscribe"]   # nichts bei Twitch angelegt


async def test_limit_free_und_premium(dash, sa):
    await add(dash, sa, username="shroud")
    assert await dash.error(SA, "add", {"platform": "twitch", "username": "pokimane", "channel_id": str(sa.alerts.id)}) == \
        t("dash_err_sa_limit", limit=1, platform="Twitch")
    await add(dash, sa, platform="kick", username="trainwreck")   # Limit gilt pro Plattform

    await set_premium(sa)
    await add(dash, sa, username="pokimane")
    assert len((await dash.ok(SA, "list"))["subscriptions"]["twitch"]) == 2


async def test_premium_abgelaufen_aeltestes_abo_bleibt_aktiv(dash, sa):
    await set_premium(sa)
    await add(dash, sa, username="shroud")
    await add(dash, sa, username="pokimane")
    await set_premium(sa, False)
    entries = (await dash.ok(SA, "list"))["subscriptions"]["twitch"]
    assert [(e["streamer_name"], e["active"]) for e in entries] == [("shroud", True), ("Pokimane", False)]


async def test_derselbe_streamer_auf_zwei_servern_ein_abo(dash, sa, bot, platforms):
    zweiter = await add_guild(bot)
    zweiter.alerts = zweiter.add_text_channel("live")
    await add(dash, sa)
    await add(dash, zweiter)

    assert [c for c in platforms.calls if c[0] == "subscribe"] == [("subscribe", "twitch", "tw-1")]
    assert len(await query("SELECT * FROM monitored_channels")) == 1
    assert len(await query("SELECT * FROM guild_subscriptions")) == 2


async def test_doppelt_auf_einem_server(dash, sa):
    await set_premium(sa)
    await add(dash, sa)
    assert await dash.error(SA, "add", {"platform": "twitch", "username": "shroud", "channel_id": str(sa.other.id)}) == \
        t("dash_err_sa_already_monitored", name="shroud")


async def test_plattform_nicht_erreichbar(dash, sa, platforms):
    platforms.fail_subscriptions = True
    assert await dash.error(SA, "add", {"platform": "twitch", "username": "shroud", "channel_id": str(sa.alerts.id)}) == \
        t("dash_err_sa_subscription_failed")
    assert await query("SELECT * FROM monitored_channels") == []
    assert await subscriptions(sa) == []


# ── Bearbeiten ──────────────────────────────────────────────────────────────────────────────────

async def test_bearbeiten(dash, sa):
    added = await add(dash, sa, message="alt")
    await dash.ok(SA, "edit", {"monitored_channel_id": added["monitored_channel_id"],
                               "channel_id": str(sa.other.id), "message": "  neu  "})
    [sub] = await subscriptions(sa)
    assert (sub["channel_id"], sub["message"]) == (sa.other.id, "neu")

    # Nur die Nachricht ändern: Kanal bleibt
    await dash.ok(SA, "edit", {"monitored_channel_id": added["monitored_channel_id"], "message": "nur Text"})
    [sub] = await subscriptions(sa)
    assert (sub["channel_id"], sub["message"]) == (sa.other.id, "nur Text")


async def test_hinweis_wenn_bot_im_kanal_nicht_schreiben_darf(dash, sa):
    """Der Alert wird trotzdem gespeichert — oft werden die Kanalrechte erst danach angepasst."""
    added = await add(dash, sa)
    assert added["warning"] is None

    readonly = sa.add_text_channel("nur-lesen", bot_denied={"send_messages", "embed_links"})
    edited = await dash.ok(SA, "edit", {"monitored_channel_id": added["monitored_channel_id"], "channel_id": str(readonly.id)})
    assert edited["warning"] == t("alert_channel_missing_perms", channel="#nur-lesen", perms="Send Messages, Embed Links")
    [sub] = await subscriptions(sa)
    assert sub["channel_id"] == readonly.id

    # Nur die Nachricht geändert: kein Kanal geprüft
    assert (await dash.ok(SA, "edit", {"monitored_channel_id": added["monitored_channel_id"], "message": "x"}))["warning"] is None

    sa.alerts = readonly
    added = await add(dash, sa, "kick", "trainwreck")
    assert added["warning"] == edited["warning"]
    assert len(await subscriptions(sa)) == 2


async def test_bearbeiten_ungueltig(dash, sa, bot):
    added = await add(dash, sa)
    assert await dash.error(SA, "edit", {"monitored_channel_id": added["monitored_channel_id"], "channel_id": "999"}) == \
        t("dash_err_channel_gone")
    # Abo eines anderen Servers lässt sich nicht bearbeiten
    zweiter = await add_guild(bot)
    assert await dash.error(SA, "edit", {"monitored_channel_id": added["monitored_channel_id"], "message": "x"},
                            guild=zweiter, user=zweiter.admin) == t("dash_err_sa_not_found")


# ── Entfernen ───────────────────────────────────────────────────────────────────────────────────

async def test_entfernen_raeumt_plattform_abo_auf(dash, sa, platforms):
    added = await add(dash, sa)
    await dash.ok(SA, "remove", {"monitored_channel_id": added["monitored_channel_id"]})
    assert await subscriptions(sa) == []
    assert await query("SELECT * FROM monitored_channels") == []
    assert ("unsubscribe", "twitch", "tw-1") in platforms.calls
    assert await dash.error(SA, "remove", {"monitored_channel_id": added["monitored_channel_id"]}) == t("dash_err_sa_not_found")


async def test_entfernen_behaelt_abo_fuer_andere_server(dash, sa, bot, platforms):
    zweiter = await add_guild(bot)
    zweiter.alerts = zweiter.add_text_channel("live")
    added = await add(dash, sa)
    await add(dash, zweiter)

    await dash.ok(SA, "remove", {"monitored_channel_id": added["monitored_channel_id"]})
    assert len(await query("SELECT * FROM monitored_channels")) == 1   # der zweite Server braucht es noch
    assert not [c for c in platforms.calls if c[0] == "unsubscribe"]


# ── Ping-Rollen ─────────────────────────────────────────────────────────────────────────────────

async def test_ping_rolle_fuer_alle(dash, sa):
    await dash.ok(SA, "set-ping-role", {"platform": "all", "role_id": str(sa.ping.id)})
    assert (await dash.ok(SA, "list"))["ping_roles"]["all"] == str(sa.ping.id)

    await dash.ok(SA, "set-ping-role", {"platform": "all", "role_id": None})   # entfernen
    assert (await dash.ok(SA, "list"))["ping_roles"]["all"] is None


async def test_ping_rolle_pro_plattform_nur_mit_premium(dash, sa):
    payload = {"platform": "twitch", "role_id": str(sa.ping.id)}
    assert await dash.error(SA, "set-ping-role", payload) == t("dash_err_sa_platform_roles_premium")
    await set_premium(sa)
    await dash.ok(SA, "set-ping-role", payload)
    assert (await dash.ok(SA, "list"))["ping_roles"] == {"all": None, "twitch": str(sa.ping.id), "youtube": None, "kick": None}


async def test_ping_rolle_ungueltig(dash, sa):
    assert await dash.error(SA, "set-ping-role", {"platform": "all", "role_id": "999"}) == t("dash_err_role_gone")
    assert await dash.error(SA, "set-ping-role", {"platform": "tiktok", "role_id": str(sa.ping.id)}) == t("dash_err_invalid_request")
    assert await query("SELECT * FROM guild_ping_roles") == []


# ── Verknüpfungen (derselbe Streamer auf zwei Plattformen → nur ein Alert) ──────────────────────────

async def test_verknuepfen(dash, sa):
    a = await add(dash, sa, username="shroud")
    b = await add(dash, sa, platform="kick", username="trainwreck")
    payload = {"subscription_a_id": a["subscription_id"], "subscription_b_id": b["subscription_id"]}

    link = await dash.ok(SA, "create-link", payload)
    [listed] = (await dash.ok(SA, "list-links"))["links"]
    assert listed["id"] == link["id"]
    assert {listed["a"]["streamer_name"], listed["b"]["streamer_name"]} == {"shroud", "Trainwreck"}

    assert await dash.error(SA, "create-link", payload) == t("dash_err_sa_link_exists")
    reversed_payload = {"subscription_a_id": b["subscription_id"], "subscription_b_id": a["subscription_id"]}
    assert await dash.error(SA, "create-link", reversed_payload) == t("dash_err_sa_link_exists")

    await dash.ok(SA, "delete-link", {"link_id": link["id"]})
    assert (await dash.ok(SA, "list-links"))["links"] == []
    assert await dash.error(SA, "delete-link", {"link_id": link["id"]}) == t("dash_err_sa_link_not_found")


async def test_verknuepfen_ungueltig(dash, sa, bot):
    a = await add(dash, sa)
    assert await dash.error(SA, "create-link", {"subscription_a_id": a["subscription_id"]}) == t("dash_err_sa_link_both_required")
    assert await dash.error(SA, "create-link", {"subscription_a_id": a["subscription_id"],
                                                "subscription_b_id": a["subscription_id"]}) == t("dash_err_sa_link_self")
    # Abo eines anderen Servers
    zweiter = await add_guild(bot)
    zweiter.alerts = zweiter.add_text_channel("live")
    fremd = await add(dash, zweiter, platform="kick", username="trainwreck")
    assert await dash.error(SA, "create-link", {"subscription_a_id": a["subscription_id"],
                                                "subscription_b_id": fremd["subscription_id"]}) == t("dash_err_sa_not_found")


async def test_verknuepfung_verschwindet_mit_abo(dash, sa):
    a = await add(dash, sa)
    b = await add(dash, sa, platform="kick", username="trainwreck")
    await dash.ok(SA, "create-link", {"subscription_a_id": a["subscription_id"], "subscription_b_id": b["subscription_id"]})
    await dash.ok(SA, "remove", {"monitored_channel_id": b["monitored_channel_id"]})
    assert (await dash.ok(SA, "list-links"))["links"] == []


# ── Gleichzeitige Änderungen ────────────────────────────────────────────────────────────────────

async def add_raw(dash, guild, platform="twitch", username="shroud"):
    """Wie add(), gibt aber (Status, Antwort) zurück statt bei Fehlern abzubrechen."""
    return await dash.action(SA, "add", {"platform": platform, "username": username, "channel_id": str(guild.alerts.id)},
                             guild=guild, user=guild.admin)


async def test_gleichzeitig_zwei_server_neuer_streamer(dash, sa, bot, platforms):
    """Beide Server fügen fast zeitgleich einen Streamer hinzu, den noch niemand überwacht."""
    zweiter = await add_guild(bot)
    zweiter.alerts = zweiter.add_text_channel("live")
    results = await asyncio.gather(add_raw(dash, sa), add_raw(dash, zweiter))
    assert [status for status, _ in results] == [200, 200], results
    assert [c for c in platforms.calls if c[0] == "subscribe"] == [("subscribe", "twitch", "tw-1")]
    assert len(await subscriptions(sa)) == len(await subscriptions(zweiter)) == 1


async def test_gleichzeitig_doppelt_auf_einem_server(dash, sa):
    """Doppelklick oder zwei Admins: einer gewinnt, der andere bekommt eine verständliche Meldung."""
    await set_premium(sa)   # sonst greift beim Zweiten schon das Free-Limit
    results = sorted(await asyncio.gather(add_raw(dash, sa), add_raw(dash, sa)), key=lambda r: r[0])
    assert results[0][0] == 200
    assert results[1] == (400, {"ok": False, "error": t("dash_err_sa_already_monitored", name="shroud")})
    assert len(await subscriptions(sa)) == 1


async def test_gleichzeitig_limit_wird_eingehalten(dash, sa):
    results = await asyncio.gather(add_raw(dash, sa, username="shroud"), add_raw(dash, sa, username="pokimane"))
    assert sorted(status for status, _ in results) == [200, 400]
    assert len(await subscriptions(sa)) == 1


async def test_gleichzeitig_entfernen_und_hinzufuegen(dash, sa, bot, platforms):
    """Server A entfernt den letzten Alert für einen Streamer, während Server B ihn hinzufügt:
    B's Alert darf dabei nicht still mit verschwinden."""
    added = await add(dash, sa)
    zweiter = await add_guild(bot)
    zweiter.alerts = zweiter.add_text_channel("live")

    removed, (status, body) = await asyncio.gather(
        dash.action(SA, "remove", {"monitored_channel_id": added["monitored_channel_id"]}, guild=sa, user=sa.admin),
        add_raw(dash, zweiter),
    )
    assert removed[0] == 200 and status == 200, (removed, body)
    assert await subscriptions(sa) == []
    [sub] = await subscriptions(zweiter)
    assert sub["platform_channel_id"] == "tw-1"
    assert ("twitch", "tw-1") in platforms.active   # Webhook-Abo bei Twitch besteht weiter


def test_jeder_abbruchgrund_hat_eine_meldung():
    """Slash-Commands und Dashboard übersetzen jeden Abbruchgrund aus stream_alert_service — in allen Sprachen."""
    from cogs.stream_alerts import ALERT_ERRORS, alert_error_text
    from dashboard.stream_alerts import _ALERT_ERRORS, _alert_error
    from systems.stream_alert_service import AlertError
    from translations import TRANSLATIONS

    reasons = {"limit", "user_not_found", "subscription_failed", "already_monitored", "not_monitored"}
    assert set(ALERT_ERRORS) == set(_ALERT_ERRORS) == reasons
    for reason in reasons:
        error = AlertError(reason, limit=3) if reason == "limit" else AlertError(reason)
        for code, lang in TRANSLATIONS.items():
            text = alert_error_text(lang, error, "shroud")
            assert "{" not in text and ("shroud" in text or "3" in text or reason == "subscription_failed"), (code, text)
            assert "{" not in _alert_error(error, "shroud", "twitch").render(lang), (code, reason)
