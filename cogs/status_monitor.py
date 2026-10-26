import asyncio
import logging
import math
import time
import config
import database.database as db
import discord
from datetime import datetime, timezone
from discord.ext import commands, tasks
from systems.hierarchy import can_use_channel, format_issue

log = logging.getLogger(__name__)

UPDATE_INTERVAL_MINUTES = 10
DB_TIMEOUT_SECONDS = 3
LATENCY_WARN_MS = 200
TITLE = "Nexus • System Status"

COLOR_OK   = 0x2ECC71
COLOR_WARN = 0xE67E22
COLOR_DOWN = 0xE74C3C

def _to_epoch(value) -> int:
    """bot.started_at may be a datetime (naive = UTC) or a unix timestamp; fall back to now."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return int(time.time())

async def _check_db() -> bool:
    """True if the database answers a trivial query within the timeout."""
    async def _ping():
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
    try:
        await asyncio.wait_for(_ping(), timeout=DB_TIMEOUT_SECONDS)
        return True
    except Exception:
        log.warning("[STATUS] Database check failed.", exc_info=True)
        return False

class StatusMonitor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id: int = int(getattr(config, "STATUS_CHANNEL_ID", 0) or 0)
        self.message_id: int | None = None
        self.started_at: int = _to_epoch(getattr(bot, "started_at", None))

    async def cog_load(self):
        if not self.channel_id:
            log.info("[STATUS] STATUS_CHANNEL_ID not set — status monitor disabled.")
            return
        self.update_status.start()

    async def cog_unload(self):
        self.update_status.cancel()

    # ─── Embed ───────────────────────────────────────────────────────────────

    async def _build_embed(self) -> discord.Embed:
        latency = self.bot.latency  # NaN/inf while not connected
        latency_ok = math.isfinite(latency)
        latency_ms = round(latency * 1000) if latency_ok else None
        db_ok = await _check_db()

        if not db_ok:
            status, color = "🔴 Degraded — database not responding", COLOR_DOWN
        elif not latency_ok:
            status, color = "🟠 Reconnecting to Discord", COLOR_WARN
        elif latency_ms >= LATENCY_WARN_MS:
            status, color = "🟠 Operational — high latency", COLOR_WARN
        else:
            status, color = "🟢 Fully operational", COLOR_OK

        now = int(time.time())
        embed = discord.Embed(title=TITLE, color=color)
        embed.add_field(name="Status", value=status, inline=False)
        embed.add_field(name="Gateway latency", value=f"`{latency_ms} ms`" if latency_ok else "`n/a`", inline=True)
        embed.add_field(name="Database", value="`OK`" if db_ok else "`Error`", inline=True)
        embed.add_field(name="Servers", value=f"`{len(self.bot.guilds)}`", inline=True)
        embed.add_field(name="Online since", value=f"<t:{self.started_at}:R>", inline=True)
        embed.add_field(name="Last checked", value=f"<t:{now}:R>", inline=True)
        embed.set_footer(text=f"Updates every {UPDATE_INTERVAL_MINUTES} minutes — if the last check is much older, the bot is down.")
        return embed

    # ─── Message handling ────────────────────────────────────────────────────

    async def _find_existing_message(self, channel: discord.abc.Messageable) -> int | None:
        async for msg in channel.history(limit=20):
            if msg.author.id == self.bot.user.id and msg.embeds and msg.embeds[0].title == TITLE:
                return msg.id
        return None

    @tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
    async def update_status(self):
        # Never let an exception escape: an unhandled error stops a tasks.loop for good.
        try:
            channel = self.bot.get_channel(self.channel_id)
            if channel is None:
                log.warning("[STATUS] Channel %s not found (wrong ID or bot not in that server).", self.channel_id)
                return

            if not (check := can_use_channel(channel, "send_messages", "embed_links", "read_message_history")):
                log.warning("[STATUS] Cannot post in %s: %s", channel, format_issue(check))
                return

            embed = await self._build_embed()

            if self.message_id is None:
                self.message_id = await self._find_existing_message(channel)

            if self.message_id is not None:
                try:
                    await channel.get_partial_message(self.message_id).edit(embed=embed)
                    return
                except discord.NotFound:
                    log.info("[STATUS] Status message was deleted — posting a new one.")
                    self.message_id = None

            msg = await channel.send(embed=embed)
            self.message_id = msg.id

        except discord.Forbidden:
            log.warning("[STATUS] Missing permissions to update the status message.")
        except discord.HTTPException:
            log.exception("[STATUS] Discord API error while updating the status message.")
        except Exception:
            log.exception("[STATUS] Unexpected error in update loop.")

    @update_status.before_loop
    async def _before_update(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(StatusMonitor(bot))