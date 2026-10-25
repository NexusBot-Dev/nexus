import asyncio
import logging

from discord.ext import commands, tasks
from lists.botboard import post_commands_to_botboard
from lists.discordbotlist import post_server_count_to_dbl
from lists.topgg import post_server_count

log = logging.getLogger(__name__)

class BotLists(commands.Cog):
    """Stündlicher Stats-Push an die Bot-Listen (Top.gg, DBL, Botboard)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.update_stats.start()

    async def cog_unload(self):
        self.update_stats.cancel()

    @tasks.loop(hours=1)
    async def update_stats(self):
        """Aktualisiert Stats fehlerisoliert und parallel auf allen Plattformen."""
        tasks_map = {
            "Top.gg": post_server_count(self.bot),
            "DBL": post_server_count_to_dbl(self.bot),
            "Botboard": post_commands_to_botboard(self.bot),
        }

        # Alle 3 Requests parallel abfeuern
        results = await asyncio.gather(*tasks_map.values(), return_exceptions=True)

        for name, result in zip(tasks_map.keys(), results):
            if isinstance(result, Exception):
                log.warning("%s Stats-Push fehlgeschlagen: %s", name, result)

    @update_stats.before_loop
    async def _before_update_stats(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(BotLists(bot))