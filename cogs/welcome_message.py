import discord
from discord import app_commands
from discord.ext import commands
from database import db_welcome
from cogs.utils import get_lang, module_required
from systems.message_panel import COMMON_FALLBACKS, MessageKind, open_panel

# Eigene Willkommensnachrichten — Ablauf und Panel: systems/message_panel.py
WELCOME = MessageKind(
    key="welcome",
    limit_key="welcome_messages",
    upsell="welcome_limit",
    get=db_welcome.get_welcome_messages,
    add=db_welcome.add_welcome_message,
    update=db_welcome.update_welcome_message,
    delete=db_welcome.delete_welcome_message,
    fallbacks={
        **COMMON_FALLBACKS,
        "modal_title": "Create welcome message",
        "modal_placeholder": "e.g. Welcome {user} to {server}! You are member #{membercount}.",
        "panel_title": "👋 Welcome Messages",
        "panel_placeholders": "Placeholders: {user} {username} {server} {membercount}",
    },
)

class WelcomeMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="welcome_message", description=app_commands.locale_str("cmd_welcome_message_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("welcome")
    async def welcome_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await open_panel(interaction, WELCOME, await get_lang(interaction.guild_id))

async def setup(bot):
    await bot.add_cog(WelcomeMessages(bot))
