import logging
import discord
from discord.ext import commands, tasks
from database import db_stream_alerts
from systems.alert_dispatch import dispatch_alerts, end_stream_now
from systems.youtube_api import get_latest_video_status, QuotaExceededError

log = logging.getLogger(__name__)

POLL_INTERVAL_MINUTES = 15

class YoutubePolling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poll_youtube_channels.start()

    async def cog_unload(self):
        self.poll_youtube_channels.cancel()

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_youtube_channels(self):
        try:
            channels = await db_stream_alerts.get_monitored_channels_by_platform("youtube")
        except Exception:
            log.exception("Konnte überwachte YouTube-Kanäle nicht laden.")
            return

        if not channels:
            return

        log.info("Starte YouTube-Poll für %s Kanäle...", len(channels))

        for channel in channels:
            try:
                await self._check_channel(channel)
            except QuotaExceededError:
                log.warning("YouTube-Quota erschöpft — Poll-Zyklus wird abgebrochen, Rest verschoben auf nächsten Lauf.")
                break
            except Exception:
                log.exception("Fehler beim Pollen von YouTube-Kanal %s.", channel.get("streamer_name"))

    async def _check_channel(self, channel: dict):
        playlist_id = channel.get("youtube_uploads_playlist_id")
        if not playlist_id:
            log.warning("Kein uploads_playlist_id für %s gespeichert — überspringe.", channel["streamer_name"])
            return

        status = await get_latest_video_status(playlist_id)
        if status is None:
            return

        previous_video_id = channel.get("youtube_last_video_id")
        previous_live     = bool(channel.get("live"))
        new_video_id      = status["video_id"]
        is_live_now       = status["live_broadcast_content"] == "live"

        video_changed = new_video_id != previous_video_id

        # Nichts hat sich geändert — weder Video noch Live-Status
        if not video_changed and is_live_now == previous_live:
            return

        await db_stream_alerts.set_youtube_last_video_id(channel["id"], new_video_id)
        streamer_name = channel["streamer_name"]

        # Fall A: War live, ist jetzt nicht mehr live (egal ob gleiches oder neues Video) -> Live-Nachricht als
        # beendet bearbeiten. Ohne Schonfrist: Bei 15 Minuten Abfrage-Intervall fällt ein kurzer Abbruch kaum auf.
        if previous_live and not is_live_now:
            await end_stream_now(self.bot, channel["id"])
        await db_stream_alerts.set_live_status(channel["id"], is_live_now)
        subs = await db_stream_alerts.get_subscriptions_for_channel(channel["id"])

        # Fall B: Ist jetzt live (egal ob vorher live oder nicht) -> Live-Alert
        if is_live_now:
            await dispatch_alerts(
                self.bot,
                platform="youtube",
                subs=subs,
                is_live=True,
                streamer_name=streamer_name,
                build_live_embed=lambda lang: self._build_live_embed(lang, streamer_name, status),
                stream_url=f"https://youtube.com/watch?v={status['video_id']}",
                dedupe=False,  # YouTube war bisher nicht Teil der plattformübergreifenden Duplikat-Logik
            )

    @staticmethod
    def _build_live_embed(lang: dict, streamer_name: str, status: dict) -> discord.Embed:
        embed = discord.Embed(
            title=lang.get("stream_live_title", "🔴 {streamer} is now live!").format(streamer=streamer_name),
            description=status.get("title", ""),
            url=f"https://youtube.com/watch?v={status['video_id']}",
            color=discord.Color.red(),
        )
        if status.get("thumbnail_url"):
            embed.set_image(url=status["thumbnail_url"])
        return embed

    @poll_youtube_channels.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(YoutubePolling(bot))