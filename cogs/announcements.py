import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings
from cogs.utils import get_lang
from config import NEXUS_COLOR, NEXUS_FOOTER
from emojis import NexusEmojis
from systems.ui import NexusView, send_ephemeral

log = logging.getLogger(__name__)

class AnnouncementSettingsView(NexusView):
    def __init__(self, owner_id: int, lang: dict, enabled: bool):
        super().__init__(lang, owner_id=owner_id, timeout=120)
        self.enabled = enabled
        self.select_channel.placeholder = lang.get("announcement_select_placeholder", "📋 Select channel...")
        self._update_toggle_button()

    def _update_toggle_button(self):
        if self.enabled:
            self.toggle_btn.label = self.lang.get("announcement_status_on", "Enabled")
            self.toggle_btn.style = discord.ButtonStyle.success
            self.toggle_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)
        else:
            self.toggle_btn.label = self.lang.get("announcement_status_off", "Disabled")
            self.toggle_btn.style = discord.ButtonStyle.secondary
            self.toggle_btn.emoji = "❌"

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0,
    )
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        # Channel wählen = Opt-in: Announcements werden gleichzeitig aktiviert.
        await db_settings.update_guild(
            interaction.guild_id,
            announcement_channel_id=channel.id,
            announcements_enabled=True,
        )
        self.enabled = True
        self._update_toggle_button()

        embed = await _build_status_embed(interaction.guild_id, self.lang)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(row=1)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Einschalten ergibt ohne Channel keinen Sinn (es gibt keinen Fallback mehr)
        if not self.enabled:
            settings = await db_settings.get_or_create_guild(interaction.guild_id)
            if not settings.get("announcement_channel_id"):
                await send_ephemeral(
                    interaction,
                    self.lang.get("announcement_need_channel", "❌ Please select a channel first."),
                )
                return

        self.enabled = not self.enabled
        await db_settings.update_guild(interaction.guild_id, announcements_enabled=self.enabled)
        self._update_toggle_button()

        embed = await _build_status_embed(interaction.guild_id, self.lang)
        await interaction.response.edit_message(embed=embed, view=self)


async def _build_status_embed(guild_id: int, lang: dict) -> discord.Embed:
    settings = await db_settings.get_or_create_guild(guild_id)
    status = (
        lang.get("announcement_status_on", "Enabled")
        if settings.get("announcements_enabled", False)
        else lang.get("announcement_status_off", "Disabled")
    )

    if settings.get("announcement_channel_id"):
        ch_display = f"<#{settings['announcement_channel_id']}>"
    else:
        ch_display = lang.get("announcement_no_channel", "Not Set")

    embed = discord.Embed(
        title=lang.get("announcement_status_title", "📢 Announcement Settings").format(
            announcement=NexusEmojis.Modules.BROADCAST
        ),
        color=NEXUS_COLOR,
    )
    embed.add_field(name=lang.get("announcement_status_field", "Status"), value=status, inline=True)
    embed.add_field(name=lang.get("announcement_channel_field", "Channel"), value=ch_display, inline=True)
    return embed

class AnnouncementDisableView(NexusView):
    """Persistente View — muss auch nach einem Bot-Neustart noch funktionieren,
    da Announcement-Embeds dauerhaft in Kanälen stehen bleiben.

    Das Label wird beim Senden festgelegt (pro Guild-Sprache); für das Routing
    nach einem Neustart zählt nur die custom_id. Die Rechteprüfung läuft im
    Callback mit der Sprache der klickenden Guild, da die registrierte Instanz
    guild-übergreifend geteilt wird (self.lang ist dort leer)."""

    def __init__(self, lang: dict | None = None):
        super().__init__(lang or {}, require=None, timeout=None)
        self.disable_btn.label = self.lang.get("announcement_disable_button", "Disable announcements")

    @discord.ui.button(
        style=discord.ButtonStyle.secondary,
        custom_id="nexus_announcement_disable",
    )
    async def disable_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = await get_lang(interaction.guild_id)

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                lang.get("no_permission", "You don't have permission to use this command."),
                ephemeral=True,
            )
            return

        await db_settings.update_guild(interaction.guild_id, announcements_enabled=False)
        await interaction.response.send_message(
            lang.get(
                "announcement_disabled_via_button",
                "Announcements have been disabled. You can re-enable them at any time using `/announcement`.",
            ),
            ephemeral=True,
        )

async def resolve_announcement_channel(bot, guild: discord.Guild, settings: dict) -> discord.TextChannel | None:
    """Opt-in: nur der explizit gewählte Announcement-Channel, kein Fallback
    (weder Log-Channel noch irgendein beschreibbarer Kanal)."""
    channel_id = settings.get("announcement_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        perms = channel.permissions_for(guild.me)
        if perms.send_messages and perms.embed_links:
            return channel
    return None

async def broadcast_announcement(bot, title: dict, description: dict, target_guild_ids: list[int] | None = None):
    """title/description: {"de": "...", "en": "..."} — pro Guild wird die passende Sprache gewählt.
    Gesendet wird nur an Guilds mit aktivierten Announcements UND gesetztem Channel (Opt-in)."""
    sent, skipped, failed = 0, 0, 0

    guilds = bot.guilds if target_guild_ids is None else [g for g in bot.guilds if g.id in target_guild_ids]

    for guild in guilds:
        try:
            settings = await db_settings.get_guild(guild.id)
            if not settings or not settings.get("announcements_enabled", False):
                skipped += 1
                continue

            channel = await resolve_announcement_channel(bot, guild, settings)
            if channel is None:
                skipped += 1
                continue

            locale = settings.get("locale", "en")
            lang_title = title.get(locale, title.get("en", ""))
            lang_desc = description.get(locale, description.get("en", ""))

            lang = await get_lang(guild.id)
            embed = discord.Embed(
                title=f"{NexusEmojis.Modules.BROADCAST} {lang_title}",
                description=lang_desc,
                color=NEXUS_COLOR,
            )
            embed.set_footer(text=NEXUS_FOOTER)

            await channel.send(embed=embed, view=AnnouncementDisableView(lang))
            sent += 1

        except discord.Forbidden:
            failed += 1
        except Exception:
            log.exception("Announcement für Guild %s fehlgeschlagen.", guild.id)
            failed += 1

    log.info("Announcement-Broadcast abgeschlossen: %s gesendet, %s übersprungen, %s fehlgeschlagen.", sent, skipped, failed)
    return sent, skipped, failed

class Announcements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="announcement", description=app_commands.locale_str("cmd_announcement_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def announcement(self, interaction: discord.Interaction):
        lang = await get_lang(interaction.guild_id)
        settings = await db_settings.get_or_create_guild(interaction.guild_id)

        embed = await _build_status_embed(interaction.guild_id, lang)
        view = AnnouncementSettingsView(
            interaction.user.id, lang, enabled=settings.get("announcements_enabled", False)
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(Announcements(bot))
    bot.add_view(AnnouncementDisableView())