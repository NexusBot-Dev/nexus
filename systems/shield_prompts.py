"""Rückfragen zur Shield-Whitelist (systems/shield_whitelist.py) direkt in Discord.

1. Ein Bot bekommt Mod-Rechte — beim Beitritt, über eine neue Rolle oder weil eine seiner Rollen neue Rechte
   bekommt. Nexus fragt im Log-Channel: „Bot xy hat Mod-Rechte — whitelisten?“ mit Ja/Nein. Klicken dürfen
   nur Serverbesitzer und Administratoren. Gefragt wird nur beim Wechsel „keine → Mod-Rechte“, nur bei
   eingeschaltetem Shield und höchstens einmal in PROMPT_COOLDOWN je Bot (Beitritt und Bot-Rolle kommen
   meist kurz nacheinander). Die Buttons tragen die Bot-ID und funktionieren auch nach einem Neustart.
2. Das Shield wird über /automod eingeschaltet: Nexus bietet eine Auswahl der Bots mit Mod-Rechten an, die
   noch nicht auf der Whitelist stehen.
"""

import logging

import discord
from cachetools import TTLCache

from cogs.utils import get_lang
from database import db_automod, db_settings
from emojis import NexusEmojis
from systems import shield_whitelist
from systems.hierarchy import perm_label
from systems.ui import NexusView, error_msg, with_emoji

log = logging.getLogger(__name__)

PROMPT_COOLDOWN = 600   # Sekunden
MAX_OPTIONS = 25        # Discord: Einträge in einem Auswahlmenü

_prompted: TTLCache[tuple[int, int], bool] = TTLCache(maxsize=5_000, ttl=PROMPT_COOLDOWN)


def mod_permissions(perms: discord.Permissions) -> list[str]:
    if perms.administrator:
        return ["administrator"]   # schließt alles andere ein
    return [name for name in shield_whitelist.MOD_PERMISSIONS if getattr(perms, name)]


# ─── 1. Rückfrage im Log-Channel ─────────────────────────────────────────────

async def _log_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    settings = await db_settings.get_guild(guild.id)
    channel = guild.get_channel(settings["log_channel_id"]) if settings and settings.get("log_channel_id") else None
    if channel is None:
        return None
    perms = channel.permissions_for(guild.me)
    return channel if perms.view_channel and perms.send_messages and perms.embed_links else None


async def prompt(member: discord.Member) -> bool:
    """Rückfrage für einen Bot mit Mod-Rechten senden, falls sinnvoll. True, wenn gesendet."""
    guild = member.guild
    if not member.bot or member.id == guild.me.id or (guild.id, member.id) in _prompted:
        return False
    perms = mod_permissions(member.guild_permissions)
    if not perms or not await db_automod.get_antinuke(guild.id):
        return False
    if member.id in (await db_automod.get_shield_whitelist(guild.id)).bots:
        return False
    channel = await _log_channel(guild)
    if channel is None:
        return False
    _prompted[(guild.id, member.id)] = True

    lang = await get_lang(guild.id)
    embed = discord.Embed(
        title=with_emoji(NexusEmojis.SHIELD, lang.get("shield_prompt_title", "Bot with moderator permissions")),
        description=lang.get(
            "shield_prompt_desc",
            "{bot} has moderator permissions: {perms}\n\nShould the Anti-Nuke Shield trust it? Whitelisted bots "
            "are no longer scored — handy for moderation bots, dangerous if the bot gets compromised.",
        ).format(bot=f"**{discord.utils.escape_markdown(member.display_name)}**", perms=", ".join(f"`{perm_label(p)}`" for p in perms)),
        color=discord.Color.orange(),
    )
    # Name und Avatar im Kopf: ein <@id> zeigt Discord bei frisch beigetretenen Bots oft nur als rohe ID an
    embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar.url)
    embed.set_footer(text=lang.get("shield_prompt_footer", "Only the server owner and administrators can decide."))
    try:
        await channel.send(embed=embed, view=prompt_view(member.id, lang), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        _prompted.pop((guild.id, member.id), None)   # beim nächsten Anlass wieder fragen
        log.warning("[SHIELD] Whitelist-Rückfrage für %s auf %s nicht gesendet: %s", member.id, guild.id, e)
        return False
    return True


def prompt_view(bot_id: int, lang: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PromptButton(bot_id, True, lang))
    view.add_item(PromptButton(bot_id, False, lang))
    return view


class PromptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nexus:shield_wl:(?P<choice>yes|no):(?P<bot_id>[0-9]{15,21})"):
    """Ja/Nein unter der Rückfrage. Die Bot-ID steckt in der custom_id — so kennt der Button nach einem
    Neustart noch seinen Bot (registriert in cogs/shield.py über bot.add_dynamic_items)."""

    def __init__(self, bot_id: int, whitelist: bool, lang: dict | None = None):
        lang = lang or {}
        super().__init__(discord.ui.Button(
            custom_id=f"nexus:shield_wl:{'yes' if whitelist else 'no'}:{bot_id}",
            label=lang.get("shield_prompt_yes", "Yes, whitelist") if whitelist else lang.get("shield_prompt_no", "No"),
            style=discord.ButtonStyle.success if whitelist else discord.ButtonStyle.secondary,
        ))
        self.bot_id, self.whitelist = bot_id, whitelist

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["bot_id"]), match["choice"] == "yes")

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        lang = await get_lang(guild.id)
        await interaction.response.defer()
        try:   # prüft auch, ob der Klickende Owner/Admin ist
            await shield_whitelist.set_bot(guild, interaction.user, self.bot_id, self.whitelist)
        except shield_whitelist.WhitelistError as e:
            await interaction.followup.send(error_msg(lang.get(e.key, e.key).format(**e.params)), ephemeral=True)
            return
        decision = (
            with_emoji(NexusEmojis.CHECKMARK, lang.get("shield_prompt_done_yes", "Whitelisted by {user}"))
            if self.whitelist else
            with_emoji(NexusEmojis.ERROR, lang.get("shield_prompt_done_no", "Not whitelisted ({user})"))
        ).format(user=interaction.user.mention)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed()
        embed.add_field(name=lang.get("shield_prompt_decision", "Decision"), value=decision, inline=False)
        embed.set_footer(text=lang.get(
            "shield_prompt_footer_done", "Can be changed anytime in the dashboard (AutoMod → Shield).",
        ))
        await interaction.edit_original_response(embed=embed, view=None)
        log.info("[SHIELD] Bot %s auf %s per Rückfrage %s (von %s).", self.bot_id, guild.id,
                 "gewhitelistet" if self.whitelist else "nicht gewhitelistet", interaction.user.id)


# ─── Auslöser (cogs/shield.py) ───────────────────────────────────────────────

async def on_member_join(member: discord.Member) -> None:
    if member.bot:
        await prompt(member)


async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if after.bot and not mod_permissions(before.guild_permissions) and mod_permissions(after.guild_permissions):
        await prompt(after)


async def on_role_update(before: discord.Role, after: discord.Role) -> None:
    """Eine Rolle bekommt Mod-Rechte: alle Bots fragen, die erst dadurch Mod-Rechte haben."""
    if mod_permissions(before.permissions) or not mod_permissions(after.permissions):
        return
    for member in after.members:
        if not member.bot:
            continue
        # Rechte vorher: alle anderen Rollen plus diese Rolle mit ihren alten Rechten
        value = before.permissions.value
        for role in member.roles:
            if role.id != after.id:
                value |= role.permissions.value
        if not mod_permissions(discord.Permissions(value)):
            await prompt(member)


# ─── 2. Auswahl nach dem Einschalten über /automod ───────────────────────────

async def offer_after_enable(interaction: discord.Interaction, lang: dict) -> None:
    """Nach dem Einschalten (Interaction ist schon beantwortet): Bots mit Mod-Rechten zur Auswahl anbieten."""
    guild = interaction.guild
    if not shield_whitelist.can_manage_bots(guild, interaction.user):
        return
    whitelist = await db_automod.get_shield_whitelist(guild.id)
    candidates = [
        m for m in await shield_whitelist.bot_members(guild)
        if m.id not in whitelist.bots and mod_permissions(m.guild_permissions)
    ][:MAX_OPTIONS]
    if not candidates:
        return
    view = WhitelistSelectView(lang, interaction.user.id, candidates)
    await interaction.followup.send(
        lang.get(
            "shield_select_prompt",
            "These bots have moderator permissions. Which ones should the Shield trust? "
            "Whitelisted bots are no longer scored.",
        ),
        view=view, ephemeral=True,
    )
    view.bind(interaction)


class WhitelistSelectView(NexusView):
    def __init__(self, lang: dict, owner_id: int, bots: list[discord.Member]):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        select = discord.ui.Select(
            custom_id="nexus_shield_wl_select",
            placeholder=lang.get("shield_select_placeholder", "Choose bots to whitelist"),
            min_values=1, max_values=len(bots),
            options=[
                discord.SelectOption(
                    label=m.display_name[:100], value=str(m.id),
                    description=", ".join(perm_label(p) for p in mod_permissions(m.guild_permissions))[:100],
                )
                for m in bots
            ],
        )
        select.callback = self._chosen
        self.add_item(select)
        self.select = select
        self.names = {str(m.id): m.display_name for m in bots}

    async def _chosen(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        added, failed = [], None
        for value in self.select.values:
            try:
                await shield_whitelist.set_bot(guild, interaction.user, int(value), True)
                added.append(f"**{discord.utils.escape_markdown(self.names[value])}**")
            except shield_whitelist.WhitelistError as e:
                failed = self.lang.get(e.key, e.key).format(**e.params)
                break
        text = self.lang.get("shield_select_done", "Whitelisted: {bots}").format(bots=", ".join(added) or "—")
        if failed:
            text += "\n" + error_msg(failed)
        self.stop()
        await interaction.edit_original_response(content=text, view=None)
