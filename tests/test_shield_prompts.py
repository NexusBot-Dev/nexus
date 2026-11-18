"""Rückfragen zur Shield-Whitelist in Discord (systems/shield_prompts.py, cogs/shield.py, cogs/automod.py)."""
import re
import types

import discord
import pytest

from cogs import shield
from cogs.automod import AutoModView, _load_state
from cogs.utils import get_lang
from database import db_automod, db_settings
from systems import shield_prompts
from emojis import NexusEmojis
from systems.ui import error_msg, with_emoji
from tests.conftest import run, t
from tests.fakes import FakeInteraction, FakeMember, bad_request


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


def with_roles(member, *roles):
    """Derselbe Bot mit anderen Rollen (wie before/after bei on_member_update)."""
    copy = FakeMember(member.guild, member.name, list(roles), bot=member.bot, id=member.id)
    return copy


def prompts(channel):
    return [m for m in channel.messages.values() if m.view is not None]


def button_ids(message):
    return [item.custom_id for item in message.view.children]


def interaction(guild, user, message=None):
    inter = FakeInteraction(guild)
    inter.user = user
    inter.message = message
    return inter


async def click(message, user, choice: str):
    [item] = [i for i in message.view.children if i.custom_id.split(":")[2] == choice]
    match = re.fullmatch(shield_prompts.PromptButton.__discord_ui_compiled_template__, item.custom_id)
    button = await shield_prompts.PromptButton.from_custom_id(None, item.item, match)
    inter = interaction(message.guild, user, message)
    await button.callback(inter)
    return inter


# ── Wann gefragt wird ───────────────────────────────────────────────────────────────────────────

async def test_bot_bekommt_mod_rolle_rueckfrage(cog, guild, log_channel):
    mod_role = guild.add_role("Bot-Rolle", 4, ban_members=True, kick_members=True)
    mod_bot = add_bot(guild, "Mod-Bot")
    after = with_roles(mod_bot, mod_role)
    guild.members_by_id[mod_bot.id] = after

    await cog.on_member_update(mod_bot, after)

    [message] = prompts(log_channel)
    embed = message.embeds[0]
    assert embed.title.endswith(t("shield_prompt_title"))
    assert "**Mod-Bot**" in embed.description and "`Ban Members`" in embed.description and "`Kick Members`" in embed.description
    assert mod_bot.mention not in embed.description   # zeigt Discord bei neuen Bots oft nur als rohe ID
    assert (embed.author.name, embed.author.icon_url) == (f"Mod-Bot ({mod_bot.id})", str(mod_bot.display_avatar.url))
    assert button_ids(message) == [f"nexus:shield_wl:yes:{mod_bot.id}", f"nexus:shield_wl:no:{mod_bot.id}"]
    assert message.allowed_mentions.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_name_wird_entschaerft(guild, log_channel):
    bot = add_bot(guild, "**fett**_bot", guild.add_role("Bot-Rolle", 4, kick_members=True))
    await shield_prompts.prompt(bot)
    assert prompts(log_channel)[0].embeds[0].description.startswith(r"**\*\*fett\*\*\_bot**")


async def test_admin_recht_wird_allein_genannt(guild, log_channel):
    admin_role = guild.add_role("Bot-Admin", 4, administrator=True)
    mod_bot = add_bot(guild, "Mod-Bot", admin_role)
    assert await shield_prompts.prompt(mod_bot)
    assert "`Administrator`" in log_channel.messages[next(iter(log_channel.messages))].embeds[0].description
    assert shield_prompts.mod_permissions(mod_bot.guild_permissions) == ["administrator"]


async def test_beitritt_mit_mod_rechten(cog, guild, log_channel):
    mod_role = guild.add_role("Bot-Rolle", 4, manage_roles=True)
    await cog.on_member_join(add_bot(guild, "Mod-Bot", mod_role))
    await cog.on_member_join(add_bot(guild, "Musikbot"))
    await cog.on_member_join(guild.add_member("mensch", mod_role))
    assert len(prompts(log_channel)) == 1


async def test_beitritt_und_bot_rolle_kurz_danach_nur_eine_rueckfrage(cog, guild, log_channel):
    mod_role = guild.add_role("Bot-Rolle", 4, manage_channels=True)
    mod_bot = add_bot(guild, "Mod-Bot", mod_role)
    await cog.on_member_join(mod_bot)
    await cog.on_member_update(with_roles(mod_bot), mod_bot)
    assert len(prompts(log_channel)) == 1


async def test_keine_rueckfrage(cog, guild, log_channel):
    mod_role = guild.add_role("Bot-Rolle", 4, kick_members=True)
    other_mod = guild.add_role("Alt", 3, ban_members=True)

    # hatte schon vorher Mod-Rechte
    a = add_bot(guild, "A", other_mod)
    await cog.on_member_update(a, with_roles(a, other_mod, mod_role))
    # Mensch
    human = guild.add_member("mensch")
    await cog.on_member_update(human, guild.add_member("mensch2", mod_role))
    # schon auf der Whitelist
    b = add_bot(guild, "B")
    await db_automod.add_shield_whitelist(guild.id, "bot", [b.id], guild.owner_id)
    await cog.on_member_update(b, with_roles(b, mod_role))
    # Rolle ohne Mod-Rechte
    c = add_bot(guild, "C")
    await cog.on_member_update(c, with_roles(c, guild.add_role("Farbe", 2)))
    # Nexus selbst
    await cog.on_member_update(with_roles(guild.me), guild.me)

    assert prompts(log_channel) == []


async def test_keine_rueckfrage_ohne_shield_oder_log_channel(guild, log_channel):
    mod_role = guild.add_role("Bot-Rolle", 4, kick_members=True)
    mod_bot = add_bot(guild, "Mod-Bot", mod_role)

    await db_automod.set_antinuke(guild.id, False)
    assert not await shield_prompts.prompt(mod_bot)
    await db_automod.set_antinuke(guild.id, True)
    await db_settings.update_guild(guild.id, log_channel_id=None)
    assert not await shield_prompts.prompt(mod_bot)
    assert prompts(log_channel) == []
    # Erst danach gesendet (die Sperre gegen Doppelte greift nur, wenn wirklich gefragt wurde)
    await db_settings.update_guild(guild.id, log_channel_id=log_channel.id)
    assert await shield_prompts.prompt(mod_bot)


async def test_ohne_embed_recht_im_log_channel_still(guild, log_channel):
    log_channel.bot_denied = {"embed_links"}
    mod_bot = add_bot(guild, "Mod-Bot", guild.add_role("Bot-Rolle", 4, kick_members=True))
    assert not await shield_prompts.prompt(mod_bot)
    assert prompts(log_channel) == []


async def test_senden_scheitert_naechster_anlass_fragt_wieder(guild, log_channel, monkeypatch):
    async def stoerung(*args, **kwargs):
        raise bad_request(0, "Discord-Störung")
    monkeypatch.setattr(log_channel, "send", stoerung)
    mod_bot = add_bot(guild, "Mod-Bot", guild.add_role("Bot-Rolle", 4, kick_members=True))
    assert not await shield_prompts.prompt(mod_bot)
    monkeypatch.undo()
    assert await shield_prompts.prompt(mod_bot)


async def test_menschen_werden_nie_gefragt(guild, log_channel):
    assert not await shield_prompts.prompt(guild.add_member("mensch", guild.add_role("Mod", 4, administrator=True)))


async def test_rolle_bekommt_mod_rechte(cog, guild, log_channel):
    deko = guild.add_role("Bots", 3)
    alt = guild.add_role("Alt", 2, ban_members=True)
    neu = add_bot(guild, "Neu", deko)
    add_bot(guild, "Schon Mod", deko, alt)   # hatte schon Mod-Rechte über „Alt“
    guild.add_member("mensch", deko)

    before = types.SimpleNamespace(id=deko.id, permissions=discord.Permissions.none())
    deko.permissions = discord.Permissions(manage_roles=True)
    await cog.on_guild_role_update(before, deko)

    [message] = prompts(log_channel)
    assert button_ids(message)[0] == f"nexus:shield_wl:yes:{neu.id}"
    assert "Schon Mod" not in message.embeds[0].description


async def test_rolle_hatte_schon_mod_rechte(cog, guild, log_channel):
    role = guild.add_role("Bots", 3, kick_members=True)
    add_bot(guild, "Neu", role)
    before = types.SimpleNamespace(id=role.id, permissions=discord.Permissions(kick_members=True))
    role.permissions = discord.Permissions(kick_members=True, ban_members=True)
    await cog.on_guild_role_update(before, role)
    assert prompts(log_channel) == []


async def test_fehler_in_rueckfrage_bricht_listener_nicht(cog, guild, log_channel, monkeypatch):
    async def kaputt(member):
        raise RuntimeError("Discord weg")
    monkeypatch.setattr(shield_prompts, "prompt", kaputt)
    mod_bot = add_bot(guild, "Mod-Bot", guild.add_role("Bot-Rolle", 4, kick_members=True))
    await cog.on_member_join(mod_bot)   # wirft nicht


# ── Ja/Nein ─────────────────────────────────────────────────────────────────────────────────────

async def prompt_message(guild, log_channel):
    mod_bot = add_bot(guild, "Mod-Bot", guild.add_role("Bot-Rolle", 4, kick_members=True))
    await shield_prompts.prompt(mod_bot)
    return mod_bot, prompts(log_channel)[0]


async def test_admin_klickt_ja(guild, log_channel):
    mod_bot, message = await prompt_message(guild, log_channel)
    inter = await click(message, guild.admin, "yes")

    assert (await db_automod.get_shield_whitelist(guild.id)).bots == {mod_bot.id}
    [edit] = inter.edits
    assert edit["view"] is None
    field = edit["embed"].fields[-1]
    assert (field.name, field.value) == (t("shield_prompt_decision"), with_emoji(NexusEmojis.CHECKMARK, t("shield_prompt_done_yes", user=guild.admin.mention)))
    assert edit["embed"].footer.text == t("shield_prompt_footer_done")


async def test_owner_klickt_nein(guild, log_channel):
    mod_bot, message = await prompt_message(guild, log_channel)
    await db_automod.add_shield_whitelist(guild.id, "bot", [mod_bot.id], guild.admin.id)   # inzwischen im Dashboard
    inter = await click(message, guild.owner_member, "no")
    assert (await db_automod.get_shield_whitelist(guild.id)).bots == set()
    assert inter.edits[0]["embed"].fields[-1].value == with_emoji(NexusEmojis.ERROR, t("shield_prompt_done_no", user=guild.owner_member.mention))


async def test_server_verwalten_darf_nicht_klicken(guild, log_channel):
    mod_bot, message = await prompt_message(guild, log_channel)
    inter = await click(message, guild.moderator, "yes")
    assert inter.sent[0]["content"].endswith(t("dash_err_shield_admin_only"))
    assert inter.sent[0]["ephemeral"] is True
    assert inter.edits == []
    assert (await db_automod.get_shield_whitelist(guild.id)).bots == set()


async def test_ja_bei_voller_whitelist(guild, log_channel):
    mod_bot, message = await prompt_message(guild, log_channel)
    others = [add_bot(guild, f"Bot {i}") for i in range(25)]
    await db_automod.add_shield_whitelist(guild.id, "bot", [b.id for b in others], guild.owner_id)
    inter = await click(message, guild.admin, "yes")
    assert inter.sent[-1]["content"].endswith(t("dash_err_shield_too_many_bots", max=25))
    assert inter.edits == []


async def test_bot_inzwischen_weg(guild, log_channel):
    mod_bot, message = await prompt_message(guild, log_channel)
    del guild.members_by_id[mod_bot.id]
    inter = await click(message, guild.admin, "yes")
    assert inter.sent[-1]["content"].endswith(t("dash_err_shield_not_a_bot"))


def test_button_template_passt_nur_auf_eigene_ids():
    pattern = shield_prompts.PromptButton.__discord_ui_compiled_template__
    assert pattern.fullmatch("nexus:shield_wl:yes:123456789012345678")
    assert not pattern.fullmatch("nexus:shield_wl:maybe:123456789012345678")
    assert not pattern.fullmatch("nexus:shield_wl:yes:12")


# ── Auswahl nach dem Einschalten über /automod ──────────────────────────────────────────────────

async def enable_via_automod(bot, guild, user):
    lang = await get_lang(guild.id)
    view = AutoModView(guild, user.id, lang, await _load_state(guild, lang))
    inter = interaction(guild, user)
    await view._toggle_shield(inter)
    assert await db_automod.get_antinuke(guild.id) == 1
    return inter


def select_message(inter):
    return [s for s in inter.sent if isinstance(s.get("view"), shield_prompts.WhitelistSelectView)]


async def test_einschalten_bietet_mod_bots_an(bot, guild):
    mod_role = guild.add_role("Mods", 4, ban_members=True)
    mod_bot = add_bot(guild, "Mod-Bot", mod_role)
    helper_bot = add_bot(guild, "Helfer-Bot", mod_role)
    schon = add_bot(guild, "Schon drauf", mod_role)
    add_bot(guild, "Musikbot")
    await db_automod.add_shield_whitelist(guild.id, "bot", [schon.id], guild.owner_id)

    inter = await enable_via_automod(bot, guild, guild.admin)
    [sent] = select_message(inter)
    assert sent["ephemeral"] is True
    view = sent["view"]
    assert sorted(o.value for o in view.select.options) == sorted([str(mod_bot.id), str(helper_bot.id)])
    assert view.select.max_values == 2
    assert view.select.options[0].description == "Ban Members"

    # Auswahl: nur Mod-Bot
    view.select._values = [str(mod_bot.id)]   # wie von Discord übermittelt
    pick = interaction(guild, guild.admin)
    await view._chosen(pick)
    assert (await db_automod.get_shield_whitelist(guild.id)).bots == {mod_bot.id, schon.id}
    assert pick.edits[-1] == {"content": t("shield_select_done", bots="**Mod-Bot**"), "view": None}


async def test_einschalten_ohne_mod_bots_keine_auswahl(bot, guild):
    add_bot(guild, "Musikbot")
    inter = await enable_via_automod(bot, guild, guild.admin)
    assert select_message(inter) == []


async def test_einschalten_durch_nicht_admin_keine_auswahl(bot, guild):
    add_bot(guild, "Mod-Bot", guild.add_role("Mods", 4, ban_members=True))
    inter = await enable_via_automod(bot, guild, guild.moderator)
    assert select_message(inter) == []


async def test_auswahl_hoechstens_25(bot, guild):
    mod_role = guild.add_role("Mods", 4, kick_members=True)
    for i in range(30):
        add_bot(guild, f"Bot {i:02d}", mod_role)
    inter = await enable_via_automod(bot, guild, guild.owner_member)
    [sent] = select_message(inter)
    assert len(sent["view"].select.options) == 25


async def test_auswahl_stoppt_an_der_grenze(bot, guild):
    mod_role = guild.add_role("Mods", 4, kick_members=True)
    candidates = [add_bot(guild, f"Kandidat {i}", mod_role) for i in range(2)]
    full = [add_bot(guild, f"Voll {i}") for i in range(24)]
    await db_automod.add_shield_whitelist(guild.id, "bot", [b.id for b in full], guild.owner_id)

    view = shield_prompts.WhitelistSelectView(await get_lang(guild.id), guild.admin.id, candidates)
    view.select._values = [str(c.id) for c in candidates]
    pick = interaction(guild, guild.admin)
    await view._chosen(pick)
    content = pick.edits[-1]["content"]
    assert content == t("shield_select_done", bots="**Kandidat 0**") + "\n" + error_msg(t("dash_err_shield_too_many_bots", max=25))
