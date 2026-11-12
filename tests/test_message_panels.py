"""/welcome_message und /boost_message: eigene Nachrichten anlegen, bearbeiten, löschen (Panel in Discord)."""
import pytest

from cogs.boost_message import BoostMessages
from cogs.welcome_message import WelcomeMessages
from database import db_boosts, db_welcome
from emojis import NexusEmojis
from tests.conftest import set_premium, t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

# (Textpräfix, Cog, Befehl, Lesen, Anlegen)
PANELS = [
    ("welcome", WelcomeMessages, "welcome_message", db_welcome.get_welcome_messages, db_welcome.add_welcome_message),
    ("boost", BoostMessages, "boost_message", db_boosts.get_boost_messages, db_boosts.add_boost_message),
]
IDS = [p[0] for p in PANELS]


@pytest.fixture(params=PANELS, ids=IDS)
def panel(request, bot):
    key, cog_cls, command, get, add = request.param

    class Panel:
        def __init__(self):
            self.key, self.get, self.add = key, get, add

        async def open(self, guild):
            """Befehl ausführen; gibt (Embed, View) der Antwort zurück."""
            interaction = FakeInteraction(guild)
            await getattr(cog_cls, command).callback(cog_cls(bot), interaction)
            [message] = interaction.sent
            return message["embed"], message["view"]

        def t(self, suffix, **params):
            return t(f"{key}_msg_{suffix}", **params)

    return Panel()


async def texts(panel, guild) -> list[str]:
    return [m["message"] for m in await panel.get(guild.id)]


async def test_leeres_panel(panel, guild):
    embed, view = await panel.open(guild)
    assert embed.title == panel.t("panel_title")
    assert embed.description == panel.t("panel_default_notice")
    assert embed.footer.text == TRANSLATIONS["de"][f"{panel.key}_msg_panel_placeholders"]   # enthält {user} usw. wörtlich
    assert (view.add_btn.disabled, view.edit_btn.disabled, view.delete_btn.disabled) == (False, True, True)


async def test_hinzufuegen(panel, guild):
    _, view = await panel.open(guild)
    interaction = FakeInteraction(guild)
    await view.add_btn.callback(interaction)
    [modal] = interaction.modals
    assert modal.title == panel.t("modal_title")
    modal.message_input._value = "Hallo {user}!"
    reply = FakeInteraction(guild)
    await modal.on_submit(reply)
    assert await texts(panel, guild) == ["Hallo {user}!"]
    assert reply.sent[0]["content"] == panel.t("added", nexus_checkmark=NexusEmojis.CHECKMARK)


async def test_eine_nachricht_bearbeiten_und_loeschen(panel, guild):
    await panel.add(guild.id, "Alt")
    embed, view = await panel.open(guild)
    assert [f.value for f in embed.fields] == ["Alt"]

    interaction = FakeInteraction(guild)
    await view.edit_btn.callback(interaction)
    [modal] = interaction.modals
    assert (modal.title, modal.message_input.default) == (panel.t("modal_edit_title"), "Alt")
    modal.message_input._value = "Neu"
    await modal.on_submit(FakeInteraction(guild))
    assert await texts(panel, guild) == ["Neu"]

    [row] = await panel.get(guild.id)
    interaction = FakeInteraction(guild)
    await view.delete_btn.callback(interaction)
    [confirm] = interaction.sent
    assert confirm["content"] == f"{NexusEmojis.WARNING} " + panel.t("delete_confirm", id=row["id"])
    await confirm["view"].confirm_btn.callback(FakeInteraction(guild))
    assert await texts(panel, guild) == []


async def test_mehrere_nachrichten_ueber_auswahl(panel, guild):
    await set_premium(guild)   # Platz für mehrere
    first = await panel.add(guild.id, "Eins")
    await panel.add(guild.id, "Zwei")
    _, view = await panel.open(guild)

    interaction = FakeInteraction(guild)
    await view.edit_btn.callback(interaction)
    [pick] = interaction.sent
    assert pick["content"] == panel.t("pick_edit")
    select_view = pick["view"]
    select_view.select_msg._values = [str(first)]
    choose = FakeInteraction(guild)
    await select_view.select_msg.callback(choose)
    [modal] = choose.modals
    assert modal.message_input.default == "Eins"

    interaction = FakeInteraction(guild)
    await view.delete_btn.callback(interaction)
    select_view = interaction.sent[0]["view"]
    select_view.select_msg._values = [str(first)]
    choose = FakeInteraction(guild)
    await select_view.select_msg.callback(choose)
    [step] = choose.edits   # Bestätigung an derselben Stelle
    assert step["content"] == f"{NexusEmojis.WARNING} " + panel.t("delete_confirm", id=first)
    await step["view"].confirm_btn.callback(FakeInteraction(guild))
    assert await texts(panel, guild) == ["Zwei"]


async def test_abbrechen_loescht_nichts(panel, guild):
    await panel.add(guild.id, "Bleibt")
    _, view = await panel.open(guild)
    interaction = FakeInteraction(guild)
    await view.delete_btn.callback(interaction)
    cancel = FakeInteraction(guild)
    await interaction.sent[0]["view"].cancel_btn.callback(cancel)
    assert cancel.edits[0]["content"] == panel.t("delete_cancelled")
    assert await texts(panel, guild) == ["Bleibt"]


async def test_limit_ohne_und_mit_premium(panel, guild):
    await panel.add(guild.id, "Eins")   # Free-Limit 1
    _, view = await panel.open(guild)
    assert not view.add_btn.disabled   # ohne Premium klickbar → Premium-Hinweis
    interaction = FakeInteraction(guild)
    await view.add_btn.callback(interaction)
    assert interaction.sent[0]["embed"].title == f"{NexusEmojis.PREMIUM} " + t(f"premium_upsell_{panel.key}_limit_title")
    assert interaction.modals == []

    await set_premium(guild)
    for i in range(4):
        await panel.add(guild.id, f"Mehr {i}")
    _, view = await panel.open(guild)
    assert view.add_btn.disabled   # mit Premium am Limit (5) gesperrt


async def test_nachrichten_ueber_dem_limit_sind_inaktiv(panel, guild):
    await panel.add(guild.id, "Aktiv")
    await panel.add(guild.id, "Zu viel")   # z. B. nach abgelaufenem Premium
    embed, _ = await panel.open(guild)
    assert embed.description == f"{NexusEmojis.LOCKED} " + t("msg_inactive_hint", limit=1)
    assert [f.name.endswith(t("msg_inactive_tag")) for f in embed.fields] == [False, True]

