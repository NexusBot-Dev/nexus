"""Reaction Roles im Dashboard (dashboard/reaction_roles.py → cogs/reaction_roles.py, db_reaction_roles)."""
import pytest

from tests.conftest import query, query_one, run, set_premium, t

RR = "reaction_roles"


@pytest.fixture
def rr(guild):
    """Ein Kanal für die Panels und ein paar vergebbare Rollen (alle unter der Bot-Rolle auf Position 8)."""
    guild.channel = guild.add_text_channel("rollen")
    guild.blau = guild.add_role("Blau", 2)
    guild.rot = guild.add_role("Rot", 3)
    guild.gruen = guild.add_role("Grün", 4)
    # Mitglied, das Rollen verwalten darf, aber kein Admin ist
    guild.role_manager = guild.add_member(
        "rollenverwalter", guild.add_role("Rollenverwalter", 6, manage_roles=True, manage_guild=True))
    return guild


async def create_panel(dash, guild, **overrides) -> dict:
    payload = {"channel_id": str(guild.channel.id), "title": "Farben", "description": "Wähle deine Farbe", **overrides}
    return await dash.ok(RR, "create-message", payload)


async def add_entry(dash, guild, panel, role, emoji, **overrides) -> dict:
    payload = {"channel_id": panel["channel_id"], "message_id": panel["message_id"],
               "role_id": str(role.id), "emoji": emoji, **overrides}
    return await dash.ok(RR, "add-entry", payload)


def panel_message(guild, panel):
    return guild.get_channel(int(panel["channel_id"])).messages.get(int(panel["message_id"]))


async def entries_in_db(guild):
    return await query("SELECT role_id, emoji, mode, group_id, message_id FROM reaction_roles WHERE guild_id = %s ORDER BY id", guild.id)


# ── Panel anlegen ───────────────────────────────────────────────────────────────────────────────

async def test_panel_anlegen(dash, rr):
    panel = await create_panel(dash, rr)
    assert (panel["channel_name"], panel["display_mode"]) == ("rollen", "reaction")

    message = panel_message(rr, panel)
    assert (message.embeds[0].title, message.embeds[0].description) == ("Farben", "Wähle deine Farbe")
    row = await query_one("SELECT * FROM reaction_role_messages WHERE guild_id = %s", rr.id)
    assert (row["message_id"], row["channel_id"], row["title"], row["display_mode"]) == \
        (message.id, rr.channel.id, "Farben", "reaction")


async def test_panel_anlegen_ungueltig(dash, rr):
    assert await dash.error(RR, "create-message", {"channel_id": str(rr.channel.id), "title": "", "description": "x"}) == \
        t("dash_err_rr_text_required")
    assert await dash.error(RR, "create-message", {"channel_id": str(rr.channel.id), "title": "x", "description": "x",
                                                   "display_mode": "liste"}) == t("dash_err_invalid_request")
    assert await dash.error(RR, "create-message", {"channel_id": "12345", "title": "x", "description": "x"}) == \
        t("dash_err_channel_gone")
    assert rr.channel.messages == {}
    assert await query("SELECT * FROM reaction_role_messages") == []


async def test_panel_dropdown_nur_mit_premium(dash, rr):
    assert await dash.error(RR, "create-message", {"channel_id": str(rr.channel.id), "title": "x", "description": "x",
                                                   "display_mode": "dropdown"}) == t("dash_err_rr_dropdown_premium")
    await set_premium(rr)
    panel = await create_panel(dash, rr, display_mode="dropdown")
    assert panel["display_mode"] == "dropdown"


async def test_panel_anlegen_braucht_rollen_verwalten(dash, rr):
    """'Server verwalten' reicht fürs Dashboard, aber nicht, um selbst vergebbare Rollen einzurichten."""
    error = await dash.error(RR, "create-message", {"channel_id": str(rr.channel.id), "title": "x", "description": "x"},
                             user=rr.moderator)
    assert error == t("perm_issue_actor_missing_perms", perms="`Manage Roles`")
    assert rr.channel.messages == {}


async def test_panel_anlegen_ohne_schreibrecht_im_kanal(dash, rr):
    rr.channel.bot_denied = {"send_messages"}
    assert await dash.error(RR, "create-message", {"channel_id": str(rr.channel.id), "title": "x", "description": "x"}) == \
        t("dash_err_no_post_permission")
    assert await query("SELECT * FROM reaction_role_messages") == []


# ── Rollen zuordnen ─────────────────────────────────────────────────────────────────────────────

async def test_rolle_zuordnen_setzt_reaktion(dash, rr):
    panel = await create_panel(dash, rr)
    entry = await add_entry(dash, rr, panel, rr.blau, "🔵")

    assert (entry["role_name"], entry["emoji"], entry["mode"]) == ("Blau", "🔵", "toggle")
    assert panel_message(rr, panel).reactions == ["🔵"]
    assert await entries_in_db(rr) == [
        {"role_id": rr.blau.id, "emoji": "🔵", "mode": "toggle", "group_id": None, "message_id": int(panel["message_id"])},
    ]


async def test_server_emoji_ohne_premium(dash, rr):
    """Seit f1d774c sind eigene Server-Emojis für alle frei."""
    emoji = rr.add_emoji("blau")
    panel = await create_panel(dash, rr)
    await add_entry(dash, rr, panel, rr.blau, emoji)
    assert panel_message(rr, panel).reactions == [emoji]


async def test_ungueltiges_emoji(dash, rr):
    panel = await create_panel(dash, rr)
    for emoji in ("abc", "<:fremd:123456789012345678>"):   # Text bzw. Emoji von einem anderen Server
        error = await dash.error(RR, "add-entry", {"channel_id": panel["channel_id"], "message_id": panel["message_id"],
                                                   "role_id": str(rr.blau.id), "emoji": emoji})
        assert error == t("dash_err_rr_emoji_invalid"), emoji
    assert await entries_in_db(rr) == []


async def test_emoji_und_rolle_nur_einmal_pro_panel(dash, rr):
    panel = await create_panel(dash, rr)
    await add_entry(dash, rr, panel, rr.blau, "🔵")
    base = {"channel_id": panel["channel_id"], "message_id": panel["message_id"]}
    assert await dash.error(RR, "add-entry", {**base, "role_id": str(rr.rot.id), "emoji": "🔵"}) == t("dash_err_rr_emoji_taken")
    assert await dash.error(RR, "add-entry", {**base, "role_id": str(rr.blau.id), "emoji": "🔴"}) == t("dash_err_rr_role_taken")
    assert len(await entries_in_db(rr)) == 1


async def test_hoechstens_20_rollen_pro_reaktions_panel(dash, rr):
    panel = await create_panel(dash, rr)
    emojis = [chr(0x1F600 + i) for i in range(21)]
    roles = [rr.add_role(f"R{i}", 1) for i in range(21)]
    for role, emoji in zip(roles[:20], emojis[:20]):
        await add_entry(dash, rr, panel, role, emoji)
    error = await dash.error(RR, "add-entry", {"channel_id": panel["channel_id"], "message_id": panel["message_id"],
                                               "role_id": str(roles[20].id), "emoji": emojis[20]})
    assert error == t("dash_err_rr_entry_limit", cap=20)


async def test_unique_modus_nur_mit_premium(dash, rr):
    panel = await create_panel(dash, rr)
    group = await dash.ok(RR, "create-group", {"name": "Farben"})
    base = {"channel_id": panel["channel_id"], "message_id": panel["message_id"], "role_id": str(rr.blau.id),
            "emoji": "🔵", "mode": "unique", "group_id": str(group["id"])}
    assert await dash.error(RR, "add-entry", base) == t("dash_err_rr_unique_premium")
    assert panel_message(rr, panel).reactions == []   # abgelehnt, bevor Discord etwas tut

    await set_premium(rr)
    entry = await dash.ok(RR, "add-entry", base)
    assert entry["mode"] == "unique"
    assert (await entries_in_db(rr))[0]["group_id"] == group["id"]


async def test_rolle_ueber_dem_bot_abgelehnt(dash, rr):
    panel = await create_panel(dash, rr)
    error = await dash.error(RR, "add-entry", {"channel_id": panel["channel_id"], "message_id": panel["message_id"],
                                               "role_id": str(rr.admin_role.id), "emoji": "👑"})
    # Mentions werden fürs Dashboard in lesbare Namen umgewandelt
    assert error == t("perm_issue_bot_hierarchy", subject="@Admin")
    assert await entries_in_db(rr) == []


async def test_rolle_mit_mod_rechten_nur_fuer_admins(dash, rr):
    panel = await create_panel(dash, rr)
    mod_rolle = rr.add_role("Mini-Mod", 2, ban_members=True)
    payload = {"channel_id": panel["channel_id"], "message_id": panel["message_id"],
               "role_id": str(mod_rolle.id), "emoji": "🔨"}
    error = await dash.error(RR, "add-entry", payload, user=rr.role_manager)
    assert error.startswith("❌ @Mini-Mod hat Moderations-/Admin-Rechte")
    await dash.ok(RR, "add-entry", payload, user=rr.admin)


async def test_zuordnen_an_unbekannte_nachricht(dash, rr):
    fremd = rr.channel.add_message()   # existiert in Discord, ist aber kein Panel
    error = await dash.error(RR, "add-entry", {"channel_id": str(rr.channel.id), "message_id": str(fremd.id),
                                               "role_id": str(rr.blau.id), "emoji": "🔵"})
    assert error == t("dash_err_rr_unknown_message")
    assert await dash.error(RR, "add-entry", {"channel_id": str(rr.channel.id), "message_id": "999",
                                              "role_id": str(rr.blau.id), "emoji": "🔵"}) == t("dash_err_message_not_found")


async def test_dropdown_panel_bekommt_button_statt_reaktion(dash, rr):
    await set_premium(rr)
    panel = await create_panel(dash, rr, display_mode="dropdown")
    await add_entry(dash, rr, panel, rr.blau, "🔵")

    message = panel_message(rr, panel)
    assert message.reactions == []
    [button] = message.view.children
    assert button.custom_id == f"rr_open_{panel['message_id']}"

    # Letzte Rolle entfernt → Button weg, sonst öffnet er ein leeres Menü
    [entry] = (await dash.ok(RR, "list-messages"))["messages"][0]["entries"]
    await dash.ok(RR, "delete-entry", {"entry_id": str(entry["id"]), "channel_id": panel["channel_id"],
                                       "message_id": panel["message_id"], "emoji": "🔵"})
    assert message.view is None


# ── Übersicht ───────────────────────────────────────────────────────────────────────────────────

async def test_uebersicht(dash, rr):
    panel = await create_panel(dash, rr)
    await add_entry(dash, rr, panel, rr.blau, "🔵")
    await add_entry(dash, rr, panel, rr.rot, "🔴")
    rr.role_list.remove(rr.rot)   # Rolle in Discord gelöscht

    data = await dash.ok(RR, "list-messages")
    assert data["premium_active"] is False
    [msg] = data["messages"]
    assert (msg["message_id"], msg["channel_name"], msg["title"], msg["max_entries"]) == \
        (panel["message_id"], "rollen", "Farben", 20)
    assert [(e["emoji"], e["role_name"]) for e in msg["entries"]] == [("🔵", "Blau"), ("🔴", t("dash_deleted_role"))]
    # Snowflakes als String, JavaScript-Zahlen wären zu ungenau
    assert all(isinstance(e["role_id"], str) for e in msg["entries"])


# ── Bearbeiten ──────────────────────────────────────────────────────────────────────────────────

async def test_text_aendern(dash, rr):
    panel = await create_panel(dash, rr)
    result = await dash.ok(RR, "update-message", {"message_id": panel["message_id"], "title": "Neu", "description": "Neu!"})
    assert (result["status"], result["message_updated"]) == ("updated", True)
    message = panel_message(rr, panel)
    assert (message.embeds[0].title, message.embeds[0].description) == ("Neu", "Neu!")


async def test_text_aendern_wenn_nachricht_weg(dash, rr):
    panel = await create_panel(dash, rr)
    rr.channel.messages.clear()
    result = await dash.ok(RR, "update-message", {"message_id": panel["message_id"], "title": "Neu", "description": "Neu!"})
    assert (result["status"], result["message_updated"]) == ("saved_only", False)
    row = await query_one("SELECT title, description FROM reaction_role_messages WHERE guild_id = %s", rr.id)
    assert row == {"title": "Neu", "description": "Neu!"}   # gespeichert — wird beim Neu-Posten verwendet


async def test_text_aendern_ungueltig(dash, rr):
    panel = await create_panel(dash, rr)
    base = {"message_id": panel["message_id"]}
    assert await dash.error(RR, "update-message", {**base, "title": "", "description": "x"}) == t("dash_err_rr_text_required")
    assert await dash.error(RR, "update-message", {**base, "title": "x" * 257, "description": "x"}) == t("dash_err_rr_title_too_long")
    assert await dash.error(RR, "update-message", {"message_id": "999", "title": "x", "description": "x"}) == \
        t("dash_err_rr_unknown_message")


async def test_rolle_austauschen(dash, rr):
    panel = await create_panel(dash, rr)
    entry = await add_entry(dash, rr, panel, rr.blau, "🔵")
    await add_entry(dash, rr, panel, rr.rot, "🔴")
    base = {"entry_id": str(entry["entry_id"]), "message_id": panel["message_id"]}

    assert await dash.error(RR, "update-entry", {**base, "role_id": str(rr.rot.id)}) == t("dash_err_rr_role_taken")
    result = await dash.ok(RR, "update-entry", {**base, "role_id": str(rr.gruen.id)})
    assert (result["role_name"], result["emoji"]) == ("Grün", "🔵")
    assert [(e["role_id"], e["emoji"]) for e in await entries_in_db(rr)] == [(rr.gruen.id, "🔵"), (rr.rot.id, "🔴")]
    # Gleiche Rolle nochmal: kein Fehler
    await dash.ok(RR, "update-entry", {**base, "role_id": str(rr.gruen.id)})


# ── Entfernen ───────────────────────────────────────────────────────────────────────────────────

async def test_zuordnung_entfernen(dash, rr):
    panel = await create_panel(dash, rr)
    entry = await add_entry(dash, rr, panel, rr.blau, "🔵")
    await add_entry(dash, rr, panel, rr.rot, "🔴")
    payload = {"entry_id": str(entry["entry_id"]), "channel_id": panel["channel_id"],
               "message_id": panel["message_id"], "emoji": "🔵"}

    await dash.ok(RR, "delete-entry", payload)
    assert panel_message(rr, panel).reactions == ["🔴"]
    assert [e["emoji"] for e in await entries_in_db(rr)] == ["🔴"]
    assert await dash.error(RR, "delete-entry", payload) == t("dash_err_rr_entry_not_found")


async def test_panel_entfernen(dash, rr):
    panel = await create_panel(dash, rr)
    await add_entry(dash, rr, panel, rr.blau, "🔵")
    payload = {"channel_id": panel["channel_id"], "message_id": panel["message_id"]}

    assert await dash.ok(RR, "delete-message", payload) == {"message_id": panel["message_id"]}
    assert panel_message(rr, panel).reactions == []   # Nachricht bleibt, nur die Reaktionen gehen
    assert await entries_in_db(rr) == []
    assert await query("SELECT * FROM reaction_role_messages") == []
    assert await dash.error(RR, "delete-message", payload) == t("dash_err_rr_unknown_message")


async def test_panel_entfernen_auch_wenn_kanal_weg(dash, rr):
    panel = await create_panel(dash, rr)
    await add_entry(dash, rr, panel, rr.blau, "🔵")
    del rr.channels_by_id[rr.channel.id]
    await dash.ok(RR, "delete-message", {"channel_id": panel["channel_id"], "message_id": panel["message_id"]})
    assert await entries_in_db(rr) == []


# ── Neu posten ──────────────────────────────────────────────────────────────────────────────────

async def test_neu_posten_nach_geloeschter_nachricht(dash, rr):
    panel = await create_panel(dash, rr)
    await add_entry(dash, rr, panel, rr.blau, "🔵")
    await add_entry(dash, rr, panel, rr.rot, "🔴")
    rr.channel.messages.clear()   # Panel in Discord gelöscht
    neu = rr.add_text_channel("neue-rollen")

    result = await dash.ok(RR, "repost-message", {"message_id": panel["message_id"], "channel_id": str(neu.id)})
    assert (result["old_message_id"], result["channel_name"], result["failed"]) == (panel["message_id"], "neue-rollen", [])

    [message] = neu.messages.values()
    assert str(message.id) == result["message_id"]
    assert message.embeds[0].title == "Farben"
    assert message.reactions == ["🔵", "🔴"]
    # Alle Zuordnungen und der Panel-Eintrag sind auf die neue Nachricht umgezogen
    assert {e["message_id"] for e in await entries_in_db(rr)} == {message.id}
    row = await query_one("SELECT channel_id, message_id FROM reaction_role_messages WHERE guild_id = %s", rr.id)
    assert row == {"channel_id": neu.id, "message_id": message.id}


async def test_neu_posten_nicht_wenn_nachricht_noch_da(dash, rr):
    panel = await create_panel(dash, rr)
    error = await dash.error(RR, "repost-message", {"message_id": panel["message_id"], "channel_id": panel["channel_id"]})
    assert error == t("dash_err_rr_repost_still_there")
    assert len(rr.channel.messages) == 1


async def test_neu_posten_ohne_rechte_im_zielkanal(dash, rr):
    panel = await create_panel(dash, rr)
    rr.channel.messages.clear()
    neu = rr.add_text_channel("gesperrt", bot_denied={"add_reactions"})
    error = await dash.error(RR, "repost-message", {"message_id": panel["message_id"], "channel_id": str(neu.id)})
    assert error.startswith("❌") and "Add Reactions" in error
    assert neu.messages == {}


# ── Gruppen ─────────────────────────────────────────────────────────────────────────────────────

async def test_gruppen(dash, rr):
    group = await dash.ok(RR, "create-group", {"name": "  Teams  "})
    assert group["name"] == "Teams"
    assert await dash.ok(RR, "list-groups") == {"groups": [{"id": group["id"], "name": "Teams"}]}
    for name in ("", "x" * 101):
        assert await dash.error(RR, "create-group", {"name": name}) == t("dash_err_rr_group_name_invalid")


def test_fakes_sind_frisch_pro_test(rr):
    """Absicherung fürs Gerüst: Jeder Test bekommt einen neuen Server und eine leere Datenbank."""
    assert rr.channel.messages == {}
    assert run(query("SELECT * FROM reaction_roles")) == []
