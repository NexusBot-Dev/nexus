"""Premium-Hinweis an gesperrten Funktionen und die /premium-Übersicht (systems/premium_ui.py)."""
import types

import discord

from cogs.embeds import EmbedHubView
from config import PREMIUM_SKU_ID
from systems import premium_ui
from tests.conftest import query, set_premium, t
from tests.fakes import FakeInteraction
from translations import TRANSLATIONS

LANG = TRANSLATIONS["de"]


def buttons(view) -> list:
    return list(view.children) if view else []


def has_buy_button(view) -> bool:
    return any(b.sku_id == PREMIUM_SKU_ID for b in buttons(view))


async def click_schedule(guild, **kwargs) -> FakeInteraction:
    interaction = FakeInteraction(guild, **kwargs)
    view = EmbedHubView(LANG, guild.admin.id)
    await view.btn_schedule.callback(interaction)
    return interaction


async def test_planen_ohne_premium_zeigt_hinweis_mit_kauf_button(guild):
    interaction = await click_schedule(guild)
    [message] = interaction.sent
    assert message["ephemeral"] is True
    assert message["embed"].title == t("premium_upsell_schedule_title")
    assert message["embed"].description == t("premium_upsell_schedule_desc", embeds=100)   # Limit aus feature_limits
    view = message["view"]
    assert has_buy_button(view)
    assert [b.label for b in buttons(view) if not b.sku_id] == [t("premium_upsell_more_info")]


async def test_mehr_infos_zeigt_premium_uebersicht_an_derselben_stelle(guild):
    interaction = await click_schedule(guild)
    view = interaction.sent[0]["view"]
    await view.more_info.callback(interaction)

    [edit] = interaction.edits
    assert edit["embed"].title == t("premium_title")
    assert edit["embed"].fields[0].value == t("premium_status_none")
    assert has_buy_button(edit["view"])
    assert len(interaction.sent) == 1   # keine zweite Nachricht


async def test_sku_abgelehnt_hinweis_ohne_kauf_button(guild):
    """Ist die SKU (noch) nicht veröffentlicht, kommt der Hinweis trotzdem — nur ohne Kauf-Button."""
    interaction = await click_schedule(guild, reject_sku=True)
    [message] = interaction.sent
    assert message["embed"].title == t("premium_upsell_schedule_title")
    assert not has_buy_button(message["view"])
    await message["view"].more_info.callback(interaction)
    assert interaction.edits[0]["view"] is None


async def test_mit_premium_kein_hinweis(guild):
    await set_premium(guild)
    interaction = await click_schedule(guild)
    # Ohne gespeicherte Embeds kommt direkt der normale Hinweis der Auswahl — kein Premium-Hinweis
    assert [m["content"] for m in interaction.sent] == [t("embed_list_empty")]


async def test_premium_uebersicht_mit_premium_ohne_kauf_button(guild):
    await set_premium(guild)
    embed, show_buy = await premium_ui.build_overview(guild.id, LANG)
    assert show_buy is False
    assert isinstance(embed, discord.Embed)


# ── Weitere Premium-Stellen ─────────────────────────────────────────────────────────────────────

FEATURES = {
    "schedule": {}, "rr_unique": {}, "color": {},
    "alert_limit": {"limit": 1, "max": 5, "platform": "Twitch"},
    "ticket_limit": {"limit": 2, "max": 10},
    "embed_limit": {"limit": 5},
    "welcome_limit": {"limit": 1, "max": 5},
    "boost_limit": {"limit": 1, "max": 5},
}


def test_jeder_hinweis_hat_texte_in_allen_sprachen():
    for code, lang in TRANSLATIONS.items():
        for feature, params in FEATURES.items():
            values = {"embeds": 100, **params}
            title = lang[f"premium_upsell_{feature}_title"].format(**values)
            desc = lang[f"premium_upsell_{feature}_desc"].format(**values)
            assert title.startswith("⭐") and "{" not in title + desc, (code, feature)


def upsell_of(interaction) -> discord.Embed:
    [message] = interaction.sent
    assert has_buy_button(message["view"])
    return message["embed"]


async def test_reaction_roles_unique_modus(guild, bot):
    from cogs.reaction_roles import ModeSelectView
    interaction = FakeInteraction(guild)
    view = ModeSelectView(bot, guild.admin.id, None, None, None, "🎮", LANG, has_premium=False)
    await view.unique_btn.callback(interaction)
    assert upsell_of(interaction).title == t("premium_upsell_rr_unique_title")


async def test_eigene_farbe_mit_gespeicherter_farbe(guild):
    from cogs.admin_commands import SettingsView
    await query("UPDATE guild_settings SET color = %s WHERE guild_id = %s", 0xFF0000, guild.id)
    interaction = FakeInteraction(guild)
    await SettingsView(guild.admin.id, LANG, guild.id).change_color.callback(interaction)
    embed = upsell_of(interaction)
    assert embed.title == t("premium_upsell_color_title")
    assert embed.description.endswith(t("color_premium_stored_hint", color="`#FF0000`"))

    await set_premium(guild)   # mit Premium öffnet sich das Farb-Formular
    interaction = FakeInteraction(guild)
    await SettingsView(guild.admin.id, LANG, guild.id).change_color.callback(interaction)
    assert len(interaction.modals) == 1 and interaction.sent == []


async def test_ticket_kategorien_limit(guild, bot):
    from cogs.tickets import Tickets
    cog = types.SimpleNamespace()   # Tickets(bot) startet Hintergrund-Schleifen — für add_category nicht nötig
    for i in range(2):   # Free-Limit 2
        await query("INSERT INTO ticket_categories (guild_id, name, category_channel_id) VALUES (%s, %s, 1)", guild.id, f"Kat {i}")
    interaction = FakeInteraction(guild)
    await Tickets.add_category.callback(cog, interaction)
    assert upsell_of(interaction).description == t("premium_upsell_ticket_limit_desc", limit=2, max=10)

    await set_premium(guild)
    interaction = FakeInteraction(guild)
    await Tickets.add_category.callback(cog, interaction)
    assert len(interaction.modals) == 1   # mit Premium noch Platz


async def test_embed_limit(guild):
    from cogs.embeds import _embed_limit_reached
    interaction = FakeInteraction(guild)
    await _embed_limit_reached(interaction, LANG)
    assert upsell_of(interaction).description == t("premium_upsell_embed_limit_desc", limit=5, embeds=100)

    await set_premium(guild)   # mit Premium: normale Meldung
    interaction = FakeInteraction(guild)
    await _embed_limit_reached(interaction, LANG)
    assert [m["content"] for m in interaction.sent] == [t("embed_limit_reached", limit=100)]


async def test_willkommensnachrichten_limit(guild):
    from cogs.welcome_message import WelcomeManageView
    messages = [{"id": 1, "message": "Hallo {user}"}]
    view = WelcomeManageView(guild.admin.id, guild.id, LANG, messages, 1, upgrade_max=5)
    assert not view.add_btn.disabled   # ohne Premium klickbar, zeigt den Hinweis
    interaction = FakeInteraction(guild)
    await view.add_btn.callback(interaction)
    assert upsell_of(interaction).description == t("premium_upsell_welcome_limit_desc", limit=1, max=5)

    # Mit Premium (kein Upgrade mehr möglich) ist der Button am Limit gesperrt
    assert WelcomeManageView(guild.admin.id, guild.id, LANG, messages, 1).add_btn.disabled


async def test_boost_nachrichten_limit(guild):
    from cogs.boost_message import BoostManageView
    view = BoostManageView(guild.admin.id, guild.id, LANG, [{"id": 1, "message": "Danke!"}], 1, upgrade_max=5)
    interaction = FakeInteraction(guild)
    await view.add_btn.callback(interaction)
    assert upsell_of(interaction).title == t("premium_upsell_boost_limit_title")


async def test_stream_alert_limit(guild, platforms):
    from cogs.stream_alerts import AddChannelModal
    from systems.platform_registry import PLATFORM_HANDLERS
    live = guild.add_text_channel("live")
    platforms.add("twitch", "shroud", "tw-1")
    platforms.add("twitch", "pokimane", "tw-2")

    async def submit(login):
        modal = AddChannelModal(LANG, "twitch", "Twitch", PLATFORM_HANDLERS["twitch"], live)
        modal.streamer_input._value, modal.message_input._value = login, ""
        interaction = FakeInteraction(guild)
        await modal.on_submit(interaction)
        return interaction

    await submit("shroud")   # Free-Limit 1 erreicht
    interaction = await submit("pokimane")
    [message] = interaction.sent
    assert has_buy_button(message["view"])
    embed = message["embed"]
    assert embed.title == t("premium_upsell_alert_limit_title", platform="Twitch")
    assert embed.description == t("premium_upsell_alert_limit_desc", limit=1, max=5, platform="Twitch")
