import asyncio
import logging
import time
import discord
from discord import app_commands
from discord.ext import commands
from config import INVITE_URL, NEXUS_FOOTER
from database import db_giveaways, db_levels, db_settings, db_activity
from emojis import NexusEmojis
from systems.card_generator import AVATAR_SIZE, generate_rank_card
from systems.exp_system import xp_for_level
from systems.ui import with_emoji
from .utils import get_user_lang, module_required

log = logging.getLogger(__name__)


class DeleteDataConfirmView(discord.ui.View):
    def __init__(self, lang: dict, color: int, orig_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.lang = lang
        self.color = color
        self.orig_interaction = orig_interaction
        self._is_processing = False

        self.confirm_btn.label = lang.get("rr_btn_delete_confirm", "Yes, delete everything")
        self.confirm_btn.emoji = NexusEmojis.WARNING
        self.cancel_btn.label = lang.get("rr_btn_cancel", "Abort")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.orig_interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._is_processing:
            return
        self._is_processing = True

        for item in self.children:
            item.disabled = True

        await interaction.response.defer(ephemeral=True)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(db_levels.wipe_xp(interaction.guild_id, interaction.user.id))
            tg.create_task(db_giveaways.delete_user(interaction.guild_id, interaction.user.id))
            tg.create_task(db_activity.delete_user(interaction.guild_id, interaction.user.id))

        embed = discord.Embed(
            title=self.lang.get("delete_data_title", "🗑️ Data deleted"),
            description=self.lang.get(
                "delete_data_description",
                "The following data has been deleted from this server:\n\n"
                "{nexus_checkmark} Level & XP\n"
                "{nexus_checkmark} Giveaway participations\n"
                "⚠️ Warnings are kept for moderation purposes.",
            ).format(nexus_checkmark=NexusEmojis.CHECKMARK),
            color=self.color,
        )
        embed.set_footer(text=NEXUS_FOOTER)

        await interaction.edit_original_response(embed=embed, view=None)
        self.stop()
        log.info(
            "DSGVO: %s hat seine Daten auf %s unwiderruflich gelöscht.",
            interaction.user.id,
            interaction.guild_id,
        )

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._is_processing:
            return
        self._is_processing = True

        embed = discord.Embed(
            title=self.lang.get("delete_data_cancelled_title", "Aborted"),
            description=self.lang.get(
                "delete_data_cancelled_desc",
                "No data was deleted. Everything is secure!",
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class UserCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="invite", description=app_commands.locale_str("cmd_invite_desc"))
    async def invite(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = get_user_lang(interaction)
        color = await db_settings.get_color(interaction.guild_id)

        invite_view = discord.ui.View()
        invite_view.add_item(
            discord.ui.Button(
                label=lang.get("invite_bot", "Invite Bot"),
                style=discord.ButtonStyle.link,
                url=INVITE_URL,
                emoji="🔗",
            )
        )

        invite_embed = discord.Embed(
            title=lang.get("invite_title", "Get Nexus for your server!"),
            description=lang.get(
                "invite_desc",
                "Manage your server with ease — from leveling and reaction roles to stream alerts and powerful protection with Nexus Shield.",
            ),
            color=color,
        )
        invite_embed.set_footer(text=NEXUS_FOOTER)

        await interaction.followup.send(embed=invite_embed, view=invite_view, ephemeral=True)

    @app_commands.command(name="delete_my_data", description=app_commands.locale_str("cmd_delete_desc"))
    async def delete_my_data(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = get_user_lang(interaction)
        color = await db_settings.get_color(interaction.guild_id)

        warn_embed = discord.Embed(
            title=with_emoji(NexusEmojis.WARNING, lang.get("delete_data_warn_title", "Are you absolutely sure?")),
            description=lang.get(
                "delete_data_warn_desc",
                "This step will permanently delete all your **XP and levels** on this server!\n"
                "A staff member **cannot** restore this data.",
            ),
            color=discord.Color.red(),
        )
        warn_embed.set_footer(text=NEXUS_FOOTER)

        view = DeleteDataConfirmView(lang, color, interaction)
        await interaction.followup.send(
            embed=warn_embed,
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="rank", description=app_commands.locale_str("cmd_rank_desc"))
    @app_commands.describe(
        silent=app_commands.locale_str("cmd_silent_desc"),
        member=app_commands.locale_str("cmd_member_desc"),
    )
    @module_required("levels")
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        silent: bool = False,
    ):
        await interaction.response.defer(ephemeral=silent)
        started = time.perf_counter()

        target = member or interaction.user
        data, rank = await asyncio.gather(
            db_levels.get_user_xp(interaction.guild_id, target.id),
            db_levels.get_user_rank(interaction.guild_id, target.id),
        )
        xp = data["xp"]
        level = data["level"]

        level_start = xp_for_level(level)
        level_end   = xp_for_level(level + 1)
        xp_in_level = max(0, xp - level_start)
        xp_needed   = level_end - level_start
        t_db = time.perf_counter()

        # Statisch und in 512 px statt 1024 px (bei animierten Avataren sonst ein GIF mit mehreren MB)
        try:
            avatar_bytes = await target.display_avatar.with_format("png").with_size(AVATAR_SIZE).read()
        except discord.HTTPException:
            log.warning("Avatar-Download fehlgeschlagen für %s, Fallback auf Default-Avatar.", target.id)
            avatar_bytes = await target.default_avatar.read()
        t_avatar = time.perf_counter()

        card_buffer = await asyncio.to_thread(
            generate_rank_card,
            username=target.display_name,
            avatar_bytes=avatar_bytes,
            xp=xp_in_level,
            xp_for_next=xp_needed,
            rank=rank,
            level=level,
        )
        card_size = card_buffer.getbuffer().nbytes
        t_card = time.perf_counter()

        file = discord.File(card_buffer, filename="rank.webp")
        await interaction.followup.send(file=file, ephemeral=silent)
        t_sent = time.perf_counter()
        log.info(
            "/rank in Guild %s: DB %.2fs · Avatar %.2fs (%d KB) · Karte %.2fs (%d KB) · Senden %.2fs · gesamt %.2fs",
            interaction.guild_id, t_db - started, t_avatar - t_db, len(avatar_bytes) // 1024,
            t_card - t_avatar, card_size // 1024, t_sent - t_card, t_sent - started,
        )

async def setup(bot):
    await bot.add_cog(UserCommands(bot))