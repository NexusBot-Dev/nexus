"""/stream_schedule: Button „Text & Bild“ mit Formular (cogs/stream_alerts.py: ScheduleCustomModal) — Premium."""
import io
from datetime import datetime, timedelta, timezone

import discord
import pytest
from PIL import Image

from cogs.stream_alerts import ScheduleCustomModal, ScheduleCustomPickView, StreamAlerts
from database import db_stream_schedule
from emojis import NexusEmojis
from systems import schedule_images, stream_schedule
from tests.conftest import set_premium, t
from tests.fakes import FakeInteraction
from tests.stream_schedule_fakes import cover, follow, segment
from translations import TRANSLATIONS

SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
TEMPLATE = "Heute {category}: {title} – {link}"


@pytest.fixture(autouse=True)
def released(monkeypatch):
    monkeypatch.setattr(stream_schedule, "RELEASED", True)


@pytest.fixture
def cog(bot):
    return StreamAlerts(bot)


class FakeAttachment:
    """Hochgeladene Datei aus dem Formular; read() zählt mit, ob heruntergeladen wurde."""

    def __init__(self, data: bytes, *, size: int | None = None, fail=False):
        self.data, self.size, self.fail, self.reads = data, len(data) if size is None else size, fail, 0

    async def read(self):
        self.reads += 1
        if self.fail:
            raise discord.HTTPException(type("R", (), {"status": 404, "reason": "Not Found"})(), "weg")
        return self.data


def picture(fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 600), (30, 160, 90)).save(buffer, fmt)
    return buffer.getvalue()


async def panel(cog, guild):
    interaction = FakeInteraction(guild)
    await StreamAlerts.stream_schedule_cmd.callback(cog, interaction)
    return interaction.sent[0]


def field(embed) -> str:
    return embed.fields[0].value


async def open_form(cog, guild) -> ScheduleCustomModal:
    view = (await panel(cog, guild))["view"]
    interaction = FakeInteraction(guild)
    await view.custom_button.callback(interaction)
    [modal] = interaction.modals
    return modal


async def submit(modal, guild, *, text=None, upload=None, remove=None) -> FakeInteraction:
    if text is not None:
        modal.text_input._value = text
    modal.image_upload._values = [upload] if upload else []
    if remove is not None:
        modal.remove_image._value = remove
    interaction = FakeInteraction(guild)
    await modal.on_submit(interaction)
    return interaction


def errors(interaction) -> list[str]:
    return [m["content"] for m in interaction.sent if m.get("content")]


@pytest.fixture
def plan(twitch):
    twitch.plan("tw-1", segment("a", SOON, title="Blind-Run"))
    twitch.covers["1"] = cover()
    return twitch


# ── Button ───────────────────────────────────────────────────────────────────────────────────

async def test_ohne_eingeschalteten_streamer_kein_button(cog, guild, twitch):
    await follow(guild, sync=False)
    view = (await panel(cog, guild))["view"]
    assert view.custom_button not in view.children


async def test_ohne_premium_hinweis_statt_formular(cog, guild, plan):
    await follow(guild)
    view = (await panel(cog, guild))["view"]
    assert view.custom_button.label == t("sched_custom_button")
    interaction = FakeInteraction(guild)
    await view.custom_button.callback(interaction)
    assert interaction.modals == []
    [upsell] = interaction.sent
    assert upsell["embed"].title == f"{NexusEmojis.PREMIUM} " + t("premium_upsell_stream_schedule_custom_title")


async def test_ein_streamer_formular_direkt(cog, guild, plan):
    await set_premium(guild)
    await follow(guild)
    modal = await open_form(cog, guild)
    assert modal.title == t("sched_custom_title", streamer="Rusirda")
    assert modal.text_input.value == "" and modal.text_input.max_length == stream_schedule.MAX_TEXT_LENGTH
    assert modal.remove_image is None   # noch kein eigenes Bild


async def test_mehrere_streamer_erst_auswahl(cog, guild, plan):
    await set_premium(guild)
    await follow(guild)
    second = await follow(guild, "tw-2", "Max")
    await follow(guild, "tw-3", "Aus", sync=False)
    view = (await panel(cog, guild))["view"]
    interaction = FakeInteraction(guild)
    await view.custom_button.callback(interaction)
    [edit] = interaction.edits
    pick = edit["view"]
    assert isinstance(pick, ScheduleCustomPickView) and "embed" not in edit   # Übersicht bleibt stehen
    assert [o.label for o in pick.select_entry.options] == ["Rusirda", "Max"]   # nur eingeschaltete

    pick.select_entry._values = [str(second)]
    interaction = FakeInteraction(guild)
    await pick.select_entry.callback(interaction)
    [modal] = interaction.modals
    assert modal.entry.monitored_channel_id == second

    interaction = await submit(modal, guild, text="Max {title}")
    assert (await db_stream_schedule.get_custom_meta(guild.id))[second]["description"] == "Max {title}"
    assert interaction.edits[0]["view"].custom_button   # zurück in der Übersicht

    interaction = FakeInteraction(guild)
    await pick.back.callback(interaction)
    assert field(interaction.edits[0]["embed"]).startswith("> **Rusirda**")


# ── Formular absenden ────────────────────────────────────────────────────────────────────────

async def test_text_speichern_gleicht_ab(cog, guild, plan):
    await set_premium(guild)
    mc_id = await follow(guild)
    interaction = await submit(await open_form(cog, guild), guild, text=f"  {TEMPLATE}  ")
    assert (await db_stream_schedule.get_custom_meta(guild.id))[mc_id]["description"] == TEMPLATE
    [event] = guild.events_by_id.values()
    assert event.description == "Heute Elden Ring: Blind-Run – https://twitch.tv/rusirda"
    assert field(interaction.edits[0]["embed"]).endswith(" · " + t("sched_text_custom"))
    assert errors(interaction) == []

    modal = await open_form(cog, guild)
    assert modal.text_input.value == TEMPLATE   # beim nächsten Öffnen vorausgefüllt
    await submit(modal, guild, text="")
    assert not (await db_stream_schedule.get_custom_meta(guild.id))[mc_id]["description"]


async def test_bild_und_text_einmal_abgleichen(cog, guild, plan, monkeypatch):
    await set_premium(guild)
    mc_id = await follow(guild)
    modal = await open_form(cog, guild)
    calls = []
    real = stream_schedule.sync_guild

    async def counting(bot, guild_id, **kwargs):
        calls.append(guild_id)
        return await real(bot, guild_id, **kwargs)
    monkeypatch.setattr(stream_schedule, "sync_guild", counting)

    upload = FakeAttachment(picture("WEBP"))
    interaction = await submit(modal, guild, text=TEMPLATE, upload=upload)
    assert calls == [guild.id]
    [event] = guild.events_by_id.values()
    assert event.image == await db_stream_schedule.get_custom_image(guild.id, mc_id)
    assert Image.open(io.BytesIO(event.image)).size == (1600, 640)
    assert t("sched_image_line", source=t("sched_image_custom")) in field(interaction.edits[0]["embed"])


async def test_nichts_geaendert_kein_abgleich(cog, guild, plan, monkeypatch):
    await set_premium(guild)
    await follow(guild)
    modal = await open_form(cog, guild)
    calls = []
    monkeypatch.setattr(stream_schedule, "sync_guild", lambda *a, **k: calls.append(a))
    interaction = await submit(modal, guild, text="")
    assert calls == [] and len(interaction.edits) == 1


async def test_zu_grosses_bild_wird_nicht_geladen(cog, guild, plan):
    await set_premium(guild)
    mc_id = await follow(guild)
    upload = FakeAttachment(b"x", size=schedule_images.MAX_UPLOAD_BYTES + 1)
    interaction = await submit(await open_form(cog, guild), guild, text=TEMPLATE, upload=upload)
    assert upload.reads == 0
    assert errors(interaction) == [f"{NexusEmojis.ERROR} " + t("sched_custom_err_too_large", mb=2)]
    meta = (await db_stream_schedule.get_custom_meta(guild.id))[mc_id]
    assert (meta["description"], meta["image_hash"]) == (TEMPLATE, None)   # Text trotzdem gespeichert


async def test_kein_bild_oder_download_fehlgeschlagen(cog, guild, plan):
    await set_premium(guild)
    mc_id = await follow(guild)
    interaction = await submit(await open_form(cog, guild), guild, upload=FakeAttachment(b"%PDF-1.7 kein Bild"))
    assert errors(interaction) == [f"{NexusEmojis.ERROR} " + t("sched_custom_err_not_an_image")]
    interaction = await submit(await open_form(cog, guild), guild, upload=FakeAttachment(picture(), fail=True))
    assert errors(interaction) == [f"{NexusEmojis.ERROR} " + t("sched_custom_err_download")]
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)
    assert [r["image_source"] for r in await db_stream_schedule.get_syncs_for_guild(guild.id)] == ["banner"]


async def test_bild_entfernen(cog, guild, plan):
    await set_premium(guild)
    mc_id = await follow(guild)
    await submit(await open_form(cog, guild), guild, upload=FakeAttachment(picture()))
    modal = await open_form(cog, guild)
    assert modal.remove_image is not None and modal.remove_image.value is False

    await submit(modal, guild, remove=False)   # nicht angehakt: bleibt
    assert (await db_stream_schedule.get_custom_meta(guild.id))[mc_id]["image_hash"]

    interaction = await submit(await open_form(cog, guild), guild, remove=True)
    assert not (await db_stream_schedule.get_custom_meta(guild.id)).get(mc_id, {}).get("image_hash")
    assert t("sched_image_line", source=t("sched_image_banner")) in field(interaction.edits[0]["embed"])


async def test_premium_unterwegs_abgelaufen(cog, guild, plan):
    await set_premium(guild)
    mc_id = await follow(guild)
    modal = await open_form(cog, guild)
    await set_premium(guild, active=False)
    interaction = await submit(modal, guild, text=TEMPLATE, upload=FakeAttachment(picture()))
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)
    [upsell] = interaction.sent
    assert upsell["embed"].title == f"{NexusEmojis.PREMIUM} " + t("premium_upsell_stream_schedule_custom_title")
    assert len(interaction.edits) == 1   # Übersicht trotzdem aktuell


async def test_unterwegs_ausgeschaltet(cog, guild, plan):
    await set_premium(guild)
    mc_id = await follow(guild)
    modal = await open_form(cog, guild)
    await stream_schedule.set_enabled(cog.bot, guild.id, mc_id, False)
    interaction = await submit(modal, guild, text=TEMPLATE)
    assert mc_id not in await db_stream_schedule.get_custom_meta(guild.id)
    assert interaction.sent == [] and len(interaction.edits) == 1


# ── Premium abgelaufen: Übersicht zeigt, was wirklich gilt ──────────────────────────────────

async def test_ohne_premium_zeigt_die_uebersicht_banner(cog, guild, plan):
    await set_premium(guild)
    await follow(guild)
    await submit(await open_form(cog, guild), guild, text=TEMPLATE, upload=FakeAttachment(picture()))
    await set_premium(guild, active=False)
    message = await panel(cog, guild)
    value = field(message["embed"])
    assert t("sched_image_line", source=t("sched_image_banner")) in value
    assert t("sched_text_custom") not in value
    labels = [o.label for o in message["view"].select_image.options]
    assert labels == [f"Rusirda → {t('sched_image_offline')}", f"Rusirda → {t('sched_image_none')}"]


# ── Texte ────────────────────────────────────────────────────────────────────────────────────

def test_texte_in_allen_sprachen_und_discord_limits():
    placeholders = " ".join(f"{{{p}}}" for p in stream_schedule.TEXT_PLACEHOLDERS)
    for code, lang in TRANSLATIONS.items():
        for key in ("sched_custom_text_label", "sched_custom_image_label", "sched_custom_remove_label"):
            assert 0 < len(lang[key]) <= 45, (code, key)   # Label-Titel
        assert 0 < len(lang["sched_custom_text_hint"].format(placeholders=placeholders)) <= 100, code
        assert 0 < len(lang["sched_custom_image_hint"].format(mb=2)) <= 100, code
        assert 0 < len(lang["sched_custom_remove_hint"]) <= 100, code
        assert len(lang["sched_custom_text_placeholder"]) <= 100, code
        for key in ("sched_text_custom", "sched_custom_button", "sched_custom_pick", "sched_custom_back",
                    "sched_custom_err_too_many_pixels", "sched_custom_err_not_an_image", "sched_custom_err_download"):
            assert lang.get(key), (code, key)
        assert "{" not in lang["sched_custom_err_too_large"].format(mb=2) + lang["sched_custom_err_text_too_long"].format(max=800)
        assert "{" not in lang["sched_custom_title"].format(streamer="Rusirda")


async def test_formular_passt_in_discords_grenzen(cog, guild, plan):
    """Label mit Textfeld, Datei-Upload und (mit Bild) Haken — so, wie discord.py es an Discord schickt."""
    await set_premium(guild)
    await follow(guild, "tw-1", "EinSehrLangerStreamerNameMitVielenZeichen")
    await submit(await open_form(cog, guild), guild, upload=FakeAttachment(picture()))
    data = (await open_form(cog, guild)).to_dict()
    assert len(data["title"]) <= 45
    assert [(c["type"], c["component"]["type"]) for c in data["components"]] == [(18, 4), (18, 19), (18, 23)]
    assert data["components"][1]["component"]["max_values"] == 1
    assert data["components"][1]["component"]["required"] is False
