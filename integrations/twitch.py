import discord
import logging
from database.db_stream_alerts import get_monitored_channel, get_subscriptions_for_channel
from database import db_stream_alerts
from discord.ext import commands
from systems.alert_dispatch import dispatch_alerts, stream_came_back, stream_went_offline
from systems.twitch_api import get_stream_by_user_id

log = logging.getLogger(__name__)

class Twitch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def twitch_process_notification(self, data: dict):
        event_type         = data["subscription"]["type"]
        broadcaster_id     = data["event"]["broadcaster_user_id"]
        broadcaster_login  = data["event"]["broadcaster_user_login"]
        broadcaster_name   = data["event"]["broadcaster_user_name"]

        channel = await get_monitored_channel("twitch", broadcaster_id)
        if channel is None:
            log.warning(
                "Notification für unbekannten Twitch-Kanal %s (%s) erhalten.",
                broadcaster_name, broadcaster_id,
            )
            return

        if event_type != "stream.online":
            await stream_went_offline(channel["id"])
            return
        if await stream_came_back(channel["id"]):
            log.info("Twitch: %s ist innerhalb der Schonfrist wieder live — kein neuer Alert.", broadcaster_name)
            return
        await db_stream_alerts.set_live_status(channel["id"], True)

        stream_details = None
        try:
            stream_details = await get_stream_by_user_id(broadcaster_id)
        except Exception:
            log.exception("Stream-Details-Abruf für %s fehlgeschlagen — Alert ohne Zusatzinfos.", broadcaster_name)

        subs = await get_subscriptions_for_channel(channel["id"])
        await dispatch_alerts(
            self.bot,
            platform="twitch",
            subs=subs,
            is_live=True,
            streamer_name=broadcaster_name,
            build_live_embed=lambda lang: self._build_live_embed(
                lang, broadcaster_name, broadcaster_login, stream_details
            ),
            stream_url=f"https://twitch.tv/{broadcaster_login}",
        )

    @staticmethod
    def _build_live_embed(lang: dict, streamer_name: str, streamer_login: str, stream_details: dict | None) -> discord.Embed:
        embed = discord.Embed(
            title=lang.get("stream_live_title", "🔴 {streamer} is now live!").format(streamer=streamer_name),
            url=f"https://twitch.tv/{streamer_login}",
            color=discord.Color.purple(),
        )
        if stream_details:
            if stream_details.get("title"):
                embed.description = stream_details["title"]
            if stream_details.get("game_name"):
                embed.add_field(
                    name=lang.get("stream_category", "🎮 Category"),
                    value=stream_details["game_name"],
                    inline=True,
                )
            thumb = stream_details.get("thumbnail_url", "")
            if thumb:
                embed.set_image(url=thumb.replace("{width}", "1280").replace("{height}", "720"))
        return embed

async def setup(bot):
    await bot.add_cog(Twitch(bot))