import logging
import discord
import asyncio
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_levels, db_warnings, db_activity, db_giveaways
from cogs.utils import get_lang
from config import NEXUS_FOOTER
from emojis import NexusEmojis
from systems.ui import NexusView

log = logging.getLogger(__name__)

class GoodbyeConfigView(NexusView):
    def __init__(self, lang: dict[str, str], enabled: bool):
        super().__init__(lang, timeout=300)
        self.enabled = enabled
        self.select_goodbye_channel.placeholder = self.lang.get(
            "setup_placeholder", "📋 Choose an existing Channel..."
        )
        self._update_button()

    def _update_button(self):
        self.toggle_goodbye.style = discord.ButtonStyle.success if self.enabled else discord.ButtonStyle.secondary
        self.toggle_goodbye.label = (
            f"{self.lang.get('goodbye_title', '👋 Goodbye!')}: "
            f"{self.lang.get('setup_on', 'ON') if self.enabled else self.lang.get('setup_off', 'OFF')}"
        )

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text])
    async def select_goodbye_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        # Channel wählen = Opt-in: Goodbye wird gleichzeitig aktiviert.
        await db_settings.update_guild(interaction.guild_id, goodbye_channel_id=channel.id, goodbye_enabled=True)
        self.enabled = True
        self._update_button()
        await interaction.response.edit_message(
            embed=await _goodbye_status_embed(interaction.guild_id, self.lang),
            view=self,
        )

    @discord.ui.button(label="Goodbye: ON", style=discord.ButtonStyle.success)
    async def toggle_goodbye(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Einschalten ergibt ohne Channel keinen Sinn (es gibt keinen Log-Channel-Fallback mehr)
        if not self.enabled:
            settings = await db_settings.get_guild(interaction.guild_id) or {}
            if not settings.get("goodbye_channel_id"):
                await interaction.response.send_message(
                    self.lang.get("announcement_need_channel", "❌ Please select a channel first."),
                    ephemeral=True,
                )
                return

        self.enabled = not self.enabled
        await db_settings.update_guild(interaction.guild_id, goodbye_enabled=self.enabled)
        self._update_button()
        await interaction.response.edit_message(
            embed=await _goodbye_status_embed(interaction.guild_id, self.lang),
            view=self,
        )

async def _goodbye_status_embed(guild_id: int, lang: dict[str, str]) -> discord.Embed:
    settings = await db_settings.get_guild(guild_id) or {}
    color    = await db_settings.get_color(guild_id)

    channel_id = settings.get("goodbye_channel_id")
    ch_display = f"<#{channel_id}>" if channel_id else lang.get("announcement_no_channel", "Not set")
    status = (
        f"{NexusEmojis.CHECKMARK} {lang.get('setup_on', 'ON')}"
        if settings.get("goodbye_enabled", False)
        else f"❌ {lang.get('setup_off', 'OFF')}"
    )

    embed = discord.Embed(
        title=lang.get("goodbye_title", "👋 Goodbye!"),
        description=lang.get("goodbye_description", "{user} has left the server.").format(user="**Username**"),
        color=color,
    )
    embed.add_field(name=lang.get("settings_goodbye_status", "Status"), value=status, inline=True)
    embed.add_field(name=lang.get("settings_goodbye_channel", "Channel"), value=ch_display, inline=True)
    embed.set_footer(text=NEXUS_FOOTER)
    return embed

class Goodbye(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _delete_user_data(self, guild_id: int, user_id: int):
        """Löscht die Nutzerdaten DSGVO-konform. Ein Fehler hier darf niemals die
        Goodbye-Nachricht verhindern, deshalb eigenes Error-Handling statt es
        an on_member_remove durchzureichen (ExceptionGroup würde dort sonst
        durch das reguläre `except Exception` rutschen)."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(db_levels.wipe_xp(guild_id, user_id))
                tg.create_task(db_warnings.clear_warnings(guild_id, user_id))
                tg.create_task(db_activity.delete_user(guild_id, user_id))
                tg.create_task(db_giveaways.delete_user(guild_id, user_id))
            log.info("DSGVO: Daten von User %s auf Guild %s gelöscht.", user_id, guild_id)
        except* Exception as eg:
            for e in eg.exceptions:
                log.exception(
                    "DSGVO-Löschung für User %s auf Guild %s teilweise fehlgeschlagen:",
                    user_id, guild_id, exc_info=e,
                )

    @app_commands.command(name="goodbye", description=app_commands.locale_str("cmd_goodbye_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def goodbye_config(self, interaction: discord.Interaction):
        lang     = await get_lang(interaction.guild_id)
        settings = await db_settings.get_or_create_guild(interaction.guild_id)

        embed = await _goodbye_status_embed(interaction.guild_id, lang)
        view  = GoodbyeConfigView(lang, settings.get("goodbye_enabled", False))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            if member.id == self.bot.user.id:
                return

            await self._delete_user_data(member.guild.id, member.id)

            settings = await db_settings.get_guild(member.guild.id)
            if settings is None:
                return

            if not settings.get("goodbye_enabled", False):
                return

            channel_id = settings.get("goodbye_channel_id")
            if not channel_id:
                return

            channel = member.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    await db_settings.update_guild(member.guild.id, goodbye_channel_id=None)
                    log.warning("Goodbye-Channel %s existiert nicht mehr, Eintrag bereinigt.", channel_id)
                    return
                except discord.Forbidden:
                    log.warning("Kein Zugriff auf Channel %s auf Guild %s.", channel_id, member.guild.id)
                    return

            lang  = await get_lang(member.guild.id)
            color = await db_settings.get_color(member.guild.id)
            embed = discord.Embed(
                title=lang.get("goodbye_title", "👋 Goodbye!"),
                description=lang.get("goodbye_description", "{user} has left the server.").format(
                    user=f"**{member.display_name}**"
                ),
                color=color,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=member.guild.name)
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                log.warning(
                    "Goodbye-Nachricht auf %s (%s) nicht gesendet: Missing Permissions in Channel %s.",
                    member.guild.name, member.guild.id, channel_id,
                )
                return
            log.info("%s hat %s verlassen.", member, member.guild.name)
        except Exception:
            log.exception("Fehler in on_member_remove für %s auf %s", member, member.guild.name)

async def setup(bot):
    await bot.add_cog(Goodbye(bot))