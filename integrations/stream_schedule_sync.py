"""Gleicht stündlich die Twitch-Streampläne mit den Discord-Events ab (Logik: systems/stream_schedule.py)."""
import logging

from discord.ext import commands, tasks

from systems import stream_schedule

log = logging.getLogger(__name__)

SYNC_INTERVAL_MINUTES = 60


class StreamScheduleSync(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sync_schedules.start()

    async def cog_unload(self):
        self.sync_schedules.cancel()

    @tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
    async def sync_schedules(self):
        # Kein Fehler darf die Schleife beenden — sonst gleicht nach einem Ausfall nichts mehr ab
        try:
            await stream_schedule.sync_all(self.bot)
        except Exception:
            log.exception("Streamplan-Abgleich fehlgeschlagen.")

    @sync_schedules.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(StreamScheduleSync(bot))
