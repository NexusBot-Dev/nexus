import discord
from discord import app_commands
from discord.ext import commands
from cogs.utils import get_lang
from systems.premium_ui import send_overview

class PremiumCommand(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="premium", description=app_commands.locale_str("cmd_premium_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def premium(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await send_overview(interaction, await get_lang(interaction.guild_id))

async def setup(bot):
    await bot.add_cog(PremiumCommand(bot))
