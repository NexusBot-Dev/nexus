"""/stream_schedule: Streamplan-Abgleich pro Twitch-Kanal ein- und ausschalten (cogs/stream_alerts.py)."""
import asyncio
from datetime import datetime, timedelta, timezone

import discord
import pytest

from cogs.help import MODULE_INFO
from cogs.stream_alerts import StreamAlerts
from config import PREMIUM_SKU_ID
from database import db_stream_alerts, db_stream_schedule
from emojis import NexusEmojis
from systems import stream_schedule
from tests.conftest import query, set_premium, t
from tests.fakes import FakeInteraction
from tests.stream_schedule_fakes import follow, segment
from translations import TRANSLATIONS

RELEASED_AT_IMPORT = stream_schedule.RELEASED   # vor jeder Fixture gelesen

# Der Befehl gleicht sofort ab — mit der echten Uhrzeit, also Termine relativ zu jetzt
SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)


@pytest.fixture(autouse=True)
def released(monkeypatch):
    """Diese Tests prüfen die Funktion selbst — die Sperre (RELEASED = False) steht in test_stream_schedule_testphase.py."""
    monkeypatch.setattr(stream_schedule, "RELEASED", True)


@pytest.fixture
def cog(bot):
    return StreamAlerts(bot)


async def open_panel(cog, guild) -> dict:
    interaction = FakeInteraction(guild)
    await StreamAlerts.stream_schedule_cmd.callback(cog, interaction)
    [message] = interaction.sent
    assert message["ephemeral"] is True
    return message


async def pick(view, guild, monitored_channel_id) -> FakeInteraction:
    """Einen Streamer in der Auswahl anklicken (schaltet um)."""
    view.select_streamer._values = [str(monitored_channel_id)]
    interaction = FakeInteraction(guild)
    await view.select_streamer.callback(interaction)
    return interaction


def twitch_field(embed):
    [field] = embed.fields
    return field


async def enabled_ids(guild) -> list[int]:
    return [r["monitored_channel_id"] for r in await db_stream_schedule.get_syncs_for_guild(guild.id)]


# ── Übersicht ────────────────────────────────────────────────────────────────────────────────

async def test_ohne_twitch_alert_nur_ein_hinweis(cog, guild):
    message = await open_panel(cog, guild)
    assert "view" not in message
    assert twitch_field(message["embed"]).value == t("sched_no_twitch")


async def test_uebersicht_free(cog, guild, twitch):
    await follow(guild, "tw-1", "Rusirda", sync=False)
    await follow(guild, "tw-2", "Max", sync=False)
    message = await open_panel(cog, guild)
    embed = message["embed"]
    assert embed.title == t("sched_title")
    assert embed.description == (t("sched_desc", days=7) + "\n" + t("sched_premium_hint", max=5))
    field = twitch_field(embed)
    assert field.name.endswith("Twitch · 0/1")
    off = f"{NexusEmojis.OFF} {t('sched_off')}"
    assert field.value == f"> **Rusirda** — {off}\n> **Max** — {off}"
    options = message["view"].select_streamer.options
    assert [(o.label, o.description) for o in options] == [("Rusirda", t("sched_opt_enable")), ("Max", t("sched_opt_enable"))]


async def test_uebersicht_premium_ohne_hinweis(cog, guild, twitch):
    await set_premium(guild)
    await follow(guild, sync=False)
    embed = (await open_panel(cog, guild))["embed"]
    assert embed.description == t("sched_desc", days=7)   # Zeitfenster für alle gleich, kein Premium-Hinweis
    assert twitch_field(embed).name.endswith("0/5")


async def test_youtube_und_kick_stehen_nicht_zur_auswahl(cog, guild, twitch):
    channel = guild.add_text_channel("yt")
    monitored = await db_stream_alerts.get_or_create_monitored_channel("youtube", "yt-1", "TubeStar")
    await db_stream_alerts.add_subscription(guild.id, channel.id, monitored["id"])
    await follow(guild, sync=False)
    message = await open_panel(cog, guild)
    assert [o.label for o in message["view"].select_streamer.options] == ["Rusirda"]


async def test_ohne_recht_events_erstellen_ein_hinweis(cog, guild, twitch):
    guild.bot_role.permissions = discord.Permissions(view_channel=True, send_messages=True)
    await follow(guild, sync=False)
    embed = (await open_panel(cog, guild))["embed"]
    assert embed.description.endswith("\n\n" + f"{NexusEmojis.WARNING} " + t("sched_missing_permission"))


# ── Ein- und ausschalten ─────────────────────────────────────────────────────────────────────

async def test_einschalten_legt_sofort_die_events_an(cog, guild, twitch):
    twitch.plan("tw-1", segment("a", SOON), segment("b", SOON + timedelta(days=2)))
    mc_id = await follow(guild, sync=False)
    view = (await open_panel(cog, guild))["view"]

    interaction = await pick(view, guild, mc_id)
    assert await enabled_ids(guild) == [mc_id]
    assert len(guild.events_by_id) == 2   # nicht erst beim nächsten stündlichen Lauf

    [edit] = interaction.edits   # dieselbe Nachricht, neu aufgebaut
    assert twitch_field(edit["embed"]).value == (
        f"> **Rusirda** — {NexusEmojis.ON} {t('sched_on')} · " + t("sched_events", count=2)
        + " · " + t("sched_image_line", source=t("sched_image_banner")))
    assert edit["view"].select_streamer.options[0].description == t("sched_opt_disable")
    assert interaction.sent == []


async def test_ausschalten_loescht_sofort_die_events(cog, guild, twitch):
    twitch.plan("tw-1", segment("a", SOON))
    mc_id = await follow(guild, sync=False)
    await pick((await open_panel(cog, guild))["view"], guild, mc_id)
    assert len(guild.events_by_id) == 1

    interaction = await pick((await open_panel(cog, guild))["view"], guild, mc_id)
    assert await enabled_ids(guild) == []
    assert guild.events_by_id == {}
    assert twitch_field(interaction.edits[0]["embed"]).value == f"> **Rusirda** — {NexusEmojis.OFF} {t('sched_off')}"


async def test_bildquelle_im_panel_waehlen(cog, guild, twitch):
    twitch.images["tw-1"] = "https://img/offline.png"
    twitch.plan("tw-1", segment("a", SOON))
    mc_id = await follow(guild)
    await follow(guild, "tw-2", "Max", sync=False)
    view = (await open_panel(cog, guild))["view"]
    # Nur eingeschaltete Kanäle, jeweils die anderen Quellen
    assert [o.label for o in view.select_image.options] == [
        f"Rusirda → {t('sched_image_offline')}", f"Rusirda → {t('sched_image_none')}"]

    view.select_image._values = [f"{mc_id}:none"]
    interaction = FakeInteraction(guild)
    await view.select_image.callback(interaction)
    assert [r["image_source"] for r in await db_stream_schedule.get_syncs_for_guild(guild.id)] == ["none"]
    assert [e.image for e in guild.events_by_id.values()] == [None]   # sofort abgeglichen
    assert t("sched_image_line", source=t("sched_image_none")) in twitch_field(interaction.edits[0]["embed"]).value


async def test_ohne_eingeschalteten_kanal_keine_bildauswahl(cog, guild, twitch):
    await follow(guild, sync=False)
    view = (await open_panel(cog, guild))["view"]
    assert view.select_image not in view.children


async def test_free_zweiter_streamer_zeigt_premium_hinweis(cog, guild, twitch):
    first = await follow(guild, "tw-1", "Rusirda")
    second = await follow(guild, "tw-2", "Max", sync=False)
    view = (await open_panel(cog, guild))["view"]

    interaction = await pick(view, guild, second)
    assert await enabled_ids(guild) == [first]
    [upsell] = interaction.sent
    assert upsell["embed"].title == f"{NexusEmojis.PREMIUM} " + t("premium_upsell_stream_schedule_limit_title")
    assert upsell["embed"].description == t("premium_upsell_stream_schedule_limit_desc", limit=1, max=5)
    assert any(getattr(b, "sku_id", None) == PREMIUM_SKU_ID for b in upsell["view"].children)
    assert len(interaction.edits) == 1   # die Übersicht bleibt und wird aktualisiert

    await set_premium(guild)
    await pick(view, guild, second)
    assert await enabled_ids(guild) == [first, second]


async def test_premium_am_limit_normale_meldung(cog, guild, twitch):
    """Mit Premium gibt es nichts mehr zu kaufen — dann nur die Meldung, kein Premium-Hinweis."""
    await set_premium(guild)
    await query("INSERT INTO guild_limit_overrides (guild_id, feature_key, limit_value) VALUES (%s, %s, 1)",
                guild.id, stream_schedule.LIMIT_STREAMERS)
    await follow(guild, "tw-1", "Rusirda")
    second = await follow(guild, "tw-2", "Max", sync=False)
    interaction = await pick((await open_panel(cog, guild))["view"], guild, second)
    assert [m["content"] for m in interaction.sent] == [f"{NexusEmojis.ERROR} " + t("sched_limit_reached", limit=1)]


async def test_premium_abgelaufen_zeigt_inaktiv(cog, guild, twitch):
    await set_premium(guild)
    await follow(guild, "tw-1", "Rusirda")
    await follow(guild, "tw-2", "Max")
    await set_premium(guild, active=False)
    field = twitch_field((await open_panel(cog, guild))["embed"])
    assert field.name.endswith("1/1")
    assert field.value.splitlines()[1] == f"> **Max** — {t('alert_inactive_premium')}"


async def test_fehler_aus_dem_abgleich_stehen_beim_streamer(cog, guild, twitch):
    twitch.plans["tw-1"] = None   # kein Streamplan auf Twitch
    mc_id = await follow(guild, sync=False)
    interaction = await pick((await open_panel(cog, guild))["view"], guild, mc_id)
    assert twitch_field(interaction.edits[0]["embed"]).value.endswith(
        "\n> " + f"{NexusEmojis.WARNING} " + t("sched_err_no_schedule"))


async def test_alert_inzwischen_entfernt(cog, guild, twitch):
    mc_id = await follow(guild, sync=False)
    view = (await open_panel(cog, guild))["view"]
    await db_stream_alerts.remove_subscription(guild.id, mc_id)
    interaction = await pick(view, guild, mc_id)
    assert await enabled_ids(guild) == []
    assert interaction.sent == []
    assert twitch_field(interaction.edits[0]["embed"]).value == t("sched_no_twitch")


async def test_gleichzeitig_einschalten_haelt_das_limit(guild, bot, twitch):
    first = await follow(guild, "tw-1", "Rusirda", sync=False)
    second = await follow(guild, "tw-2", "Max", sync=False)
    results = await asyncio.gather(
        stream_schedule.set_enabled(bot, guild.id, first, True),
        stream_schedule.set_enabled(bot, guild.id, second, True),
        return_exceptions=True,
    )
    assert len(await enabled_ids(guild)) == 1
    assert [type(r).__name__ for r in results].count("ScheduleError") == 1


# ── Hilfe, Texte, Premium-Übersicht ──────────────────────────────────────────────────────────

def test_in_der_hilfe():
    assert MODULE_INFO["Stream Alerts"]["commands"]["stream_schedule"] == "help_cmd_stream_schedule"


def test_freigegeben_auf_allen_servern():
    assert getattr(StreamAlerts.stream_schedule_cmd, "_guild_ids", None) is None   # globaler Befehl
    assert RELEASED_AT_IMPORT is True   # Wert aus dem Code, nicht der der Fixture unten


def test_hilfe_zeigt_nur_befehle_dieses_servers():
    from cogs.help import HelpView
    lang = TRANSLATIONS["de"]
    modules = {"stream_alerts": True}
    elsewhere = HelpView(1, lang, 0, modules, available={"alert_add", "alert_list"})
    assert "/stream_schedule" not in elsewhere._module_embed("Stream Alerts").description
    assert "/alert_add" in elsewhere._module_embed("Stream Alerts").description
    on_dev = HelpView(1, lang, 0, modules, available={"alert_add", "alert_list", "stream_schedule"})
    assert "/stream_schedule" in on_dev._module_embed("Stream Alerts").description


def test_texte_in_allen_sprachen():
    from systems.premium_ui import PERK_LIMITS
    assert "stream_schedule_streamers" in {key for key, *_ in PERK_LIMITS}
    for code, lang in TRANSLATIONS.items():
        for key in ("sched_title", "sched_on", "sched_off", "sched_no_twitch", "sched_select_placeholder",
                    "sched_opt_enable", "sched_opt_disable", "sched_missing_permission", "help_cmd_stream_schedule",
                    "sched_err_missing_permission", "sched_err_no_schedule", "sched_err_twitch_error",
                    "sched_err_discord_error", "premium_perk_stream_schedule"):
            assert lang.get(key), (code, key)
        assert "{" not in lang["sched_desc"].format(days=7) + lang["sched_events"].format(count=2)
        assert "{" not in lang["sched_premium_hint"].format(max=5)
        assert "{" not in lang["sched_limit_reached"].format(limit=1)
