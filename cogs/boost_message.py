import discord
from discord import app_commands
from discord.ext import commands
from database import db_boosts
from cogs.utils import get_lang, module_required
from systems.message_panel import COMMON_FALLBACKS, MessageKind, open_panel

# Eigene Boost-Nachrichten — Ablauf und Panel: systems/message_panel.py
BOOST = MessageKind(
    key="boost",
    limit_key="boost_messages",
    upsell="boost_limit",
    get=db_boosts.get_boost_messages,
    add=db_boosts.add_boost_message,
    update=db_boosts.update_boost_message,
    delete=db_boosts.delete_boost_message,
    fallbacks={
        **COMMON_FALLBACKS,
        "modal_title": "Create boost message",
        "modal_placeholder": "e.g. Thank you {user} for boosting {server}! We now have {count} boosts.",
        "panel_title": "🚀 Boost Messages",
        "panel_placeholders": "Placeholders: {user} {username} {server} {count}",
    },
)

class BoostMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="boost_message", description=app_commands.locale_str("cmd_boost_message_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("boosts")
    async def boost_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await open_panel(interaction, BOOST, await get_lang(interaction.guild_id))

async def setup(bot):
    await bot.add_cog(BoostMessages(bot))
